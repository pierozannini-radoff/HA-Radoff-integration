"""Domain dataclasses for the Radoff API."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class ConnectionState(StrEnum):
    """
    A device's `connection_status` as this client reads it.

    Three states, not two: the backend has not published the full
    enumeration, so an unrecognised or absent value is INDETERMINATE and the
    device stays usable rather than being reported offline.
    """

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    INDETERMINATE = "indeterminate"


# The only `connection_status` values the API is known to send. INDETERMINATE
# is absent on purpose: it is this client's reading, never a value received.
KNOWN_CONNECTION_STATUSES: dict[str, ConnectionState] = {
    "connected": ConnectionState.CONNECTED,
    "disconnected": ConnectionState.DISCONNECTED,
}


def classify_connection_status(raw: str | None) -> ConnectionState:
    """
    Map a raw `connection_status` string onto a `ConnectionState`.

    Case- and whitespace-insensitive. Pure: it runs on every state read of
    every entity, so it must not log, count or cache anything.
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

    The API exposes one timestamp for the whole block, defined as the maximum
    of the individual fields', so every `Reading` of a device shares the same
    `measured_at` and a slow field such as radon can be older than it claims.
    """

    name: str
    value: float | int
    measured_at: datetime | None = None


@dataclass
class RadoffDevice:
    """
    One device as `GET /data/devices` describes it.

    `stale` means the device reported no telemetry this cycle, which is not
    the same as offline: the API searches a 6-hour window and does not widen
    it, so a connected device that has been silent for longer answers with no
    telemetry at all. `connection_status_updated_at` is when the status last
    changed, not when it was last checked.
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
        """Return this device's connection as one of three states."""
        return classify_connection_status(self.connection_status)
