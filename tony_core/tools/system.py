# =====================================================================
# tony_core/tools/system.py
# System control tools: volume, media, power, processes, status, shell command.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - None
# =====================================================================

import ctypes
import subprocess
from tony_core import config
from tony_core.safety import HARD_BLOCK, _cmd_needs_confirm, confirm
from tony_core.tools import ui

def run_command(command: str) -> str:
    if HARD_BLOCK.search(command or ""): return "Blocked: destructive command."
    if _cmd_needs_confirm(command) and not confirm(f"run shell command: {command[:80]}?"):
        return "The user did not approve that command. Cancelled."
    r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
    if "chrome.exe" in command and "taskkill" in command and "/f" in command.lower():
        ui.STATE["chrome_running"] = False; ui.STATE["chrome_profile"] = "default"
    return (r.stdout or r.stderr or "done")[:1500]

def set_volume(level: int = 50) -> str:
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        dev = AudioUtilities.GetSpeakers()
        interface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        vol = cast(interface, POINTER(IAudioEndpointVolume))
        lvl = max(0, min(100, int(level)))
        vol.SetMasterVolumeLevelScalar(lvl / 100.0, None)
        return f"Volume set to {lvl}%."
    except Exception:
        try:
            import pyautogui
            target = max(0, min(100, int(level)))
            for _ in range(50): pyautogui.press("volumedown")
            for _ in range(target // 2): pyautogui.press("volumeup")
            return f"Volume stepped to about {target}%."
        except Exception as e: return f"Volume control failed: {str(e)[:60]}"

def media_control(action: str = "playpause") -> str:
    try:
        import pyautogui
        key = {"play": "playpause", "pause": "playpause", "playpause": "playpause",
               "next": "nexttrack", "previous": "prevtrack", "prev": "prevtrack",
               "mute": "volumemute", "unmute": "volumemute"}.get((action or "").lower())
        if not key: return "action must be: play, pause, next, previous, or mute."
        pyautogui.press(key)
        return f"Pressed {key}."
    except Exception as e: return f"Media control failed: {str(e)[:60]}"

def top_processes(sort_by: str = "memory") -> str:
    col = "WS" if (sort_by or "").lower() != "cpu" else "CPU"
    cmd = (f"Get-Process | Sort-Object {col} -Descending | Select-Object -First 8 "
           f"Name, Id, @{{Name='RAM_MB';Expression={{[math]::Round($_.WS / 1MB)}}}}, "
           f"@{{Name='CPU_s';Expression={{[math]::Round($_.CPU, 1)}}}} | Format-Table -AutoSize | Out-String")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=8)
        return (r.stdout or "Could not read process list.").strip()[:1200]
    except Exception as e: return f"Process check failed: {str(e)[:60]}"

def system_status() -> str:
    try:
        class MEM(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MEM(); m.dwLength = ctypes.sizeof(m)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        class PSP(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                        ("BatteryLifePercent", ctypes.c_byte), ("Reserved1", ctypes.c_byte),
                        ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
        p = PSP()
        ok = ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(p))
        ram = f"RAM {m.dwMemoryLoad}% used ({m.ullAvailPhys // (2**30)}GB free of {m.ullTotalPhys // (2**30)}GB)"
        bat = f"battery {p.BatteryLifePercent}% {'(charging)' if p.ACLineStatus == 1 else '(on battery)'}" if (ok and 0 < p.BatteryLifePercent <= 100) else "on AC power"
        return f"{ram}; {bat}; Chrome {'running' if ui.STATE['chrome_running'] else 'closed'}."
    except Exception as e: return f"Status failed: {str(e)[:60]}"

def system_power(action: str = "lock") -> str:
    act = (action or "").lower().strip()
    if act == "lock":
        ctypes.windll.user32.LockWorkStation()
        return "Workstation locked, sir."
    if act == "sleep":
        if not confirm("put the computer to sleep?"): return "Sleep cancelled."
        subprocess.run("rundll32.exe powrprof.dll,SetSuspendState 0,1,0", shell=True)
        return "Putting system to sleep."
    if act == "shutdown":
        # Kept behind the human gate even in privileged mode.
        if not confirm("SHUT DOWN the computer? This is a hard power-off."): return "Shutdown cancelled."
        subprocess.run("shutdown /s /t 10", shell=True)
        return "Shutting down in 10 seconds, sir."
    if act == "restart":
        # Kept behind the human gate even in privileged mode.
        if not confirm("RESTART the computer?"): return "Restart cancelled."
        subprocess.run("shutdown /r /t 10", shell=True)
        return "Restarting in 10 seconds, sir."
    return "Supported actions: 'lock', 'sleep', 'shutdown', or 'restart'."
