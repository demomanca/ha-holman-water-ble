"""Init for Holman Water BLE integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_MAC, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    DEVICE_TYPE_MAP,
    DOMAIN,
    MANUFACTURER,
    PLATFORMS,
    SERVICE_UNPAIR,
)
from .coordinator import HolmanWaterCoordinator
from .models import DeviceConfig

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Holman Water BLE from a config entry.

    Args:
        hass: Home Assistant instance.
        entry: Config entry for this device.

    Returns:
        True if setup was successful.
    """
    mac_address: str = entry.data[CONF_MAC]
    device_type: int = entry.data.get("device_type", 0)
    device_name: str = entry.data.get("device_name", "Holman Water Device")

    # If device type is unknown (0), default to BX1
    # The real type will be updated after the first diagnostics read
    if device_type not in DEVICE_TYPE_MAP:
        device_type = 100
        _LOGGER.info("Defaulting to BX1 for %s", mac_address)

    # Get device config from type
    type_info = DEVICE_TYPE_MAP[device_type]
    device_config = DeviceConfig(
        model=type_info[0],
        name=type_info[1],
        total_zones=type_info[2],
        is_ac_device=type_info[3],
    )

    # Create a BLEDevice-like object from the MAC address
    # We need to resolve the BLE device at runtime
    from bleak import BleakScanner

    ble_device = await BleakScanner.find_device_by_address(
        mac_address, timeout=10.0
    )

    if ble_device is None:
        _LOGGER.error(
            "Could not find BLE device %s. "
            "Make sure it is powered on and in range.",
            mac_address,
        )
        return False

    coordinator = HolmanWaterCoordinator(
        ble_device=ble_device,
        device_config=device_config,
        hass_config_dir=hass.config.path(),
    )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Entries created before the device's real type was known default to BX1
    # above. Once a diagnostics read identifies a different type, persist it
    # and reload the entry so per-zone entities match the real device.
    async def _persist_device_type_and_reload(corrected_type: int) -> None:
        try:
            if entry.data.get("device_type", 0) == corrected_type:
                return
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "device_type": corrected_type}
            )
        except Exception as exc:
            _LOGGER.error(
                "Failed to persist device type %d for %s: %s",
                corrected_type,
                mac_address,
                exc,
            )
            return
        if entry.state is not ConfigEntryState.LOADED:
            return
        await hass.config_entries.async_reload(entry.entry_id)

    def _handle_device_type_changed() -> None:
        if entry.state is not ConfigEntryState.LOADED:
            return
        info = coordinator.device_info
        if info is None:
            return
        new_type = info.device_type
        if entry.data.get("device_type", 0) == new_type:
            return
        _LOGGER.info(
            "Correcting device type for %s: %d -> %d",
            mac_address,
            entry.data.get("device_type", 0),
            new_type,
        )
        # Defer to a task: this callback fires from within a tracked
        # coordinator task, so awaiting the reload here would deadlock.
        hass.async_create_task(_persist_device_type_and_reload(new_type))

    coordinator.register_device_type_changed_callback(_handle_device_type_changed)

    # Register device in device registry
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, mac_address)},
        identifiers={(DOMAIN, mac_address)},
        manufacturer=MANUFACTURER,
        name=device_name,
        model=device_config.model,
        sw_version=f"Type {device_type} ({device_config.model})",
    )

    # `async_get_or_create` does not rewrite `name`, `model`, or
    # `sw_version` on an existing device, so refresh the registry entry
    # when the stored device type was corrected and the entry reloaded
    # (e.g. BX1 -> BX2). `name` only updates the default name; a name
    # the user set in the UI still takes precedence.
    expected_sw_version = f"Type {device_type} ({device_config.model})"
    device_info = device_registry.async_get_device(
        identifiers={(DOMAIN, mac_address)}
    )
    if device_info is not None and (
        device_info.model != device_config.model
        or device_info.sw_version != expected_sw_version
    ):
        device_registry.async_update_device(
            device_info.id,
            name=device_config.name,
            model=device_config.model,
            sw_version=expected_sw_version,
        )

    # Forward to entity platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Trigger an initial diagnostics read in the background
    # so sensors show live data as soon as possible.
    diag_task = hass.async_create_task(coordinator.read_diagnostics())
    coordinator.track_background_task(diag_task)

    # Start periodic health check polling
    coordinator.start_polling()

    # Register services
    _register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.

    Args:
        hass: Home Assistant instance.
        entry: Config entry to unload.

    Returns:
        True if unload was successful.
    """
    coordinator: HolmanWaterCoordinator = hass.data[DOMAIN].get(entry.entry_id)
    if coordinator:
        # Set shutdown flag so no new operations start
        coordinator._shutdown = True
        # Cancel all background tasks (initial diagnostics, etc.)
        await coordinator._cancel_all_background_tasks()
        # Stop periodic polling
        await coordinator.stop_polling()
        # Cancel watering timers (will detect shutdown)
        coordinator.cancel_all_watering_timers()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok


def _register_services(hass: HomeAssistant) -> None:
    """Register custom services for the integration."""

    async def handle_unpair(call: Any) -> None:
        """Handle the unpair service call."""
        mac_address = call.data.get("mac_address")
        if not mac_address:
            _LOGGER.error("mac_address is required for unpair service")
            return

        for entry_id, coordinator in hass.data.get(DOMAIN, {}).items():
            if coordinator.mac_address == mac_address:
                await coordinator.unpair()
                return

        _LOGGER.error("Device %s not found", mac_address)

    hass.services.async_register(DOMAIN, SERVICE_UNPAIR, handle_unpair)
