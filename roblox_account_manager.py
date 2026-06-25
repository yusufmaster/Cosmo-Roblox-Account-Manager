"""
Custom Roblox Account Manager (Python single-file)
==================================================
Replicates the core logic of ic3w0lf's Roblox-Account-Manager:
  - Encrypted local cookie storage (Windows DPAPI)
  - Cookie -> authentication ticket -> roblox-player:// launch
  - Multi-instance via the ROBLOX_singletonEvent mutex
  - Web Login: log in through an embedded browser and auto-capture the cookie
Instead of auto-minimizing, an "Arrange Grid" feature tiles every open Roblox
window into a neat grid on your primary monitor.

Requires Windows. Install deps:
    pip install pywin32 requests pywebview

WARNING / DISCLAIMER:
  * Running multiple Roblox clients breaks Roblox's Terms of Service and can
    get your accounts banned. Use only on accounts you own and accept the risk.
  * The mutex bypass usually works without admin, but if Roblox still refuses
    to open a second client, RIGHT-CLICK the script (or your terminal) and
    "Run as administrator".
  * Web Login uses Microsoft's WebView2 runtime (preinstalled on Windows 11).
"""

import os
import re
import sys
import json
import time
import base64
import random
import tempfile
import importlib.util
import subprocess
import threading
from urllib.parse import quote

import tkinter as tk
from tkinter import ttk, simpledialog, messagebox, colorchooser

# --- Windows-only dependencies -------------------------------------------------
try:
    import win32crypt          # DPAPI encryption
    import win32event          # mutex
    import win32api
    import win32gui
    import win32con
    import ctypes
except ImportError:
    print("Missing pywin32. Run:  pip install pywin32 requests pywebview")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("Missing requests. Run:  pip install pywin32 requests pywebview")
    sys.exit(1)


# ==============================================================================
#  CONFIG
# ==============================================================================
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))


def resource_path(name: str) -> str:
    """Path to a bundled resource — works both as a script and a PyInstaller exe."""
    base = getattr(sys, "_MEIPASS", SCRIPT_DIR)   # _MEIPASS exists only when frozen
    return os.path.join(base, name)


def app_dir() -> str:
    """Folder where the app reads/writes data (next to the exe when frozen)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return SCRIPT_DIR


DATA_FILE     = os.path.join(app_dir(), "accounts.dat")    # encrypted store
SETTINGS_FILE = os.path.join(app_dir(), "settings.json")   # plain settings
BOX_W, BOX_H  = 450, 350                                   # grid cell size (px)
GRID_GAP      = 4                                          # pixels between boxes
ROBLOX_TITLE  = "Roblox"                                   # window title to match
ROBLOX_CLASS  = "WINDOWSCLIENT"                            # Roblox window class

DEFAULT_SETTINGS = {
    "launcher_delay": 8,        # seconds between consecutive launches
    "relaunch_delay": 5,        # seconds to wait before relaunching a closed acct
    "auto_grid": False,         # auto-tile every Roblox window as it appears
    "auto_relaunch": False,     # master switch for the relaunch monitor
    "relaunch_names": [],       # accounts that should be kept alive
    "place_id": "606849621",    # last used Place ID
    "fps_cap": 60,              # Roblox target FPS (5 = slow, 240 = uncapped-ish)
    "theme": "Synth Purple",    # selected theme preset (or "Custom")
    "custom_theme": {},         # per-key colour overrides for the Custom theme
}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                s.update(json.load(f))
        except Exception:
            pass
    return s


def save_settings(s: dict):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


# ==============================================================================
#  1. ENCRYPTED STORAGE  (Windows DPAPI — tied to your Windows user account)
# ==============================================================================
def _encrypt(text: str) -> str:
    """Encrypt a string with DPAPI and return base64 so it's JSON-safe."""
    blob = win32crypt.CryptProtectData(text.encode("utf-8"), "RAM", None, None, None, 0)
    return base64.b64encode(blob).decode("ascii")


def _decrypt(b64: str) -> str:
    """Reverse of _encrypt. Only works on the same Windows user that encrypted it."""
    blob = base64.b64decode(b64.encode("ascii"))
    _desc, data = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
    return data.decode("utf-8")


def load_accounts() -> list:
    """Returns a list of dicts: {name, cookie}. Cookies are decrypted on load."""
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        out = []
        for entry in raw:
            pw = entry.get("password")
            out.append({
                "name": entry["name"],
                "cookie": _decrypt(entry["cookie"]),
                "password": _decrypt(pw) if pw else "",
            })
        return out
    except Exception as e:
        messagebox.showerror("Load error", f"Could not read accounts:\n{e}")
        return []


def save_accounts(accounts: list):
    """Encrypts every cookie (and password, if set) and writes them to disk."""
    raw = []
    for a in accounts:
        item = {"name": a["name"], "cookie": _encrypt(a["cookie"])}
        if a.get("password"):
            item["password"] = _encrypt(a["password"])
        raw.append(item)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)


# ==============================================================================
#  2. MUTEX KILLER  (hold our own mutex + close the running client's handle)
# ==============================================================================
#  Rather than closing Roblox's own handle (unstable in Python), we create the
#  named Roblox singleton mutex ourselves and keep it alive for the whole
#  lifetime of this app. As long as WE own the mutex, every Roblox client that
#  starts will share it instead of refusing to open -- this is the same
#  technique ic3w0lf's Roblox Account Manager uses for "Multi Roblox".
_mutex_handles = []
_multi_enabled = False

# Both names are used across different Roblox versions; we hold all of them.
_MUTEX_NAMES = ("ROBLOX_singletonEvent", "ROBLOX_singletonMutex")


def enable_multi_instance() -> bool:
    """Create & hold the Roblox singleton mutex(es). Returns True if held."""
    global _multi_enabled
    if _multi_enabled:
        return True
    held = False
    for name in _MUTEX_NAMES:
        try:
            h = win32event.CreateMutex(None, False, name)
            if h:
                _mutex_handles.append(h)   # keep a reference so it's never closed
                held = True
        except Exception:
            pass
    _multi_enabled = held
    return held


def disable_multi_instance():
    """Release the mutex(es) so Roblox goes back to single-instance behaviour."""
    global _multi_enabled
    for h in _mutex_handles:
        try:
            win32api.CloseHandle(h)
        except Exception:
            pass
    _mutex_handles.clear()
    _multi_enabled = False


def is_multi_enabled() -> bool:
    return _multi_enabled


# ------------------------------------------------------------------------------
#  Aggressive method: find & close the singleton handle held by running clients.
#  Some Roblox builds grab the mutant before our hold takes effect, so we
#  enumerate every handle inside each RobloxPlayerBeta.exe process and remotely
#  close the one named ROBLOX_singletonEvent / ROBLOX_singletonMutex. After that,
#  the next client is free to create its own and start. (This is what RAM does.)
# ------------------------------------------------------------------------------
from ctypes import wintypes

_ntdll    = ctypes.WinDLL("ntdll")
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_SystemExtendedHandleInformation = 64
_ObjectNameInformation           = 1
_STATUS_INFO_LENGTH_MISMATCH     = 0xC0000004
_PROCESS_DUP_HANDLE              = 0x0040
_DUPLICATE_SAME_ACCESS           = 0x0002
_DUPLICATE_CLOSE_SOURCE          = 0x0001
_TH32CS_SNAPPROCESS              = 0x0002
# Handles with this exact access mask can dead-lock NtQueryObject -> skip them.
_HANG_ACCESS                     = 0x0012019F

_ROBLOX_EXES = ("robloxplayerbeta.exe", "windows10universal.exe")


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260),
    ]


