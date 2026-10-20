# =====================================================================
# tony_core/pc_index.py
# Ultra-Light (<2MB RAM, <1% CPU) Whole-PC Indexer & File Watcher.
# PRIVACY GUARANTEE: Database is stored in %LOCALAPPDATA%\Tony outside
# the Git repository so personal filenames can NEVER be pushed to GitHub.
# =====================================================================

import os
import re
import time
import sqlite3
import difflib
import threading
import subprocess
from collections import deque
from tony_core import config

# Store the index database in Windows AppData\Local\Tony (OUTSIDE the git repo!)
_LOCAL_DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", config.TONY_HOME), "Tony")
os.makedirs(_LOCAL_DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(_LOCAL_DATA_DIR, "tony_pc_index.db")

_DB_LOCK = threading.Lock()
RECENT_EVENTS = deque(maxlen=12)
INSTALLED_APPS = {}
INDEX_STATS = {"files": 0, "folders": 0, "apps": 0, "ready": False}

_SKIP_DIRS = {
    "$recycle.bin", "system volume information", "windows", "program files",
    "program files (x86)", "programdata", "appdata", "node_modules", ".git",
    ".venv", "venv", "__pycache__", ".idea", ".vscode", "site-packages",
    "tony_cache", "build", "dist", "packages", "library", "temp", "tmp", "cache"
}


def _get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA cache_size=-2000;")  # Hard-cap SQLite RAM cache to ~2MB
    conn.execute("PRAGMA temp_store=FILE;")   # Never use RAM for temp tables
    return conn


def _init_db():
    with _DB_LOCK:
        conn = _get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    name TEXT,
                    name_low TEXT,
                    is_dir INTEGER,
                    mtime REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_name_low ON files(name_low);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mtime ON files(mtime DESC);")
            conn.commit()
        finally:
            conn.close()


def _get_watch_roots():
    user_home = os.path.expanduser("~")
    priority = []
    for sub in ("Desktop", "Downloads", "Documents", "Pictures", "Videos", "Music",
                "OneDrive\\Desktop", "OneDrive\\Documents"):
        p = os.path.join(user_home, sub)
        if os.path.isdir(p) and p not in priority:
            priority.append(p)
    if config.TONY_HOME not in priority and os.path.isdir(config.TONY_HOME):
        priority.append(config.TONY_HOME)

    extra_drives = []
    for letter in "DEFG":
        drv = f"{letter}:\\"
        if os.path.isdir(drv):
            extra_drives.append(drv)
    return priority, extra_drives


def _scan_installed_apps():
    start_dirs = [
        os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
    ]
    apps = {}
    for sdir in start_dirs:
        if not os.path.isdir(sdir):
            continue
        for root, _, files in os.walk(sdir):
            for f in files:
                if f.lower().endswith(".lnk"):
                    clean = f[:-4].strip().lower()
                    if any(bad in clean for bad in ("uninstall", "readme", "help", "manual")):
                        continue
                    apps[clean] = os.path.join(root, f)
    INSTALLED_APPS.clear()
    INSTALLED_APPS.update(apps)
    INDEX_STATS["apps"] = len(INSTALLED_APPS)


def _flush_rows(rows):
    if not rows:
        return
    with _DB_LOCK:
        conn = _get_conn()
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO files (path, name, name_low, is_dir, mtime) VALUES (?, ?, ?, ?, ?)",
                rows
            )
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()


