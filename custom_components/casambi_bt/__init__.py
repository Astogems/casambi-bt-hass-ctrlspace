"""The Casambi Bluetooth integration."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta
import logging
from pathlib import Path
from typing import Final

from CasambiBt import Casambi, Group, Scene, Unit, UnitControlType
from CasambiBt.errors import (
    AuthenticationError,
    BluetoothError,
    NetworkNotFoundError,
    ProtocolError,
)

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.httpx_client import get_async_client

from .const import DOMAIN, PLATFORMS

_LOGGER: Final = logging.getLogger(__name__)

# How often the watchdog checks that the controller is still connected and,
# if not, attempts to reconnect. This is the safety net that recovers the
# integration after an ungraceful drop (e.g. cutting mains power at the wall
# switch) without requiring a Home Assistant restart.
#
# The interval is deliberately short so recovery is bounded by roughly this
# many seconds plus the reconnect itself. Ticks are cheap: while connected the
# check is a single property read, and while disconnected the reconnect lock
# means a tick during an in-flight attempt is a no-op, so a short interval
# cannot pile up connection attempts.
RECONNECT_WATCHDOG_INTERVAL: Final = timedelta(seconds=5)

# How many consecutive watchdog checks may report the device as invisible
# before we force a teardown of any stale BLE link.
#
# A powered-on Casambi node stops advertising while it believes it still has a
# client. If the Casambi session dies but the underlying BLE link is left
# half-open, the node goes quiet, Home Assistant stops seeing advertisements,
# and the watchdog can never find a device to reconnect to: a deadlock that
# previously only an integration reload or a Home Assistant restart could
# break. Forcing a disconnect drops the stale link so the node advertises
# again and the next watchdog tick can reconnect.
WATCHDOG_FORCE_TEARDOWN_AFTER: Final = 3

# No BLE operation may block forever. Without these, a single hung connect or
# disconnect holds _reconnect_lock indefinitely, and because every recovery
# path either takes that lock or gives up when it is held, the integration
# wedges permanently: the watchdog skips every tick, commands block, and even
# unloading the config entry hangs, so only a Home Assistant restart recovers.
CONNECT_TIMEOUT: Final = 30.0
DISCONNECT_TIMEOUT: Final = 10.0
# How long anything else will wait for an in-flight reconnect to finish before
# giving up and proceeding without the lock.
LOCK_WAIT_TIMEOUT: Final = 5.0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Casambi Bluetooth from a config entry."""
    api = CasambiApi(hass, entry, entry.data[CONF_ADDRESS], entry.data[CONF_PASSWORD])
    await api.connect()
    api.start_watchdog()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = api

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    casa_api: CasambiApi = hass.data[DOMAIN][entry.entry_id]
    await casa_api.disconnect()

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


def get_cache_dir(hass: HomeAssistant) -> Path:
    """Return the cache dir that should be used by CasambiBt."""
    conf_path = Path(hass.config.config_dir)
    return conf_path / ".storage" / DOMAIN


