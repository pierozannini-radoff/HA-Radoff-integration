"""
Bait values on the three paths that produce log records, and on the dump.

Covers: the levels every declared field may be written at, the scrubbing of
text the integration did not write, and the diagnostics dump.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

import pytest
import requests
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radoff import async_migrate_entry
from custom_components.radoff.const import DOMAIN, PERSONAL, SECRETS, TENANT
from custom_components.radoff.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)

from .conftest import (
    BASE_URL,
    auth_result,
    load_devices_fixture,
    load_fixture,
    make_id_token,
    patch_authenticate_user,
    register_devices,
    register_domains,
)

# One bait value per declared field, recognisable on sight in a log or a
# dump, and shaped like the real thing where the shape is what the code
# reads: a legacy identifier is a UUID, a username is an email address.
CANARY_PASSWORD = "RDF-CANARY-PASSWORD-0001"
CANARY_ACCESS_TOKEN = "RDF-CANARY-ACCESS-0001"
CANARY_REFRESH_TOKEN = "RDF-CANARY-REFRESH-0001"
CANARY_USERNAME = "canary.user@canary.invalid"
CANARY_DOMAIN_PREFIX = "canary-tenant"
CANARY_DOMAIN_ID = "ca9a1c00-0000-4000-8000-000000000001"
CANARY_LEGACY_DEVICE_ID = "ca9a1c00-0000-4000-8000-000000000002"
CANARY_SERIAL = "RDF-CANARY-0001"
CANARY_NESTED_SERIAL = "RDF-CANARY-0002"

CANARY_ID_TOKEN = make_id_token([CANARY_DOMAIN_ID])

SECRET_VALUES = frozenset(
    {
        CANARY_PASSWORD,
        CANARY_ID_TOKEN,
        CANARY_ACCESS_TOKEN,
        CANARY_REFRESH_TOKEN,
    }
)
PERSONAL_VALUES = frozenset({CANARY_USERNAME})
TENANT_VALUES = frozenset(
    {
        CANARY_DOMAIN_PREFIX,
        CANARY_DOMAIN_ID,
        CANARY_LEGACY_DEVICE_ID,
        CANARY_SERIAL,
        CANARY_NESTED_SERIAL,
    }
)

CANARY_ENTRY_DATA = {
    "username": CANARY_USERNAME,
    "password": CANARY_PASSWORD,
    "domain_prefix": CANARY_DOMAIN_PREFIX,
}

# Everything a record is made of, the traceback included: what reaches
# `home-assistant.log` is the formatted record, not the format string.
_FORMATTER = logging.Formatter("%(message)s")

# Records of this integration only. What Home Assistant writes about an entry
# of its own accord is written by code this repository does not own.
_OURS = f"custom_components.{DOMAIN}"


def records_of(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return every record of one test, the ones its fixtures emitted included."""
    return [*caplog.get_records("setup"), *caplog.records]


def _rendered(record: logging.LogRecord) -> str:
    """Return one record as the log file would hold it."""
    return _FORMATTER.format(record)


def _leak(record: logging.LogRecord, value: str) -> str:
    """Return the failure text naming the value, the level and the line."""
    return (
        f"{record.levelname} record from {record.name}:{record.lineno} "
        f"carries {value!r}: {_rendered(record)}"
    )


def assert_log_holds_the_line(records: list[logging.LogRecord]) -> None:
    """Assert no bait value reached a record it is not allowed at."""
    for record in records:
        if not record.name.startswith(_OURS):
            continue

        text = _rendered(record)

        for value in SECRET_VALUES:
            assert value not in text, _leak(record, value)

        if record.levelno <= logging.DEBUG:
            continue

        for value in PERSONAL_VALUES | TENANT_VALUES:
            assert value not in text, _leak(record, value)


def assert_dump_holds_the_line(diagnostics: dict[str, Any]) -> None:
    """Assert no bait value survived anywhere in a diagnostics dump."""
    dumped = json.dumps(diagnostics, default=str)
    for value in SECRET_VALUES | PERSONAL_VALUES | TENANT_VALUES:
        assert value not in dumped, f"the diagnostics dump carries {value!r}"


def canary_devices_page() -> dict[str, Any]:
    """Return a one-device page carrying bait values in every identifying field."""
    page = load_devices_fixture("devices_one_device.json")
    device = page["devices"][0]
    device["serial_number"] = CANARY_SERIAL
    device["domain_prefix"] = CANARY_DOMAIN_PREFIX

    # A status outside the taxonomy and a device model this version does not
    # know: the two points that report a device by name at all.
    device["connection_status"] = "canary-unknown-status"

    nested = copy.deepcopy(device)
    nested["serial_number"] = CANARY_NESTED_SERIAL
    nested["type"] = "life"
    nested["managed_by_device_serial"] = CANARY_SERIAL
    device["controller_of_device_serial"] = CANARY_NESTED_SERIAL
    device["controller_of_device"] = nested

    return page


