"""End-to-end tests. Each run uses a throwaway HOME, so your real files are never touched.
Run: python3 -m unittest discover tests"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def write(path, mb):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * mb * 1_000_000)


class MCPServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        write(self.home / "Library/Caches/com.example.app/blob", 3)
        write(self.home / "Downloads/Example.dmg", 2)
        write(self.home / "Documents/keep.txt", 2)
        env = dict(os.environ, HOME=str(self.home), PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        self.proc = subprocess.Popen([sys.executable, "-m", "macsweep.mcp_server"], env=env, text=True,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.next_id = 0

    def tearDown(self):
        self.proc.stdin.close()
        self.proc.wait(5)
        self.proc.stdout.close()
        self.tmp.cleanup()

    def rpc(self, method, params=None):
        self.next_id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        reply = json.loads(self.proc.stdout.readline())
        self.assertEqual(reply["id"], self.next_id)
        return reply

    def call(self, name, **args):
        res = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        return json.loads(res["content"][0]["text"]), res["isError"]

    def test_full_flow(self):
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "test", "version": "0"}})["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        tools = {t["name"] for t in self.rpc("tools/list")["result"]["tools"]}
        self.assertIn("move_to_trash", tools)

        self.call("start_scan", categories=["app_caches", "installers"])
        status, _ = self.call("get_scan_status", wait_seconds=20)
        self.assertEqual(status["phase"], "done")

        cache = str(self.home / "Library/Caches/com.example.app")
        dmg = str(self.home / "Downloads/Example.dmg")
        keep = str(self.home / "Documents/keep.txt")
        items, _ = self.call("list_items", category="app_caches")
        self.assertEqual([i["path"] for i in items["items"]], [cache])

        # dry run moves nothing and refuses paths the scan never offered
        dry, _ = self.call("move_to_trash", paths=[cache, keep])
        self.assertEqual(dry["would_move"], [cache])
        self.assertEqual(dry["refused"][0]["path"], keep)
        self.assertTrue(os.path.exists(cache))

        # protected items are refused even if the scan found them
        self.call("protect_path", path=dmg)
        done, _ = self.call("move_to_trash", paths=[cache, dmg, keep], dry_run=False)
        self.assertEqual(done["moved"], [cache])
        self.assertFalse(os.path.exists(cache))
        self.assertTrue((self.home / ".Trash/com.example.app").exists())
        self.assertTrue(os.path.exists(dmg) and os.path.exists(keep))

        _, err = self.call("empty_trash")
        self.assertTrue(err)  # refused without confirm: true

    def test_unknown_method(self):
        self.assertEqual(self.rpc("does/not/exist")["error"]["code"], -32601)


class ProtectedPathsTest(unittest.TestCase):
    def test_is_protected(self):
        from macsweep.core import is_protected
        self.assertTrue(is_protected("/a/b", ["/a"]))
        self.assertTrue(is_protected("/a", ["/a"]))
        self.assertFalse(is_protected("/ab", ["/a"]))


if __name__ == "__main__":
    unittest.main()
