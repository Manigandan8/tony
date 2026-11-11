# =====================================================================
# tony_core/tools/system.py
# Complete Windows OS & Hardware System Controller:
# Volume, Brightness, Screenshots, Wi-Fi, Bluetooth, OS Panels,
# Virtual Desktops, Processes, Disk Space, Recycle Bin, Power & Shell.
# =====================================================================

import os
import re
import sys
import csv
import io
import time
import shutil
import socket
import ctypes
import datetime
import subprocess
from tony_core import config
from tony_core.safety import HARD_BLOCK, _cmd_needs_confirm, confirm
from tony_core.tools import ui

_PROTECTED_PROCS = {
    "explorer", "explorer.exe", "python", "python.exe", "pythonw", "pythonw.exe",
    "cmd", "cmd.exe", "powershell", "powershell.exe", "svchost", "dwm", "csrss",
    "winlogon", "system", "lsass", "services"
}

_SETTINGS_URIS = {
    "settings": "ms-settings:",
    "wifi": "ms-settings:network-wifi",
    "wi-fi": "ms-settings:network-wifi",
    "network": "ms-settings:network",
    "bluetooth": "ms-settings:bluetooth",
    "sound": "ms-settings:sound",
    "audio": "ms-settings:sound",
    "volume mixer": "ms-settings:apps-volume",
    "display": "ms-settings:display",
    "night light": "ms-settings:nightlight",
    "battery": "ms-settings:batterysaver",
    "power": "ms-settings:powersleep",
    "storage": "ms-settings:storagesense",
    "apps": "ms-settings:appsfeatures",
    "startup": "ms-settings:startupapps",
    "update": "ms-settings:windowsupdate",
    "windows update": "ms-settings:windowsupdate",
    "hotspot": "ms-settings:network-mobilehotspot",
    "mobile hotspot": "ms-settings:network-mobilehotspot",
    "airplane": "ms-settings:network-airplanemode",
    "airplane mode": "ms-settings:network-airplanemode",
    "personalization": "ms-settings:personalization",
    "theme": "ms-settings:themes",
    "privacy": "ms-settings:privacy",
    "camera": "ms-settings:privacy-webcam",
    "microphone": "ms-settings:privacy-microphone",
    "time": "ms-settings:dateandtime",
    "gaming": "ms-settings:gaming-gamebar",
}


def run_command(command: str) -> str:
    cmd_str = (command or "").strip()
    if not cmd_str:
        return "No command provided."
    if HARD_BLOCK.search(cmd_str):
        return "Blocked: destructive command."

    # 1) Launch Python (.py) scripts/games IMMEDIATELY without asking [CONFIRM]!
    py_match = re.match(r'^\s*(?:python3?|py)\s+(?:"([^"]+\.py)"|\'([^\']+\.py)\'|(\S+\.py))(.*)$', cmd_str, re.I)
    if py_match:
        script_path = (py_match.group(1) or py_match.group(2) or py_match.group(3) or "").strip()
        extra_args = (py_match.group(4) or "").strip()
        # Auto-correct any wrong C:\Users\<name>\Desktop path to the real user Desktop
        script_path = re.sub(
            r"^[a-zA-Z]:\\Users\\[^\\]+\\Desktop\\",
            files.FOLDERS["desktop"].rstrip("\\") + "\\\\",
            script_path,
            flags=re.I
        )
        if not os.path.isabs(script_path) or not os.path.isfile(script_path):
            from tony_core import pc_index
            hits = pc_index.search_pc(os.path.basename(script_path), kind="file", limit=1)
            if hits and os.path.isfile(hits[0]["path"]):
                script_path = hits[0]["path"]
        if not os.path.isfile(script_path):
            return f"No such Python file: {script_path}. Do NOT claim success."
        try:
            work_dir = os.path.dirname(script_path) or None
            launch_cmd = f'cmd /c ""{sys.executable}" "{script_path}" {extra_args} & pause"'
            subprocess.Popen(launch_cmd, cwd=work_dir, creationflags=subprocess.CREATE_NEW_CONSOLE)
            return f"Launched '{os.path.basename(script_path)}' in a new window."
        except Exception as e:
            return f"Couldn't launch '{script_path}': {str(e)[:80]}"

    # 2) Standard shell commands require confirmation if not read-only
    if _cmd_needs_confirm(cmd_str) and not confirm(f"run shell command: {cmd_str[:80]}?"):
        return "The user did not approve that command. Cancelled."

    try:
        r = subprocess.run(
            cmd_str, shell=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30
        )
    except subprocess.TimeoutExpired:
        return "Command timed out after 30 seconds. Do NOT claim success."
    except Exception as e:
        return f"Command failed: {str(e)[:80]}"

    if "chrome.exe" in cmd_str.lower() and "taskkill" in cmd_str.lower():
        ui._chrome_running()

    out = ((r.stdout or "") + ("\n" + r.stderr if r.stderr else "")).strip()
    if r.returncode != 0:
        return f"Command failed (exit code {r.returncode}): {out[:1200] or 'no output'}. Do NOT claim success."
    return (out or "Command completed successfully.")[:1500]


