"""Data update coordinator for Roborock custom integration."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DeviceSnapshot, RoborockAuthenticationError, RoborockCloudApi, RoborockConnectionError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, MAX_SCAN_INTERVAL, MIN_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


class RoborockDataUpdateCoordinator(DataUpdateCoordinator[dict[str, DeviceSnapshot]]):
    """Fetches and caches Roborock device state."""

    def __init__(self, hass, api: RoborockCloudApi, scan_interval_seconds: int = DEFAULT_SCAN_INTERVAL) -> None:
        self.api = api
        interval = max(MIN_SCAN_INTERVAL, min(MAX_SCAN_INTERVAL, int(scan_interval_seconds)))
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )

    async def _async_update_data(self) -> dict[str, DeviceSnapshot]:
        try:
            return await self.api.async_refresh_snapshots()
        except RoborockAuthenticationError as err:
            raise ConfigEntryAuthFailed(f"Authentification invalide: {err}") from err
        except RoborockConnectionError as err:
            raise UpdateFailed(f"Erreur Roborock: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Erreur inattendue: {err}") from err
