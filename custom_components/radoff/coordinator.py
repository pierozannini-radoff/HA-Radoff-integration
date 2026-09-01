"""Class which represent the Radoff Coordinator."""

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import requests
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_CLIENT_ID,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.core import DOMAIN as HOMEASSISTANT_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import API, APIAuthError, Device
from .const import (
    CONF_DOMAIN_ID,
    CONF_INDEX,
    CONF_POOL_ID,
    CONF_POOL_REGION,
    DEFAULT_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class APIData(dict[str, Any]):
    """Class to hold api data."""

    controller_name: str
    generate_index: bool
    devices: list[Device]


class RadoffCoordinator(DataUpdateCoordinator):
    """The implementation of the Radoff coordinator."""

    data: APIData

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize coordinator."""
        self.client_id = config_entry.data[CONF_CLIENT_ID]
        self.username = config_entry.data[CONF_USERNAME]
        self.password = config_entry.data[CONF_PASSWORD]
        self.pool_id = config_entry.data[CONF_POOL_ID]
        self.pool_region = config_entry.data[CONF_POOL_REGION]
        self.domain_id = config_entry.data[CONF_DOMAIN_ID]
        self.generate_index = config_entry.data.get(CONF_INDEX, True)

        self.poll_interval = config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )

        super().__init__(
            hass,
            _LOGGER,
            name=f"{HOMEASSISTANT_DOMAIN} ({config_entry.unique_id})",
            update_method=self.async_update_data,
            update_interval=timedelta(seconds=self.poll_interval),
        )

        self.api = API(
            username=self.username,
            password=self.password,
            client_id=self.client_id,
            pool_id=self.pool_id,
            pool_region=self.pool_region,
            domain_id=self.domain_id,
        )

    async def async_update_data(self) -> APIData:
        """Fetch data from API endpoint."""
        _LOGGER.debug("Radoff async_update_data starting")
        try:
            if not self.api.connected:
                _LOGGER.info("API not connected, attempting to connect...")
                await self.hass.async_add_executor_job(self.api.connect)
                _LOGGER.info("API connection established")

            devices = await self.hass.async_add_executor_job(self.api.get_devices)

            _LOGGER.debug("Successfully fetched data for %d device", len(devices))
            return APIData(
                controller_name=self.api.controller_name,
                devices=devices,
                generate_index=self.generate_index,
            )

        except APIAuthError as err:
            _LOGGER.exception("Authentication error")
            msg = f"Authentication error: {err}"
            raise UpdateFailed(msg) from err

        except requests.exceptions.Timeout as err:
            _LOGGER.exception(
                "Request timeout after %s seconds", self.api.DEFAULT_TIMEOUT
            )
            msg = f"Request timeout: {err}"
            raise UpdateFailed(msg) from err

        except requests.exceptions.ConnectionError as err:
            _LOGGER.exception("Connection error")
            msg = f"Connection error: {err}"
            raise UpdateFailed(msg) from err

        except Exception as err:
            _LOGGER.exception("Unexpected error")
            msg = f"Unexpected error: {err}"
            raise UpdateFailed(msg) from err

    def get_device_by_id(self, device_type: str, device_id: str) -> Device | None:
        """Return device by device id."""
        _LOGGER.debug("Radoff get_device_by_id")
        try:
            for device in self.data.devices:
                if device.device_type == device_type and device.device_id == device_id:
                    return device
        except IndexError:
            return None
        return None
