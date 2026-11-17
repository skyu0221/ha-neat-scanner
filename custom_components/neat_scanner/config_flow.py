"""Config and options flow."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.core import callback

from .const import (
    COLOR_OPTIONS, CONF_AUTO_CROP, CONF_DUPLEX, DEFAULT_DUPLEX, CONF_MONITOR, CONF_SCAN_ON_BUTTON, DEFAULT_MONITOR, DEFAULT_SCAN_ON_BUTTON, CONF_BUSID, CONF_COLOR, CONF_DPI, CONF_FORMAT, CONF_HOST,
    CONF_PORT, CONF_SAVE_DIR, CONF_SCAN_INTERVAL, CONF_SHEETS, DEFAULT_AUTO_CROP, DEFAULT_COLOR, DEFAULT_DPI,
    DEFAULT_FORMAT, DEFAULT_PORT, DEFAULT_SAVE_DIR, DEFAULT_SHEETS, DEFAULT_SCAN_INTERVAL,
    DOMAIN, DPI_MAX, DPI_MIN, FORMAT_OPTIONS, NEAT_PID, NEAT_VID,
)
from .usbip_client import UsbIpError, list_devices


class NeatScannerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict = {}
        self._devices: dict[str, str] = {}

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            if not self.hass.config.is_allowed_path(user_input[CONF_SAVE_DIR]):
                errors["base"] = "dir_not_allowed"
            else:
                try:
                    devices = await list_devices(user_input[CONF_HOST], user_input[CONF_PORT])
                except UsbIpError:
                    errors["base"] = "cannot_connect"
                else:
                    if not devices:
                        errors["base"] = "no_devices"
                    else:
                        self._data = user_input
                        self._devices = {
                            d.busid: f"{d.busid} ({d.vid:04x}:{d.pid:04x})"
                            + (" – Neat ADF scanner" if (d.vid, d.pid) == (NEAT_VID, NEAT_PID) else "")
                            for d in devices
                        }
                        return await self.async_step_device()
        schema = vol.Schema({
            vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
            vol.Required(CONF_SAVE_DIR, default=DEFAULT_SAVE_DIR): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_device(self, user_input=None):
        if user_input is not None:
            await self.async_set_unique_id(f"{self._data[CONF_HOST]}_{user_input[CONF_BUSID]}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="Neat ADF scanner", data={**self._data, **user_input}
            )
        default = next((b for b, label in self._devices.items() if "Neat" in label), None)
        schema = vol.Schema({
            vol.Required(CONF_BUSID, **({"default": default} if default else {})): vol.In(self._devices)
        })
        return self.async_show_form(step_id="device", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return NeatScannerOptionsFlow()


class NeatScannerOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        cur = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            if self.hass.config.is_allowed_path(user_input[CONF_SAVE_DIR]):
                return self.async_create_entry(data=user_input)
            errors["base"] = "dir_not_allowed"
        schema = vol.Schema({
            vol.Required(CONF_SAVE_DIR, default=cur.get(CONF_SAVE_DIR, DEFAULT_SAVE_DIR)): str,
            vol.Required(CONF_DPI, default=cur.get(CONF_DPI, DEFAULT_DPI)): vol.All(int, vol.Range(min=DPI_MIN, max=DPI_MAX)),
            vol.Required(CONF_COLOR, default=cur.get(CONF_COLOR, DEFAULT_COLOR)): vol.In(COLOR_OPTIONS),
            vol.Required(CONF_FORMAT, default=cur.get(CONF_FORMAT, DEFAULT_FORMAT)): vol.In(FORMAT_OPTIONS),
            vol.Required(CONF_SHEETS, default=cur.get(CONF_SHEETS, DEFAULT_SHEETS)): vol.All(int, vol.Range(min=1, max=100)),
            vol.Required(CONF_MONITOR, default=cur.get(CONF_MONITOR, DEFAULT_MONITOR)): bool,
            vol.Required(CONF_SCAN_ON_BUTTON, default=cur.get(CONF_SCAN_ON_BUTTON, DEFAULT_SCAN_ON_BUTTON)): bool,
            vol.Required(CONF_DUPLEX, default=cur.get(CONF_DUPLEX, DEFAULT_DUPLEX)): bool,
            vol.Required(CONF_AUTO_CROP, default=cur.get(CONF_AUTO_CROP, DEFAULT_AUTO_CROP)): bool,
            vol.Required(CONF_SCAN_INTERVAL, default=cur.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)):
                vol.All(int, vol.Range(min=10, max=600)),
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