class _SYSTEM_HANDLE_ENTRY_EX(ctypes.Structure):
    _fields_ = [
        ("Object", ctypes.c_void_p), ("UniqueProcessId", ctypes.c_void_p),
        ("HandleValue", ctypes.c_void_p), ("GrantedAccess", wintypes.ULONG),
        ("CreatorBackTraceIndex", wintypes.USHORT), ("ObjectTypeIndex", wintypes.USHORT),
        ("HandleAttributes", wintypes.ULONG), ("Reserved", wintypes.ULONG),
    ]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                ("Buffer", ctypes.c_void_p)]


_ntdll.NtQuerySystemInformation.restype = ctypes.c_ulong
_ntdll.NtQueryObject.restype            = ctypes.c_ulong
_kernel32.OpenProcess.restype           = wintypes.HANDLE
_kernel32.OpenProcess.argtypes          = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.DuplicateHandle.argtypes      = [
    wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
    ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
]


def _roblox_pids() -> set:
    """Return the PIDs of every running Roblox client process."""
    pids = set()
    snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap == -1 or snap is None:
        return pids
    entry = _PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
    ok = _kernel32.Process32First(snap, ctypes.byref(entry))
    while ok:
        if entry.szExeFile.decode(errors="ignore").lower() in _ROBLOX_EXES:
            pids.add(entry.th32ProcessID)
        ok = _kernel32.Process32Next(snap, ctypes.byref(entry))
    _kernel32.CloseHandle(snap)
    return pids


def _handle_name(handle) -> str:
    """Query the kernel object name for a duplicated handle (or '')."""
    size = 0x1000
    buf = ctypes.create_string_buffer(size)
    ret = wintypes.ULONG(0)
    status = _ntdll.NtQueryObject(handle, _ObjectNameInformation, buf, size,
                                  ctypes.byref(ret))
    if status != 0:
        return ""
    us = ctypes.cast(buf, ctypes.POINTER(_UNICODE_STRING))[0]
    if not us.Buffer or us.Length == 0:
        return ""
    try:
        return ctypes.wstring_at(us.Buffer, us.Length // 2)
    except Exception:
        return ""


def close_roblox_mutexes() -> int:
    """Close the singleton handle inside every running Roblox process.

    Returns the number of handles closed. Lets the next client launch.
    """
    pids = _roblox_pids()
    if not pids:
        return 0

    # Snapshot every handle in the system, growing the buffer until it fits.
    info_len = 0x20000
    while True:
        buf = ctypes.create_string_buffer(info_len)
        ret = wintypes.ULONG(0)
        status = _ntdll.NtQuerySystemInformation(
            _SystemExtendedHandleInformation, buf, info_len, ctypes.byref(ret))
        if status == _STATUS_INFO_LENGTH_MISMATCH:
            info_len *= 2
            continue
        if status != 0:
            return 0
        break

    number = ctypes.cast(buf, ctypes.POINTER(ctypes.c_size_t))[0]
    base = ctypes.addressof(buf) + ctypes.sizeof(ctypes.c_size_t) * 2
    entries = ctypes.cast(
        base, ctypes.POINTER(_SYSTEM_HANDLE_ENTRY_EX * number))[0]

    cur = _kernel32.GetCurrentProcess()
    proc_cache = {}
    closed = 0

    for e in entries:
        pid = e.UniqueProcessId
        if pid not in pids:
            continue
        if e.GrantedAccess == _HANG_ACCESS:        # skip dead-lock-prone handles
            continue

        if pid not in proc_cache:
            proc_cache[pid] = _kernel32.OpenProcess(_PROCESS_DUP_HANDLE, False, pid)
        src = proc_cache[pid]
        if not src:
            continue

        dup = wintypes.HANDLE()
        if not _kernel32.DuplicateHandle(src, e.HandleValue, cur,
                                         ctypes.byref(dup), 0, False,
                                         _DUPLICATE_SAME_ACCESS):
            continue
        try:
            name = _handle_name(dup)
            if name and any(m in name for m in _MUTEX_NAMES):
                dummy = wintypes.HANDLE()
                if _kernel32.DuplicateHandle(src, e.HandleValue, cur,
                                             ctypes.byref(dummy), 0, False,
                                             _DUPLICATE_CLOSE_SOURCE):
                    _kernel32.CloseHandle(dummy)
                    closed += 1
        finally:
            _kernel32.CloseHandle(dup)

    for h in proc_cache.values():
        if h:
            _kernel32.CloseHandle(h)
    return closed


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


# ==============================================================================
#  3. LAUNCHING  (cookie -> auth ticket -> roblox-player protocol)
# ==============================================================================
def get_username(cookie: str) -> str:
    """Resolve the account's display name from its cookie (best-effort)."""
    try:
        r = requests.get(
            "https://users.roblox.com/v1/users/authenticated",
            cookies={".ROBLOSECURITY": cookie}, timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("name", "Unknown")
    except Exception:
        pass
    return "Unknown"


def get_auth_ticket(cookie: str) -> str:
    """Trade a .ROBLOSECURITY cookie for a one-time authentication ticket."""
    s = requests.Session()
    s.cookies[".ROBLOSECURITY"] = cookie
    headers = {
        "User-Agent": "Roblox/WinInet",
        "Referer": "https://www.roblox.com/",
        "Origin": "https://www.roblox.com",
        "Content-Type": "application/json",
    }
    url = "https://auth.roblox.com/v1/authentication-ticket/"

    # First POST is expected to fail with 403 and hand back an x-csrf-token.
    r = s.post(url, headers=headers, timeout=15)
    csrf = r.headers.get("x-csrf-token")
    if not csrf:
        raise RuntimeError("Could not get CSRF token (cookie may be invalid/expired).")

    headers["x-csrf-token"] = csrf
    r = s.post(url, headers=headers, timeout=15)
    ticket = r.headers.get("rbx-authentication-ticket")
    if not ticket:
        raise RuntimeError("Auth ticket missing (cookie invalid or rate-limited).")
    return ticket


def launch_account(cookie: str, place_id: str):
    """Build the official roblox-player launch URL and hand it to Windows."""
    ticket = get_auth_ticket(cookie)
    btid   = random.randint(10_000_000, 99_999_999)        # browser tracker id
    ltime  = int(time.time() * 1000)

    place_launcher = (
        "https://assetgame.roblox.com/game/PlaceLauncher.ashx"
        f"?request=RequestGame&browserTrackerId={btid}"
        f"&placeId={place_id}&isPlayTogetherGame=false"
    )

    url = (
        "roblox-player:1"
        "+launchmode:play"
        f"+gameinfo:{ticket}"
        f"+launchtime:{ltime}"
        f"+placelauncherurl:{quote(place_launcher, safe='')}"
        f"+browsertrackerid:{btid}"
        "+robloxLocale:en_us+gameLocale:en_us+channel:"
    )

    # Multi Roblox is controlled by the toggle button (held for the app's
    # lifetime), so we don't force it on here -- we respect the user's choice.
    os.startfile(url)         # hands the protocol link to the Roblox installer


# ==============================================================================
#  4. WEB LOGIN  (pywebview embedded browser -> auto-capture .ROBLOSECURITY)
# ==============================================================================
#  pywebview's window loop must own the main thread, which clashes with tkinter.
#  So the GUI re-launches THIS script with "--weblogin <tmpfile>" as a separate
#  process. That child opens the Roblox login page, polls the browser cookies,
#  and writes the captured .ROBLOSECURITY value to <tmpfile> for the GUI to read.

def _extract_roblosecurity(cookies) -> str:
    """Pull the .ROBLOSECURITY value out of whatever shape get_cookies() returns."""
    if not cookies:
        return None
    for c in cookies:
        # http.cookiejar.Cookie style (.name / .value)
        if getattr(c, "name", None) == ".ROBLOSECURITY":
            return c.value
        # SimpleCookie / dict-like style
        try:
            if ".ROBLOSECURITY" in c:
                return c[".ROBLOSECURITY"].value
        except TypeError:
            pass
    return None


def run_web_login_child(out_path: str):
    """Child-process entry point. Opens the login window and captures the cookie."""
    import webview   # imported here so the main GUI never hard-depends on it

    # JS that reads whatever is currently typed into the password box.
    _PW_JS = ("(function(){var e=document.getElementById('login-password');"
              "if(!e){var l=document.querySelectorAll('input[type=password]');"
              "if(l.length)e=l[0];}return e?e.value:'';})()")

    def poll_for_cookie(window):
        # Poll cookies until login completes; remember the last typed password.
        last_pw = ""
        while True:
            time.sleep(1.5)
            try:
                pw = window.evaluate_js(_PW_JS)
                if pw:
                    last_pw = pw
            except Exception:
                pass
            try:
                cookies = window.get_cookies()
            except Exception:
                continue
            value = _extract_roblosecurity(cookies)
            if value:
                try:
                    with open(out_path, "w", encoding="utf-8") as f:
                        json.dump({"cookie": value, "password": last_pw}, f)
                finally:
                    window.destroy()
                return

    window = webview.create_window(
        "Roblox Web Login  —  log in, then this window closes automatically",
        "https://www.roblox.com/login",
        width=500, height=720,
    )
    webview.start(poll_for_cookie, window)


# ==============================================================================
#  5. GRID ARRANGEMENT  (find every "Roblox" window and tile it)
# ==============================================================================
def find_roblox_windows() -> list:
    """Return HWNDs of every visible Roblox client window."""
    hwnds = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        cls   = win32gui.GetClassName(hwnd)
        if title == ROBLOX_TITLE or cls == ROBLOX_CLASS:
            hwnds.append(hwnd)

    win32gui.EnumWindows(_cb, None)
    return hwnds


def arrange_grid_core() -> int:
    """Resize every Roblox window to the smallest box and tile across screen 1.

    Returns the number of windows arranged. No popups (safe for background use).
    """
    hwnds = find_roblox_windows()
    if not hwnds:
        return 0

    screen_w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
    screen_h = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)

    cols = max(1, screen_w // (BOX_W + GRID_GAP))          # how many fit across
    for i, hwnd in enumerate(hwnds):
        col = i % cols
        row = i // cols
        x = col * (BOX_W + GRID_GAP)
        y = row * (BOX_H + GRID_GAP)

        # Wrap to top if we run off the bottom of the screen.
        if y + BOX_H > screen_h:
            y = y % max(1, (screen_h - BOX_H))

        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)  # un-minimize first
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_TOP, x, y, BOX_W, BOX_H,
                win32con.SWP_NOZORDER | win32con.SWP_SHOWWINDOW,
            )
        except Exception:
            pass
    return len(hwnds)


