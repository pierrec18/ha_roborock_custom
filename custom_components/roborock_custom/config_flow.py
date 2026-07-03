"""Config flow for Roborock custom integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from roborock.exceptions import RoborockException, RoborockInvalidCode, RoborockTooFrequentCodeRequests
from roborock.web_api import RoborockApiClient

from .const import (
    CONF_BASE_URL,
    CONF_CODE,
    CONF_DEVICE_IDENTIFIER,
    CONF_SCAN_INTERVAL,
    CONF_USER_DATA,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)


def _normalize_code(code: str) -> str:
    return "".join(code.split())


class RoborockCustomConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Roborock custom integration."""

    VERSION = 1

    def __init__(self) -> None:
        self._pending_username: str | None = None
        self._pending_base_url: str | None = None
        self._pending_device_identifier: str | None = None
        self._pending_error: str | None = None
        self._reauth_entry = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """First step: password login, and fallback to 2FA code if required."""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = str(user_input.get(CONF_USERNAME, "")).strip().lower()
            password = str(user_input.get(CONF_PASSWORD, ""))
            if not username:
                errors["base"] = "invalid_auth"
            else:
                await self.async_set_unique_id(username)
                self._abort_if_unique_id_configured()

                reused_entry_data = self._import_official_roborock_entry(username)
                if reused_entry_data is not None:
                    self._reset_pending_state()
                    return self.async_create_entry(title=username, data=reused_entry_data)

                if not password:
                    errors["base"] = "official_auth_not_found"
                else:
                    login_result = await self._async_login_with_password(username, password)
                    if isinstance(login_result, dict):
                        self._reset_pending_state()
                        return self.async_create_entry(title=username, data=login_result)

                    if login_result == "two_factor":
                        return await self.async_step_two_factor()

                    errors["base"] = login_result

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Optional(CONF_PASSWORD, default=""): str,
                }
            ),
            errors=errors,
        )

    async def async_step_two_factor(self, user_input: dict[str, Any] | None = None):
        """Second step: code login with the same client identifier."""
        errors: dict[str, str] = {}
        if self._pending_username is None or self._pending_device_identifier is None:
            return self.async_abort(reason="two_factor_context_missing")

        if self._pending_error is not None:
            errors["base"] = self._pending_error
            self._pending_error = None

        if user_input is not None:
            code = _normalize_code(str(user_input.get(CONF_CODE, "")))
            if not code:
                errors["base"] = "invalid_code"
            else:
                web_api = RoborockApiClient(
                    self._pending_username,
                    base_url=self._pending_base_url,
                    session=async_get_clientsession(self.hass),
                )
                web_api._device_identifier = self._pending_device_identifier
                try:
                    user_data = await self._async_login_with_code(web_api, code)
                except RoborockInvalidCode:
                    errors["base"] = "invalid_code"
                except RoborockException:
                    errors["base"] = "cannot_connect"
                except Exception:
                    errors["base"] = "unknown"
                else:
                    data = await self._build_entry_data(self._pending_username, web_api, user_data)
                    return await self._async_finalize_login(data)

        return self.async_show_form(
            step_id="two_factor",
            data_schema=vol.Schema({vol.Required(CONF_CODE): str}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        """Handle entry reauthentication."""
        self._reauth_entry = self._get_reauth_entry()
        username = entry_data.get(CONF_USERNAME)
        if not isinstance(username, str) or not username:
            return self.async_abort(reason="invalid_auth")
        self._pending_username = username
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        """Confirm credentials during reauth."""
        if self._pending_username is None:
            return self.async_abort(reason="invalid_auth")

        errors: dict[str, str] = {}
        if user_input is not None:
            reused_entry_data = self._import_official_roborock_entry(self._pending_username)
            if reused_entry_data is not None:
                return await self._async_finalize_login(reused_entry_data)

            password = str(user_input.get(CONF_PASSWORD, ""))
            if not password:
                errors["base"] = "official_auth_not_found"
            else:
                login_result = await self._async_login_with_password(self._pending_username, password)
                if isinstance(login_result, dict):
                    return await self._async_finalize_login(login_result)
                if login_result == "two_factor":
                    return await self.async_step_two_factor()
                errors["base"] = login_result

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Optional(CONF_PASSWORD, default=""): str}),
            errors=errors,
        )

    async def _async_login_with_password(self, username: str, password: str) -> dict[str, Any] | str:
        web_api = RoborockApiClient(username, session=async_get_clientsession(self.hass))
        try:
            user_data = await web_api.pass_login(password)
            return await self._build_entry_data(username, web_api, user_data)
        except RoborockException as err:
            message = str(err)
            if "response code: 2031" in message:
                self._pending_username = username
                self._pending_base_url = await self._safe_base_url(web_api)
                self._pending_device_identifier = web_api._device_identifier
                self._pending_error = await self._async_try_send_code(web_api)
                return "two_factor"
            if "response code: 2012" in message:
                return "invalid_auth"
            return "cannot_connect"
        except Exception:
            return "unknown"

    async def _async_try_send_code(self, web_api: RoborockApiClient) -> str | None:
        try:
            await web_api.request_code_v4()
            return None
        except RoborockTooFrequentCodeRequests:
            return "too_many_code_requests"
        except Exception:
            return "cannot_send_code"

    async def _async_login_with_code(self, web_api: RoborockApiClient, code: str):
        return await web_api.code_login_v4(code)

    async def _safe_base_url(self, web_api: RoborockApiClient) -> str | None:
        try:
            base_url = await web_api.base_url
        except Exception:
            return None
        return base_url if isinstance(base_url, str) else None

    async def _build_entry_data(self, username: str, web_api: RoborockApiClient, user_data) -> dict[str, Any]:
        base_url = await self._safe_base_url(web_api)
        data: dict[str, Any] = {
            CONF_USERNAME: username,
            CONF_USER_DATA: user_data.as_dict(),
        }
        if base_url:
            data[CONF_BASE_URL] = base_url
        if self._pending_device_identifier:
            data[CONF_DEVICE_IDENTIFIER] = self._pending_device_identifier
        return data

    async def _async_finalize_login(self, data: dict[str, Any]):
        if self._reauth_entry is not None:
            updated = {**self._reauth_entry.data, **data}
            self.hass.config_entries.async_update_entry(self._reauth_entry, data=updated)
            await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
            self._reset_pending_state()
            return self.async_abort(reason="reauth_successful")

        self._reset_pending_state()
        return self.async_create_entry(title=data[CONF_USERNAME], data=data)

    def _reset_pending_state(self) -> None:
        self._pending_username = None
        self._pending_base_url = None
        self._pending_device_identifier = None
        self._pending_error = None
        self._reauth_entry = None

    def _import_official_roborock_entry(self, username: str) -> dict[str, Any] | None:
        """Reuse auth payload from official 'roborock' integration if available."""
        official_entries = self.hass.config_entries.async_entries("roborock")
        normalized_username = username.strip().lower()
        fallback_match: dict[str, Any] | None = None

        for entry in official_entries:
            entry_data = entry.data if isinstance(entry.data, Mapping) else {}
            raw_entry_username = entry_data.get(CONF_USERNAME) or entry.title
            entry_username = str(raw_entry_username).strip().lower()

            user_data = entry_data.get(CONF_USER_DATA)
            if not isinstance(user_data, Mapping):
                continue

            data: dict[str, Any] = {
                CONF_USERNAME: normalized_username or entry_username,
                CONF_USER_DATA: dict(user_data),
            }
            base_url = entry_data.get(CONF_BASE_URL)
            if isinstance(base_url, str) and base_url:
                data[CONF_BASE_URL] = base_url

            device_identifier = entry_data.get(CONF_DEVICE_IDENTIFIER)
            if isinstance(device_identifier, str) and device_identifier:
                data[CONF_DEVICE_IDENTIFIER] = device_identifier

            if entry_username == normalized_username:
                return data

            if fallback_match is None:
                fallback_match = data

        if fallback_match is not None and len(official_entries) == 1:
            return fallback_match

        return None

    @staticmethod
    def async_get_options_flow(config_entry):
        return RoborockCustomOptionsFlow(config_entry)


class RoborockCustomOptionsFlow(config_entries.OptionsFlowWithReload):
    """Manage options for the Roborock custom integration."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        interval_default = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=interval_default): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                    )
                }
            ),
        )
