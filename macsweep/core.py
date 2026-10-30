"""Scanning and cleaning engine shared by the desktop app and the MCP server.

Safety model:
- Only paths found by the latest scan can be removed.
- Protected paths (and anything inside them) are never offered or removed.
- Removal means "move to the Trash". Only empty_trash() deletes permanently.
"""
import fnmatch
import json
import os
import shutil
import stat
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOME = Path.home()
TRASH = HOME / ".Trash"
CONFIG_DIR = HOME / ".config" / "macsweep"
PROTECTED_FILE = CONFIG_DIR / "protected.json"
MIN_SIZE = 1_000_000  # hide items smaller than 1 MB
LARGE_FILE = 500_000_000
# Never look inside these (bundles whose internals must not be touched, or covered elsewhere).
PACKAGES = ["*.photoslibrary", "*.musiclibrary", "*.tvlibrary", "*.app", "*.fcpbundle", "*.logicx", ".git", "node_modules"]

LOCK = threading.Lock()
CANCEL = threading.Event()
PROCS = set()  # running `du` processes, killed on cancel
STATE = {"phase": "idle", "progress": 0.0, "step": -1, "verb": "", "path": "", "started": 0.0, "cats": []}
RESULTS = {}  # category id -> list of items
SIZES = {}  # path -> size; the only paths that may be moved to the Trash


# ---------- finders ----------

def _kids(rel):
    try:
        return [str(p) for p in (HOME / rel).iterdir()]
    except OSError:
        return []


def _existing(*rels):
    return [str(HOME / r) for r in rels if (HOME / r).exists()]


def _walk(root, skip, on_dir):
    """Yield every entry under root without following symlinks."""
    stack = [root]
    while stack and not CANCEL.is_set():
        d = stack.pop()
        on_dir(d)
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        yield e
                        if not skip(e.name):
                            stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        yield e
                except OSError:
                    pass


def _by_root(report, roots, fn):
    """Run fn on each root. Folder sizes are unknown up front, so progress moves one notch
    per root and creeps forward inside each one so it never looks frozen."""
    out = []
    for i, root in enumerate(roots):
        if CANCEL.is_set():
            break
        seen = [0]

        def on(d, i=i, seen=seen):
            seen[0] += 1
            report((i + seen[0] / (seen[0] + 3000)) / len(roots), d)
        out += fn(root, on)
    report(1, None)
    return out


def _find_node_modules(report):
    roots = [str(HOME / r) for r in ("Desktop", "Documents", "Projects", "Developer", "code", "dev", "src")
             if (HOME / r).is_dir()]

    def skip(n):
        return n == "node_modules" or n.startswith(".")
    return _by_root(report, roots, lambda root, on: [
        e.path for e in _walk(root, skip, on) if e.name == "node_modules" and e.is_dir(follow_symlinks=False)])


def _find_large(report):
    try:
        roots = sorted(e.path for e in os.scandir(HOME) if e.name not in ("Library", ".Trash"))
    except OSError:
        roots = []

    def skip(n):
        return any(fnmatch.fnmatch(n, p) for p in PACKAGES)

    def large_in(root, on):
        if os.path.isfile(root) and not os.path.islink(root):
            return [root] if os.lstat(root).st_blocks * 512 >= LARGE_FILE else []
        return [e.path for e in _walk(root, skip, on)
                if e.is_file(follow_symlinks=False) and e.stat(follow_symlinks=False).st_blocks * 512 >= LARGE_FILE]
    return _by_root(report, roots, large_in)


def _old_downloads(days=90):
    limit = time.time() - days * 86400
    out = []
    for p in _kids("Downloads"):
        try:
            if os.lstat(p).st_mtime < limit:
                out.append(p)
        except OSError:
            pass
    return out


