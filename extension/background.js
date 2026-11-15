// Syncs ChatGPT conversations to the local chatgpt-sync listener.
//
// Everything runs here in the service worker, which uses the browser's
// ChatGPT login cookies via the chatgpt.com host permission. No tab is needed;
// the content script only nudges a sync when a ChatGPT page loads.

const SERVER = 'http://127.0.0.1:8765';
const CHATGPT = 'https://chatgpt.com';
const PAGE = 100;
const BATCH = 5;
const PAGE_LOAD_INTERVAL_MS = 10 * 60 * 1000;
const ALARM_INTERVAL_MS = 30 * 60 * 1000;
const HEARTBEAT_STALE_MS = 3 * 60 * 1000;  // a run that stopped reporting

const get = async (k) => (await chrome.storage.local.get(k))[k];
const set = (obj) => chrome.storage.local.set(obj);
const sleep = (ms) => new Promise((res) => setTimeout(res, ms));
const FETCH_GAP_MS = 2000;  // pace conversation fetches to stay under rate limits

const VERSION = chrome.runtime.getManifest().version;

// Hashed the same way by extension_code_hash() in bin/chatgpt-sync, which
// reports the hash of these files on disk. Keep the two lists identical.
const CODE_FILES = ["manifest.json", "background.js", "content.js", "popup.html", "popup.js"];

async function sha256hex(data) {
  const digest = await crypto.subtle.digest('SHA-256', data);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

async function codeHash() {
  let lines = '';
  for (const name of CODE_FILES) {
    const r = await fetch(chrome.runtime.getURL(name));
    lines += `${name}\n${await sha256hex(await r.arrayBuffer())}\n`;
  }
  return sha256hex(new TextEncoder().encode(lines));
}

function ensureAlarm() {
  chrome.alarms.create('tick', {periodInMinutes: 15});
}
chrome.runtime.onStartup.addListener(ensureAlarm);

// Fires on install and on every reload, including the self-reload below.
chrome.runtime.onInstalled.addListener(async () => {
  ensureAlarm();
  // A reload kills any run in progress; drop its state so it doesn't block
  // syncs (and the popup's button) until its heartbeat goes stale.
  // The files read now are the code that was just loaded.
  await set({run: null, loadedCode: await codeHash()});
  const pending = await get('pendingSync');
  if (pending) {
    await set({pendingSync: null});
    maybeSync(0, pending.force || []);
  }
});

// Each stage write also refreshes the run's heartbeat and, being an extension
// API call, keeps the service worker alive during long runs.
async function stage(name) {
  await set({run: {stage: name, heartbeat: Date.now()}});
}

// Chrome stops a service worker after 30 s without extension events or API
// calls, so long waits must keep touching an extension API.
async function wakefulSleep(ms, label) {
  for (let left = ms; left > 0; left -= 10000) {
    await stage(`${label} (${Math.ceil(left / 1000)}s left)`);
    await sleep(Math.min(left, 10000));
  }
}

async function setBadge(error) {
  await chrome.action.setBadgeText({text: error ? '!' : ''});
  await chrome.action.setBadgeBackgroundColor({color: '#c62828'});
  const last = await get('lastSuccess');
  const when = last ? new Date(last).toLocaleString() : 'never';
  await chrome.action.setTitle({
    title: `ChatGPT Sync\nLast success: ${when}` + (error ? `\nError: ${error}\nClick for details.` : ''),
  });
}

async function server(method, path, body) {
  let r;
  try {
    r = await fetch(SERVER + path, {
      method,
      headers: body ? {'Content-Type': 'application/json'} : {},
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(120000),
    });
  } catch (e) {
    throw new Error(`listener not reachable at ${SERVER} (is \`chatgpt-sync serve\` running?): ${e.message}`);
  }
  const json = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`listener ${path}: HTTP ${r.status} ${json.error || ''}`);
  return json;
}