def _get_pycaw_volume():
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    dev = AudioUtilities.GetSpeakers()
    if hasattr(dev, "EndpointVolume"):
        return dev.EndpointVolume
    interface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def set_volume(level: int = 50) -> str:
    lvl = max(0, min(100, int(level)))
    try:
        vol = _get_pycaw_volume()
        vol.SetMasterVolumeLevelScalar(lvl / 100.0, None)
        return f"Volume set to {lvl}%."
    except Exception:
        try:
            import pyautogui
            pyautogui.press("volumedown", presses=50, interval=0.01)
            if lvl > 0:
                pyautogui.press("volumeup", presses=lvl // 2, interval=0.01)
            return f"Volume set to about {lvl}%."
        except Exception as e:
            return f"Volume control failed: {str(e)[:60]}"


def step_volume(direction: str = "up", step: int = 10) -> str:
    is_up = (direction or "up").lower().strip() in ("up", "increase", "higher", "louder")
    delta = max(2, min(50, int(step or 10)))
    try:
        vol = _get_pycaw_volume()
        curr = int(round(vol.GetMasterVolumeLevelScalar() * 100))
        new_lvl = max(0, min(100, curr + (delta if is_up else -delta)))
        vol.SetMasterVolumeLevelScalar(new_lvl / 100.0, None)
        return f"Volume {'increased' if is_up else 'decreased'} to {new_lvl}%."
    except Exception:
        try:
            import pyautogui
            key = "volumeup" if is_up else "volumedown"
            pyautogui.press(key, presses=max(1, delta // 2), interval=0.02)
            return f"Turned volume {'up' if is_up else 'down'}."
        except Exception as e:
            return f"Volume step failed: {str(e)[:60]}"


def _get_current_brightness() -> int:
    cmd = "(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightness).CurrentBrightness"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip().isdigit():
            return int(r.stdout.strip())
    except Exception:
        pass
    return 60


def set_brightness(level: int = 70) -> str:
    lvl = max(0, min(100, int(level)))
    cmd = f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1, {lvl})"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=8)
        if r.returncode == 0:
            return f"Screen brightness set to {lvl}%."
        return f"Brightness control unavailable: {(r.stderr or '').strip()[:60]}"
    except Exception as e:
        return f"Brightness control failed: {str(e)[:60]}"


def step_brightness(direction: str = "up", step: int = 15) -> str:
    is_up = (direction or "up").lower().strip() in ("up", "increase", "higher", "brighter")
    curr = _get_current_brightness()
    delta = max(5, min(50, int(step or 15)))
    target = max(5, min(100, curr + (delta if is_up else -delta)))
    return set_brightness(target)


def take_screenshot(open_after: bool = False) -> str:
    """Captures a full-resolution screenshot and saves it to Pictures\\Screenshots."""
    from tony_core.vision import _screenshot_image
    from tony_core.tools import files
    from tony_core import pc_index
    try:
        img, _ = _screenshot_image()
        pics_dir = files.FOLDERS.get("pictures", os.path.join(os.path.expanduser("~"), "Pictures"))
        shot_dir = os.path.join(pics_dir, "Screenshots")
        os.makedirs(shot_dir, exist_ok=True)
        fname = f"Screenshot_{datetime.datetime.now():%Y-%m-%d_%H-%M-%S}.png"
        full_p = os.path.join(shot_dir, fname)
        img.save(full_p, format="PNG")
        pc_index.RECENT_EVENTS.appendleft(f"SCREENSHOT SAVED: {fname} ({full_p})")
        pc_index._flush_rows([(full_p, fname, fname.lower(), 0, time.time())])
        if open_after:
            os.startfile(full_p)
            return f"Took a screenshot and opened it: {full_p}"
        return f"Saved screenshot to {full_p}."
    except Exception as e:
        return f"Screenshot failed: {str(e)[:60]}"


def media_control(action: str = "playpause") -> str:
    try:
        import pyautogui
        act = (action or "").lower().strip()
        key = {
            "play": "playpause", "pause": "playpause", "playpause": "playpause",
            "next": "nexttrack", "previous": "prevtrack", "prev": "prevtrack",
            "mute": "volumemute", "unmute": "volumemute"
        }.get(act)
        if not key:
            return "action must be: play, pause, next, previous, or mute."
        pyautogui.press(key)
        return f"Sent media key '{act}'."
    except Exception as e:
        return f"Media control failed: {str(e)[:60]}"


def top_processes(sort_by: str = "memory") -> str:
    mode = (sort_by or "memory").lower().strip()
    if mode != "cpu":
        try:
            r = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
            )
            if r.returncode == 0 and r.stdout:
                procs = {}
                for row in csv.reader(io.StringIO(r.stdout)):
                    if len(row) >= 5:
                        name = row[0]
                        mem_digits = "".join(ch for ch in row[4] if ch.isdigit())
                        mem_kb = int(mem_digits) if mem_digits else 0
                        procs[name] = procs.get(name, 0) + mem_kb
                top = sorted(procs.items(), key=lambda x: x[1], reverse=True)[:8]
                if top:
                    lines = [f"{name}: {kb / 1024:.0f} MB" for name, kb in top]
                    return "Top processes by RAM:\n" + "\n".join(lines)
        except Exception:
            pass

    col = "WS" if mode != "cpu" else "CPU"
    cmd = (
        f"Get-Process | Sort-Object {col} -Descending | Select-Object -First 8 "
        f"Name, Id, @{{Name='RAM_MB';Expression={{[math]::Round($_.WS / 1MB)}}}}, "
        f"@{{Name='CPU_s';Expression={{[math]::Round($_.CPU, 1)}}}} | Format-Table -AutoSize | Out-String"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=15)
        return (r.stdout or "Could not read process list.").strip()[:1200]
    except Exception as e:
        return f"Process check failed: {str(e)[:60]}"


def kill_process(app_name: str) -> str:
    """Terminates a frozen or high-RAM background application safely."""
    raw = (app_name or "").strip().lower()
    if not raw:
        return "Tell me which process or application to kill."
    clean = re.sub(r"\.exe$", "", raw).strip()
    if clean in _PROTECTED_PROCS:
        return f"Refused: '{clean}' is a protected Windows or Tony system process."
    exe = config.EXES.get(clean, f"{clean}.exe")
    r = subprocess.run(f'taskkill /IM "{exe}" /F', shell=True, capture_output=True, text=True)
    if r.returncode == 0:
        ui._chrome_running()
        return f"Terminated process '{exe}'."
    return f"No running process matching '{exe}' found. Do NOT claim success."


def disk_space() -> str:
    """Reports free and total storage space across all fixed drives (C:, D:, etc.)."""
    parts = []
    for letter in "CDEFG":
        drv = f"{letter}:\\"
        if os.path.isdir(drv):
            try:
                usage = shutil.disk_usage(drv)
                free_gb = usage.free / (2**30)
                total_gb = usage.total / (2**30)
                used_pct = int(round((usage.used / usage.total) * 100))
                parts.append(f"Drive {letter}: {free_gb:.1f} GB free of {total_gb:.1f} GB ({used_pct}% used)")
            except Exception:
                continue
    return "; ".join(parts) if parts else "Could not read disk storage."


def wifi_control(action: str = "status", network_name: str = "") -> str:
    """
    Wi-Fi & Network Controller:
    status, list, connect (pass network_name), disconnect,ip, settings.
    """
    act = (action or "status").lower().strip()
    ssid = (network_name or "").strip()

    if act in ("status", "info", "connected"):
        try:
            r = subprocess.run("netsh wlan show interfaces", shell=True, capture_output=True, text=True, timeout=5)
            out = r.stdout or ""
            m_ssid = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.M)
            m_sig = re.search(r"^\s*Signal\s*:\s*(.+)$", out, re.M)
            m_state = re.search(r"^\s*State\s*:\s*(.+)$", out, re.M)
            state = (m_state.group(1).strip() if m_state else "unknown")
            if m_ssid and "connected" in state.lower():
                sig = m_sig.group(1).strip() if m_sig else "?"
                return f"Connected to Wi-Fi '{m_ssid.group(1).strip()}' (Signal: {sig})."
            return f"Wi-Fi is currently {state}."
        except Exception as e:
            return f"Couldn't check Wi-Fi status: {str(e)[:60]}"

    if act in ("list", "scan", "networks", "available"):
        try:
            r = subprocess.run("netsh wlan show networks", shell=True, capture_output=True, text=True, timeout=6)
            ssids = [m.group(1).strip() for m in re.finditer(r"SSID\s+\d+\s*:\s*(.+)", r.stdout or "") if m.group(1).strip()]
            return f"Available Wi-Fi networks: {', '.join(ssids[:10])}." if ssids else "No Wi-Fi networks found."
        except Exception as e:
            return f"Wi-Fi scan failed: {str(e)[:60]}"

    if act == "connect" and ssid:
        r = subprocess.run(f'netsh wlan connect name="{ssid}"', shell=True, capture_output=True, text=True, timeout=8)
        if r.returncode == 0:
            return f"Connecting to Wi-Fi network '{ssid}'."
        subprocess.run("start ms-settings:network-wifi", shell=True)
        return f"Couldn't auto-connect to '{ssid}' (opened Wi-Fi settings)."

    if act == "disconnect":
        subprocess.run("netsh wlan disconnect", shell=True, capture_output=True, timeout=5)
        return "Disconnected from Wi-Fi."

    if act in ("ip", "ip_address", "ping"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(2.0)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            return f"Local IP address is {local_ip} (Internet connection is online)."
        except Exception:
            return "No active internet connection detected."

    subprocess.run("start ms-settings:network-wifi", shell=True)
    return "Opened Windows Wi-Fi settings."


def system_control(action: str, value: str = "") -> str:
    """
    Universal Windows OS & Hardware Controller:
    Audio: volume_up, volume_down, mute, unmute, mute_mic, sound_settings, volume_mixer
    Display: brightness_up, brightness_down, screenshot, open_last_screenshot, snipping_tool, night_light, display_settings, project_screen
    Wireless: wifi_status, wifi_list, wifi_connect (value=ssid), wifi_disconnect, wifi_settings, bluetooth_settings, hotspot_settings, airplane_mode, ip_address
    Windows Panels: start_menu, quick_settings, notifications, clipboard_history, emoji_panel, task_manager, run_dialog, settings (value=page)
    Virtual Desktops: new_desktop, close_desktop, next_desktop, prev_desktop
    Storage & Cleanup: disk_space, empty_recycle_bin, kill_process (value=app_name), battery_status
    """
    try:
        import pyautogui
    except ImportError:
        return "The pyautogui package is needed."

    act = re.sub(r"[\s\-]+", "_", (action or "").lower().strip())
    val = (value or "").strip()

    # 1) Audio & Volume Stepping
    if act in ("volume_up", "increase_volume", "louder"):
        return step_volume("up", int(val) if val.isdigit() else 10)
    if act in ("volume_down", "decrease_volume", "quieter", "softer"):
        return step_volume("down", int(val) if val.isdigit() else 10)
    if act in ("mute", "unmute"):
        return media_control("mute")
    if act in ("mute_mic", "unmute_mic", "toggle_mic"):
        pyautogui.hotkey("win", "alt", "k")
        return "Toggled Windows microphone mute (Win+Alt+K)."
    if act == "volume_mixer":
        subprocess.run("start ms-settings:apps-volume", shell=True)
        return "Opened Windows Volume Mixer."

    # 2) Brightness, Display & Screenshots
    if act in ("brightness_up", "increase_brightness", "brighter"):
        return step_brightness("up", int(val) if val.isdigit() else 15)
    if act in ("brightness_down", "decrease_brightness", "dimmer", "darker"):
        return step_brightness("down", int(val) if val.isdigit() else 15)
    if act in ("screenshot", "take_screenshot", "screen_capture"):
        return take_screenshot(open_after=("open" in val.lower() or "show" in val.lower()))
    if act in ("open_last_screenshot", "show_screenshot", "last_screenshot"):
        from tony_core.tools import files
        shot_dir = os.path.join(files.FOLDERS.get("pictures", ""), "Screenshots")
        if os.path.isdir(shot_dir):
            shots = [os.path.join(shot_dir, f) for f in os.listdir(shot_dir) if f.lower().endswith((".png", ".jpg"))]
            if shots:
                newest = max(shots, key=os.path.getmtime)
                os.startfile(newest)
                return f"Opened latest screenshot: '{os.path.basename(newest)}'."
        return "No saved screenshots found."
    if act in ("snipping_tool", "screen_snip"):
        pyautogui.hotkey("win", "shift", "s")
        return "Opened Windows Snipping Tool overlay."
    if act == "project_screen":
        pyautogui.hotkey("win", "p")
        return "Opened Project Screen menu."

    # 3) Wi-Fi, Bluetooth & Network
    if act in ("wifi_status", "wifi_info"):
        return wifi_control("status")
    if act in ("wifi_list", "wifi_networks", "scan_wifi"):
        return wifi_control("list")
    if act == "wifi_connect":
        return wifi_control("connect", network_name=val)
    if act == "wifi_disconnect":
        return wifi_control("disconnect")
    if act in ("ip_address", "ip", "network_info"):
        return wifi_control("ip")
    if act in ("bluetooth", "bluetooth_settings"):
        subprocess.run("start ms-settings:bluetooth", shell=True)
        return "Opened Windows Bluetooth settings."
    if act in ("wifi_settings", "network_settings"):
        subprocess.run("start ms-settings:network-wifi", shell=True)
        return "Opened Windows Wi-Fi settings."
    if act in ("hotspot", "hotspot_settings", "mobile_hotspot"):
        subprocess.run("start ms-settings:network-mobilehotspot", shell=True)
        return "Opened Windows Mobile Hotspot settings."
    if act in ("airplane_mode", "airplane"):
        subprocess.run("start ms-settings:network-airplanemode", shell=True)
        return "Opened Windows Airplane Mode settings."

    # 4) Windows OS Panels & Virtual Desktops
    win_hotkeys = {
        "start_menu": ("win",),
        "quick_settings": ("win", "a"),
        "action_center": ("win", "a"),
        "notifications": ("win", "n"),
        "notification_center": ("win", "n"),
        "calendar": ("win", "n"),
        "clipboard_history": ("win", "v"),
        "emoji_panel": ("win", "."),
        "task_manager": ("ctrl", "shift", "esc"),
        "run_dialog": ("win", "r"),
        "file_explorer": ("win", "e"),
        "new_desktop": ("ctrl", "win", "d"),
        "new_virtual_desktop": ("ctrl", "win", "d"),
        "close_desktop": ("ctrl", "win", "f4"),
        "close_virtual_desktop": ("ctrl", "win", "f4"),
        "next_desktop": ("ctrl", "win", "right"),
        "next_virtual_desktop": ("ctrl", "win", "right"),
        "prev_desktop": ("ctrl", "win", "left"),
        "previous_virtual_desktop": ("ctrl", "win", "left"),
    }
    if act in win_hotkeys:
        keys = win_hotkeys[act]
        if len(keys) == 1:
            pyautogui.press(keys[0])
        else:
            pyautogui.hotkey(*keys)
        return f"Opened {act.replace('_', ' ')}."

    # 5) Windows Settings Pages
    if act in ("settings", "windows_settings") or act.endswith("_settings"):
        page_key = val.lower().strip() if val else act.replace("_settings", "").replace("_", " ").strip()
        uri = _SETTINGS_URIS.get(page_key, "ms-settings:")
        subprocess.run(f"start {uri}", shell=True)
        return f"Opened Windows {page_key or 'System'} Settings."

    # 6) Disk Space, Recycle Bin, Battery & Process Killer
    if act in ("disk_space", "storage", "storage_status", "drive_space"):
        return disk_space()
    if act == "battery_status":
        return system_status()
    if act == "kill_process" and val:
        return kill_process(val)
    if act in ("empty_recycle_bin", "clear_recycle_bin"):
        if not confirm("empty the Windows Recycle Bin permanently?"):
            return "Cancelled emptying Recycle Bin."
        try:
            # SHERB_NOCONFIRMATION=1 | SHERB_NOPROGRESSUI=2 | SHERB_NOSOUND=4 -> 7
            ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 7)
            return "Emptied the Windows Recycle Bin."
        except Exception as e:
            return f"Couldn't empty Recycle Bin: {str(e)[:60]}"

    return f"Unknown system control action '{action}'."


