# Getting Your PowerPal API Key

This guide walks through retrieving your PowerPal serial number and API key using Bluetooth, so `correlate.py` can fetch your energy data automatically.

---

## Prerequisites

### Python
- **Windows:** Download from [python.org](https://www.python.org/downloads/) — during install tick **"Add Python to PATH"**
- **Mac:** Python 3 is available via `brew install python` or [python.org](https://www.python.org/downloads/)

### Required libraries

**Windows:**
```powershell
py -m pip install bleak requests
```

**Mac:**
```bash
pip3 install bleak requests
```

### Mac only — blueutil (recommended)
`blueutil` allows the script to automatically remove stale Bluetooth pairings that can block the connection:
```bash
brew install blueutil
```

---

## Step 1 — Find your PowerPal pairing code

The pairing code is a 6-digit number set when your PowerPal was installed.

**To find it in the app:**
1. Open the PowerPal app on your phone
2. Go to **Settings → Your PowerPal hardware → Manage**
3. The pairing code is displayed there

**If you can't find it:**
- Check the welcome card that came in the box with the device
- Or tap **"I can't find my pairing code"** on the pairing screen in the app — it will send the code to your registered email and phone

---

## Step 2 — Find your PowerPal's Bluetooth address

### Windows
Run the scanner script to find the device MAC address:
```powershell
py find_powerpal.py
```
Look for a device named **"Powerpal XXXXXXXX"** in the output. Note the MAC address (format: `EF:5C:89:44:C6:7F`).

### Mac
macOS hides real MAC addresses — run the scanner to get the device UUID instead:
```bash
python3 find_powerpal.py
```
Note the UUID (format: `197E010A-E889-29CE-2301-50431B6C37A1`).

---

## Step 3 — Prepare your phone

Before running the retrieval script you need to release the Bluetooth connection from your phone:

1. **Force-close the PowerPal app** on your phone (don't just background it — fully close it)
2. **Turn Bluetooth off** on your phone entirely — this prevents it from automatically reconnecting and interrupting the script

---

## Step 4 — Remove any existing Bluetooth pairing

If your computer has previously paired with the PowerPal, remove it first to avoid stale security keys causing connection failures.

### Windows
1. Open **Start → Settings → Bluetooth & devices**
2. Find **"Powerpal XXXXXXXX"** in the device list
3. Click the three-dot menu → **Remove device**

### Mac
Either use the script (if blueutil is installed — it does this automatically), or:
1. Open **System Settings → Bluetooth**
2. Find the PowerPal in the device list
3. Click the **X** or **Forget This Device**

---

## Step 5 — Run the retrieval script

**Important:** Run this on a computer physically close to the PowerPal unit (ideally the same room, within 2 metres, line of sight). A weak Bluetooth signal can cause the connection to fail at the authentication step.

### Windows
```powershell
py get_powerpal_key.py
```

### Mac
```bash
python3 get_powerpal_key.py
```

When prompted:
- Enter the Bluetooth address from Step 2
- Enter your 6-digit pairing code from Step 1
- Press Enter when ready to connect

The script will connect, authenticate, and print your **serial number** and **API key**. Both are saved automatically to `powerpal_ble.json`.

---

## Step 6 — Verify it worked

The script will offer to validate your credentials against the PowerPal API. Enter `y` — a successful response looks like:

```json
{"serial_number":"XXXXXXXX","first_reading_at":...}
```

If it succeeds, you're done. `correlate.py` will load credentials from `powerpal_ble.json` automatically from now on.

---

## Troubleshooting

**"Could not find device"**
- Make sure phone Bluetooth is off
- Move closer to the PowerPal unit
- Run `find_powerpal.py` first to confirm it's visible, then immediately run `get_powerpal_key.py`

**"Read Not Permitted" or "Insufficient Authentication"**
- Move closer to the device — signal quality affects the Bluetooth security handshake
- On Windows: remove the device from Bluetooth settings (Step 4) and try again
- On Mac: install `blueutil` (`brew install blueutil`) so the script can clear the pairing automatically

**"The operation was canceled by the user"**
- A Windows pairing dialog appeared in the background and timed out
- Remove the device from Windows Bluetooth settings (Step 4) and try again

**Forgot pairing code**
- Open the PowerPal app → pairing screen → tap **"I can't find my pairing code"**
- The code will be sent to your registered email and phone number

---

## Notes

- `powerpal_ble.json` stores your address, pairing code, serial, and API key — it is excluded from version control (`.gitignore`) so credentials are never committed
- You only need to run this once — the API key doesn't change
- The PowerPal app can be reopened on your phone once the script has finished