def canary_domains_payload() -> dict[str, Any]:
    """Return a discovery payload with the bait domain and a malformed entry."""
    payload = load_fixture("domains_multi.json")
    payload["email"] = CANARY_USERNAME
    payload["domains"][0]["domain"]["prefix"] = CANARY_DOMAIN_PREFIX
    payload["domains"][0]["domain"]["name"] = "Canary"

    # An element the flow cannot use, which is exactly the one whose contents
    # are unknown: it must be reported by its keys.
    payload["domains"].append(
        {
            "domain": {"id": CANARY_DOMAIN_ID, "password": CANARY_PASSWORD},
            "role": {"code": "user"},
        }
    )
    return payload


def canary_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return an entry of the current version, carrying bait values."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CANARY_ENTRY_DATA,
        options={"generate_index": True},
        version=3,
        unique_id=CANARY_USERNAME,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def canary_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the Cognito handshake with bait tokens."""
    patch_authenticate_user(
        monkeypatch,
        result=auth_result(
            CANARY_ID_TOKEN,
            access_token=CANARY_ACCESS_TOKEN,
            refresh_token=CANARY_REFRESH_TOKEN,
        ),
    )


@pytest.fixture
async def canary_entry(
    hass: HomeAssistant,
    requests_mock: Any,
    canary_auth: None,  # noqa: ARG001
) -> MockConfigEntry:
    """Set up one entry against a device page full of bait values."""
    register_devices(requests_mock, canary_devices_page())

    entry = canary_config_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_a_poll_writes_no_identifier_above_debug(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    requests_mock: Any,
    canary_auth: None,  # noqa: ARG001
) -> None:
    """A full poll writes no credential at any level and no identifier above DEBUG."""
    caplog.set_level(logging.DEBUG)
    register_devices(requests_mock, canary_devices_page())

    entry = canary_config_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert_log_holds_the_line(records_of(caplog))


async def test_a_failed_request_writes_no_identifier_above_debug(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    requests_mock: Any,
    canary_auth: None,  # noqa: ARG001
) -> None:
    """A network failure holds its request URL above DEBUG, traceback included."""
    caplog.set_level(logging.DEBUG)

    # The message a failed request carries is the library's, and quotes the
    # URL whole: the query string is where the domain travels.
    requests_mock.get(
        f"{BASE_URL}/data/devices",
        exc=requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='v2.api.dev.iot.radoff.life', port=443): "
            "Max retries exceeded with url: /data/devices?domain_prefix="
            f"{CANARY_DOMAIN_PREFIX}&page=1 (Caused by NewConnectionError('no route'))"
        ),
    )

    entry = canary_config_entry(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert_log_holds_the_line(records_of(caplog))


async def test_a_refused_domain_writes_no_identifier_above_debug(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    requests_mock: Any,
    canary_entry: MockConfigEntry,
) -> None:
    """A 403 naming the domain in its own body reaches no record above DEBUG."""
    caplog.set_level(logging.DEBUG)
    caplog.clear()

    register_devices(
        requests_mock,
        {
            "message": (
                f"Forbidden: {CANARY_USERNAME} does not belong to domain "
                f"'{CANARY_DOMAIN_PREFIX}' ({CANARY_DOMAIN_ID}), which owns "
                f"device {CANARY_SERIAL}"
            )
        },
        status_code=403,
    )
    await canary_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert_log_holds_the_line(caplog.records)


async def test_the_migration_writes_no_identifier_above_debug(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Migrating entities writes their identifiers no higher than DEBUG."""
    caplog.set_level(logging.DEBUG)

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        data={
            "username": CANARY_USERNAME,
            "password": CANARY_PASSWORD,
            "domain_id": CANARY_DOMAIN_ID,
        },
        unique_id=CANARY_USERNAME,
    )
    entry.add_to_hass(hass)
    _seed_legacy_registry(hass, entry)

    assert await async_migrate_entry(hass, entry)

    assert_log_holds_the_line(caplog.records)


async def test_the_config_flow_writes_no_identifier_above_debug(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    requests_mock: Any,
    canary_auth: None,  # noqa: ARG001
) -> None:
    """Configuring an account writes neither its credentials nor its domain."""
    caplog.set_level(logging.DEBUG)
    register_domains(requests_mock, canary_domains_payload())
    register_devices(requests_mock, canary_devices_page())

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"username": CANARY_USERNAME, "password": CANARY_PASSWORD},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"domain_prefix": CANARY_DOMAIN_PREFIX}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert_log_holds_the_line(caplog.records)


async def test_the_dump_carries_no_identifier(
    hass: HomeAssistant,
    canary_entry: MockConfigEntry,
) -> None:
    """The diagnostics dump of a healthy entry carries no bait value."""
    diagnostics = await async_get_config_entry_diagnostics(hass, canary_entry)

    assert_dump_holds_the_line(diagnostics)


async def test_the_dump_carries_no_identifier_from_last_exception(
    hass: HomeAssistant,
    canary_entry: MockConfigEntry,
) -> None:
    """An exception naming the domain in its text is scrubbed out of the dump."""
    coordinator = canary_entry.runtime_data
    coordinator.last_exception = RuntimeError(
        f"Access to Radoff domain '{CANARY_DOMAIN_PREFIX}' was refused for "
        f"{CANARY_USERNAME} on device {CANARY_SERIAL} ({CANARY_DOMAIN_ID})"
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, canary_entry)

    assert diagnostics["last_exception"] is not None
    assert_dump_holds_the_line(diagnostics)


