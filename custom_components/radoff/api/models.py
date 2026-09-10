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
"""

from dataclasses import dataclass, field
from datetime import datetime


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
    current. `RadoffEntity.available` judges freshness from this value, so
    until the API exposes a per-field timestamp that check is structurally
    unable to see a single stale field - only a device that has gone quiet
    altogether. Written down here rather than left implicit because the
    freshness logic reads correct and is not.
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

    - `connection_status` / `connection_status_updated_at`: availability
      based on them is M-06. This card only brings them into the model.
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
    for it. Deriving availability from the connection status is M-06's job;
    until then `stale` stays what it says: no data this cycle, not "down".

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
    firmware_version: str | None = None
    room_name: str | None = None
    room_slug: str | None = None
    building_name: str | None = None
    building_slug: str | None = None
    domain_prefix: str | None = None
    telemetry_timestamp: datetime | None = None
    stale: bool = False
