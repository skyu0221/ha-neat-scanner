"""DPI and color mode selectors used by the scan button."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity

from .const import COLOR_OPTIONS, DOMAIN, DPI_OPTIONS
from .entity import NeatEntity


async def async_setup_entry(hass, entry, async_add_entities):
    c = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DpiSelect(c), ColorSelect(c)])


class DpiSelect(NeatEntity, SelectEntity):
    _attr_translation_key = "dpi"
    _attr_options = [str(d) for d in DPI_OPTIONS]

    def __init__(self, coordinator):
        super().__init__(coordinator, "dpi")

    @property
    def current_option(self) -> str:
        return str(self.coordinator.dpi)

    async def async_select_option(self, option: str) -> None:
        self.coordinator.dpi = int(option)
        self.async_write_ha_state()


class ColorSelect(NeatEntity, SelectEntity):
    _attr_translation_key = "color"
    _attr_options = COLOR_OPTIONS

    def __init__(self, coordinator):
        super().__init__(coordinator, "color")

    @property
    def current_option(self) -> str:
        return self.coordinator.color

    async def async_select_option(self, option: str) -> None:
        self.coordinator.color = option
        self.async_write_ha_state()