def arrange_grid():
    """Button handler: tile windows now and report the result."""
    n = arrange_grid_core()
    if n == 0:
        messagebox.showinfo("Arrange Grid", "No open Roblox windows were found.")
    else:
        messagebox.showinfo("Arrange Grid", f"Tiled {n} Roblox window(s) into a grid.")


# ==============================================================================
#  6. FPS CAP  (write DFIntTaskSchedulerTargetFps into every Roblox version)
# ==============================================================================
def roblox_version_dirs() -> list:
    """Return every installed Roblox version folder that holds a player exe."""
    dirs = []
    bases = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Roblox", "Versions"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Roblox", "Versions"),
    ]
    for base in bases:
        if not base or not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            vdir = os.path.join(base, name)
            if os.path.isfile(os.path.join(vdir, "RobloxPlayerBeta.exe")):
                dirs.append(vdir)
    return dirs


def global_settings_files() -> list:
    """Return Roblox's GlobalBasicSettings_*.xml files (holds <FramerateCap>)."""
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Roblox")
    found = []
    if os.path.isdir(base):
        for fn in os.listdir(base):
            if fn.startswith("GlobalBasicSettings") and fn.endswith(".xml"):
                found.append(os.path.join(base, fn))
    return found


_FPS_RE = re.compile(r'(<int name="FramerateCap">)(-?\d+)(</int>)')