async function api(path, token) {
  for (let attempt = 0; ; attempt++) {
    const r = await fetch(CHATGPT + path, {
      headers: {Authorization: 'Bearer ' + token},
      signal: AbortSignal.timeout(60000),
    });
    if (r.status === 429 && attempt < 8) {
      const wait = Number(r.headers.get('Retry-After')) || 30 * (attempt + 1);
      await wakefulSleep(wait * 1000, `rate limited; waiting ${wait}s`);
      continue;
    }
    if (!r.ok) throw Object.assign(new Error(`${path.split('?')[0]}: HTTP ${r.status}`), {status: r.status});
    return r.json();
  }
}

async function listConversations(token) {
  const byId = new Map();
  for (const archived of [false, true]) {
    for (let off = 0; ; off += PAGE) {
      const j = await api(`/backend-api/conversations?offset=${off}&limit=${PAGE}` +
        `&order=updated&is_archived=${archived}&hide_snorlax=false`, token);
      for (const it of j.items || []) byId.set(it.id, it);
      if (!j.items || j.items.length < PAGE) break;
    }
  }
  // Project ("snorlax") chats, in case the main list leaves them out.
  for (let cursor = null, first = true; first || cursor; first = false) {
    const sb = await api('/backend-api/gizmos/snorlax/sidebar?conversations_per_gizmo=0' +
      (cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''), token);
    for (const item of sb.items || []) {
      const gid = item.gizmo && item.gizmo.gizmo && item.gizmo.gizmo.id;
      if (!gid) continue;
      for (let c = '0'; c != null; ) {
        const j = await api(`/backend-api/gizmos/${gid}/conversations?cursor=${encodeURIComponent(c)}`, token);
        for (const it of j.items || []) if (!byId.has(it.id)) byId.set(it.id, it);
        c = j.cursor != null && (j.items || []).length ? j.cursor : null;
      }
    }
    cursor = sb.cursor || null;
  }
  return [...byId.values()];
}

async function runSync(force = []) {
  const errors = [];
  const summary = {listed: 0, fetched: 0, documents: 0};
  let error = null;
  let errorKind = null;
  try {
    // Ask the listener first, so a stopped or paused listener ends the run
    // before anything is requested from ChatGPT.
    await stage('checking listener');
    const loadedCode = await get('loadedCode');
    const state = await server('GET', `/state?v=${VERSION}&code=${loadedCode || ''}`);
    const ids = state.ids;
    // The listener reports the hash of the extension code on disk. If it
    // differs from the loaded code, reload to pick it up, then rerun this sync.
    // Reload at most once per disk version, so a mismatch that a reload can't
    // fix (e.g. the extension loaded from another folder) fails loudly
    // instead of looping.
    if (state.extension_code && loadedCode && state.extension_code !== loadedCode) {
      if ((await get('reloadedFor')) === state.extension_code) {
        throw new Error('extension code on disk differs from the loaded code even after a ' +
                        'reload; is the extension loaded from this repository?');
      }
      await set({reloadedFor: state.extension_code, pendingSync: {force}, run: null});
      chrome.runtime.reload();
      await new Promise(() => {});  // the reload ends this worker
    }
    await stage('session');
    const r = await fetch(CHATGPT + '/api/auth/session', {signal: AbortSignal.timeout(30000)});
    const session = await r.json().catch(() => ({}));
    if (!session.accessToken) {
      throw Object.assign(
        new Error(`no ChatGPT session (HTTP ${r.status}); log in at chatgpt.com`), {kind: 'login'});
    }
    const token = session.accessToken;
    await stage('listing');
    const listing = await listConversations(token);
    summary.listed = listing.length;
    await stage(`listed ${listing.length}`);
    // The listing's update_time can trail new messages for many minutes (seen
    // on a voice chat), so "Sync now" also refetches the chat open in the
    // current tab; the listener writes it only if it changed.
    const forced = new Set(force);
    const todo = listing.filter((m) =>
      forced.has(m.id) || Date.parse(m.update_time) / 1000 > (ids[m.id] || 0) + 0.001);
    for (const id of forced) if (!listing.some((m) => m.id === id)) todo.push({id});
    let batch = [];
    for (const [i, meta] of todo.entries()) {
      await stage(`fetching ${i + 1}/${todo.length}`);
      if (i > 0) await sleep(FETCH_GAP_MS);
      try {
        batch.push({meta, conversation: await api(`/backend-api/conversation/${meta.id}`, token)});
        summary.fetched++;
      } catch (e) {
        errors.push(`${meta.id}: ${e.message}`);
      }
      if (batch.length >= BATCH || i === todo.length - 1) {
        if (batch.length) await server('POST', '/conversations', {items: batch});
        batch = [];
      }
    }
    await syncDocuments(token, summary, errors);
    await stage('finishing');
    const slim = listing.map(({id, is_archived, is_starred}) => ({id, is_archived, is_starred}));
    await server('POST', '/sync-done', {listing: slim, summary, errors,
                                        extension: {version: VERSION, code: loadedCode}});
    if (errors.length) error = `${errors.length} conversation(s) or document(s) failed; first: ${errors[0]}`;
    await set({lastSuccess: Date.now()});
  } catch (e) {
    error = String(e && e.message || e);
    errorKind = e && e.kind || null;
  }
  await set({lastFinished: Date.now(), lastSummary: summary, lastError: error,
             lastErrorKind: errorKind, run: null});
  await setBadge(error);
}

