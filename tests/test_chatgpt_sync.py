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


PID = "page_" + "0123456789abcdef" * 2


def conversation_with_page(pid=PID, **kwargs):
    """A conversation whose last turn is a tool result reporting a created page."""
    conv = conversation(**kwargs)
    result = {"result": {"_meta": {"openai/resourceActivities": {"version": 1, "resources": [
        {"id": pid, "url": f"https://chatgpt.com/space/{pid}", "title": "Spec",
         "activities": ["created"]}]}}}}
    tool = {"author": {"role": "tool", "name": "api_tool.call_tool"}, "metadata": {},
            "content": {"content_type": "code", "language": "json", "text": json.dumps(result)},
            "recipient": "all"}
    conv["mapping"]["n3"] = {"id": "n3", "parent": "n2", "message": tool}
    conv["current_node"] = "n3"
    return conv


def page(pid=PID, title="Spec", markdown="# Goals\n\nShip it.", sequence=0,
         edited="2026-01-02T03:04:05Z"):
    """A page as GET /backend-api/pages/<id>?include_content=true returns it (trimmed)."""
    return {"metadata": {"page_id": pid, "title": title, "layout": "document",
                         "created_at": "2026-01-02T03:00:00Z", "updated_at": "2026-01-02T03:00:01Z",
                         "deleted_at": None, "current_sequence": sequence},
            "content": {"page_id": pid, "title": title, "markdown": markdown,
                        "through_sequence": sequence,
                        "blocks": [{"id": "b1", "kind": "markdown", "markdown": markdown,
                                    "last_modified": {"changed_at": edited, "sequence": sequence}}]}}


class RenderMarkdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cs = load({"data_dir": os.path.join(self.tmp, "data")}, self.tmp)

    def render(self):
        conv = conversation()
        return self.cs.render_markdown(conv, {"title": conv["title"], "update_time": conv["update_time"]}, {})

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
                                   "org_id": "ABC-123"}, {})
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
        self.assertTrue(self.cs.store(conversation(), None, index, {}))
        first = index["65000000-1111-2222-3333-444444444444"]["file"]
        self.assertTrue(first.endswith("--650000004444.md"))
        self.assertFalse(self.cs.store(conversation(), None, index, {}))  # not newer
        self.assertTrue(self.cs.store(conversation(title="Renamed", update=1759503700.0), None, index, {}))
        second = index["65000000-1111-2222-3333-444444444444"]["file"]
        self.assertIn("renamed", second)
        self.assertFalse(os.path.exists(os.path.join(self.cs.OUTPUT, first)))
        self.assertTrue(os.path.exists(os.path.join(self.cs.OUTPUT, second)))

    def test_same_second_chats_get_distinct_files(self):
        index = {}
        self.cs.store(conversation(cid="65000000-aaaa-bbbb-cccc-000000000001"), None, index, {})
        self.cs.store(conversation(cid="65000000-aaaa-bbbb-cccc-000000000002"), None, index, {})
        self.assertEqual(len({e["file"] for e in index.values()}), 2)

    def test_rerender_keeps_flags_changed_after_fetch(self):
        cid = "65000000-1111-2222-3333-444444444444"
        conv = conversation()
        conv["is_starred"] = True                        # as fetched
        index = {}
        self.cs.store(conv, None, index, {})
        self.cs.update_flags([{"id": cid, "is_starred": False}], index, {})  # unstarred later
        self.cs.store(conv, None, index, {}, force=True)  # `render` re-stores from raw JSON
        self.assertFalse(index[cid]["is_starred"])

    def test_data_repo_ignores_rendered_output(self):
        # No trailing slash, so a symlinked output folder is ignored too.
        with open(os.path.join(self.cs.DATA, ".gitignore")) as f:
            self.assertEqual(f.read(), "/markdown\n/org\n")