def set_fps_cap(fps: int) -> int:
    """Set the Roblox FPS cap. Returns how many places were updated.

    Primary method (matches manual editing): rewrite <int name="FramerateCap">
    in GlobalBasicSettings_*.xml. Use -1 / 0 for uncapped. Also writes the
    ClientAppSettings.json FFlag as a secondary measure. Takes effect for any
    client launched AFTER this is applied.
    """
    fps = int(fps)
    updated = 0

    # 1) GlobalBasicSettings_*.xml  -> <int name="FramerateCap">N</int>
    for path in global_settings_files():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = f.read()
            new_data, n = _FPS_RE.subn(rf"\g<1>{fps}\g<3>", data)
            if n == 0:
                continue                      # field not present in this file
            if new_data != data:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new_data)
            updated += 1
        except Exception:
            pass

    # 2) ClientAppSettings.json FFlag (secondary / harmless if ignored)
    for vdir in roblox_version_dirs():
        cs_dir = os.path.join(vdir, "ClientSettings")
        try:
            os.makedirs(cs_dir, exist_ok=True)
            with open(os.path.join(cs_dir, "ClientAppSettings.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"DFIntTaskSchedulerTargetFps": fps}, f)
        except Exception:
            pass

    return updated


# ==============================================================================
#  7. THEME ENGINE  (dark, high-tech palettes + customisation)
# ==============================================================================
def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_to_hex(r, g, b):
    clamp = lambda v: max(0, min(255, int(v)))
    return "#%02x%02x%02x" % (clamp(r), clamp(g), clamp(b))


def _darken(h, f=0.78):
    r, g, b = _hex_to_rgb(h)
    return _rgb_to_hex(r * f, g * f, b * f)


def _contrast_text(h):
    """Pick near-black or near-white text for best contrast on colour h."""
    r, g, b = _hex_to_rgb(h)
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#0a0c0c" if lum > 0.55 else "#f5f7f6"


# Shared "command console" base — deep obsidian plates with dark-steel bezels.
# Every theme uses the same physical chassis; only the accent glow changes.
def _mk(accent, bg="#101014", panel="#16161a", inset="#22222b",
        border="#2a2a32", text="#e8e8ee", muted="#5a5a66",
        primary="#262630", field=None):
    return {
        "accent": accent, "accent_dark": _darken(accent),
        "accent_text": _contrast_text(accent),
        "bg": bg, "panel": panel, "inset": inset, "border": border,
        "text": text, "muted": muted,
        "field": field or inset,                       # matte input background
        "primary": primary,                            # main action button tone
        "primary_text": _contrast_text(primary),
        "bevel": _darken(panel, 2.0),                  # top highlight (light catch)
        "danger": "#c44a4a", "danger_dark": "#9c3838",
    }


THEMES = {
    # Same obsidian/steel console everywhere — pick your accent glow.
    "Matte Hardware": _mk("#10b981"),   # premium emerald (default)
    "Neuro Green":    _mk("#00ff66"),   # vibrant matrix green
    "Cyber Blue":     _mk("#3aa0ff"),
    "Synth Purple":   _mk("#a877ff"),
    "Crimson":        _mk("#ff5168"),
    "Amber":          _mk("#ffb43a"),
    "Mono Slate":     _mk("#8a93a6"),
}


def resolve_theme(settings: dict) -> dict:
    name = settings.get("theme", "Synth Purple")
    if name == "Custom":
        base = dict(THEMES["Synth Purple"])
        base.update(settings.get("custom_theme", {}))
        return base
    return dict(THEMES.get(name, THEMES["Synth Purple"]))


def make_rocket_icon(accent="#10b981") -> str:
    """Generate a small rocket .ico at runtime (no Pillow needed) and return
    its path. Transparent background, body tinted with the theme accent."""
    import struct
    W = H = 32
    ar, ag, ab = _hex_to_rgb(accent)
    body, flame, win = (ar, ag, ab), (255, 170, 40), (22, 22, 28)
    grid = [[(0, 0, 0, 0)] * W for _ in range(H)]  # top-down RGBA, transparent

    def put(x, y, c):
        if 0 <= x < W and 0 <= y < H:
            grid[y][x] = c

    for y in range(11, 24):                         # body
        for x in range(11, 22):
            put(x, y, (*body, 255))
    for y in range(3, 11):                          # nose cone
        hw = round((y - 3) / 8 * 5)
        for x in range(16 - hw, 16 + hw + 1):
            put(x, y, (*body, 255))
    for y in range(13, 18):                         # porthole
        for x in range(14, 19):
            if (x - 16) ** 2 + (y - 15) ** 2 <= 4:
                put(x, y, (*win, 255))
    for i in range(6):                              # fins
        for x in range(11 - i, 12):
            put(x, 19 + i, (*body, 255))
        for x in range(21, 22 + i):
            put(x, 19 + i, (*body, 255))
    for i in range(5):                              # exhaust flame
        hw = max(0, 3 - i)
        for x in range(16 - hw, 16 + hw + 1):
            put(x, 24 + i, (*flame, 255))

    rows = []                                        # DIB is bottom-up BGRA
    for y in range(H - 1, -1, -1):
        for x in range(W):
            r, g, b, a = grid[y][x]
            rows.append(bytes((b, g, r, a)))
    xor = b"".join(rows)
    and_mask = b"\x00" * (4 * H)
    dib = struct.pack("<IiiHHIIiiII", 40, W, H * 2, 1, 32, 0, len(xor),
                      0, 0, 0, 0) + xor + and_mask
    ico = (struct.pack("<HHH", 0, 1, 1) +
           struct.pack("<BBBBHHII", W, H, 0, 0, 1, 32, len(dib), 22) + dib)
    path = os.path.join(tempfile.gettempdir(), "cosmo_ram.ico")
    with open(path, "wb") as f:
        f.write(ico)
    return path


# ==============================================================================
#  8. GUI  (tkinter, themed dashboard)
# ==============================================================================
class AccountManagerGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Cosmo RAM")
        self.geometry("660x830")
        self.minsize(620, 700)

        self.accounts = load_accounts()
        self.settings = load_settings()
        enable_multi_instance()          # hold the mutex as soon as we start

        # --- runtime state for auto-relaunch -------------------------------
        self._launch_lock   = threading.Lock()   # serialise launches for PID tracking
        self._last_launch_time = 0.0             # for global launcher-delay spacing
        self.account_pids   = {}                 # account name -> tracked Roblox PID
        self._relaunching   = set()              # accounts mid-relaunch
        self.relaunch_names = list(self.settings.get("relaunch_names", []))
        self.auto_relaunch_on = False
        self.auto_grid_on     = bool(self.settings.get("auto_grid", False))
        # plain-int mirrors of the delay fields (thread-safe to read)
        self._launcher_delay  = int(self.settings.get("launcher_delay", 8))
        self._relaunch_delay  = int(self.settings.get("relaunch_delay", 5))
        self._place_id        = str(self.settings.get("place_id", "606849621"))
        self._fps_cap         = int(self.settings.get("fps_cap", 60))

        # --- theme ---------------------------------------------------------
        self.theme = resolve_theme(self.settings)
        self.stat_labels = {}
        self.configure(bg=self.theme["bg"])
        # Give Windows an explicit app id so the taskbar uses OUR icon, not Python's.
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CosmoRAM.App")
        except Exception:
            pass
        self._set_window_icon()
        self._style_ttk()

        self._build_widgets()
        self._refresh_account_lists()

        # background workers
        self._start_mutex_watcher()      # auto-clears the mutex
        self._start_relaunch_monitor()   # keeps auto-relaunch accounts alive
        self._start_autogrid_watcher()   # auto-tiles new windows when enabled
        self._start_dashboard_tick()     # live "clients connected" + toggle cards

        if not is_admin():
            self._set_status("Running as standard user. If a 2nd client won't "
                             "open, restart as administrator.")

    # ======================================================================
    #  LAYOUT
    # ======================================================================
    def _set_window_icon(self):
        """Apply the Cosmo RAM icon (bundled cosmo.ico); fall back to a drawn one."""
        try:
            ico = resource_path("cosmo.ico")
            if os.path.exists(ico):
                self.iconbitmap(default=ico)
            else:
                self.iconbitmap(make_rocket_icon(self.theme["accent"]))
        except Exception:
            pass

    # ---- themed widget factories -----------------------------------------
    def _style_ttk(self):
        t = self.theme
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("TNotebook", background=t["bg"], borderwidth=0)
        st.configure("TNotebook.Tab", background=t["panel"], foreground=t["muted"],
                     padding=[16, 9], borderwidth=0, font=("Segoe UI", 9, "bold"))
        st.map("TNotebook.Tab",
               background=[("selected", t["inset"])],
               foreground=[("selected", t["accent"])])

    def _card(self, parent, title=None, expand=False):
        t = self.theme
        outer = tk.Frame(parent, bg=t["panel"], highlightbackground=t["border"],
                         highlightthickness=1, bd=0)
        outer.pack(fill="both", expand=expand, pady=(0, 12))
        # 1px top-edge highlight — fakes light catching the bevel.
        tk.Frame(outer, bg=t["bevel"], height=1).pack(fill="x")
        if title:
            tk.Label(outer, text=title, bg=t["panel"], fg=t["muted"],
                     font=("Consolas", 8, "bold")).pack(anchor="w", padx=16, pady=(12, 0))
        body = tk.Frame(outer, bg=t["panel"])
        body.pack(fill="both", expand=True, padx=16, pady=(8 if title else 16, 16))
        return body

    def _btn(self, parent, text, command, kind="secondary"):
        t = self.theme
        border = t["border"]                       # 1px dark border (cutout look)
        if kind == "primary":
            bg, fg, ab = t["primary"], t["primary_text"], _darken(t["primary"], 0.85)
            border = t["accent"]                   # thin accent outline = main action
        elif kind == "danger":
            bg, fg, ab = t["danger"], "#f5f7f6", t["danger_dark"]
        else:
            bg, fg, ab = t["inset"], t["text"], _darken(t["inset"], 1.3)
        return tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                         activebackground=ab, activeforeground=fg, relief="flat",
                         bd=0, highlightthickness=1, highlightbackground=border,
                         highlightcolor=border, font=("Segoe UI", 9, "bold"),
                         cursor="hand2", padx=14, pady=9)

    def _listbox(self, parent, **kw):
        t = self.theme
        return tk.Listbox(parent, bg=t["field"], fg=t["text"],
                          selectbackground=t["accent"], selectforeground=t["accent_text"],
                          highlightthickness=1, highlightbackground=t["border"],
                          bd=0, relief="flat", font=("Consolas", 10),
                          activestyle="none", **kw)

    def _entry(self, parent, width=None):
        t = self.theme
        return tk.Entry(parent, width=width, bg=t["field"], fg=t["text"],
                        insertbackground=t["accent"], relief="flat", bd=0,
                        highlightthickness=1, highlightbackground=t["border"],
                        highlightcolor=t["accent"], font=("Consolas", 10))

    def _spin(self, parent, var, frm, to, command=None, width=6):
        t = self.theme
        return tk.Spinbox(parent, from_=frm, to=to, width=width, textvariable=var,
                          command=command, bg=t["field"], fg=t["text"],
                          buttonbackground=t["inset"], insertbackground=t["accent"],
                          relief="flat", bd=0, highlightthickness=1,
                          highlightbackground=t["border"], font=("Consolas", 10))

    # ---- top-level layout -------------------------------------------------
    def _build_widgets(self):
        t = self.theme
        self.configure(bg=t["bg"])
        self.stat_labels = {}

        self._build_header(self)
        self._build_dashboard(self)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        self.tab_accounts = tk.Frame(nb, bg=t["bg"])
        self.tab_control  = tk.Frame(nb, bg=t["bg"])
        self.tab_settings = tk.Frame(nb, bg=t["bg"])
        nb.add(self.tab_accounts, text="ACCOUNTS")
        nb.add(self.tab_control,  text="ACCOUNT CONTROL")
        nb.add(self.tab_settings, text="SETTINGS")

        self._build_accounts_tab(self.tab_accounts)
        self._build_control_tab(self.tab_control)
        self._build_settings_tab(self.tab_settings)

        self.status = tk.Label(self, text="READY", bg=t["bg"], fg=t["muted"],
                               anchor="w", wraplength=620, justify="left",
                               font=("Consolas", 9))
        self.status.pack(fill="x", pady=(8, 0))
        self._update_dashboard()

    def _build_header(self, parent):
        t = self.theme
        h = tk.Frame(parent, bg=t["bg"])
        h.pack(fill="x", pady=(2, 12))
        tk.Label(h, text="🚀 COSMO RAM", bg=t["bg"], fg=t["text"],
                 font=("Consolas", 13, "bold")).pack(side="left")
        user = (os.environ.get("USERNAME") or "OPERATOR").upper()
        tk.Label(h, text=f"  //  GREETINGS, {user}", bg=t["bg"], fg=t["muted"],
                 font=("Consolas", 10)).pack(side="left")
        tk.Label(h, text="● ONLINE", bg=t["bg"], fg=t["accent"],
                 font=("Consolas", 9, "bold")).pack(side="right")

    def _build_dashboard(self, parent):
        t = self.theme
        dash = tk.Frame(parent, bg=t["bg"])
        dash.pack(fill="x", pady=(0, 12))
        cards = [
            ("clients",  "CLIENTS CONNECTED", None),
            ("multi",    "MULTI ROBLOX",      self.toggle_multi_roblox),
            ("relaunch", "AUTO RELAUNCH",     self.toggle_auto_relaunch),
            ("grid",     "AUTO GRID",         self.toggle_auto_grid),
        ]
        for i, (key, title, cmd) in enumerate(cards):
            c = self._stat_card(dash, key, title, cmd)
            c.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
            dash.grid_columnconfigure(i, weight=1)

    def _stat_card(self, parent, key, title, command=None):
        t = self.theme
        card = tk.Frame(parent, bg=t["panel"], highlightbackground=t["border"],
                        highlightthickness=1, bd=0)
        tk.Frame(card, bg=t["bevel"], height=1).pack(fill="x")   # top bevel highlight
        inner = tk.Frame(card, bg=t["panel"])
        inner.pack(fill="both", expand=True, padx=16, pady=14)
        val = tk.Label(inner, text="--", bg=t["panel"], fg=t["accent"],
                       font=("Consolas", 23, "bold"), anchor="w")
        val.pack(anchor="w", fill="x")
        tk.Label(inner, text=title, bg=t["panel"], fg=t["muted"],
                 font=("Consolas", 8, "bold"), anchor="w").pack(anchor="w", fill="x",
                                                                pady=(4, 0))
        self.stat_labels[key] = val
        if command:
            for w in (card, inner, val):
                w.configure(cursor="hand2")
                w.bind("<Button-1>", lambda e, c=command: c())
        return card

    # ---- Tab 1: Accounts --------------------------------------------------
    def _build_accounts_tab(self, parent):
        t = self.theme
        parent.configure(bg=t["bg"])
        pad = tk.Frame(parent, bg=t["bg"])
        pad.pack(fill="both", expand=True, padx=2, pady=8)

        body = self._card(pad, "SAVED ACCOUNTS", expand=True)
        lf = tk.Frame(body, bg=t["panel"])
        lf.pack(fill="both", expand=True)
        scroll = tk.Scrollbar(lf, bg=t["inset"], troughcolor=t["field"],
                              bd=0, highlightthickness=0)
        scroll.pack(side="right", fill="y")
        self.listbox = self._listbox(lf, selectmode=tk.EXTENDED,
                                     yscrollcommand=scroll.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        scroll.config(command=self.listbox.yview)
        self.listbox.bind("<Button-3>", self._account_context_menu)  # right-click

        pid_frame = tk.Frame(body, bg=t["panel"])
        pid_frame.pack(fill="x", pady=(10, 0))
        tk.Label(pid_frame, text="PLACE ID", bg=t["panel"], fg=t["muted"],
                 font=("Consolas", 8, "bold")).pack(side="left")
        self.place_entry = self._entry(pid_frame, width=18)
        self.place_entry.insert(0, self._place_id)
        self.place_entry.pack(side="left", padx=8, ipady=5)
        self.place_entry.bind("<FocusOut>", lambda e: self._sync_place_id())

        add_row = tk.Frame(pad, bg=t["bg"])
        add_row.pack(fill="x", pady=(0, 8))
        self._btn(add_row, "+ ADD COOKIE", self.add_account).pack(side="left", expand=True, fill="x", padx=(0, 4))
        self._btn(add_row, "🌐 WEB LOGIN", self.web_login).pack(side="left", expand=True, fill="x", padx=4)
        self._btn(add_row, "✕ REMOVE", self.remove_selected, kind="danger").pack(side="left", expand=True, fill="x", padx=(4, 0))

        self._btn(pad, "▶  LAUNCH SELECTED", self.launch_selected,
                  kind="primary").pack(fill="x", ipady=5, pady=(0, 6))
        self._btn(pad, "⊞  ARRANGE GRID", arrange_grid).pack(fill="x", ipady=3)

    # ---- Tab 2: Account Control (auto-relaunch) ---------------------------
    def _build_control_tab(self, parent):
        t = self.theme
        parent.configure(bg=t["bg"])
        pad = tk.Frame(parent, bg=t["bg"])
        pad.pack(fill="both", expand=True, padx=2, pady=8)

        body = self._card(pad, "AUTO-RELAUNCH — KEEP THESE ACCOUNTS ALIVE", expand=True)
        tk.Label(body, text="Move accounts to the right. If a Roblox window closes "
                            "(crash or manual), it relaunches automatically.",
                 bg=t["panel"], fg=t["muted"], font=("Segoe UI", 8),
                 wraplength=580, justify="left").pack(anchor="w", pady=(0, 8))

        cols = tk.Frame(body, bg=t["panel"])
        cols.pack(fill="both", expand=True)

        left = tk.Frame(cols, bg=t["panel"])
        left.pack(side="left", fill="both", expand=True)
        tk.Label(left, text="ALL ACCOUNTS", bg=t["panel"], fg=t["muted"],
                 font=("Consolas", 8, "bold")).pack(anchor="w", pady=(0, 2))
        self.ctrl_all = self._listbox(left, selectmode=tk.EXTENDED)
        self.ctrl_all.pack(fill="both", expand=True)

        mid = tk.Frame(cols, bg=t["panel"])
        mid.pack(side="left", padx=8)
        self._btn(mid, "ADD ▶", self._control_add).pack(pady=(34, 6), fill="x")
        self._btn(mid, "◀ REMOVE", self._control_remove, kind="danger").pack(fill="x")

        right = tk.Frame(cols, bg=t["panel"])
        right.pack(side="left", fill="both", expand=True)
        tk.Label(right, text="AUTO-RELAUNCH", bg=t["panel"], fg=t["accent"],
                 font=("Consolas", 8, "bold")).pack(anchor="w", pady=(0, 2))
        self.ctrl_auto = self._listbox(right, selectmode=tk.EXTENDED)
        self.ctrl_auto.pack(fill="both", expand=True)

        self.relaunch_btn = self._btn(pad, "", self.toggle_auto_relaunch)
        self.relaunch_btn.pack(fill="x", ipady=5)
        self._update_relaunch_btn()

    # ---- Tab 3: Settings --------------------------------------------------
    def _build_settings_tab(self, parent):
        t = self.theme
        parent.configure(bg=t["bg"])
        pad = tk.Frame(parent, bg=t["bg"])
        pad.pack(fill="both", expand=True, padx=2, pady=8)

        # --- Launch timing ---
        b = self._card(pad, "LAUNCH TIMING")
        r1 = tk.Frame(b, bg=t["panel"]); r1.pack(fill="x", pady=2)
        tk.Label(r1, text="Launcher delay (sec)", bg=t["panel"], fg=t["text"],
                 font=("Segoe UI", 9), width=22, anchor="w").pack(side="left")
        self.launcher_delay_var = tk.StringVar(value=str(self._launcher_delay))
        self._spin(r1, self.launcher_delay_var, 0, 120, self._sync_delays).pack(side="left")

        r2 = tk.Frame(b, bg=t["panel"]); r2.pack(fill="x", pady=2)
        tk.Label(r2, text="Relaunch delay (sec)", bg=t["panel"], fg=t["text"],
                 font=("Segoe UI", 9), width=22, anchor="w").pack(side="left")
        self.relaunch_delay_var = tk.StringVar(value=str(self._relaunch_delay))
        self._spin(r2, self.relaunch_delay_var, 0, 300, self._sync_delays).pack(side="left")

        self._btn(b, "💾 SAVE LAUNCH TIMINGS",
                  lambda: self._sync_delays(announce=True)).pack(anchor="w", pady=(8, 0))

        # --- Multi Roblox ---
        b2 = self._card(pad, "MULTI ROBLOX")
        self.multi_btn = self._btn(b2, "", self.toggle_multi_roblox)
        self.multi_btn.pack(fill="x", ipady=3)
        self._update_multi_btn()

        # --- Window layout / auto grid ---
        b3 = self._card(pad, "WINDOW LAYOUT")
        self.autogrid_btn = self._btn(b3, "", self.toggle_auto_grid)
        self.autogrid_btn.pack(fill="x", ipady=3)
        tk.Label(b3, text="When ON, every Roblox window that opens is shrunk to the "
                          "smallest box and auto-tiled across your screen.",
                 bg=t["panel"], fg=t["muted"], font=("Segoe UI", 8),
                 wraplength=580, justify="left").pack(anchor="w", pady=(4, 0))
        self._update_autogrid_btn()

        # --- Performance / FPS ---
        b4 = self._card(pad, "PERFORMANCE — SET FPS")
        fr = tk.Frame(b4, bg=t["panel"]); fr.pack(fill="x")
        tk.Label(fr, text="Target FPS", bg=t["panel"], fg=t["text"],
                 font=("Segoe UI", 9), width=22, anchor="w").pack(side="left")
        self.fps_var = tk.StringVar(value=str(self._fps_cap))
        self._spin(fr, self.fps_var, 1, 1000).pack(side="left")
        self._btn(fr, "APPLY", self.apply_fps, kind="primary").pack(side="left", padx=8)
        tk.Label(b4, text="5 = very slow · 60 = normal · 240 = uncapped. Applies to "
                          "every client launched next (restart open clients to update).",
                 bg=t["panel"], fg=t["muted"], font=("Segoe UI", 8),
                 wraplength=580, justify="left").pack(anchor="w", pady=(4, 0))

        # --- Appearance / theme ---
        b5 = self._card(pad, "APPEARANCE — THEME")
        pr = tk.Frame(b5, bg=t["panel"]); pr.pack(fill="x")
        current = self.settings.get("theme", "Neuro Green")
        for i, name in enumerate(THEMES):
            th = THEMES[name]
            sel = (current == name)
            sw = tk.Button(pr, text=("● " if sel else "  ") + name,
                           command=lambda n=name: self._apply_theme(n),
                           bg=th["panel"], fg=th["accent"], activebackground=th["inset"],
                           activeforeground=th["accent"], relief="flat", bd=0,
                           highlightthickness=1,
                           highlightbackground=th["accent"] if sel else t["border"],
                           font=("Segoe UI", 9, "bold"), cursor="hand2", padx=6, pady=6)
            sw.grid(row=i // 3, column=i % 3, sticky="ew", padx=3, pady=3)
        for c in range(3):
            pr.grid_columnconfigure(c, weight=1)

        cust = tk.Frame(b5, bg=t["panel"]); cust.pack(fill="x", pady=(8, 0))
        tk.Label(cust, text="CUSTOM", bg=t["panel"], fg=t["muted"],
                 font=("Consolas", 8, "bold")).pack(side="left", padx=(0, 6))
        for key, label in (("accent", "Accent"), ("bg", "Background"),
                           ("panel", "Panel"), ("text", "Text")):
            self._btn(cust, label, lambda k=key: self._pick_color(k)).pack(side="left", padx=2)

    # ======================================================================
    #  THEME APPLY / CUSTOMISE
    # ======================================================================
    def _apply_theme(self, name):
        self.settings["theme"] = name
        save_settings(self.settings)
        self.theme = resolve_theme(self.settings)
        self._rebuild_ui()
        self._set_status(f"Theme set to {name}.")

    def _pick_color(self, key):
        res = colorchooser.askcolor(color=self.theme.get(key, "#000000"),
                                    parent=self, title=f"Pick {key} colour")
        if not res or not res[1]:
            return
        hx = res[1]
        ct = dict(self.settings.get("custom_theme", {}))
        ct[key] = hx
        if key == "accent":                      # keep derived accent colours in sync
            ct["accent_dark"] = _darken(hx)
            ct["accent_text"] = _contrast_text(hx)
        self.settings["custom_theme"] = ct
        self.settings["theme"] = "Custom"
        save_settings(self.settings)
        self.theme = resolve_theme(self.settings)
        self._rebuild_ui()
        self._set_status(f"Custom theme updated ({key} = {hx}).")

    def _rebuild_ui(self):
        for w in self.winfo_children():
            w.destroy()
        self.configure(bg=self.theme["bg"])
        self._set_window_icon()
        self._style_ttk()
        self._build_widgets()
        self._refresh_account_lists()

    # ======================================================================
    #  SHARED HELPERS
    # ======================================================================
    def _set_status(self, text):
        self.status.config(text=text)

    def _account_by_name(self, name):
        for a in self.accounts:
            if a["name"] == name:
                return a
        return None

    def _sync_place_id(self):
        pid = self.place_entry.get().strip()
        if pid:
            self._place_id = pid
            self.settings["place_id"] = pid
            save_settings(self.settings)

    def _sync_delays(self, announce=False):
        """Read the delay fields, validate, store and persist. Returns success."""
        try:
            launcher = int(str(self.launcher_delay_var.get()).strip())
            relaunch = int(str(self.relaunch_delay_var.get()).strip())
        except (ValueError, TypeError):
            if announce:
                messagebox.showerror("Settings",
                                     "Delays must be whole numbers of seconds.")
            return False    # leave the previous (good) values untouched

        self._launcher_delay = max(0, launcher)
        self._relaunch_delay = max(0, relaunch)
        self.settings["launcher_delay"] = self._launcher_delay
        self.settings["relaunch_delay"] = self._relaunch_delay
        save_settings(self.settings)
        if announce:
            self._set_status(f"Saved — launcher delay {self._launcher_delay}s, "
                             f"relaunch delay {self._relaunch_delay}s.")
        return True

    def _refresh_account_lists(self):
        # Accounts tab
        self.listbox.delete(0, tk.END)
        for a in self.accounts:
            self.listbox.insert(tk.END, a["name"])
        # Control tab: drop names that no longer exist
        names = {a["name"] for a in self.accounts}
        self.relaunch_names = [n for n in self.relaunch_names if n in names]
        if hasattr(self, "ctrl_all"):
            self.ctrl_all.delete(0, tk.END)
            for a in self.accounts:
                if a["name"] not in self.relaunch_names:
                    self.ctrl_all.insert(tk.END, a["name"])
            self.ctrl_auto.delete(0, tk.END)
            for n in self.relaunch_names:
                self.ctrl_auto.insert(tk.END, n)

    # ======================================================================
    #  MULTI ROBLOX
    # ======================================================================
    def _toggle_style(self, btn, on, on_text, off_text):
        t = self.theme
        if on:
            btn.config(text=on_text, bg=t["accent"], fg=t["accent_text"],
                       activebackground=t["accent_dark"], activeforeground=t["accent_text"])
        else:
            btn.config(text=off_text, bg=t["inset"], fg=t["muted"],
                       activebackground=t["border"], activeforeground=t["muted"])

    def _update_dashboard(self):
        if not hasattr(self, "stat_labels"):
            return
        t = self.theme

        def setv(key, text, on):
            lbl = self.stat_labels.get(key)
            if lbl is not None:
                lbl.config(text=text, fg=t["accent"] if on else t["muted"])

        try:
            clients = len(find_roblox_windows())
        except Exception:
            clients = 0
        setv("clients", str(clients), clients > 0)
        setv("multi", "ON" if is_multi_enabled() else "OFF", is_multi_enabled())
        setv("relaunch", "ON" if self.auto_relaunch_on else "OFF", self.auto_relaunch_on)
        setv("grid", "ON" if self.auto_grid_on else "OFF", self.auto_grid_on)

    def _start_dashboard_tick(self):
        self._update_dashboard()
        self.after(1500, self._start_dashboard_tick)

    def _update_multi_btn(self):
        if hasattr(self, "multi_btn"):
            self._toggle_style(self.multi_btn, is_multi_enabled(),
                               "MULTI ROBLOX: ON", "MULTI ROBLOX: OFF")
        self._update_dashboard()

    def toggle_multi_roblox(self):
        if is_multi_enabled():
            disable_multi_instance()
            self._set_status("Multi Roblox OFF — Roblox is back to single-instance.")
        else:
            if enable_multi_instance():
                self._set_status("Multi Roblox ON — keep this window open!")
            else:
                self._set_status("Could not grab the Roblox mutex. Try running as admin.")
        self._update_multi_btn()

    def _start_mutex_watcher(self):
        def loop():
            while True:
                if is_multi_enabled():
                    try:
                        close_roblox_mutexes()
                    except Exception:
                        pass
                time.sleep(3)
        threading.Thread(target=loop, daemon=True).start()

    # ======================================================================
    #  ADD / REMOVE / WEB LOGIN
    # ======================================================================
    def _add_cookie(self, cookie: str, password: str = ""):
        cookie = cookie.strip()
        name = get_username(cookie)
        for a in self.accounts:
            if a["name"] == name and name != "Unknown":
                a["cookie"] = cookie
                if password:                 # only overwrite if we captured one
                    a["password"] = password
                break
        else:
            self.accounts.append({"name": name, "cookie": cookie,
                                  "password": password})
        save_accounts(self.accounts)
        self.after(0, self._refresh_account_lists)
        self.after(0, lambda: self._set_status(f"Saved account: {name}"))

    def add_account(self):
        cookie = simpledialog.askstring(
            "Add Account",
            "Paste the .ROBLOSECURITY cookie value:\n"
            "(the long string, with or without the _|WARNING... prefix)",
            parent=self,
        )
        if not cookie:
            return
        self._set_status("Verifying cookie...")
        self.update_idletasks()
        threading.Thread(target=self._add_cookie, args=(cookie,), daemon=True).start()

    def web_login(self):
        if importlib.util.find_spec("webview") is None:
            messagebox.showerror(
                "Web Login",
                "pywebview is not installed.\n\nRun this in Command Prompt:\n"
                "    pip install pywebview",
            )
            return

        tmp = os.path.join(
            tempfile.gettempdir(), f"ram_cookie_{os.getpid()}_{random.randint(1000,9999)}.txt"
        )
        self._set_status("Opening web login window... log in there; it auto-closes.")

        # When frozen, the exe IS the interpreter — don't pass a script path.
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--weblogin", tmp]
        else:
            cmd = [sys.executable, os.path.abspath(__file__), "--weblogin", tmp]
        try:
            proc = subprocess.Popen(
                cmd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            messagebox.showerror("Web Login", f"Could not start login window:\n{e}")
            return

        def wait_for_result():
            proc.wait()
            if os.path.exists(tmp):
                cookie, password = "", ""
                try:
                    with open(tmp, "r", encoding="utf-8") as f:
                        raw = f.read().strip()
                    try:
                        data = json.loads(raw)
                        cookie = data.get("cookie", "")
                        password = data.get("password", "")
                    except (ValueError, TypeError):
                        cookie = raw          # backward-compatible plain cookie
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                if cookie:
                    self._add_cookie(cookie, password)
                    return
            self.after(0, lambda: self._set_status(
                "Web login cancelled or no cookie captured."))

        threading.Thread(target=wait_for_result, daemon=True).start()

    def remove_selected(self):
        sel = list(self.listbox.curselection())
        if not sel:
            return
        if not messagebox.askyesno("Remove", f"Remove {len(sel)} account(s)?"):
            return
        for idx in sorted(sel, reverse=True):
            del self.accounts[idx]
        save_accounts(self.accounts)
        self._refresh_account_lists()

    # ---- right-click context menu ----------------------------------------
    def _account_context_menu(self, event):
        idx = self.listbox.nearest(event.y)
        if idx < 0 or idx >= len(self.accounts):
            return
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(idx)
        self.listbox.activate(idx)
        acct = self.accounts[idx]
        t = self.theme
        menu = tk.Menu(self, tearoff=0, bg=t["panel"], fg=t["text"],
                       activebackground=t["accent"], activeforeground=t["accent_text"],
                       bd=0, relief="flat", font=("Segoe UI", 9))
        menu.add_command(label="Copy Cookie",
                         command=lambda: self._copy_field(acct, "cookie"))
        menu.add_command(label="Copy Password",
                         command=lambda: self._copy_field(acct, "password"))
        menu.add_command(label="Copy User",
                         command=lambda: self._copy_field(acct, "name"))
        menu.add_command(label="Copy User:Pass Combo",
                         command=lambda: self._copy_combo(acct))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _clip(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()              # keep clipboard contents after the app closes

    def _copy_field(self, acct, key):
        label = {"cookie": "cookie", "password": "password", "name": "username"}[key]
        val = acct.get(key, "")
        if not val:
            self._set_status(f"No {label} saved for {acct['name']}.")
            return
        self._clip(val)
        self._set_status(f"Copied {label} for {acct['name']} to clipboard.")

    def _copy_combo(self, acct):
        pw = acct.get("password", "")
        if not pw:
            self._set_status(f"No password captured for {acct['name']} "
                             "(add it via Web Login to capture the password).")
            return
        self._clip(f"{acct['name']}:{pw}")
        self._set_status(f"Copied user:pass combo for {acct['name']}.")

    # ======================================================================
    #  ACCOUNT CONTROL (auto-relaunch membership + master switch)
    # ======================================================================
    def _control_add(self):
        for i in self.ctrl_all.curselection():
            name = self.ctrl_all.get(i)
            if name not in self.relaunch_names:
                self.relaunch_names.append(name)
        self._persist_relaunch_names()
        self._refresh_account_lists()

    def _control_remove(self):
        for i in self.ctrl_auto.curselection():
            name = self.ctrl_auto.get(i)
            if name in self.relaunch_names:
                self.relaunch_names.remove(name)
        self._persist_relaunch_names()
        self._refresh_account_lists()

    def _persist_relaunch_names(self):
        self.settings["relaunch_names"] = self.relaunch_names
        save_settings(self.settings)

    def _update_relaunch_btn(self):
        if hasattr(self, "relaunch_btn"):
            self._toggle_style(self.relaunch_btn, self.auto_relaunch_on,
                               "AUTO-RELAUNCH: ON", "AUTO-RELAUNCH: OFF")
        self._update_dashboard()

    def toggle_auto_relaunch(self):
        self.auto_relaunch_on = not self.auto_relaunch_on
        if self.auto_relaunch_on:
            if not self.relaunch_names:
                self.auto_relaunch_on = False
                messagebox.showinfo("Auto-Relaunch",
                                    "Add at least one account to the right-hand list first.")
            elif not self._place_id.isdigit():
                self.auto_relaunch_on = False
                messagebox.showerror("Auto-Relaunch",
                                     "Set a valid numeric Place ID on the Accounts tab first.")
            else:
                self._set_status("Auto-Relaunch ON — keeping your accounts alive.")
        else:
            self._set_status("Auto-Relaunch OFF.")
        self._update_relaunch_btn()

    def _start_relaunch_monitor(self):
        def loop():
            while True:
                time.sleep(2)
                if not self.auto_relaunch_on:
                    continue
                place_id = self._place_id
                if not place_id.isdigit():
                    continue
                alive = _roblox_pids()
                for name in list(self.relaunch_names):
                    if name in self._relaunching:
                        continue
                    acct = self._account_by_name(name)
                    if not acct:
                        continue
                    pid = self.account_pids.get(name)
                    if pid is None:
                        # never started (or lost) -> start it now
                        self._spawn_relaunch(acct, place_id, initial=True)
                    elif pid not in alive:
                        # it died -> relaunch after the configured delay
                        self.account_pids.pop(name, None)
                        self._spawn_relaunch(acct, place_id, initial=False)
        threading.Thread(target=loop, daemon=True).start()

    def _spawn_relaunch(self, acct, place_id, initial):
        name = acct["name"]
        self._relaunching.add(name)

        # Abort if the user turned Auto-Relaunch off or removed this account.
        def cancelled():
            return (not self.auto_relaunch_on) or (name not in self.relaunch_names)

        def work():
            try:
                if not initial:
                    delay = self._relaunch_delay
                    self.after(0, lambda: self._set_status(
                        f"{name} closed — relaunching in {delay}s..."))
                    time.sleep(delay)
                # Re-check the switch after the (possibly long) delay slept.
                if cancelled():
                    self.after(0, lambda: self._set_status(
                        f"Auto-Relaunch off — skipped relaunch of {name}."))
                    return
                self.after(0, lambda: self._set_status(f"Auto-launching {name}..."))
                self._launch_tracked(acct, place_id, cancel_check=cancelled)
                time.sleep(max(1, self._launcher_delay))   # cooldown vs. spam
            except Exception as e:
                self.after(0, lambda err=e:
                           self._set_status(f"Relaunch failed for {name}: {err}"))
            finally:
                self._relaunching.discard(name)

        threading.Thread(target=work, daemon=True).start()

    # ======================================================================
    #  LAUNCHING (with PID tracking)
    # ======================================================================
    def _launch_tracked(self, acct, place_id, cancel_check=None):
        """Launch one account and record the new Roblox PID for relaunch tracking.

        cancel_check: optional callable; if it returns True right before the
        launch fires, the launch is aborted (used so toggling Auto-Relaunch off
        stops a launch that's still waiting out a delay).
        """
        with self._launch_lock:
            if is_multi_enabled():
                close_roblox_mutexes()
                time.sleep(0.5)
            # Global launcher-delay spacing: make sure at least _launcher_delay
            # seconds have passed since the PREVIOUS launch fired, no matter
            # which path (Launch Selected or Auto-Relaunch) triggered it.
            gap = self._launcher_delay - (time.time() - self._last_launch_time)
            if gap > 0:
                self.after(0, lambda g=gap: self._set_status(
                    f"Waiting {g:.0f}s (launcher delay) before launching "
                    f"{acct['name']}..."))
                time.sleep(gap)
            # Final abort check after all waiting, just before we actually launch.
            if cancel_check and cancel_check():
                return None
            # Re-apply the FPS cap so this client picks it up even if Roblox
            # rewrote GlobalBasicSettings when an earlier client closed.
            try:
                set_fps_cap(self._fps_cap)
            except Exception:
                pass
            before = _roblox_pids()
            launch_account(acct["cookie"], place_id)
            self._last_launch_time = time.time()
            # Wait for this account's RobloxPlayerBeta process to appear.
            new_pid = None
            for _ in range(50):                 # up to ~25s
                time.sleep(0.5)
                diff = _roblox_pids() - before
                if diff:
                    new_pid = max(diff)
                    break
            if new_pid:
                self.account_pids[acct["name"]] = new_pid
            return new_pid

    def launch_selected(self):
        sel = list(self.listbox.curselection())
        if not sel:
            messagebox.showinfo("Launch", "Select one or more accounts first.")
            return
        self._sync_place_id()
        place_id = self._place_id
        if not place_id.isdigit():
            messagebox.showerror("Launch", "Place ID must be a number.")
            return

        chosen = [self.accounts[i] for i in sel]

        def work():
            for acct in chosen:
                self.after(0, lambda n=acct["name"]: self._set_status(f"Launching {n}..."))
                try:
                    self._launch_tracked(acct, place_id)   # handles delay spacing
                except Exception as e:
                    self.after(0, lambda n=acct["name"], err=e:
                               messagebox.showerror("Launch failed", f"{n}: {err}"))
            self.after(0, lambda: self._set_status(
                "Done launching. Use Arrange Grid (or enable Auto Grid in Settings)."))

        threading.Thread(target=work, daemon=True).start()

    # ======================================================================
    #  AUTO GRID
    # ======================================================================
    def _update_autogrid_btn(self):
        if hasattr(self, "autogrid_btn"):
            self._toggle_style(self.autogrid_btn, self.auto_grid_on,
                               "AUTO GRID: ON", "AUTO GRID: OFF")
        self._update_dashboard()

    def toggle_auto_grid(self):
        self.auto_grid_on = not self.auto_grid_on
        self.settings["auto_grid"] = self.auto_grid_on
        save_settings(self.settings)
        self._set_status("Auto Grid " + ("ON — new windows tile automatically."
                                         if self.auto_grid_on else "OFF."))
        self._update_autogrid_btn()

    def _start_autogrid_watcher(self):
        def loop():
            last = -1
            while True:
                time.sleep(2)
                if not self.auto_grid_on:
                    last = -1
                    continue
                count = len(find_roblox_windows())
                if count and count != last:   # only re-tile when the count changes
                    try:
                        arrange_grid_core()
                    except Exception:
                        pass
                    last = count
                elif count == 0:
                    last = 0
        threading.Thread(target=loop, daemon=True).start()

    # ======================================================================
    #  FPS CAP
    # ======================================================================
    def apply_fps(self):
        try:
            fps = int(str(self.fps_var.get()).strip())
        except (ValueError, TypeError):
            messagebox.showerror("Set FPS", "FPS must be a whole number.")
            return
        if fps < 1:
            messagebox.showerror("Set FPS", "FPS must be at least 1.")
            return

        self._fps_cap = fps
        self.settings["fps_cap"] = fps
        save_settings(self.settings)

        updated = set_fps_cap(fps)
        if updated:
            self._set_status(f"FramerateCap set to {fps} in GlobalBasicSettings. "
                             "Restart any open client to apply.")
        else:
            messagebox.showwarning(
                "Set FPS",
                "Couldn't find GlobalBasicSettings_*.xml in:\n"
                "%LOCALAPPDATA%\\Roblox\n\n"
                "Open Roblox once so the file exists, then try again.")


# ==============================================================================
#  ENTRY POINT
# ==============================================================================
if __name__ == "__main__":
    # Child mode: run only the embedded web-login window, then exit.
    # (Works whether launched as a script or as a frozen .exe.)
    if "--weblogin" in sys.argv:
        idx = sys.argv.index("--weblogin")
        out_path = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        if out_path:
            run_web_login_child(out_path)
        sys.exit(0)

    # Normal mode: the GUI.
    AccountManagerGUI().mainloop()