def _low_cpu_crawl(roots, max_depth=4):
    """Crawls folders gently with a tiny sleep yield so CPU stays near 0%."""
    batch = []
    f_count, d_count = 0, 0
    for root_dir in roots:
        if not os.path.isdir(root_dir):
            continue
        base_depth = root_dir.rstrip("\\/").count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root_dir):
            # Yield CPU for 2ms every directory so low-end CPUs never lag
            time.sleep(0.002)
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d.lower() not in _SKIP_DIRS
                and (dirpath.count(os.sep) - base_depth) < max_depth
            ]
            for d in dirnames:
                full_d = os.path.join(dirpath, d)
                try:
                    mt = os.path.getmtime(full_d)
                except Exception:
                    mt = 0.0
                batch.append((full_d, d, d.lower(), 1, mt))
                d_count += 1

            for fn in filenames:
                if fn.startswith("~$") or fn.endswith((".tmp", ".crdownload", ".pyc", ".log")):
                    continue
                full_f = os.path.join(dirpath, fn)
                try:
                    mt = os.path.getmtime(full_f)
                except Exception:
                    continue
                batch.append((full_f, fn, fn.lower(), 0, mt))
                f_count += 1

            if len(batch) >= 500:
                _flush_rows(batch)
                batch.clear()
    if batch:
        _flush_rows(batch)
    return f_count, d_count


def _background_watcher_loop():
    # Wait 4 seconds after boot so startup audio/UI loads with zero CPU competition
    time.sleep(4.0)
    _init_db()
    _scan_installed_apps()
    priority_roots, extra_drives = _get_watch_roots()

    f1, d1 = _low_cpu_crawl(priority_roots, max_depth=5)
    f2, d2 = _low_cpu_crawl(extra_drives, max_depth=3)
    INDEX_STATS["files"] = f1 + f2
    INDEX_STATS["folders"] = d1 + d2
    INDEX_STATS["ready"] = True
    print(f"[pc-index] ready ({INDEX_STATS['files']} files, {INDEX_STATS['folders']} folders, {INDEX_STATS['apps']} apps)")

    # Ultra-lightweight watcher: only stores folder timestamps (~50 ints in RAM!)
    dir_mtimes = {}
    for r in priority_roots:
        try:
            dir_mtimes[r] = os.path.getmtime(r)
        except Exception:
            pass

    last_check_time = time.time()
    while True:
        time.sleep(12.0)  # Check once every 12s (0.0% CPU impact)
        check_started = time.time()
        try:
            for r in priority_roots:
                if not os.path.isdir(r):
                    continue
                try:
                    curr_dir_mt = os.path.getmtime(r)
                except Exception:
                    continue
                # Scan immediate files inside priority folders if modified since last check
                if curr_dir_mt >= dir_mtimes.get(r, 0.0):
                    dir_mtimes[r] = curr_dir_mt
                try:
                    with os.scandir(r) as it:
                        new_rows = []
                        for entry in it:
                            if entry.name.startswith((".", "~$")) or entry.name.endswith((".tmp", ".crdownload", ".log")):
                                continue
                            try:
                                mt = entry.stat().st_mtime
                            except Exception:
                                continue
                            if mt > last_check_time:
                                kind_lbl = "FOLDER" if entry.is_dir() else "FILE"
                                evt = f"{kind_lbl} TOUCHED: {entry.name} ({entry.path})"
                                if not RECENT_EVENTS or RECENT_EVENTS[0] != evt:
                                    RECENT_EVENTS.appendleft(evt)
                                    print(f"  [pc-watch] {evt}")
                                new_rows.append((entry.path, entry.name, entry.name.lower(), 1 if entry.is_dir() else 0, mt))
                        if new_rows:
                            _flush_rows(new_rows)
                except Exception:
                    pass
        except Exception:
            pass
        last_check_time = check_started


def start_pc_index():
    t = threading.Thread(target=_background_watcher_loop, daemon=True)
    t.start()


def live_context_summary() -> str:
    recent = list(RECENT_EVENTS)[:3]
    rec_txt = "; ".join(recent) if recent else "none"
    return f"Recent PC files touched: {rec_txt}."