# weight = relative duration of the step; disc = share of the step spent finding (the rest is measuring).
# risk "safe": apps or tools rebuild it on their own. risk "review": personal data, look before removing.
CATEGORIES = [
    dict(id="app_caches", title="App caches", risk="safe", selected=True, weight=15, disc=0.05,
         hint="Temporary files apps rebuild on their own when needed.",
         find=lambda r: _kids("Library/Caches")),
    dict(id="logs", title="Logs", risk="safe", selected=True, weight=2, disc=0.05,
         hint="Diagnostic history from apps and macOS.",
         find=lambda r: _kids("Library/Logs")),
    dict(id="dev_caches", title="Developer caches", risk="safe", selected=True, weight=5, disc=0.05,
         hint="Xcode, npm, pnpm, Gradle and Cargo caches. Rebuilt on the next build or install.",
         find=lambda r: _existing(
             "Library/Developer/Xcode/DerivedData", "Library/Developer/Xcode/iOS DeviceSupport",
             "Library/Developer/CoreSimulator/Caches", ".npm/_cacache", "Library/pnpm/store",
             ".gradle/caches", ".cargo/registry/cache")),
    dict(id="dot_cache", title="~/.cache folder", risk="review", selected=False, weight=40, disc=0.02,
         hint="Tool and model downloads (Hugging Face, pip, Playwright…). Some are slow to download again.",
         find=lambda r: _kids(".cache")),
    dict(id="installers", title="Installers", risk="safe", selected=True, weight=1, disc=0.1,
         hint=".dmg and .pkg files left in Downloads.",
         find=lambda r: [p for p in _kids("Downloads") if p.lower().endswith((".dmg", ".pkg"))]),
    dict(id="node_modules", title="node_modules folders", risk="review", selected=False, weight=20, disc=0.6,
         hint="Project dependencies. npm install brings them back.",
         find=_find_node_modules),
    dict(id="old_downloads", title="Old downloads", risk="review", selected=False, weight=3, disc=0.1,
         hint="Items in Downloads not modified for 90 days.",
         find=lambda r: _old_downloads()),
    dict(id="ios_backups", title="iPhone and iPad backups", risk="review", selected=False, weight=1, disc=0.1,
         hint="Local device backups made by Finder.",
         find=lambda r: _kids("Library/Application Support/MobileSync/Backup")),
    dict(id="large_files", title="Large files", risk="review", selected=False, weight=100, disc=0.97,
         hint="Files over 500 MB in your home folder, outside Library.",
         find=_find_large),
]
CATEGORY_IDS = [c["id"] for c in CATEGORIES]


def categories():
    return [{k: c[k] for k in ("id", "title", "hint", "risk", "selected")} for c in CATEGORIES]


# ---------- protected paths ----------

def protected():
    try:
        return json.loads(PROTECTED_FILE.read_text())
    except (OSError, ValueError):
        return []


def _save_protected(lst):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PROTECTED_FILE.write_text(json.dumps(sorted(set(lst)), indent=1, ensure_ascii=False))


def is_protected(path, lst=None):
    lst = protected() if lst is None else lst
    return any(path == e or path.startswith(e.rstrip("/") + "/") for e in lst)


def protect(path):
    path = os.path.abspath(os.path.expanduser(path))
    _save_protected(protected() + [path])
    SIZES.pop(path, None)
    for items in RESULTS.values():
        items[:] = [i for i in items if not is_protected(i["path"], [path])]
    return protected()


def unprotect(path):
    path = os.path.abspath(os.path.expanduser(path))
    _save_protected([e for e in protected() if e != path])
    return protected()


# ---------- scanning ----------