class DocumentTests(unittest.TestCase):
    """ChatGPT pages: found in chats, stored, rendered, linked, and refreshed."""

    CID = "65000000-1111-2222-3333-444444444444"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cs = load({"data_dir": os.path.join(self.tmp, "data")}, self.tmp)
        self.cs.ensure_data_repo()
        self.index, self.docs = {}, {}
        self.cs.store(conversation_with_page(), None, self.index, self.docs)

    def read(self, relpath):
        with open(os.path.join(self.cs.OUTPUT, relpath), encoding="utf-8") as f:
            return f.read()

    def chat_link(self):
        """The chat file's link to the page, checked to resolve to a file."""
        chat = self.index[self.CID]["file"]
        md = self.read(chat)
        self.assertIn("\n## Documents\n\n", md)
        target = md.rsplit("](", 1)[1].rstrip(")\n")
        if "://" not in target:
            self.assertTrue(os.path.exists(os.path.join(self.cs.OUTPUT, os.path.dirname(chat), target)))
        return md.rsplit("\n- ", 1)[1].rstrip("\n")

    def test_refs_come_from_tool_results_links_and_any_branch(self):
        self.assertEqual(self.cs.document_refs(conversation_with_page()), [PID])
        self.assertEqual(self.cs.document_refs(conversation()), [])
        conv = conversation()
        conv["mapping"]["alt"]["message"]["content"]["parts"] = [
            f"See https://chatgpt.com/space/{PID}."]
        self.assertEqual(self.cs.document_refs(conv), [PID])
        self.assertEqual(self.index[self.CID]["documents"], [PID])

    def test_chat_links_page_by_url_until_fetched_then_by_file(self):
        self.assertEqual(self.chat_link(), f"[{PID}](https://chatgpt.com/space/{PID})")
        self.assertTrue(self.cs.store_document(page(), self.docs, self.index))
        doc = self.docs[PID]
        self.assertEqual(doc["file"], os.path.join("documents", "2026", "2026-01-02--spec--456789abcdef.md"))
        self.assertTrue(os.path.exists(os.path.join(self.cs.DOCS_RAW, f"{PID}.json")))
        text = self.read(doc["file"])
        self.assertIn('chatgpt_page_id: "%s"' % PID, text)
        self.assertIn("\n# Spec\n\n## Goals\n\nShip it.\n", text)  # page headings nest under the title
        self.assertEqual(self.chat_link(), "[Spec](../documents/2026/2026-01-02--spec--456789abcdef.md)")

    def test_page_edited_after_its_chat_ended_is_stored_again(self):
        self.cs.store_document(page(), self.docs, self.index)
        self.assertFalse(self.cs.store_document(page(), self.docs, self.index))  # unchanged
        checks = self.cs.documents_to_check(self.index, self.docs, 0)
        self.assertEqual(checks[PID]["version"]["current_sequence"], 0)
        # An edit advances current_sequence only; updated_at stays as it was.
        edited = page(markdown="# Goals\n\nShipped.", sequence=3, edited="2026-02-01T00:00:00Z")
        self.assertTrue(self.cs.store_document(edited, self.docs, self.index))
        self.assertIn("Shipped.", self.read(self.docs[PID]["file"]))
        self.assertEqual(self.docs[PID]["update_time"], self.cs.to_epoch("2026-02-01T00:00:00Z"))
        self.assertEqual(self.cs.documents_to_check(self.index, self.docs, 0)[PID]["version"],
                         {"current_sequence": 3, "updated_at": "2026-01-02T03:00:01Z", "deleted_at": None})

    def test_content_trailing_metadata_is_fetched_again(self):
        lagging = page(sequence=2)
        lagging["metadata"]["current_sequence"] = 3
        self.cs.store_document(lagging, self.docs, self.index)
        self.assertEqual(self.docs[PID]["version"]["current_sequence"], 2)

    def test_renamed_page_moves_its_file_and_relinks_the_chat(self):
        self.cs.store_document(page(), self.docs, self.index)
        first = self.docs[PID]["file"]
        self.cs.store_document(page(title="Final spec", sequence=1), self.docs, self.index)
        self.assertFalse(os.path.exists(os.path.join(self.cs.OUTPUT, first)))
        self.assertTrue(self.chat_link().startswith("[Final spec](../documents/2026/2026-01-02--final-spec--"))

    def test_missing_page_is_kept_and_checked_until_trash_expires(self):
        self.cs.store_document(page(), self.docs, self.index)
        self.cs.mark_missing(PID, self.docs, 1000.0)
        self.assertTrue(os.path.exists(os.path.join(self.cs.OUTPUT, self.docs[PID]["file"])))
        # Fetched in full, so a page restored unchanged from the trash is seen.
        self.assertIsNone(self.cs.documents_to_check(self.index, self.docs, 1000.0)[PID]["version"])
        self.assertNotIn(PID, self.cs.documents_to_check(self.index, self.docs, 1000.0 + 31 * 86400))
        self.assertTrue(self.cs.store_document(page(), self.docs, self.index))  # restored
        self.assertNotIn("missing_since", self.docs[PID])

    def test_rejects_page_ids_that_are_not_page_ids(self):
        with self.assertRaises(ValueError):
            self.cs.store_document(page(pid="../../escape"), self.docs, self.index)
        with self.assertRaises(ValueError):
            self.cs.mark_missing("../../escape", self.docs, 0)


