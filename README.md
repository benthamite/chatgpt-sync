# chatgpt-sync

Keep a local, always-current copy of your ChatGPT conversations as Markdown
(or Org) files, with every change versioned in git.

A small Chrome extension uses your logged-in ChatGPT session to fetch new and
changed conversations every 30 minutes and hands them to a listener on
`127.0.0.1`, which saves the raw JSON, commits it, and renders one file per
chat. After the first backfill, each sync downloads only what changed.
Pages that ChatGPT creates or edits in a chat are kept too, at their current
version.

> [!WARNING]
> chatgpt-sync reads your chats through ChatGPT's **undocumented internal web
> API** (the one chatgpt.com itself uses). It is not affiliated with or endorsed
> by OpenAI. The API can change without notice and break the sync, and
> automated access to the service may conflict with
> [OpenAI's Terms of Use](https://openai.com/policies/terms-of-use). Use it at
> your own risk, on your own account only. If you just want a one-off copy,
> ChatGPT's official export (Settings → Data controls → Export data) is the
> sanctioned route; `chatgpt-sync import` can read it.

## What you get

```
data/
├── raw/<conversation-id>.json   # full conversation trees (source of truth)
├── index.json                   # titles, timestamps, flags, file names
├── documents/<page-id>.json     # ChatGPT pages the chats refer to
├── documents.json               # their titles, versions, file names
├── markdown/2025/2025-06-19--finding-deepl-proofreaders--68548965ac48.md
├── markdown/documents/2026/2026-11-15--project-specification--4f1c2e8a9b07.md
└── .git/                        # one commit per sync
```

Each Markdown file has YAML front matter (title, URL, created/updated, model,
archived/starred) and one `##` heading per turn. Only the branch you see in
ChatGPT is rendered; edits and regenerations stay in the raw JSON. Archived
chats are included. Chats you delete in ChatGPT are kept locally.

**Pages.** When ChatGPT creates, edits or reads a page (the documents that
open at `chatgpt.com/space/page_…`), the chat itself holds only the tool calls
and a link. chatgpt-sync stores each such page separately, renders it to its
own file under `documents/`, and ends the chat's file with a *Documents*
section linking to it. A page can change after its chat ends, so every sync
checks each page and fetches the ones that changed. Pages you delete in
ChatGPT are kept locally.

## Requirements

- macOS or Linux with Python 3.9+ and git
- Google Chrome (or another Chromium browser that loads unpacked extensions)
- [pandoc](https://pandoc.org/installing.html), only for Org output

## Setup

1. **Clone** the repo anywhere, e.g. `~/chatgpt-sync`.
2. **Start the listener** and leave it running:
   ```sh
   ~/chatgpt-sync/bin/chatgpt-sync serve
   ```
   To start it at login on macOS, adapt `contrib/chatgpt-sync.plist` (the
   comments inside say how). On Linux, a systemd user service running the same
   command works.
3. **Load the extension:** open `chrome://extensions`, turn on Developer mode,
   click *Load unpacked*, and choose the `extension/` folder. Use the Chrome
   profile in which you are logged in to ChatGPT.
4. **Sync:** open chatgpt.com, or click the extension's toolbar icon and
   then *Sync now*. The first
   run downloads every conversation; ChatGPT rate-limits this to roughly one
   per minute, so a large history takes a while. Progress continues across
   runs.

Check progress with `bin/chatgpt-sync status`.

## Configuration

Optional settings live in `~/.config/chatgpt-sync/config.json`:

```json
{
  "format": "org",
  "output_dir": "~/notes/chatgpt"
}
```

| Key | Default | Meaning |
|---|---|---|
| `format` | `"markdown"` | `"markdown"` or `"org"` |
| `output_dir` | `<data_dir>/<format>` | Where rendered files go |
| `data_dir` | `data/` in the clone | Raw JSON, index and its git repo |
| `state_dir` | `~/.local/state/chatgpt-sync` | Heartbeat and pause file |

Restart the listener after changing settings, then run `bin/chatgpt-sync
render` to re-render everything in the new format or location. Instead of
setting `output_dir`, you can also replace `data/<format>` with a symlink.

## Commands

```sh
bin/chatgpt-sync serve           # run the listener (127.0.0.1:8765)
bin/chatgpt-sync status          # counts, paths and the last sync
bin/chatgpt-sync render          # re-render all chats and pages from raw JSON
bin/chatgpt-sync import EXPORT   # merge an official export (.zip or folder)
```

To pause syncing, `touch ~/.local/state/chatgpt-sync/paused`; the listener
refuses requests and the extension stops before contacting ChatGPT. Delete the
file to resume.

## How it works

- **Extension** (`extension/`): its background service worker lists your
  conversations via `/backend-api/conversations`, compares each `update_time`
  with what the listener already has, and fetches changed ones via
  `/backend-api/conversation/<id>`. It runs every 30 minutes while Chrome is
  open, when you load chatgpt.com (at most every 10 minutes), and when you
  click *Sync now* in its popup. No ChatGPT tab needs to stay open. It waits out rate limits.
  The listed `update_time` can trail a chat's newest messages for many
  minutes, so *Sync now* also refetches the chat open in the current tab.
- **Pages**: after the chats, the extension asks the listener which pages
  the stored chats refer to. It reads each page's metadata
  (`/backend-api/pages/<id>?include_content=false`) and fetches the page in
  full only if it changed. An edit to a page's text or title advances its
  `current_sequence` but not its `updated_at`, so the extension compares both.
  A page that answers 404 (deleted, or in ChatGPT's 30-day trash) is checked
  until the trash would have emptied, then no longer.
- **Self-reload**: on each sync the extension sends the listener a hash of
  the code it has loaded; the listener compares it with the files in
  `extension/` and logs a mismatch. The extension then reloads itself, so
  edits to its code take effect without clicking reload on the Extensions
  page (once it runs a version with this feature).
- **Listener** (`bin/chatgpt-sync`): a dependency-free Python script bound to
  `127.0.0.1`. It only accepts writes from the extension's fixed origin
  (`chrome-extension://peikpbjilajgjpmkipcbgkfamfpihhno`, pinned by the `key`
  in the manifest), writes `raw/` and `index.json`, commits them, and renders.

**Errors** show as a red `!` badge on the toolbar icon. Click the icon for the
message; if ChatGPT logged you out, the popup links to the login page.
The listener records each completed sync in
`~/.local/state/chatgpt-sync/last-sync.json`, which you can monitor.

## Limitations

- Images and uploaded files are not downloaded; files show their asset
  pointers. The official export includes them.
- Project chats are enumerated through an endpoint that was untested at
  release, for lack of an account with projects.
- Only one ChatGPT account (the one logged in to Chrome) is synced.
- Canvas documents are not synced separately, so the file of a chat that used
  canvas shows canvas's tool calls rather than the document's final text.
  This is untested: no account with canvas chats was available, and ChatGPT's
  composer no longer offers canvas.
- Each sync spends one request per page to check it, so a very large number
  of pages slows syncing down.

## Development

```sh
python3 -m unittest discover -s tests
```

## License

MIT
