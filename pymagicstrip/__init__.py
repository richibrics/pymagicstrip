"""Hub for communicating with pymagicstrip devices."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import logging
import re
from typing import Any

from bleak import BleakClient
from bleak.backends.bluezdbus.client import BleakClientBlueZDBus
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

from . import const
from .const import CHARACTERISTIC_UUID, CMD_ACK, EFFECTS, TOGGLE_POWER
from .errors import BleConnectionError, BleTimeoutError, OutOfRange

__version__ = "0.1.1"

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.DEBUG)

# handler = logging.StreamHandler()
# formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
# handler.setFormatter(formatter)
# _LOGGER.addHandler(handler)


def device_filter(device: BLEDevice, advertisement_data: AdvertisementData) -> bool:
    """Return True if device is a MagicStrip device."""

    if device.name.lower() in [
        d.lower() for d in const.HARDCODED_NAMES
    ] and const.SERVICE_UUID in device.metadata.get("uuids", []):
        return True

    return False


def _judge_rssi(rssi: int | None) -> str | None:
    """Return qualitative assessment of RSSI."""

    if rssi is None:
        return None

    if rssi >= 0:
        return "Unknown"
    if rssi >= -55:
        return "Excellent"
    if rssi >= -75:
        return "Good"
    if rssi >= -85:
        return "Bad"
    if rssi < -85:
        return "Terrible"

    return None


@dataclass(frozen=True)
class MagicStripState:
    """Device class."""

    on: bool | None = None
    brightness: int | None = None
    color: tuple[int, int, int] | None = None
    effect: str | None = None
    effect_speed: int | None = None
    rssi: int | None = None

    def replace_from_notification(
        self, on: bool, brightness: int, **changes: Any
    ) -> MagicStripState:
        """Update state based on device notification."""

        # New object returned, since this is a dataclass.
        return replace(self, on=on, brightness=brightness, **changes)

    @property
    def connection_quality(self) -> str | None:
        """Get connection quality as a ordinal string value."""

        return _judge_rssi(self.rssi)

    @property
    def effects_list(self) -> list[str]:
        """Get list of effects."""

        return list(EFFECTS)


class MagicStripDevice:
    """Communication handler."""
    
    def __init__(self, device: BLEDevice | str) -> None:
        """Initialize handler."""
        _LOGGER.debug("Initializing MagicStripDevice.")
        self.ble_device = device
        self.state = MagicStripState()
        self.lock = asyncio.Lock()
        self._client = BleakClient(self.ble_device)
        self._client.set_disconnected_callback(self._on_disconnect)
        self._is_connected = False
        self._retry_interval = 5  # Interval in seconds between retries

    async def __aenter__(self) -> MagicStripDevice:
        """Enter context, retrying connection until successful."""
        while not self._is_connected:
            try:
                _LOGGER.info("Attempting to connect to the device...")
                await self._client.__aenter__()
                self._is_connected = True
                _LOGGER.info("Device connected successfully.")
            except (asyncio.TimeoutError, asyncio.exceptions.TimeoutError) as exc:
                _LOGGER.warning("Timeout on connect, retrying in %d seconds...", self._retry_interval)
                await asyncio.sleep(self._retry_interval)
            except asyncio.CancelledError as exc:
                _LOGGER.warning("Connection cancelled, retrying in %d seconds...", self._retry_interval)
                await asyncio.sleep(self._retry_interval)
            except BleakError as exc:
                _LOGGER.warning("Error on connect: %s. Retrying in %d seconds...", exc, self._retry_interval)
                await asyncio.sleep(self._retry_interval)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore
        """Exit context."""
        pass  # avoid manual disconnect after each command

    async def disconnect(self) -> None:
        """Disconnect from device."""
        await self._client.disconnect()

    def _on_disconnect(self, dbus: BleakClientBlueZDBus) -> None:
        """Callback called when Bleak notices the device disconnected."""
        if dbus.address == self.address:
            _LOGGER.info("Device disconnected.")
            self._is_connected = False

    async def _onoff_notification_handler(self, sender, data) -> None:  # type: ignore
        """Handle HCI event notifications."""

        """
        Connection workflow:
        1. Write 0xF0 to CHARACTERISTIC_UUID (Triggers 0xF00201 response.)
        2. Write 0x0F to CHARACTERISTIC_UUID (Returns state string.)
        3. Write 0x0100 to attribute handle 4 (Unsure of purpose. Ignoring.)
        4. Receive 0xF00201 from device (Seems to be generic acknowledgement.)
        5. Receive current status message from device in format 0F WW XX YY ZZ, where:
            a. WW = 00 for 'off', 01 for 'on'
            b. XX = brightness (0: Min Brightness - 255: Max brightness)
            c. YY = last used effect speed (0: Fastest - 255: Slowest)
            d. ZZ = last used effect, with ZZ being the second byte of the effect commands starting with 0x07.

        Unfortunately, the device doesn't report current color or whether an effect is currently active, rendering
        ZZ useless.
        """

        status_components: re.Match | None
        if (
            status_components := re.search(
                const.STATUS_REGEX, status_str := bytearray.hex(data)
            )
        ) is not None:
            on = status_components.group(1) == "01"
            brightness = int(status_components.group(2), 16)

            self.state = self.state.replace_from_notification(
                on=on, brightness=brightness
            )

            _LOGGER.debug(
                "%s: Device status reported as %s. On: %s, Brightness: %s",
                self.address,
                bytearray.hex(data),
                on,
                brightness,
            )

            _LOGGER.debug("New state: %s", str(self.state))

        elif status_str == CMD_ACK:
            _LOGGER.debug("Got status ack.")
        else:
            _LOGGER.debug("Invalid status message: %s", status_str)

    @property
    def address(self) -> str:
        """Return address of the device."""
        # If string, return it since it is the address, otherwise get the address from the BLE device
        if isinstance(self.ble_device, str):
            return self.ble_device
        return str(self.ble_device.address)

    async def _send_command(self, cmd: str | list, attempts: int = 1) -> None:
        """Send given command(s) to the BLE Strip device."""

        if isinstance(cmd, list):
            for cmd_single in cmd:
                # recursive call with single elements of the commands list
                await self._send_command(cmd_single)
            return

        # when "async with self" is used, the methods __aenter__ and __aexit__ are called (before and after respectively)
        async with self.lock:
            async with self:
                try:
                    _LOGGER.debug("Sending command: %s", cmd)
                    await self._client.write_gatt_char(
                        CHARACTERISTIC_UUID, bytes.fromhex(cmd)
                    )
                    await self._client.write_gatt_char(
                        CHARACTERISTIC_UUID, bytes.fromhex("F0")
                    )
                except asyncio.TimeoutError as exc:
                    _LOGGER.debug("Timeout on write", exc_info=True)
                    raise BleTimeoutError from exc
                except BleakError as exc:
                    _LOGGER.debug("Failed to write", exc_info=True)
                    raise BleConnectionError("Failed to write") from exc
                except OSError:
                    _LOGGER.debug("Encountered OSError.")
                    if attempts <= 2:
                        _LOGGER.debug(
                            "Assuming connection has been closed. Trying again..."
                        )
                        self._send_command(cmd, attempts + 1)
                    else:
                        raise

        _LOGGER.debug("Command sent.")

    async def set_color(self, red: int, green: int, blue: int) -> None:
        """Set strip to specified color; no effects."""

        """
        Command submission workflow:
        1. Write command CHARACTERISTIC_UUID.
        2. Write 0xF0 to CHARACTERISTIC_UUID (Triggers 0xF00201 response.)
        3. Receive 0xF00201 from device (Seems to be generic acknowledgement.)

        It's easier to send a forget, so we've just implemented step 1.
        """

        for color in (red, green, blue):
            if not 0 <= color <= 255:
                raise OutOfRange

        await self._send_command(f"03{''.join(f'{i:02x}' for i in (red, green, blue))}")

        # new state is set with just set color
        self.state = replace(
            self.state, color=(red, green, blue), effect_speed=None, effect=None
        )

    async def set_brightness(self, brightness: int) -> None:
        """Set strip to specified brightness; no effects."""

        if not 0 <= brightness <= 255:
            raise OutOfRange

        await self._send_command(f"08{''.join(f'{brightness:02x}')}")

        # new state is set with just set brightness
        self.state = replace(self.state, brightness=brightness)

        await self.update()

    async def set_effect_name(self, effect: str | None) -> None:
        """Set strip to specified effect."""

        if effect not in list(EFFECTS) and effect is not None:
            raise OutOfRange

        if effect is not None:
            effect_cmd = EFFECTS[effect]
            await self._send_command(effect_cmd)

        # new state is set with just set effect
        self.state = replace(self.state, effect=effect, color=None)

    async def set_effect_speed(self, speed: int) -> None:
        """Set strip to specified effect."""

        if not 0 <= speed <= 255:
            raise OutOfRange

        # Speed is inverted. 0 is fastest; 255 is slowest. Let's keep that to ourselves.
        inv_speed = 255 - speed

        speed_cmd = f"09{inv_speed:02x}"

        # new state is set with just set effect speed
        self.state = replace(self.state, effect_speed=speed)

        await self._send_command(speed_cmd)

        _LOGGER.debug("New state: %s", self.state)

    async def toggle_power(self) -> None:
        """Set strip to specified effect."""
        await self._send_command(TOGGLE_POWER)

        self.state = replace(self.state, on=not self.state.on)

        await self.update()

    async def detection_callback(
        self, device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Handle scanner data."""

        self.state = replace(self.state, rssi=device.rssi)

        _LOGGER.debug("Discovered Device: %s", self.state)

        await self.update()

    async def update(self) -> None:
        """Query device for current power and brightness states."""

        _LOGGER.debug("Refreshing state.")

        async with self.lock:
            async with self:
                try:
                    await self._client.start_notify(
                        CHARACTERISTIC_UUID, self._onoff_notification_handler
                    )

                    await self._client.write_gatt_char(
                        CHARACTERISTIC_UUID, bytes.fromhex("F0")
                    )
                    await self._client.write_gatt_char(
                        CHARACTERISTIC_UUID, bytes.fromhex("0F")
                    )

                    # await self._client.write_gatt_descriptor(4, bytes.fromhex("0100"))

                    # Give response notification time to come in.
                    await asyncio.sleep(1)

                    await self._client.stop_notify(CHARACTERISTIC_UUID)
                except asyncio.TimeoutError as exc:
                    _LOGGER.debug("Timeout on update", exc_info=True)
                    raise BleTimeoutError from exc
                except BleakError as exc:
                    _LOGGER.debug("Failed to update", exc_info=True)
                    raise BleConnectionError("Failed to update device") from exc