@unittest.skipUnless(shutil.which("pandoc"), "pandoc not installed")
class DocumentOrgTests(unittest.TestCase):
    def test_org_page_file_and_chat_link(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        cs = load({"format": "org", "data_dir": os.path.join(tmp, "data")}, tmp)
        cs.ensure_data_repo()
        index, docs = {}, {}
        cs.store(conversation_with_page(), None, index, docs)
        cs.store_document(page(), docs, index)
        with open(os.path.join(cs.OUTPUT, docs[PID]["file"]), encoding="utf-8") as f:
            doc = f.read()
        self.assertIn(f":CHATGPT_PAGE_ID: {PID}\n", doc)
        self.assertIn("#+title: Spec\n\n* Goals\nShip it.\n", doc)
        with open(os.path.join(cs.OUTPUT, index[DocumentTests.CID]["file"]), encoding="utf-8") as f:
            chat = f.read()
        self.assertTrue(chat.endswith(
            "* Documents\n\n- [[file:../documents/2026/2026-01-02--spec--456789abcdef.org][Spec]]\n"))


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

    def test_documents_round_trip_commits(self):
        conv = conversation_with_page()
        self.request("POST", "/conversations", {"items": [{"meta": {}, "conversation": conv}]})
        status, checks = self.request("GET", "/documents")
        self.assertEqual((status, checks), (200, {"documents": {PID: {"kind": "page", "version": None}}}))
        self.assertEqual(self.request("POST", "/documents", {"pages": [page()], "missing": []}),
                         (200, {"written": 1}))
        status, checks = self.request("GET", "/documents")
        self.assertEqual(checks["documents"][PID]["version"], self.cs.page_version(page()))
        status, done = self.request("POST", "/sync-done",
                                    {"listing": [], "summary": {"fetched": 1, "documents": 1}})
        tracked = subprocess.run(["git", "-C", self.cs.DATA, "ls-files"], capture_output=True,
                                 text=True, check=True).stdout.split()
        self.assertIn(f"documents/{PID}.json", tracked)
        self.assertIn("documents.json", tracked)
        log = subprocess.run(["git", "-C", self.cs.DATA, "log", "-1", "--format=%s"],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(log, "sync: 1 fetched, 0 flag changes, 1 documents\n")

    def test_missing_page_is_recorded(self):
        self.assertEqual(self.request("POST", "/documents", {"pages": [], "missing": [PID]}),
                         (200, {"written": 0}))
        self.assertIn("missing_since", self.cs.load_documents()[PID])
        self.assertEqual(self.request("GET", "/documents", origin=None)[1],
                         {"documents": {PID: {"kind": "page", "version": None}}})

    def test_state_reports_extension_code_hash(self):
        status, state = self.request("GET", "/state?v=1.0&code=stale")
        self.assertEqual(status, 200)
        self.assertEqual(state["extension_code"], self.cs.extension_code_hash())
        self.assertRegex(state["extension_code"], r"^[0-9a-f]{64}$")

    def test_extension_code_files_match_extension(self):
        # Both sides must hash the same files, or the extension would reload
        # itself in a loop.
        ext = os.path.join(os.path.dirname(SCRIPT), "..", "extension")
        on_disk = sorted(n for n in os.listdir(ext) if os.path.isfile(os.path.join(ext, n)))
        self.assertEqual(sorted(self.cs.EXTENSION_CODE_FILES), on_disk)
        with open(os.path.join(ext, "background.js")) as f:
            js = f.read()
        self.assertIn("const CODE_FILES = " + json.dumps(self.cs.EXTENSION_CODE_FILES) + ";", js)

    def test_pause_file_refuses_requests(self):
        open(self.cs.PAUSED, "w").close()
        self.assertEqual(self.request("GET", "/state")[0], 503)


if __name__ == "__main__":
    unittest.main()