class CasambiApi:
    """Defines a Casambi API."""

    def __init__(
        self,
        hass: HomeAssistant,
        conf_entry: ConfigEntry,
        address: str,
        password: str,
    ) -> None:
        """Initialize a Casambi API."""

        self.hass = hass
        self.conf_entry = conf_entry
        self.address = address
        self.password = password
        self.casa: Casambi = Casambi(get_async_client(hass), get_cache_dir(hass))

        self._callback_map: dict[int, list[Callable[[Unit], None]]] = {}
        self._cancel_bluetooth_callback: Callable[[], None] | None = None
        self._cancel_watchdog: Callable[[], None] | None = None
        self._watchdog_misses = 0
        self._link_suspect = False
        self._reconnect_lock = asyncio.Lock()
        self._first_disconnect = True

    def _register_bluetooth_callback(self) -> None:
        self._cancel_bluetooth_callback = bluetooth.async_register_callback(
            self.hass,
            self._bluetooth_callback,
            {"address": self.address, "connectable": True},
            bluetooth.BluetoothScanningMode.ACTIVE,
        )

    async def connect(self) -> None:
        """Connect to the Casmabi network."""
        try:
            device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if not device:
                raise NetworkNotFoundError  # noqa: TRY301

            self.casa.registerDisconnectCallback(self._casa_disconnect)
            self.casa.registerUnitChangedHandler(self._unit_changed_handler)

            try:
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    await self.casa.connect(device, self.password)
            except BaseException:
                await self._release_failed_connection()
                raise
            self._first_disconnect = True
            self._link_suspect = False
        except BluetoothError as err:
            raise ConfigEntryNotReady("Failed to use bluetooth") from err
        except NetworkNotFoundError as err:
            raise ConfigEntryNotReady(
                f"Network with address {self.address} wasn't found"
            ) from err
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(
                f"Failed to authenticate to network {self.address}"
            ) from err
        except ProtocolError as err:
            # The handshake stalled or the device dropped mid-exchange. This is
            # transient, so ask HA to retry rather than failing the entry.
            self._link_suspect = True
            raise ConfigEntryNotReady(
                f"Handshake with network {self.address} did not complete: {err}"
            ) from err
        except TimeoutError as err:
            self._link_suspect = True
            raise ConfigEntryNotReady(
                f"Timed out connecting to network {self.address}"
            ) from err
        except Exception as err:  # pylint: disable=broad-except
            raise ConfigEntryError(
                f"Unexpected error creating network {self.address}"
            ) from err

        # Only register bluetooth callback after connection.
        # Otherwise we get an immediate callback and attempt two connections at once.
        if not self._cancel_bluetooth_callback:
            self._register_bluetooth_callback()

    @property
    def available(self) -> bool:
        """Return True if the controller is available."""
        return self.casa.connected

    def get_units(
        self, control_types: list[UnitControlType] | None = None
    ) -> Iterable[Unit]:
        """Return all units in the network optionally filtered by control type."""

        if not control_types:
            return self.casa.units

        return filter(
            lambda u: any(uc.type in control_types for uc in u.unitType.controls),  # type: ignore[arg-type]
            self.casa.units,
        )

    def get_groups(self) -> Iterable[Group]:
        """Return all groups in the network."""

        return self.casa.groups

    def get_scenes(self) -> Iterable[Scene]:
        """Return all scenes in the network."""

        return self.casa.scenes

    def start_watchdog(self) -> None:
        """Start the periodic reconnect watchdog.

        The watchdog is a safety net on top of the disconnect callback and the
        Bluetooth advertisement callback. Those two paths can fail to recover
        the connection after an ungraceful drop (for example when the fixture
        is switched off at the mains and then back on): the light can end up
        connected at the BLE level but with the Casambi handshake incomplete,
        so it no longer advertises and no advertisement callback ever fires.
        The watchdog periodically checks the real connection state and keeps
        retrying until the controller is back, so no HA restart is needed.
        """
        if self._cancel_watchdog is None:
            self._cancel_watchdog = async_track_time_interval(
                self.hass, self._watchdog, RECONNECT_WATCHDOG_INTERVAL
            )

    @callback
    def _watchdog(self, now: datetime | None = None) -> None:
        """Ensure the controller is connected, reconnecting if it is not."""
        if self.casa.connected or self._reconnect_lock.locked():
            return
        _LOGGER.debug(
            "Watchdog detected a disconnected controller; attempting reconnect."
        )
        self.conf_entry.async_create_background_task(
            self.hass, self._watchdog_reconnect(), "Casambi watchdog reconnect"
        )

    async def _watchdog_reconnect(self) -> None:
        """Reconnect from the watchdog, clearing a stale link if necessary."""
        device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if device is None:
            self._watchdog_misses += 1
            if self._watchdog_misses < WATCHDOG_FORCE_TEARDOWN_AFTER:
                _LOGGER.debug(
                    "Watchdog: device is not currently visible (%i/%i); "
                    "will retry later.",
                    self._watchdog_misses,
                    WATCHDOG_FORCE_TEARDOWN_AFTER,
                )
                return
            # The device staying invisible while we believe we are disconnected
            # is the signature of a stale, half-open link. Drop it so the node
            # starts advertising again.
            _LOGGER.debug(
                "Watchdog: device invisible for %i checks; forcing teardown of "
                "any stale BLE link.",
                self._watchdog_misses,
            )
            self._watchdog_misses = 0
            await self._force_teardown()
            return

        self._watchdog_misses = 0
        try:
            await self.try_reconnect()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug(
                "Watchdog reconnect attempt failed; will retry later.", exc_info=True
            )

    async def _release_failed_connection(self) -> None:
        """Release the BLE client left behind by a failed connect.

        establish_connection can bring the GATT link up and then the Casambi
        key exchange never completes: CasambiClient.exchangeKey waits on an
        asyncio.Event with no timeout, so a node that connects but never sends
        the key-exchange notification hangs the connect forever.

        The library only tears the client down in disconnect(), so a connect
        that fails or times out leaves its BleakClient connected. The node then
        has an open session it will not re-key, and every later attempt fails
        the same way. Nothing short of ending the Home Assistant process
        dropped that link, which is why only a restart used to help - a config
        entry reload built new objects but the old link was still held.
        """
        _LOGGER.debug("Releasing the BLE client left behind by a failed connect.")
        try:
            async with asyncio.timeout(DISCONNECT_TIMEOUT):
                await self.casa.disconnect()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug(
                "Failed to release the connection after a failed connect.",
                exc_info=True,
            )

    async def _force_teardown(self) -> None:
        """Drop any stale BLE link so the controller starts advertising again."""
        if self._reconnect_lock.locked():
            return
        async with self._reconnect_lock:
            try:
                async with asyncio.timeout(DISCONNECT_TIMEOUT):
                    await self.casa.disconnect()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.debug("Forced teardown failed.", exc_info=True)

    async def disconnect(self) -> None:
        """Disconnect from the controller and disable automatic reconnect.

        This runs while the config entry is unloading, so it must always
        finish. Waiting unconditionally for _reconnect_lock here is what makes
        a reload hang forever behind a stuck reconnect, leaving a restart as
        the only way out, so the lock is best-effort.
        """
        got_lock = False
        try:
            async with asyncio.timeout(LOCK_WAIT_TIMEOUT):
                await self._reconnect_lock.acquire()
            got_lock = True
        except TimeoutError:
            _LOGGER.warning(
                "A reconnect was still in flight after %ss; tearing down "
                "anyway so the config entry can unload.",
                LOCK_WAIT_TIMEOUT,
            )

        try:
            if self._cancel_watchdog is not None:
                self._cancel_watchdog()
                self._cancel_watchdog = None

            if self._cancel_bluetooth_callback is not None:
                self._cancel_bluetooth_callback()
                self._cancel_bluetooth_callback = None

            # This needs to happen before we disconnect.
            # We don't want to be informed about disconnects initiated by us.
            self.casa.unregisterDisconnectCallback(self._casa_disconnect)

            try:
                async with asyncio.timeout(DISCONNECT_TIMEOUT):
                    await self.casa.disconnect()
            except TimeoutError:
                _LOGGER.warning("Timed out disconnecting from the controller.")
            except Exception:
                _LOGGER.exception("Error during disconnect.")
            self.casa.unregisterUnitChangedHandler(self._unit_changed_handler)
        finally:
            if got_lock:
                self._reconnect_lock.release()

    @callback
    def _casa_disconnect(self) -> None:
        self._link_suspect = True
        if self._first_disconnect:
            self._first_disconnect = False
            self.conf_entry.async_create_background_task(
                self.hass, self._delayed_reconnect(), "Delayed reconnect"
            )

    async def _delayed_reconnect(self) -> None:
        await asyncio.sleep(30)

        async with self._reconnect_lock:
            if self.casa.connected:
                return

        _LOGGER.debug("Starting delayed reconnect.")
        device = bluetooth.async_ble_device_from_address(self.hass, self.address)
        if device is not None:
            try:
                await self.try_reconnect()
            except Exception:
                _LOGGER.exception("Error during reconnect. This is not unusual.")
        else:
            _LOGGER.debug("Skipping reconnect. HA reports device not present.")

    async def try_reconnect(self) -> None:
        """Attemtps to reconnect to the Casambi network. Disconnects first to ensure a consitent state."""
        if self._reconnect_lock.locked():
            return

        # Use locking to ensure that only one reconnect can happen at a time.
        # Not sure if this is necessary.
        await self._reconnect_lock.acquire()

        try:
            try:
                async with asyncio.timeout(DISCONNECT_TIMEOUT):
                    await self.casa.disconnect()
            # HACK: This is a workaround for https://github.com/lkempf/casambi-bt-hass/issues/26
            # We don't actually need to disconnect except to clean up so this should be ok to ignore.
            except AttributeError:
                _LOGGER.debug("Unexpected failure during disconnect.")
            except TimeoutError:
                _LOGGER.debug("Disconnect timed out; continuing with connect.")
            await self.connect()
        finally:
            self._reconnect_lock.release()

    async def ensure_connected(self, force: bool = False) -> None:
        """Make sure a live connection exists before sending a command.

        Entities are never marked unavailable, so a command can arrive at any
        time. Rebuild the connection on demand rather than failing.

        casa.connected being True is not by itself proof of a usable link: the
        library flag can survive a link that is actually dead, in which case
        every recovery path keyed off it silently does nothing. So a rebuild
        also happens when the link is merely suspect (a disconnect was seen,
        or the previous command failed) or when the caller forces one.
        try_reconnect disconnects before connecting, so this doubles as the
        escape from a stale, half-open link.
        """
        if self.casa.connected and not force and not self._link_suspect:
            return

        if self._reconnect_lock.locked():
            # A reconnect is already in flight; wait for it rather than
            # starting a competing one, but never block the command forever.
            _LOGGER.debug("Command issued during an in-flight reconnect; waiting.")
            try:
                async with asyncio.timeout(LOCK_WAIT_TIMEOUT):
                    async with self._reconnect_lock:
                        pass
            except TimeoutError:
                _LOGGER.debug("In-flight reconnect did not finish in time.")
        else:
            _LOGGER.debug(
                "Rebuilding connection before command (forced=%s, suspect=%s, "
                "library_connected=%s).",
                force,
                self._link_suspect,
                self.casa.connected,
            )
            await self.try_reconnect()

        if not self.casa.connected:
            raise HomeAssistantError(
                f"Casambi network {self.address} is not connected and an "
                "on-demand reconnect did not succeed"
            )

    async def execute(self, action: Callable[[], Awaitable[None]]) -> None:
        """Run a command, reconnecting first if needed and retrying once.

        `action` must be a factory returning a fresh awaitable on each call,
        because it may be awaited twice.
        """
        await self.ensure_connected()
        try:
            await action()
        except Exception:  # pylint: disable=broad-except
            # The link looked usable but the command did not get through.
            # Treat it as dead, rebuild it, and give the command one more go.
            _LOGGER.debug(
                "Command failed; forcing a reconnect and retrying once.",
                exc_info=True,
            )
            self._link_suspect = True
            await self.ensure_connected(force=True)
            await action()

    def register_unit_updates(self, unit: Unit, c: Callable[[Unit], None]) -> None:
        """Register a callback for unit updates.

        :param unit: The unit for which changes should be reported.
        :param c: The callback.
        """
        self._callback_map.setdefault(unit.deviceId, []).append(c)

    def unregister_unit_updates(self, unit: Unit, c: Callable[[Unit], None]) -> None:
        """Unregister a callback for unit updates.

        :param unit: The unit for which changes should no longer be reported.
        :param c: The callback.
        """
        self._callback_map[unit.deviceId].remove(c)

    @callback
    def _unit_changed_handler(self, unit: Unit) -> None:
        if unit.deviceId not in self._callback_map:
            return
        for c in self._callback_map[unit.deviceId]:
            c(unit)

    @callback
    def _bluetooth_callback(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        _change: bluetooth.BluetoothChange,
    ) -> None:
        if not self.casa.connected and service_info.connectable:
            _LOGGER.debug(
                "Advertisement received while disconnected; reconnecting now."
            )
            self.conf_entry.async_create_background_task(
                self.hass, self.try_reconnect(), "Reconnect"
            )
