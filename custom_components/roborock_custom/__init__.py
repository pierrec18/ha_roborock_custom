"""Roborock custom integration bootstrap."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import RoborockAuthenticationError, RoborockCloudApi, RoborockConnectionError
from .const import CONF_BASE_URL, CONF_SCAN_INTERVAL, CONF_USER_DATA, DEFAULT_SCAN_INTERVAL, PLATFORMS
from .coordinator import RoborockDataUpdateCoordinator


@dataclass(slots=True)
class RoborockRuntimeData:
    """Objects kept for one config entry lifecycle."""

    api: RoborockCloudApi
    coordinator: RoborockDataUpdateCoordinator


RoborockConfigEntry = ConfigEntry[RoborockRuntimeData]


async def async_setup(_: HomeAssistant, __: dict) -> bool:
    """Set up integration from YAML (not used)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: RoborockConfigEntry) -> bool:
    """Set up Roborock from a config entry."""
    username = entry.data.get(CONF_USERNAME)
    user_data = entry.data.get(CONF_USER_DATA)
    base_url = entry.data.get(CONF_BASE_URL)

    if not isinstance(username, str) or not username:
        raise ConfigEntryNotReady("Configuration invalide: username manquant")
    if not isinstance(user_data, dict):
        raise ConfigEntryNotReady("Configuration invalide: user_data manquant")
    if base_url is not None and not isinstance(base_url, str):
        base_url = None

    try:
        api = RoborockCloudApi(
            username=username,
            user_data_raw=user_data,
            base_url=base_url,
            session=async_get_clientsession(hass),
            on_mqtt_unauthorized=lambda: entry.async_start_reauth(hass),
        )
    except Exception as err:
        raise ConfigEntryAuthFailed(f"Session Roborock invalide: {err}") from err

    try:
        await api.async_connect()
        coordinator = RoborockDataUpdateCoordinator(
            hass,
            api=api,
            scan_interval_seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:
        await api.async_disconnect()
        raise
    except RoborockAuthenticationError as err:
        await api.async_disconnect()
        raise ConfigEntryAuthFailed(f"Session Roborock expiree: {err}") from err
    except (RoborockConnectionError, Exception) as err:
        await api.async_disconnect()
        raise ConfigEntryNotReady(f"Impossible de joindre Roborock: {err}") from err

    entry.runtime_data = RoborockRuntimeData(api=api, coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RoborockConfigEntry) -> bool:
    """Unload Roborock config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.api.async_disconnect()
    return unload_ok
