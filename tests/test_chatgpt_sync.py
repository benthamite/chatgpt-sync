"""Tests for bin/chatgpt-sync: rendering, storage and the localhost listener.

Run with:  python3 -m unittest discover -s tests
"""

import http.client
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "chatgpt-sync")
ORIGIN = "chrome-extension://peikpbjilajgjpmkipcbgkfamfpihhno"


def load(config: dict, tmp: str):
    """Import the script as a fresh module reading CONFIG from a temp file."""
    path = os.path.join(tmp, "config.json")
    with open(path, "w") as f:
        json.dump(config, f)
    os.environ["CHATGPT_SYNC_CONFIG"] = path
    try:
        name = f"chatgpt_sync_{abs(hash(path))}"
        loader = importlib.machinery.SourceFileLoader(name, SCRIPT)
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
        loader.exec_module(module)
        return module
    finally:
        del os.environ["CHATGPT_SYNC_CONFIG"]


def message(role, text, created=None, **extra):
    msg = {"author": {"role": role}, "content": {"content_type": "text", "parts": [text]},
           "metadata": extra.pop("metadata", {}), "recipient": "all"}
    if created:
        msg["create_time"] = created
    msg.update(extra)
    return msg


def conversation(cid="65000000-1111-2222-3333-444444444444", title="Test chat", update=1759503600.0):
    """A small conversation with a system message, a hidden branch and a citation."""
    nodes = [
        ("n0", None, message("system", "")),
        ("n1", "n0", message("user", "# not a heading\nWhat is *x*?", 1759500001)),
        ("n2", "n1", message("assistant", "# Answer\n\nSee citet0.\n\n```\n# code\n```",
                             1759500010, metadata={"content_references": [
                                 {"matched_text": "citet0", "alt": "([Ex](https://e.x))"}]})),
        ("alt", "n1", message("assistant", "abandoned branch")),
    ]
    mapping = {"root": {"id": "root", "message": None, "parent": None}}
    for nid, parent, msg in nodes:
        mapping[nid] = {"id": nid, "parent": parent or "root", "message": msg}
    return {"conversation_id": cid, "title": title, "create_time": 1759500000.0,
            "update_time": update, "default_model_slug": "gpt-5", "current_node": "n2",
            "mapping": mapping}


class RenderMarkdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cs = load({"data_dir": os.path.join(self.tmp, "data")}, self.tmp)

    def render(self):
        conv = conversation()
        return self.cs.render_markdown(conv, {"title": conv["title"], "update_time": conv["update_time"]})

    def test_default_format_is_markdown_under_data(self):
        self.assertEqual(self.cs.FORMAT, "markdown")
        self.assertEqual(self.cs.OUTPUT, os.path.join(self.tmp, "data", "markdown"))

    def test_front_matter_and_title(self):
        md = self.render()
        self.assertTrue(md.startswith('---\ntitle: "Test chat"\n'))
        self.assertIn('url: "https://chatgpt.com/c/65000000-1111-2222-3333-444444444444"', md)
        self.assertIn("\n# Test chat\n", md)

    def test_turns_nest_under_turn_headings(self):
        md = self.render()
        self.assertIn("## User · ", md)
        self.assertIn("## ChatGPT · ", md)
        self.assertIn("\n### Answer\n", md)       # assistant heading demoted
        self.assertIn("```\n# code\n```", md)      # but not inside code fences

    def test_user_text_is_literal(self):
        self.assertIn("\\# not a heading", self.render())

    def test_citations_resolved_and_hidden_content_dropped(self):
        md = self.render()
        self.assertIn("See ([Ex](https://e.x)).", md)
        self.assertNotIn("", md)
        self.assertNotIn("abandoned branch", md)


