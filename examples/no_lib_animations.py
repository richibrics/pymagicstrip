import asyncio
import time
from bleak import BleakClient
import colorsys

address = "FF:FF:22:00:7B:BD"
SERVICE_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"

# I got a great connection and transition without any delay


async def mainOld(address):
    client = BleakClient(address)
    try:
        await client.connect()
        # Make a red green colors fade
        red = 255
        green = 0
        blue = 0
        while True:
            for i in range(0, 255):
                red -= 1
                blue += 1
                await client.write_gatt_char(
                    SERVICE_UUID, b"\x03" + bytes([red, green, blue]), response=False
                )
                await asyncio.sleep(0.1)
            # then back
            for i in range(0, 255):
                red += 1
                blue -= 1
                await client.write_gatt_char(
                    SERVICE_UUID, b"\x03" + bytes([red, green, blue]), response=False
                )
                await asyncio.sleep(0.1)
            break  # remove this to loop forever

    except Exception as e:
        print(e)
    finally:
        await client.disconnect()


async def main(address):
    client = BleakClient(address)
    try:
        # measure time
        t0 = time.time()
        await client.connect()
        print("Connected in ", time.time() - t0, "s")
        # Make a red green colors fade
        red = 255
        green = 0
        blue = 0
        while True:
            for i in range(0, 360):
                hue = i / 360.0
                r, g, b = colorsys.hsv_to_rgb(hue, 1, 1)
                red = int(r * 255)
                green = int(g * 255)
                blue = int(b * 255)

                await client.write_gatt_char(
                    SERVICE_UUID, b"\x03" + bytes([red, green, blue]), response=False
                )
                await asyncio.sleep(0.01)

            # Create a smooth transition back
            for i in range(359, -1, -1):
                hue = i / 360.0
                r, g, b = colorsys.hsv_to_rgb(hue, 1, 1)
                red = int(r * 255)
                green = int(g * 255)
                blue = int(b * 255)

                await client.write_gatt_char(
                    SERVICE_UUID, b"\x03" + bytes([red, green, blue]), response=False
                )
                await asyncio.sleep(0.01)

            break  # remove this to loop forever

    except Exception as e:
        print(e)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main(address))
