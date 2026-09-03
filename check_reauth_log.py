#!/usr/bin/env python3
"""
Diagnostic (not shipped): inspect a Home Assistant log for the radoff
integration to see (a) how long ago it last successfully polled Radoff, (b)
whether the auth-error -> re-auth transition happened once (expected) or is
looping (S-08 AC5 regression), and (c) whether the ~55-minute natural
token-expiry reconnect (api/client.py::get_devices, no restart involved) has
actually fired yet - as opposed to the coordinator's own "not connected"
reconnect, which only runs after a live 401 or an integration reload/HA
restart.

(c) was added after a report that the re-auth prompt "only appears after
restarting Home Assistant": a restart always forces an immediate auth
attempt (a fresh API object starts with connected=False), while during
normal long-running operation nothing forces a re-check of the stored
password until either a live 401 happens or the token nears its natural
expiry (~55 min, see api/auth.py's docstring). Without a case (c) event in
the log, "no reauth without restart" is expected behaviour, not a bug -
distinguishing the two requires seeing whether this line ever appears on
its own during an uninterrupted run.

Usage:
    python3 check_reauth_log.py [path/to/home-assistant.log]

Defaults to .devcontainer/config/home-assistant.log (this repo's dev
harness). Requires custom_components.radoff logged at least at INFO (DEBUG
gives the most detail); the dev harness's configuration.yaml already sets
DEBUG for it.
"""

import re
import sys
from datetime import datetime
from pathlib import Path

LOG_PATH = (
    Path(sys.argv[1])
    if len(sys.argv) > 1
    else Path(".devcontainer/config/home-assistant.log")
)

TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})")

SUCCESS_RE = re.compile(r"Successfully fetched data for \d+ device")
AUTH_INVALID_RE = re.compile(r"Radoff credentials are no longer valid, starting re-auth")
AUTH_EXPIRED_RE = re.compile(r"Radoff authentication token expired, will retry")
GENERIC_AUTH_ERROR_RE = re.compile(r"^.*Authentication error$")
RECONNECT_RE = re.compile(r"API not connected, attempting to connect")

# Card S-08 follow-up: the *natural* ~55-minute reconnect, entirely internal
# to api/client.py::get_devices - never logged by coordinator.py, and
# therefore invisible to the checks above. "Token expired, reconnecting..."
# fires the moment _is_token_expired() trips (whether or not the subsequent
# connect() succeeds); "Token will expire in %d seconds" fires only on a
# *successful* connect() - initial login and periodic reconnect alike, so
# more than one occurrence means at least one periodic reconnect has already
# completed without a restart.
TOKEN_EXPIRY_CHECK_RE = re.compile(r"Token expired, reconnecting")
TOKEN_RENEWED_RE = re.compile(r"Token will expire in \d+ seconds")


def parse_ts(line: str) -> datetime | None:
    match = TIMESTAMP_RE.match(line)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S.%f")


def main() -> None:
    if not LOG_PATH.exists():
        print(f"Log file not found: {LOG_PATH}")
        sys.exit(1)

    lines = [
        line
        for line in LOG_PATH.read_text(errors="replace").splitlines()
        if "custom_components.radoff" in line
    ]
    if not lines:
        print("No radoff log lines found in this file.")
        return

    last_success = None
    events = []  # (timestamp, kind)
    token_expiry_checks = []  # ts of "Token expired, reconnecting..."
    token_renewals = []  # ts of "Token will expire in N seconds" (successful connect)

    for line in lines:
        ts = parse_ts(line)
        if ts is None:
            continue
        if SUCCESS_RE.search(line):
            last_success = ts
        elif AUTH_INVALID_RE.search(line):
            events.append((ts, "AUTH_INVALID (definitivo -> ConfigEntryAuthFailed)"))
        elif AUTH_EXPIRED_RE.search(line):
            events.append((ts, "AUTH_EXPIRED (recuperabile -> UpdateFailed)"))
        elif RECONNECT_RE.search(line):
            events.append((ts, "RECONNECT attempt (coordinator: era disconnesso)"))
        elif GENERIC_AUTH_ERROR_RE.search(line):
            events.append((ts, "AUTH_ERROR generico (403/429/5xx)"))

        if TOKEN_EXPIRY_CHECK_RE.search(line):
            token_expiry_checks.append(ts)
        elif TOKEN_RENEWED_RE.search(line):
            token_renewals.append(ts)

    now = datetime.now()

    print("=== Ultimo poll riuscito ===")
    if last_success:
        print(f"{last_success}  ->  {now - last_success} fa")
    else:
        print("Nessun poll riuscito trovato nel log.")

    print(f"\n=== Eventi di autenticazione trovati: {len(events)} ===")
    for ts, kind in events:
        print(f"{ts}  {kind}")

    invalid_events = [ts for ts, kind in events if kind.startswith("AUTH_INVALID")]

    print(f"\nAUTH_INVALID visto {len(invalid_events)} volta/e.")
    if len(invalid_events) > 1:
        gaps = [b - a for a, b in zip(invalid_events, invalid_events[1:])]
        print("LOOP SOSPETTO: ConfigEntryAuthFailed sollevato più di una volta.")
        print("Intervalli tra le occorrenze:", ", ".join(str(g) for g in gaps))
    elif len(invalid_events) == 1:
        print("OK: nessun loop, il coordinator ha smesso di ripetere l'errore auth.")
    else:
        print("Nessun AUTH_INVALID ancora nel log (password non ancora rifiutata).")

    print("\n=== Riconnessione naturale per scadenza token (client.py, ~55 min, NO restart) ===")
    print(f"'Token expired, reconnecting...' visto {len(token_expiry_checks)} volta/e.")
    print(f"'Token will expire in N seconds' (connect riuscita) visto {len(token_renewals)} volta/e.")

    if not token_expiry_checks:
        if last_success:
            print(
                "MAI ancora scattato in questo log. Se sono passati meno di ~55 minuti "
                f"dall'ultimo avvio/riconnessione ({now - last_success} dall'ultimo poll "
                "riuscito e' un limite inferiore, non l'eta' della sessione), e' atteso: "
                "il vecchio token e' ancora valido e non c'e' ancora stato alcun tentativo "
                "di ri-autenticazione, quindi nessun errore da classificare e nessun "
                "re-auth possibile - non e' un bug, e' il limite noto F7/S-12 (nessun "
                "refresh token, si ri-autentica solo replay-ando la password ogni ~55 min "
                "o dopo un 401)."
            )
        else:
            print("MAI ancora scattato, e nessun poll riuscito nel log: verifica il livello di log.")
    else:
        print("Timestamp:")
        for ts in token_expiry_checks:
            print(f"  {ts}")
        if len(token_renewals) > 1:
            print(
                "Almeno una riconnessione periodica e' andata a buon fine senza restart: "
                "il ciclo naturale FUNZIONA. Se in questo stesso log manca un AUTH_INVALID "
                "nonostante la password fosse gia' cambiata prima dell'ultimo "
                "'Token expired, reconnecting...', e' un caso da investigare come bug reale "
                "(l'eccezione da connect() non sta arrivando al coordinator)."
            )


if __name__ == "__main__":
    main()