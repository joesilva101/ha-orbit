"""Binary sensors for Orbit hubs (connectivity, auto-close, lockout, safety)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import OrbitConfigEntry, OrbitDataUpdateCoordinator
from .entity import OrbitEntity, async_add_entities_when_present
from .orbitclient import DoorSensorStatus, OrbitDevice
from .orbitclient.const import ERRNO_SAFETY_REFUSAL


@dataclass(frozen=True, kw_only=True)
class OrbitBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[OrbitDevice], bool | None]
    exists_fn: Callable[[OrbitDevice], bool]


def _has_door_sensor(d: OrbitDevice) -> bool:
    return d.door_sensor not in (None, DoorSensorStatus.NOT_INSTALLED)


SENSORS: tuple[OrbitBinarySensorDescription, ...] = (
    OrbitBinarySensorDescription(
        key="connectivity",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.online,
        exists_fn=lambda _d: True,
    ),
    OrbitBinarySensorDescription(
        # On while an automatic close counts down: the wall console's
        # timer-to-close, or the ~10 s warning before an app/HA close.
        key="autoclose",
        translation_key="autoclose",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.autoclose_pending,
        exists_fn=lambda d: d.autoclose_pending is not None,
    ),
    OrbitBinarySensorDescription(
        # Wall console lockout: blocks the wireless remotes (not the app/HA).
        # Hubs only report it once it has been used, so it may appear later.
        key="remote_lockout",
        translation_key="remote_lockout",
        value_fn=lambda d: d.remote_lockout,
        exists_fn=lambda d: d.remote_lockout is not None,
    ),
    OrbitBinarySensorDescription(
        # On for 5-17 s after the opener refuses to close because the
        # photo-eye is blocked. (A reversal mid-travel is not reported.)
        key="close_refused",
        translation_key="close_refused",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda d: d.error_code == ERRNO_SAFETY_REFUSAL,
        exists_fn=lambda d: d.error_code is not None,
    ),
    OrbitBinarySensorDescription(
        key="door_sensor_battery",
        translation_key="door_sensor_battery",
        device_class=BinarySensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.door_sensor is DoorSensorStatus.LOW_BATTERY,
        exists_fn=_has_door_sensor,
    ),
    OrbitBinarySensorDescription(
        key="door_sensor_problem",
        translation_key="door_sensor_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.door_sensor is DoorSensorStatus.LOST,
        exists_fn=_has_door_sensor,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OrbitConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities_when_present(
        entry, async_add_entities, SENSORS, OrbitBinarySensor
    )


class OrbitBinarySensor(OrbitEntity, BinarySensorEntity):
    entity_description: OrbitBinarySensorDescription

    def __init__(
        self,
        coordinator: OrbitDataUpdateCoordinator,
        device_id: str,
        description: OrbitBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator, device_id)
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def available(self) -> bool:
        # Connectivity must stay available to report "disconnected".
        if self.entity_description.key == "connectivity":
            return self.coordinator.last_update_success and self.device is not None
        return super().available

    @property
    def is_on(self) -> bool | None:
        device = self.device
        return None if device is None else self.entity_description.value_fn(device)
