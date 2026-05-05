"""
PowerPal BLE API Key Retrieval
==============================
Connects to your PowerPal device over Bluetooth, authenticates with your
pairing code, and retrieves your device serial number and API key.

Usage:
    python get_powerpal_key.py
    python get_powerpal_key.py AA:BB:CC:DD:EE:FF 123456

Requirements:
    pip install bleak requests
"""

import sys
import json
import asyncio
import subprocess
import requests
from pathlib import Path
from bleak import BleakClient, BleakScanner

CONFIG_FILE = Path(__file__).parent / "powerpal_ble.json"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(address: str, pairing_code: int) -> None:
    CONFIG_FILE.write_text(json.dumps({"address": address, "pairing_code": pairing_code}, indent=2))
    print(f"  (Saved to {CONFIG_FILE.name} — won't ask again)")

PAIRING_CODE_CHAR  = '59DA0011-12F4-25A6-7D4F-55961DCE4205'
API_KEY_CHAR       = '59DA0009-12F4-25A6-7D4F-55961DCE4205'
SERIAL_CHAR        = '59DA0010-12F4-25A6-7D4F-55961DCE4205'


def encode_pairing_code(code: int) -> bytes:
    return int(code).to_bytes(4, byteorder='little')


def _find_paired_mac(search="PowerPal"):
    """Find a paired Bluetooth device's MAC by name substring using blueutil --paired."""
    import re
    try:
        result = subprocess.run(['blueutil', '--paired'], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            if search.lower() in line.lower():
                m = re.search(r'([0-9a-f]{2}(?:-[0-9a-f]{2}){5})', line, re.IGNORECASE)
                if m:
                    return m.group(1)
    except FileNotFoundError:
        pass
    return None


def _unpair_device(address: str, device_name: str = None) -> None:
    """Remove cached macOS bonding so the device allows application-level auth."""
    try:
        mac = _find_paired_mac("PowerPal")
        if mac:
            print(f"  Found paired PowerPal at {mac} — unpairing…")
        unpair_addr = mac or address
        result = subprocess.run(
            ['blueutil', '--unpair', unpair_addr],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            print("Unpaired device from macOS Bluetooth cache.")
            return
        err = result.stderr.strip() or result.stdout.strip()
        print(f"  (blueutil unpair failed: {err})")
    except FileNotFoundError:
        print("  (blueutil not installed — install with: brew install blueutil)")
    print("  → If reads fail, go to System Settings > Bluetooth, Forget the device, and retry.")


async def main(address=None, pairing_code=None):
    cfg = load_config()

    if address is None:
        address = cfg.get("address")
        if address:
            print(f"Using saved MAC address: {address}")
    while address is None:
        address = input("PowerPal address (MAC e.g. DF:5C:55:XX:XX:XX  or macOS UUID): ").strip()
        is_mac  = address.count(':') == 5 and len(address) == 17
        is_uuid = address.count('-') == 4 and len(address) == 36
        if not is_mac and not is_uuid:
            print("  Incorrect format — enter a MAC (12:34:56:78:9A:BC) or macOS UUID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)")
            address = None

    if pairing_code is None:
        pairing_code = cfg.get("pairing_code")
        if pairing_code:
            print(f"Using saved pairing code: {pairing_code}")
    while pairing_code is None:
        try:
            pairing_code = int(input("PowerPal 6-digit pairing code: ").strip())
            if not (0 <= pairing_code <= 999999):
                raise ValueError
        except ValueError:
            print("  Must be a 6-digit number")
            pairing_code = None

    if not cfg:
        save_config(address, pairing_code)

    input("\nMake sure the PowerPal app on your phone is closed, then press Enter to connect…")

    print("Scanning to locate device (up to 30s)…")
    device = await BleakScanner.find_device_by_address(address, timeout=30)
    if device is None:
        # Fallback: broad scan and match by address
        print("Direct scan missed it — trying broad scan…")
        all_devices = await BleakScanner.discover(timeout=15, return_adv=True)
        match = next(((d, a) for d, a in all_devices.values()
                      if d.address.upper() == address.upper()), None)
        if match is None:
            print(f"Could not find {address}. Try running find_powerpal.py first to confirm it's visible.")
            return
        device = match[0]
        print(f"Found via broad scan: {device.address}")

    _unpair_device(address, device.name)

    # Connect using existing Windows pairing (encrypted automatically)
    async with BleakClient(device) as client:
        print(f"Connected: {client.is_connected}")

        print(f"Authenticating with pairing code {pairing_code}…")
        await client.write_gatt_char(PAIRING_CODE_CHAR, encode_pairing_code(pairing_code), response=True)
        print("Auth OK\n")

        print("Reading serial number…")
        serial_bytes = await client.read_gatt_char(SERIAL_CHAR)
        serial = ''.join(f'{b:02x}' for b in reversed(serial_bytes)).lower()
        print(f"  Serial:   {serial}")
        print(f"  Endpoint: https://readings.powerpal.net/api/v1/meter_reading/{serial}\n")

        print("Reading API key…")
        key_bytes = await client.read_gatt_char(API_KEY_CHAR)
        hex_key   = ''.join(f'{b:02x}' for b in key_bytes)
        api_key   = f"{hex_key[:8]}-{hex_key[8:12]}-{hex_key[12:16]}-{hex_key[16:20]}-{hex_key[20:]}".lower()
        print(f"  API key:  {api_key}\n")

        validate = input("Validate against the PowerPal API now? (y/n): ").strip().lower()
        if validate.startswith('y'):
            url = f"https://readings.powerpal.net/api/v1/device/{serial}"
            resp = requests.get(url, headers={'Authorization': api_key})
            if resp.status_code < 300:
                print("\nSuccess! Device info:")
                print(resp.text)
            else:
                print(f"\nFailed: {resp.status_code} {resp.reason}")
                print(resp.text)
        else:
            print("To validate manually:")
            print(f'  curl -H "Authorization: {api_key}" https://readings.powerpal.net/api/v1/device/{serial}')

        cfg_save = load_config()
        cfg_save["serial"]  = serial
        cfg_save["api_key"] = api_key
        CONFIG_FILE.write_text(json.dumps(cfg_save, indent=2))

        print("\n── Saved to powerpal_ble.json ──────────────────")
        print(f"  Serial:  {serial}")
        print(f"  API key: {api_key}")
        print("────────────────────────────────────────────────")


if __name__ == "__main__":
    asyncio.run(main(
        sys.argv[1] if len(sys.argv) >= 2 else None,
        int(sys.argv[2]) if len(sys.argv) >= 3 else None,
    ))