def size_of(p):
    """Real space used on disk (what Finder reports), not the apparent size."""
    if CANCEL.is_set():
        return 0
    try:
        st = os.lstat(p)
    except OSError:
        return 0
    if not stat.S_ISDIR(st.st_mode):
        return st.st_blocks * 512
    proc = subprocess.Popen(["du", "-sk", p], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    with LOCK:
        PROCS.add(proc)
    out, _ = proc.communicate()
    with LOCK:
        PROCS.discard(proc)
    try:
        return int(out.split()[0]) * 1024
    except (IndexError, ValueError):
        return 0


def _item(p, size):
    try:
        st = os.lstat(p)
        mtime, is_dir = st.st_mtime, stat.S_ISDIR(st.st_mode)
    except OSError:
        mtime, is_dir = 0, False
    return {"path": p, "name": os.path.basename(p), "size": size, "mtime": mtime, "dir": is_dir}


def _run_scan(ids):
    prot = protected()
    todo = [c for c in CATEGORIES if c["id"] in ids]
    total_w = sum(c["weight"] for c in todo) or 1
    done_w = 0
    seen = []
    with LOCK:
        RESULTS.clear()
        SIZES.clear()
        STATE.update(phase="scanning", progress=0.0, step=-1, path="", started=time.time(),
                     cats=[{"id": c["id"], "title": c["title"], "count": 0, "size": 0,
                            "status": "wait" if c["id"] in ids else "skip"} for c in CATEGORIES])
    for c in todo:
        if CANCEL.is_set():
            break
        i = CATEGORY_IDS.index(c["id"])
        cs = STATE["cats"][i]
        cs["status"] = "run"
        STATE.update(step=i, verb="Looking in", path="")

        def prog(f, path=None, w=c["weight"], base=done_w):
            STATE["progress"] = (base + w * min(f, 1)) / total_w
            if path:
                STATE["path"] = path

        found = c["find"](lambda f, p=None, c=c: prog(f * c["disc"], p))
        # skip anything already counted, or inside something already counted
        paths = [p for p in found if not is_protected(p, prot) and not is_protected(p, seen)]
        STATE["verb"] = "Measuring"
        items = []
        with ThreadPoolExecutor(4) as pool:
            for n, (p, s) in enumerate(zip(paths, pool.map(size_of, paths))):
                prog(c["disc"] + (1 - c["disc"]) * (n + 1) / len(paths), p)
                if s >= MIN_SIZE and not CANCEL.is_set():
                    items.append(_item(p, s))
                    seen.append(p)
                    with LOCK:
                        cs["count"] += 1
                        cs["size"] += s
        with LOCK:
            RESULTS[c["id"]] = sorted(items, key=lambda x: -x["size"])
            SIZES.update({x["path"]: x["size"] for x in items})
            cs["status"] = "stopped" if CANCEL.is_set() else "done"
        done_w += c["weight"]
    with LOCK:
        for cs in STATE["cats"]:
            if cs["status"] == "wait":
                cs["status"] = "skip"
        STATE.update(phase="cancelled" if CANCEL.is_set() else "done", path="",
                     progress=STATE["progress"] if CANCEL.is_set() else 1.0)


def start_scan(ids=None):
    """Start a scan in the background. Returns False if one is already running."""
    ids = [i for i in (ids or CATEGORY_IDS) if i in CATEGORY_IDS]
    with LOCK:
        if STATE["phase"] == "scanning":
            return False
        STATE["phase"] = "scanning"
    CANCEL.clear()
    threading.Thread(target=_run_scan, args=(ids,), daemon=True).start()
    return True


def cancel_scan():
    CANCEL.set()
    with LOCK:
        for p in PROCS:
            p.kill()


def status():
    with LOCK:
        s = json.loads(json.dumps(STATE))
    s["elapsed"] = time.time() - s["started"] if s["started"] else 0
    return s


def wait(seconds):
    """Block until the scan finishes or `seconds` pass."""
    end = time.time() + seconds
    while STATE["phase"] == "scanning" and time.time() < end:
        time.sleep(0.2)


def results():
    with LOCK:
        return {c["id"]: list(RESULTS.get(c["id"], [])) for c in CATEGORIES}


def disk():
    u = shutil.disk_usage("/")
    return {"total": u.total, "free": u.free, "used": u.total - u.free}


# ---------- actions ----------

def _to_trash(path):
    p = Path(path)
    TRASH.mkdir(exist_ok=True)
    dest = TRASH / p.name
    if dest.exists() or dest.is_symlink():
        dest = TRASH / f"{p.stem} {time.strftime('%H-%M-%S')}{p.suffix}"
    shutil.move(str(p), str(dest))


def check(path, prot=None):
    """Why a path cannot be moved to the Trash, or None if it can."""
    if not path.startswith(str(HOME) + "/"):
        return "outside your home folder"
    if path not in SIZES:
        return "not found by the latest scan (run a scan first)"
    if is_protected(path, prot):
        return "protected"
    return None


def move_to_trash(paths):
    done, freed, errors = [], 0, []
    prot = protected()
    for path in paths:
        why = check(path, prot)
        if why:
            errors.append({"path": path, "reason": why})
            continue
        try:
            _to_trash(path)
            freed += SIZES.pop(path)
            done.append(path)
            for items in RESULTS.values():
                items[:] = [i for i in items if i["path"] != path]
        except OSError as e:
            errors.append({"path": path, "reason": e.strerror or str(e)})
    return {"moved": done, "freed": freed, "errors": errors}


def empty_trash():
    """Permanently delete everything in the Trash, through Finder."""
    r = subprocess.run(["osascript", "-e", 'tell application "Finder" to empty trash'], capture_output=True, text=True)
    return {"ok": r.returncode == 0, "error": r.stderr.strip()}


def reveal(path):
    """Show a scanned or protected item in Finder."""
    if path in SIZES or path in protected():
        subprocess.run(["open", "-R", path])
        return True
    return False


def human(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1000:
            return f"{n:.0f} {unit}" if unit in ("bytes", "KB", "MB") else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"