// ChatGPT pages live outside the chats that create or edit them and can
// change after a chat ends, so every page a stored chat refers to is checked
// on every run. The listener names the pages (including any that the chats
// just stored refer to) and the metadata fields that show a change; a page
// whose fields differ, or that has no stored version, is fetched in full.
async function syncDocuments(token, summary, errors) {
  await stage('checking documents');
  const {documents} = await server('GET', '/documents');
  const ids = Object.keys(documents);
  let pages = [];
  const missing = [];
  for (const [i, id] of ids.entries()) {
    await stage(`checking documents ${i + 1}/${ids.length}`);
    await sleep(FETCH_GAP_MS);
    try {
      const known = documents[id].version;
      if (known) {
        const {metadata} = await api(`/backend-api/pages/${id}?include_content=false`, token);
        if (Object.entries(known).every(([k, v]) => metadata[k] === v)) continue;
        await sleep(FETCH_GAP_MS);
      }
      pages.push(await api(`/backend-api/pages/${id}?include_content=true`, token));
      summary.documents++;
    } catch (e) {
      // A deleted page answers 404, also while it is in ChatGPT's trash.
      // The listener keeps the local copy.
      if (e.status === 404) missing.push(id);
      else errors.push(`${id}: ${e.message}`);
    }
    if (pages.length >= BATCH) {
      await server('POST', '/documents', {pages, missing: []});
      pages = [];
    }
  }
  if (pages.length || missing.length) await server('POST', '/documents', {pages, missing});
}

let running = null;
// Chats a "Sync now" asked for while a run was already going; a follow-up run
// fetches them so the click isn't lost.
let queuedForce = null;

async function maybeSync(minIntervalMs, force = []) {
  if (running) {
    if (force.length) queuedForce = [...(queuedForce || []), ...force];
    return running;
  }
  const run = await get('run');
  if (run && Date.now() - run.heartbeat < HEARTBEAT_STALE_MS) return;  // another worker instance
  if (run) {
    // The worker was stopped mid-run without reaching the end of runSync.
    const error = `previous run died at stage: ${run.stage}`;
    await set({lastError: error, lastErrorKind: null, run: null});
    await setBadge(error);
  }
  const lastFinished = (await get('lastFinished')) || 0;
  if (Date.now() - lastFinished < minIntervalMs) return;
  running = runSync(force).finally(() => {
    running = null;
    if (queuedForce) {
      const next = queuedForce;
      queuedForce = null;
      maybeSync(0, next);
    }
  });
  return running;
}

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === 'tick') maybeSync(ALARM_INTERVAL_MS);
});

// The toolbar icon opens popup.html, whose "Sync now" button sends sync-now.
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'page-loaded') maybeSync(PAGE_LOAD_INTERVAL_MS);
  if (msg.type === 'sync-now') maybeSync(0, msg.force || []);
});