@unittest.skipUnless(shutil.which("pandoc"), "pandoc not installed")
class RenderOrgTests(unittest.TestCase):
    def test_org_output(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        cs = load({"format": "org", "data_dir": os.path.join(tmp, "data")}, tmp)
        conv = conversation()
        org = cs.render_org(conv, {"title": conv["title"], "update_time": conv["update_time"],
                                   "org_id": "ABC-123"})
        self.assertTrue(org.startswith(":PROPERTIES:\n:ID:       ABC-123\n"))
        self.assertIn("#+title: Test chat", org)
        self.assertIn("#+begin_example\n# not a heading", org)
        self.assertIn("\n** Answer\n", org)
        self.assertNotIn("abandoned branch", org)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cs = load({"data_dir": os.path.join(self.tmp, "data")}, self.tmp)
        self.cs.ensure_data_repo()

    def test_store_skips_unchanged_and_renames_files(self):
        index = {}
        self.assertTrue(self.cs.store(conversation(), None, index))
        first = index["65000000-1111-2222-3333-444444444444"]["file"]
        self.assertTrue(first.endswith("--650000004444.md"))
        self.assertFalse(self.cs.store(conversation(), None, index))  # not newer
        self.assertTrue(self.cs.store(conversation(title="Renamed", update=1759503700.0), None, index))
        second = index["65000000-1111-2222-3333-444444444444"]["file"]
        self.assertIn("renamed", second)
        self.assertFalse(os.path.exists(os.path.join(self.cs.OUTPUT, first)))
        self.assertTrue(os.path.exists(os.path.join(self.cs.OUTPUT, second)))

    def test_same_second_chats_get_distinct_files(self):
        index = {}
        self.cs.store(conversation(cid="65000000-aaaa-bbbb-cccc-000000000001"), None, index)
        self.cs.store(conversation(cid="65000000-aaaa-bbbb-cccc-000000000002"), None, index)
        self.assertEqual(len({e["file"] for e in index.values()}), 2)

    def test_rerender_keeps_flags_changed_after_fetch(self):
        cid = "65000000-1111-2222-3333-444444444444"
        conv = conversation()
        conv["is_starred"] = True                        # as fetched
        index = {}
        self.cs.store(conv, None, index)
        self.cs.update_flags([{"id": cid, "is_starred": False}], index)  # unstarred later
        self.cs.store(conv, None, index, force=True)     # `render` re-stores from raw JSON
        self.assertFalse(index[cid]["is_starred"])

    def test_data_repo_ignores_rendered_output(self):
        # No trailing slash, so a symlinked output folder is ignored too.
        with open(os.path.join(self.cs.DATA, ".gitignore")) as f:
            self.assertEqual(f.read(), "/markdown\n/org\n")


class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cs = load({"data_dir": os.path.join(self.tmp, "data"),
                        "state_dir": os.path.join(self.tmp, "state")}, self.tmp)
        self.server = self.cs.make_server(0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        for args in (["config", "user.email", "t@example.com"], ["config", "user.name", "Test"]):
            subprocess.run(["git", "-C", self.cs.DATA, *args], check=True)

    def request(self, method, path, body=None, origin=ORIGIN):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        headers = {"Origin": origin} if origin else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body)
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        return r.status, json.loads(r.read() or b"{}")

    def test_origin_rules(self):
        self.assertEqual(self.request("GET", "/state", origin="https://evil.example")[0], 403)
        self.assertEqual(self.request("GET", "/state", origin=None)[0], 200)
        self.assertEqual(self.request("POST", "/sync-done", {}, origin=None)[0], 403)

    def test_sync_round_trip_commits(self):
        conv = conversation()
        item = {"meta": {"id": conv["conversation_id"], "is_archived": True}, "conversation": conv}
        self.assertEqual(self.request("POST", "/conversations", {"items": [item]}), (200, {"written": 1}))
        status, state = self.request("GET", "/state")
        self.assertEqual(state["ids"], {conv["conversation_id"]: conv["update_time"]})
        status, done = self.request("POST", "/sync-done", {"listing": [], "summary": {"fetched": 1}})
        self.assertEqual(status, 200)
        self.assertTrue(done["commit"])
        with open(self.cs.LAST_SYNC) as f:
            self.assertEqual(json.load(f)["summary"], {"fetched": 1})

    def test_pause_file_refuses_requests(self):
        open(self.cs.PAUSED, "w").close()
        self.assertEqual(self.request("GET", "/state")[0], 503)


if __name__ == "__main__":
    unittest.main()
