"""Scan button."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity

from .const import DOMAIN
from .entity import NeatEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ScanButton(hass.data[DOMAIN][entry.entry_id])])


class ScanButton(NeatEntity, ButtonEntity):
    _attr_translation_key = "scan"

    def __init__(self, coordinator):
        super().__init__(coordinator, "scan")

    @property
    def available(self) -> bool:
        return bool(self.coordinator.data and self.coordinator.data.get("online")) \
            and not self.coordinator.scanning

    async def async_press(self) -> None:
        await self.coordinator.async_scan()
