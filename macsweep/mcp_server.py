"""MacSweep MCP server: lets any MCP-compatible AI assistant find and clean junk on this Mac.

Speaks JSON-RPC 2.0 over stdio (the MCP stdio transport) using only the Python standard library.
Run it with:  python3 -m macsweep.mcp_server
"""
import json
import sys
import time
from datetime import datetime

from . import __version__, core

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

INSTRUCTIONS = """MacSweep finds files that can be removed to free disk space on this Mac.
Typical flow: get_disk_usage -> start_scan -> get_scan_status (wait_seconds=30, repeat until phase is done)
-> list_items for the categories worth reviewing -> show the user what you plan to remove and how much
space it frees -> after the user agrees, move_to_trash -> optionally empty_trash after a second explicit yes.
Categories with risk "safe" are rebuilt automatically. Categories with risk "review" contain the user's own
data: never remove those items without the user approving each one or the list explicitly.
Everything goes to the Trash first, so it can be restored until the Trash is emptied."""

SCAN_CATEGORY = {"type": "string", "enum": core.CATEGORY_IDS}

TOOLS = [
    {
        "name": "get_disk_usage",
        "description": "Total, used and free space on the startup disk.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "list_categories",
        "description": "What MacSweep can scan. Each category has a risk level: 'safe' (rebuilt automatically) "
                       "or 'review' (user data, check each item with the user).",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "start_scan",
        "description": "Start scanning in the background. Returns immediately; follow up with get_scan_status. "
                       "A full scan takes 1 to 5 minutes, mostly 'large_files' and 'dot_cache'. "
                       "Pass categories to scan only some of them (much faster).",
        "inputSchema": {"type": "object", "properties": {
            "categories": {"type": "array", "items": SCAN_CATEGORY,
                           "description": "Category ids to scan. Omit to scan everything."}}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_scan_status",
        "description": "Progress of the current scan and a size summary per category. "
                       "Set wait_seconds to block until the scan finishes or the time runs out.",
        "inputSchema": {"type": "object", "properties": {
            "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 50, "default": 0}}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "cancel_scan",
        "description": "Stop the running scan. Results found so far stay available.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_items",
        "description": "Items found in one category by the latest scan, with full path, size and last modified date. "
                       "Only these exact paths can be passed to move_to_trash.",
        "inputSchema": {"type": "object", "required": ["category"], "properties": {
            "category": SCAN_CATEGORY,
            "sort": {"type": "string", "enum": ["size", "oldest", "name"], "default": "size"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
            "offset": {"type": "integer", "minimum": 0, "default": 0}}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "move_to_trash",
        "description": "Move items found by the latest scan to the Trash (restorable until the Trash is emptied). "
                       "Always show the user the list and total size and get their explicit approval first. "
                       "dry_run defaults to true and only reports what would happen; set dry_run to false to act. "
                       "Paths not found by the latest scan, outside the home folder, or protected are refused.",
        "inputSchema": {"type": "object", "required": ["paths"], "properties": {
            "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "dry_run": {"type": "boolean", "default": True}}},
        "annotations": {"destructiveHint": True, "idempotentHint": False},
    },
    {
        "name": "empty_trash",
        "description": "Permanently delete everything in the Trash, including items the user put there themselves. "
                       "Irreversible. Only call after the user explicitly asks to empty the Trash. "
                       "Requires confirm: true. macOS may ask the user once to let the app control Finder.",
        "inputSchema": {"type": "object", "required": ["confirm"], "properties": {
            "confirm": {"type": "boolean", "description": "Must be true."}}},
        "annotations": {"destructiveHint": True},
    },
    {
        "name": "protect_path",
        "description": "Never offer or remove this file or folder (and anything inside it) again. "
                       "Use when the user says to keep something.",
        "inputSchema": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}},
        "annotations": {"idempotentHint": True},
    },
    {
        "name": "unprotect_path",
        "description": "Remove a path from the protected list. It shows up again in the next scan.",
        "inputSchema": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}},
        "annotations": {"idempotentHint": True},
    },
    {
        "name": "list_protected",
        "description": "Paths the user chose to keep. MacSweep never offers or removes them.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "reveal_in_finder",
        "description": "Open a Finder window with this item selected, so the user can look at it.",
        "inputSchema": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}},
        "annotations": {"readOnlyHint": True},
    },
]


def _disk():
    d = core.disk()
    return d | {f"{k}_human": core.human(v) for k, v in d.items()}


def _status(args):
    core.wait(min(max(int(args.get("wait_seconds", 0)), 0), 50))
    s = core.status()
    cur = s["cats"][s["step"]]["title"] if 0 <= s["step"] < len(s["cats"]) else None
    return {
        "phase": s["phase"],
        "progress_percent": round(s["progress"] * 100),
        "elapsed_seconds": round(s["elapsed"]),
        "current_step": cur,
        "current_path": s["path"] or None,
        "categories": [{"id": c["id"], "status": c["status"], "items": c["count"],
                        "size": c["size"], "size_human": core.human(c["size"])} for c in s["cats"]],
        "total_found_human": core.human(sum(c["size"] for c in s["cats"])),
    }


def _list_items(args):
    cat = args.get("category")
    if cat not in core.CATEGORY_IDS:
        raise ValueError(f"unknown category {cat!r}; use one of {core.CATEGORY_IDS}")
    items = core.results()[cat]
    key = {"size": lambda i: -i["size"], "oldest": lambda i: i["mtime"], "name": lambda i: i["name"].lower()}
    items.sort(key=key.get(args.get("sort", "size"), key["size"]))
    off, lim = int(args.get("offset", 0)), int(args.get("limit", 50))
    meta = next(c for c in core.categories() if c["id"] == cat)
    return {
        "category": cat, "risk": meta["risk"], "total_items": len(items),
        "total_size_human": core.human(sum(i["size"] for i in items)),
        "scan_phase": core.STATE["phase"],
        "items": [{"path": i["path"], "size": i["size"], "size_human": core.human(i["size"]),
                   "modified": datetime.fromtimestamp(i["mtime"]).date().isoformat() if i["mtime"] else None,
                   "kind": "folder" if i["dir"] else "file"} for i in items[off:off + lim]],
    }


def _move(args):
    paths = args.get("paths") or []
    if args.get("dry_run", True):
        prot = core.protected()
        ok = [p for p in paths if not core.check(p, prot)]
        return {"dry_run": True, "would_move": ok,
                "would_free_human": core.human(sum(core.SIZES[p] for p in ok)),
                "refused": [{"path": p, "reason": core.check(p, prot)} for p in paths if core.check(p, prot)],
                "next": "Show this to the user. If they approve, call again with dry_run: false."}
    r = core.move_to_trash(paths)
    return r | {"freed_human": core.human(r["freed"]),
                "note": "Moved to the Trash. Space is released when the Trash is emptied."}


def _empty(args):
    if args.get("confirm") is not True:
        raise ValueError("confirm must be true. Only empty the Trash when the user explicitly asks.")
    return core.empty_trash() | {"disk": _disk()}


def _start(args):
    ids = args.get("categories") or None
    bad = [i for i in ids or [] if i not in core.CATEGORY_IDS]
    if bad:
        raise ValueError(f"unknown categories {bad}; use ids from list_categories")
    started = core.start_scan(ids)
    return {"started": started, "message": "Scan started." if started else "A scan is already running.",
            "next": "Call get_scan_status with wait_seconds=30 until phase is 'done'."}


HANDLERS = {
    "get_disk_usage": lambda a: _disk(),
    "list_categories": lambda a: core.categories(),
    "start_scan": _start,
    "get_scan_status": _status,
    "cancel_scan": lambda a: core.cancel_scan() or {"cancelled": True},
    "list_items": _list_items,
    "move_to_trash": _move,
    "empty_trash": _empty,
    "protect_path": lambda a: {"protected": core.protect(a["path"])},
    "unprotect_path": lambda a: {"protected": core.unprotect(a["path"])},
    "list_protected": lambda a: {"protected": core.protected()},
    "reveal_in_finder": lambda a: {"shown": core.reveal(a["path"]) or
                                   "Only items from the latest scan or the protected list can be shown."},
}


def handle(msg):
    """Answer one JSON-RPC message. Returns None for notifications."""
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:
        return None  # notifications (initialized, cancelled…) need no answer
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "macsweep", "version": __version__},
                  "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        fn = HANDLERS.get(params.get("name"))
        if not fn:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool: {params.get('name')}"}}
        try:
            out, err = fn(params.get("arguments") or {}), False
        except (ValueError, KeyError, TypeError) as e:
            out, err = {"error": str(e)}, True
        result = {"content": [{"type": "text", "text": json.dumps(out, indent=1, ensure_ascii=False)}], "isError": err}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            reply = handle(msg) if isinstance(msg, dict) else None
        if reply:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    core.cancel_scan()
    time.sleep(0.1)


if __name__ == "__main__":
    main()