def system_status() -> str:
    try:
        class MEM(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)
            ]
        m = MEM()
        m.dwLength = ctypes.sizeof(m)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))

        class PSP(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                ("BatteryLifePercent", ctypes.c_byte), ("Reserved1", ctypes.c_byte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)
            ]
        p = PSP()
        ok = ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(p))
        avail_gb = m.ullAvailPhys / (2**30)
        total_gb = m.ullTotalPhys / (2**30)
        ram = f"RAM {m.dwMemoryLoad}% used ({avail_gb:.1f}GB free of {total_gb:.1f}GB)"
        bat = (
            f"battery {p.BatteryLifePercent}% {'(charging)' if p.ACLineStatus == 1 else '(on battery)'}"
            if (ok and 0 < p.BatteryLifePercent <= 100)
            else "on AC power"
        )
        c_free = shutil.disk_usage("C:\\").free / (2**30)
        return f"{ram}; {bat}; C: drive {c_free:.1f}GB free."
    except Exception as e:
        return f"Status failed: {str(e)[:60]}"


def system_power(action: str = "lock") -> str:
    act = (action or "").lower().strip()
    if act == "lock":
        ctypes.windll.user32.LockWorkStation()
        return "Workstation locked."
    if act == "cancel":
        subprocess.run("shutdown /a", shell=True, capture_output=True)
        return "Cancelled any pending shutdown or restart."
    if act == "sleep":
        if not confirm("put the computer to sleep?"):
            return "Sleep cancelled."
        ctypes.windll.powrprof.SetSuspendState(0, 0, 0)
        return "Putting system to sleep."
    if act == "hibernate":
        if not confirm("hibernate the computer?"):
            return "Hibernate cancelled."
        subprocess.run("shutdown /h", shell=True)
        return "Hibernating system."
    if act in ("sign_out", "log_off", "logout"):
        if not confirm("sign out of Windows?"):
            return "Sign out cancelled."
        subprocess.run("shutdown /l", shell=True)
        return "Signing out."
    if act == "shutdown":
        if not confirm("SHUT DOWN the computer? This is a hard power-off."):
            return "Shutdown cancelled."
        subprocess.run("shutdown /s /t 10", shell=True)
        return "Shutting down in 10 seconds. Say 'cancel shutdown' to abort."
    if act == "restart":
        if not confirm("RESTART the computer?"):
            return "Restart cancelled."
        subprocess.run("shutdown /r /t 10", shell=True)
        return "Restarting in 10 seconds. Say 'cancel shutdown' to abort."
    return "Supported actions: 'lock', 'sleep', 'hibernate', 'sign_out', 'shutdown', 'restart', or 'cancel'."