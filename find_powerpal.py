import asyncio
from bleak import BleakScanner

async def main():
    print("Scanning for all BLE devices (15 seconds)…")
    devices = await BleakScanner.discover(timeout=15, return_adv=True)
    print(f"\nFound {len(devices)} device(s):\n")
    for addr, (device, adv) in sorted(devices.items(), key=lambda x: x[1][1].rssi or -999, reverse=True):
        name = device.name or "(unnamed)"
        print(f"  {device.address}  RSSI:{adv.rssi:>4}  {name}")

asyncio.run(main())
