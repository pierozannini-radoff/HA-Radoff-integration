"""
Domain dataclasses for the Radoff API.

Card M-03 rewrites this module around the arch 2.0 payload. What arch 1.x
made necessary here is gone:

- The response-bucket enum and the composite (bucket, property name)
  reading key of card S-10: arch 2.0 has no buckets. `GET /data/devices`
  carries one flat `telemetry` object per device, holding the last value of
  each field, so the reading key goes back to being the field name and
  finding C4 - two buckets colliding on one property name - cannot occur by
  construction rather than by convention.
- `DeviceFetchError` (card S-13): it existed because the fetch was N+1, one
  `GET /data/devices/{id}` per device, which made "this one device failed"
  a real, isolatable state. There is one call per cycle now (see
  `api/client.py::get_devices`), so a failure is either the whole cycle's or
  nobody's; nothing is left to isolate.

`device_id` goes with them. In arch 2.0 `deviceId`, `serial_number` and
`deviceSerial` are the same value (T-02 D-02, confirmed by the real payloads
of M-01), and the model exposes exactly one of them: `serial_number`. The
UUID of arch 1.x has no counterpart at all - it is not a renamed field, it
simply does not exist any more.

Card M-06 adds the one piece of interpretation this module holds:
`ConnectionState` and `classify_connection_status`, the three-way reading of
the `connection_status` field that entity availability is now decided from.
It lives here, next to the field it interprets, rather than in `const.py`:
it is the API's vocabulary, not this integration's configuration.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class ConnectionState(StrEnum):
    """
    A device's `connection_status` as this client reads it: three states (M-06).

    Three states, not two, and the third one is the point. The backend
    tells us to decide entity availability from `connection_status` (T-02
    D-21) but has not yet given us its enumeration (T-08 D-17 is still
    open): all M-01 observed on dev is `connected` and `disconnected`. A
    client that mapped "anything that is not `connected`" to offline would
    therefore turn the first value we have never seen - `unknown`,
    `never_connected`, a spelling change, a new state added for a new
    device family - into every entity of that device going `unavailable`,
    which is the loudest possible reaction to a string we simply do not
    recognise.

    So an unrecognised value, and an absent one, land in `INDETERMINATE`:
    the device is treated as usable and the value is reported in the log
    (see `api/client.py::_build_device`, which warns once per unseen value)
    and in the diagnostics dump. Only an explicitly disconnected device is
    read as offline.
    """

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    INDETERMINATE = "indeterminate"


# The `connection_status` values M-01 actually saw on dev, and the only two
# this client claims to know. `INDETERMINATE` is not here on purpose: it is
# this client's own reading of a value, never a value the API sends. When
# T-08 D-17 answers with the real enumeration, this mapping is the one place
# that grows - `ConnectionState` and every caller stay as they are.
KNOWN_CONNECTION_STATUSES: dict[str, ConnectionState] = {
    "connected": ConnectionState.CONNECTED,
    "disconnected": ConnectionState.DISCONNECTED,
}


def classify_connection_status(raw: str | None) -> ConnectionState:
    """
    Map a raw `connection_status` string onto a `ConnectionState` (card M-06).

    Case- and whitespace-insensitive, which is a deliberate small piece of
    tolerance rather than an observed need: the field is synchronised from
    DynamoDB (T-02 D-17) and a value arriving as `"Connected"` would
    otherwise read as a brand new state and take the device to
    `INDETERMINATE`. Matching it is strictly better than warning about it.

    Everything else - an unknown string, an empty one, `None` - is
    `INDETERMINATE`. Pure by design: it is called from
    `RadoffEntity.available`, i.e. on every state read of every entity, so
    it must not log, count or cache anything. The one WARNING that an
    unrecognised value deserves is emitted once per poll cycle, where the
    string enters the model (`api/client.py::_build_device`).
    """
    if raw is None:
        return ConnectionState.INDETERMINATE
    return KNOWN_CONNECTION_STATUSES.get(
        raw.strip().lower(), ConnectionState.INDETERMINATE
    )


@dataclass
class Reading:
    """
    One field of one device's `telemetry` block, with the value it last had.

    `name` is the field name as the API spells it (`tvoc`, `aqi_value`,
    `internal_temperature`, ...) and is also the key this reading is filed
    under in `RadoffDevice.readings`.

    `measured_at` deserves care, because it is coarser than it looks. Arch
    2.0 exposes **one** timestamp for the whole telemetry block
    (`telemetry.timestamp`), not one per field, and T-08 D-15 states what
    that timestamp is: the maximum of the individual fields' timestamps, not
    the instant this particular field was sampled. So every `Reading` of one
    device carries the same `measured_at`, and for a field that updates more
    slowly than its siblings that value is optimistic - it says "something on
    this device was measured then", not "this field was".

    The field this actually matters for is radon, whose cadence is much
    slower than the rest of the block (T-08 D-15 keeps a reservation on
    exactly this point): a radon reading can be hours old while
    `measured_at`, refreshed by every faster field around it, keeps looking
    current.

    Card M-06 changes what rides on that, and it is worth being precise
    about the direction. Availability no longer depends on this timestamp
    at all - it is decided by `connection_status` (see
    `entity.py::RadoffEntity.available`), so a coarse `measured_at` can no
    longer take an entity `unavailable` for the wrong reason. What it can
    still do is *look* fresher than the value it accompanies: this
    timestamp is published as the `last_measured_at` attribute, and for
    radon it is optimistic by construction.

    The estimate that would narrow it is known and deliberately NOT
    implemented: with the cadence T-02 D-18 confirmed (one message per
    minute, radon aggregated over at least five) a per-field staleness
    could be *guessed* - radon's true age is somewhere in
    `[measured_at - 5min, measured_at]`, and wider whenever the aggregation
    window is longer than its minimum, which is exactly the residual D-18
    leaves open. Guessing it would put a number of our own invention where
    a measurement belongs, in the same shape as the arbitrary staleness
    multiplier M-06 has just removed. So the limit is documented here and
    the fix stays where it belongs: a per-field timestamp from the backend
    (T-08 D-15).
    """

    name: str
    value: float | int
    measured_at: datetime | None = None


@dataclass
class RadoffDevice:
    """
    One device as `GET /data/devices` describes it in arch 2.0.

    `serial_number` is the identity, everywhere: it keys the coordinator's
    lookup (`coordinator.py::get_device_by_id`), it is half of every
    `unique_id` (`entity.py`) and it is what `device_info.identifiers`
    already used before this card.

    Most of the remaining fields are carried, not consumed, on purpose -
    they are read here so that the cards that use them do not have to touch
    the client again:

    - `connection_status` / `connection_status_updated_at`: since card
      M-06 the first one *is* the availability signal, read through
      `connection_state` below rather than compared to a string by each
      caller; the second one is published as a diagnostic attribute and
      watched against `CONNECTION_STATUS_STALE_WINDOW` (const.py).
    - `status`: the other, coexisting status field (`active`), added to
      the model by M-06 and deliberately not consumed by any decision.
      The two are not synonyms - `status` is administrative (is this
      device registered and in service?) and `connection_status` is
      operational (is it talking to the cloud right now?) - and until
      T-08 D-17 gives us both enumerations, treating one as a proxy for
      the other would be a guess. It is exposed as an attribute so a
      support conversation can see both at once.
    - `firmware_version`: surfaced as `device_info.sw_version` (this card).
    - `room_name`/`room_slug`, `building_name`/`building_slug`,
      `domain_prefix`: context for the device registry and for diagnostics.
      The pairs are kept both halves: the name is the label a person wrote
      and is what a user recognises, the slug is the stable machine
      identifier (`875fe89b-home`) that survives a rename. A card grouping
      devices by room needs the second to key on and the first to show.

    `stale` survives from S-13 with a **different meaning**, and the change
    is easy to miss. It used to mean "this device's own fetch failed this
    cycle". It now means "this device reported no telemetry this cycle", the
    `telemetry: null` M-01 found on the majority of the 120 devices it
    censused (D-16): a device that has never transmitted, or is not
    transmitting now. That is not the same claim as "offline" - a device can
    be `connection_status: connected` and still send nothing in a cycle, and
    conversely a disconnected device keeps the last telemetry the API holds
    for it.

    Card M-06 is where that distinction stops being a note and starts
    being behaviour: `stale` is no longer read by `available` at all. The
    reason it must not be is the 6-hour window the backend described
    (D-16) - `GET /data/devices` looks at that window and does not widen
    it on a miss, so a device that has been silent for longer answers
    `telemetry: null` *while connected*. Treating that as unavailable
    would report a working device as broken on the strength of a query
    window. `stale` therefore keeps saying exactly what it says - no data
    this cycle - and nothing more.

    `telemetry_timestamp` is the same value every `Reading.measured_at` of
    this device carries (see `Reading`), kept at device level too so a
    device with no readings at all still says when it last spoke.
    """

    serial_number: str
    device_type: str
    name: str
    readings: dict[str, Reading] = field(default_factory=dict)
    connection_status: str | None = None
    connection_status_updated_at: datetime | None = None
    status: str | None = None
    firmware_version: str | None = None
    room_name: str | None = None
    room_slug: str | None = None
    building_name: str | None = None
    building_slug: str | None = None
    domain_prefix: str | None = None
    telemetry_timestamp: datetime | None = None
    stale: bool = False

    @property
    def connection_state(self) -> ConnectionState:
        """
        Return this device's connection as one of three states (card M-06).

        The single place the raw string is interpreted, so that no caller
        ends up writing `== "connected"` and inheriting the "everything
        else is offline" reading that `ConnectionState` exists to avoid.
        """
        return classify_connection_status(self.connection_status)
