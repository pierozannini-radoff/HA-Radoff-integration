"""
Base entity for the radoff integration.

Only identity is captured at creation time; everything data-bearing is read
fresh from the coordinator, so a device leaving a poll shows within a cycle.
"""

import logging
from datetime import datetime
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.models import ConnectionState, RadoffDevice, Reading
from .const import DOMAIN
from .coordinator import RadoffCoordinator

_LOGGER = logging.getLogger(__name__)


class RadoffEntity(CoordinatorEntity[RadoffCoordinator]):
    """Base class for Radoff entities backed by a single (device, field) pair."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RadoffCoordinator,
        device: RadoffDevice,
        field: str,
    ) -> None:
        """Initialize the entity from the device object seen when it was created."""
        super().__init__(coordinator, context=field)
        self._device_type = device.device_type
        self._serial_number = device.serial_number
        self._device_name = device.name
        self._firmware_version = device.firmware_version
        self.field = field

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Metadata only: field name and device serial, never a reading's value.
        _LOGGER.debug("Refreshing %s for device %s", self.field, self._serial_number)
        super()._handle_coordinator_update()

    @property
    def _device(self) -> RadoffDevice | None:
        """Look up this entity's device fresh from the coordinator's current data."""
        return self.coordinator.get_device_by_serial(self._serial_number)

    @property
    def _reading(self) -> Reading | None:
        """Return this entity's current Reading, or None if not in this poll."""
        device = self._device
        if device is None:
            return None
        return device.readings.get(self.field)

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial_number)},
            name=self._device_name,
            manufacturer="Radoff",
            model=self._device_type,
            sw_version=self._firmware_version,
        )

    @property
    def unique_id(self) -> str:
        """Return unique id: `radoff-{serial_number}-{field}`."""
        return f"{DOMAIN}-{self._serial_number}-{self.field}"

    @property
    def measured_at(self) -> datetime | None:
        """Return the timestamp the API attached to this entity's own reading."""
        reading = self._reading
        return None if reading is None else reading.measured_at

    @property
    def available(self) -> bool:
        """
        Return whether this entity's device is reachable.

        Reachability is `connection_state`, never the presence of a reading: a
        connected device can report no telemetry for hours, because the API
        searches a 6-hour window and does not widen it. Only an explicit
        `disconnected` takes entities down; an unrecognised status leaves them
        available.
        """
        if not super().available:
            return False

        device = self._device
        if device is None:
            return False

        return device.connection_state is not ConnectionState.DISCONNECTED

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """
        Expose the two timestamps and the two statuses a support case needs.

        The timestamps answer different questions: `last_measured_at` is when
        the device produced data, `connection_status_updated_at` when its
        connection last *changed* - not when it was last checked, so an old
        value beside `connected` is a stable device, not a frozen field. Keys
        whose value is unknown are omitted, so an attribute that is present is
        always a fact.
        """
        device = self._device
        if device is None:
            return None

        attributes: dict[str, Any] = {}
        measured_at = self.measured_at
        if measured_at is not None:
            attributes["last_measured_at"] = measured_at.isoformat()
        if device.connection_status is not None:
            attributes["connection_status"] = device.connection_status
        if device.connection_status_updated_at is not None:
            attributes["connection_status_updated_at"] = (
                device.connection_status_updated_at.isoformat()
            )
        if device.status is not None:
            attributes["status"] = device.status

        return attributes or None
