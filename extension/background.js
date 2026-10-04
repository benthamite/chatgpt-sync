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

function ensureAlarm() {
  chrome.alarms.create('tick', {periodInMinutes: 15});
}
chrome.runtime.onInstalled.addListener(ensureAlarm);
chrome.runtime.onStartup.addListener(ensureAlarm);

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
    title: `ChatGPT Sync\nLast success: ${when}` + (error ? `\nError: ${error}` : ''),
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
    if (!r.ok) throw new Error(`${path.split('?')[0]}: HTTP ${r.status}`);
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

async function runSync() {
  const errors = [];
  const summary = {listed: 0, fetched: 0};
  let error = null;
  try {
    // Ask the listener first, so a stopped or paused listener ends the run
    // before anything is requested from ChatGPT.
    await stage('checking listener');
    const {ids} = await server('GET', '/state');
    await stage('session');
    const r = await fetch(CHATGPT + '/api/auth/session', {signal: AbortSignal.timeout(30000)});
    const session = await r.json().catch(() => ({}));
    if (!session.accessToken) throw new Error(`no ChatGPT session (HTTP ${r.status}); log in at chatgpt.com`);
    const token = session.accessToken;
    await stage('listing');
    const listing = await listConversations(token);
    summary.listed = listing.length;
    await stage(`listed ${listing.length}`);
    const todo = listing.filter((m) => Date.parse(m.update_time) / 1000 > (ids[m.id] || 0) + 0.001);
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
    await stage('finishing');
    const slim = listing.map(({id, is_archived, is_starred}) => ({id, is_archived, is_starred}));
    await server('POST', '/sync-done', {listing: slim, summary, errors});
    if (errors.length) error = `${errors.length} conversation(s) failed; first: ${errors[0]}`;
    await set({lastSuccess: Date.now()});
  } catch (e) {
    error = String(e && e.message || e);
  }
  await set({lastFinished: Date.now(), lastSummary: summary, lastError: error, run: null});
  await setBadge(error);
}

let running = null;

async function maybeSync(minIntervalMs) {
  if (running) return running;
  const run = await get('run');
  if (run && Date.now() - run.heartbeat < HEARTBEAT_STALE_MS) return;  // another worker instance
  if (run) {
    // The worker was stopped mid-run without reaching the end of runSync.
    const error = `previous run died at stage: ${run.stage}`;
    await set({lastError: error, run: null});
    await setBadge(error);
  }
  const lastFinished = (await get('lastFinished')) || 0;
  if (Date.now() - lastFinished < minIntervalMs) return;
  running = runSync().finally(() => { running = null; });
  return running;
}

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === 'tick') maybeSync(ALARM_INTERVAL_MS);
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'page-loaded') maybeSync(PAGE_LOAD_INTERVAL_MS);
});

chrome.action.onClicked.addListener(() => maybeSync(0));
