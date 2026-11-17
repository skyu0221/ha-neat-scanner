"""Double-sided switch used by the scan button."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity

from .const import DOMAIN
from .entity import NeatEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DuplexSwitch(hass.data[DOMAIN][entry.entry_id])])


class DuplexSwitch(NeatEntity, SwitchEntity):
    _attr_translation_key = "duplex"

    def __init__(self, coordinator):
        super().__init__(coordinator, "duplex")

    @property
    def is_on(self) -> bool:
        return self.coordinator.duplex

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.duplex = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.duplex = False
        self.async_write_ha_state()