def test_the_dump_redacts_what_the_three_layers_declare() -> None:
    """The dump holds back every declared field, whatever layer declares it."""
    assert TO_REDACT == SECRETS | PERSONAL | TENANT
    assert TO_REDACT == {
        "password",
        "username",
        "domain_prefix",
        "domain_id",
        "device_id",
        "serial",
        "serial_number",
        "IdToken",
        "AccessToken",
        "RefreshToken",
        "unique_id",
        "title",
    }


def _seed_legacy_registry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Create the registries of a released installation, with bait identifiers."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, CANARY_SERIAL)},
        name="Canary device",
        manufacturer="Radoff",
        model="nowplus",
    )

    # One migrates, and the pair collides onto a single measure so the other
    # two outcomes - a removal and an entity left alone - are logged too.
    for slug in ("eco2", "airqualityindex", "airqualityindex_average"):
        entity_registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{DOMAIN}-{CANARY_LEGACY_DEVICE_ID}-{slug}",
            suggested_object_id=f"canary_{slug}",
            config_entry=entry,
            device_id=device.id,
        )

    # No device, so no serial to key on: the path that warns about an entity
    # it cannot migrate.
    entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}-{CANARY_LEGACY_DEVICE_ID}-tvoc",
        suggested_object_id="canary_orphan",
        config_entry=entry,
    )


async def test_an_unknown_status_names_the_device_type_and_four_digits(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    canary_entry: MockConfigEntry,  # noqa: ARG001
) -> None:
    """The warning about an unknown status carries the type and four digits."""
    warnings = [
        record
        for record in records_of(caplog)
        if record.levelno == logging.WARNING
        and "canary-unknown-status" in record.getMessage()
    ]

    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "nowplus" in message
    assert CANARY_SERIAL[-4:] in message
    assert CANARY_SERIAL not in message


async def test_a_device_this_version_does_not_model_is_named_at_debug(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    canary_entry: MockConfigEntry,  # noqa: ARG001
) -> None:
    """The nested device nobody can act on is reported at DEBUG, whole."""
    naming_it = [
        record
        for record in records_of(caplog)
        if CANARY_NESTED_SERIAL in record.getMessage()
    ]

    assert naming_it
    assert {record.levelno for record in naming_it} == {logging.DEBUG}
    assert "does not model" in naming_it[0].getMessage()


async def test_a_domain_with_no_prefix_is_reported_by_its_keys(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    requests_mock: Any,
    canary_auth: None,  # noqa: ARG001
) -> None:
    """An element the flow cannot use is reported by its keys, not its contents."""
    caplog.set_level(logging.DEBUG)
    register_domains(requests_mock, canary_domains_payload())
    register_devices(requests_mock, canary_devices_page())

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"username": CANARY_USERNAME, "password": CANARY_PASSWORD},
    )

    skipped = [
        record for record in caplog.records if "no prefix" in record.getMessage()
    ]

    assert len(skipped) == 1
    message = skipped[0].getMessage()
    assert "['domain', 'role']" in message
    assert CANARY_DOMAIN_ID not in message
    assert CANARY_PASSWORD not in message


async def test_the_migration_reports_what_it_did_without_naming_identifiers(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One summary line counts what a migration migrated, removed and left alone."""
    caplog.set_level(logging.DEBUG)

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        data={
            "username": CANARY_USERNAME,
            "password": CANARY_PASSWORD,
            "domain_id": CANARY_DOMAIN_ID,
        },
        unique_id=CANARY_USERNAME,
    )
    entry.add_to_hass(hass)
    _seed_legacy_registry(hass, entry)

    assert await async_migrate_entry(hass, entry)

    summaries = [
        record
        for record in caplog.records
        if record.levelno == logging.INFO and "entity migration" in record.getMessage()
    ]

    assert len(summaries) == 1
    message = summaries[0].getMessage()
    assert "2 migrated, 1 removed as duplicates, 1 left alone" in message
    assert "1 with no device to read a serial from" in message


async def test_a_migration_with_nothing_to_do_says_so_at_debug(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    canary_entry: MockConfigEntry,  # noqa: ARG001
) -> None:
    """The summary of a run that touched nothing stays out of a default log."""
    summaries = [
        record
        for record in records_of(caplog)
        if "entity migration" in record.getMessage()
    ]

    assert summaries
    assert {record.levelno for record in summaries} == {logging.DEBUG}


def test_the_message_shown_on_a_refused_domain_names_no_domain() -> None:
    """What the interface shows on a 403 is a translation, so it names nothing."""
    strings = json.loads(
        (
            Path(__file__).resolve().parent.parent
            / "custom_components"
            / DOMAIN
            / "strings.json"
        ).read_text(encoding="utf-8")
    )

    message = strings["exceptions"]["domain_access_denied"]["message"]

    assert "{" not in message
