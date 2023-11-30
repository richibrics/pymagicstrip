import asyncio
from bleak import BleakClient, BleakScanner
import colorsys
import pymagicstrip
from pymagicstrip import MagicStripDevice
from pymagicstrip import MagicStripState
from pymagicstrip.const import SERVICE_UUID
from pymagicstrip.errors import BleTimeoutError

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakDBusError
import logging

address = "FF:FF:22:00:7B:BD"
# SERVICE_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"

logging.basicConfig(level=logging.INFO)
scanner = None


async def detection_callback(
    ble_device: BLEDevice,
    advertisement_data: AdvertisementData,
) -> None:
    if not pymagicstrip.device_filter(ble_device, advertisement_data):
        # print("Not a MagicStrip device")
        return
    print("Found a MagicStrip device:", ble_device.address)

    await scanner.stop()

    device = MagicStripDevice(ble_device.address)
    await device.detection_callback(ble_device, advertisement_data)
    await device.update()
    print(device.state)
    await device.disconnect()
    print("Disconnected")


async def main():
    global scanner
    scanner = BleakScanner(filters={"UUIDs": [str(SERVICE_UUID)]})
    scanner.register_detection_callback(detection_callback)
    await scanner.start()

    await asyncio.sleep(20.0)


if __name__ == "__main__":
    asyncio.run(main())