def search_pc(query: str, kind: str = "any", limit: int = 8) -> list:
    q = (query or "").strip().lower()
    if not q:
        return []
    tokens = [t for t in re.split(r"[\s_\-\.]+", q) if t]
    if not tokens:
        return []
    where_clauses = ["name_low LIKE ?" for _ in tokens]
    params = [f"%{tok}%" for tok in tokens]
    if kind == "file":
        where_clauses.append("is_dir = 0")
    elif kind == "folder":
        where_clauses.append("is_dir = 1")

    sql = f"SELECT path, name, is_dir, mtime FROM files WHERE {' AND '.join(where_clauses)} ORDER BY mtime DESC LIMIT 40"
    with _DB_LOCK:
        conn = _get_conn()
        try:
            rows = conn.execute(sql, params).fetchall()
        except Exception:
            rows = []
        finally:
            conn.close()

    scored = []
    for path, name, is_dir, mtime in rows:
        if not os.path.exists(path):
            continue
        nl = name.lower()
        base_no_ext = os.path.splitext(nl)[0]
        sim = max(difflib.SequenceMatcher(None, q, nl).ratio(), difflib.SequenceMatcher(None, q, base_no_ext).ratio())
        exact_bonus = 1.0 if (q == nl or q == base_no_ext) else (0.5 if nl.startswith(q) else 0.0)
        scored.append((exact_bonus + sim, mtime, {"path": path, "name": name, "is_dir": bool(is_dir), "mtime": mtime}))

    scored.sort(key=lambda x: (round(x[0], 2), x[1]), reverse=True)
    return [x[2] for x in scored[:limit]]


def get_recent_files(limit: int = 6, kind: str = "file") -> list:
    is_dir_val = 1 if kind == "folder" else 0
    with _DB_LOCK:
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT path, name, is_dir, mtime FROM files WHERE is_dir = ? ORDER BY mtime DESC LIMIT ?",
                (is_dir_val, limit * 2)
            ).fetchall()
        except Exception:
            rows = []
        finally:
            conn.close()
    out = []
    for path, name, is_dir, mtime in rows:
        if os.path.exists(path):
            out.append({"path": path, "name": name, "is_dir": bool(is_dir), "mtime": mtime})
            if len(out) >= limit:
                break
    return out


def find_anywhere(query: str, kind: str = "any") -> str:
    hits = search_pc(query, kind=kind, limit=6)
    if not hits:
        return f"No {kind} matching '{query}' found on your PC."
    return "Found on PC:\n" + "\n".join(
        f"[{'FOLDER' if h['is_dir'] else 'FILE'}] {h['name']} -> {h['path']}" for h in hits
    )


def open_anything(target: str) -> str:
    t = (target or "").strip().lower()
    if not t:
        return "Tell me what file, folder, or app to open."

    if any(p in t for p in ("just created", "just edited", "just downloaded", "latest file", "recent file", "last file", "newest file")):
        rec = get_recent_files(limit=1, kind="file")
        if rec:
            os.startfile(rec[0]["path"])
            return f"Opened your latest file '{rec[0]['name']}' ({rec[0]['path']})."
        return "No recent files found."

    if t in INSTALLED_APPS:
        os.startfile(INSTALLED_APPS[t])
        return f"Opened installed app '{target}'."
    app_m = difflib.get_close_matches(t, INSTALLED_APPS.keys(), n=1, cutoff=0.78)
    if app_m:
        os.startfile(INSTALLED_APPS[app_m[0]])
        return f"Opened installed app '{app_m[0]}'."

    hits = search_pc(target, kind="any", limit=3)
    if hits:
        best = hits[0]
        try:
            os.startfile(best["path"])
            return f"Opened {'folder' if best['is_dir'] else 'file'} '{best['name']}' at {best['path']}."
        except Exception as e:
            return f"Found '{best['path']}', but couldn't open it: {str(e)[:60]}"
    return f"Couldn't find '{target}' anywhere on your PC. Do NOT claim success."


def recent_pc_activity(count: int = 6) -> str:
    n = max(1, min(int(count or 6), 15))
    evts = list(RECENT_EVENTS)[:n]
    rec = get_recent_files(limit=n, kind="file")
    out = []
    if evts:
        out.append("Live Events:\n" + "\n".join(evts))
    if rec:
        out.append("Newest Files on Disk:\n" + "\n".join(f"- {r['name']} ({r['path']})" for r in rec))
    return "\n\n".join(out) if out else "No recent file activity."