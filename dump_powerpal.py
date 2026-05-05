"""Connects to PowerPal and prints all services and characteristics."""
import asyncio
from bleak import BleakClient, BleakScanner

PAIRING_CODE_CHAR = '59DA0011-12F4-25A6-7D4F-55961DCE4205'

def encode_pairing_code(code: int) -> bytes:
    return int(code).to_bytes(4, byteorder='little')

async def main():
    address      = input("MAC address: ").strip()
    pairing_code = int(input("Pairing code: ").strip())
    input("Close phone app, then press Enter…")

    print("Scanning…")
    device = await BleakScanner.find_device_by_address(address, timeout=15)
    if device is None:
        print("Not found.")
        return

    async with BleakClient(device) as client:
        print(f"Connected: {client.is_connected}\n")

        await client.write_gatt_char(PAIRING_CODE_CHAR, encode_pairing_code(pairing_code), response=False)
        print("Auth OK\n")
        await asyncio.sleep(0.5)

        for service in client.services:
            print(f"Service: {service.uuid}  {service.description}")
            for char in service.characteristics:
                props = ", ".join(char.properties)
                print(f"  Char: {char.uuid}  [{props}]  handle={char.handle}")
                if "read" in char.properties:
                    try:
                        val = await client.read_gatt_char(char.uuid, use_cached=False)
                        print(f"    Value: {val.hex()}  ({val})")
                    except Exception as e:
                        print(f"    Read error: {e}")

asyncio.run(main())
