"""Scan button."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory

from .const import DOMAIN
from .entity import NeatEntity


async def async_setup_entry(hass, entry, async_add_entities):
    c = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ScanButton(c), CalibrateButton(c)])


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


class CalibrateButton(NeatEntity, ButtonEntity):
    """Scan a blank white letter-size sheet to measure the white level of every sensor column."""

    _attr_translation_key = "calibrate"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator):
        super().__init__(coordinator, "calibrate")

    @property
    def available(self) -> bool:
        return bool(self.coordinator.data and self.coordinator.data.get("online")) \
            and not self.coordinator.scanning

    async def async_press(self) -> None:
        await self.coordinator.async_calibrate()
