"""Resolution (any value from DPI_MIN to DPI_MAX) used by the scan button."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, DPI_MAX, DPI_MIN
from .entity import NeatEntity


async def async_setup_entry(hass, entry, async_add_entities):
    c = hass.data[DOMAIN][entry.entry_id]
    # Before 0.10 the resolution was a drop-down (select) with the same unique id: drop it
    registry = er.async_get(hass)
    old = registry.async_get_entity_id("select", DOMAIN, f"{entry.entry_id}_dpi")
    if old:
        registry.async_remove(old)
    async_add_entities([DpiNumber(c)])


class DpiNumber(NeatEntity, NumberEntity):
    _attr_translation_key = "dpi"
    _attr_native_min_value = DPI_MIN
    _attr_native_max_value = DPI_MAX
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "dpi"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator):
        super().__init__(coordinator, "dpi")

    @property
    def native_value(self) -> float:
        return self.coordinator.dpi

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.dpi = int(round(value))
        self.coordinator.async_update_listeners()
