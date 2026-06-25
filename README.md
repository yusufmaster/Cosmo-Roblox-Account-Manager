# 🚀 Cosmo RAM

A custom **Roblox Account Manager** for Windows, written in a single Python file with a dark, high-tech "command console" UI. Manage multiple accounts, run several Roblox clients at once, auto-relaunch crashed clients, tile windows into a grid, and cap your FPS — all from one app.

> ⚠️ **Disclaimer:** Running multiple Roblox clients and managing accounts via cookies may violate Roblox's Terms of Service and can get accounts banned. Use only on accounts **you own**, at your own risk. This is a personal/educational project.

---

## Features

- **Account storage** — cookies (and optionally passwords captured at web login) are encrypted locally with Windows DPAPI, so they're tied to your Windows user account.
- **Two ways to add accounts** — paste a `.ROBLOSECURITY` cookie, or use the embedded **Web Login** window (logs in through the official Roblox page and captures the cookie automatically).
- **Multi Roblox** — holds the Roblox singleton mutex and closes the running client's handle so multiple clients can run at once.
- **Auto-Relaunch** — keep selected accounts alive; if a client closes, it relaunches automatically (configurable launcher/relaunch delays).
- **Auto Grid** — every Roblox window that opens is shrunk and tiled into a neat grid across your screen.
- **Set FPS** — edits `GlobalBasicSettings_*.xml` to cap the Roblox framerate (e.g. 5 for AFK alts, 240 to uncap), re-applied on every launch.
- **Themes** — several dark presets plus a full custom colour picker.
- **Right-click an account** — Copy Cookie / Copy Password / Copy User / Copy User:Pass Combo.

---

## Requirements

- Windows 10/11
- Python 3.10+ (only needed to run from source)

Install the dependencies:

```
pip install pywin32 requests pywebview
```

## Run from source

```
python roblox_account_manager.py
```

> Tip: Some features (Multi Roblox handle-closing, FPS file edits) work best when run **as administrator**.

## Build a standalone .exe

No Python needed for end users. With [PyInstaller](https://pyinstaller.org):

```
pip install pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name "Cosmo RAM" --icon cosmo.ico --add-data "cosmo.ico;." roblox_account_manager.py
```

The finished app is `dist/Cosmo RAM.exe`.

---

## Notes & safety

- **Never share your `accounts.dat`** — it holds your encrypted account tokens. It's tied to your Windows user, so it won't work on anyone else's PC anyway, but treat it like a password file. It is excluded from this repo via `.gitignore`.
- `accounts.dat` and `settings.json` are created automatically next to the app on first run.
- Unsigned `.exe` files may trigger a Windows SmartScreen prompt (*More info → Run anyway*) and occasional antivirus false positives — normal for PyInstaller builds.

## Credits

Inspired by the core logic of ic3w0lf's open-source Roblox Account Manager.
