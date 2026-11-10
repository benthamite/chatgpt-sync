// Shows the state the background worker keeps in chrome.storage.local and
// lets the user start a sync. Re-renders live as the worker updates it.

const $ = (id) => document.getElementById(id);
const fmt = (t) => t ? new Date(t).toLocaleString() : 'never';

async function render() {
  const {run, lastError, lastErrorKind, lastSuccess, lastFinished, lastSummary} =
    await chrome.storage.local.get(
      ['run', 'lastError', 'lastErrorKind', 'lastSuccess', 'lastFinished', 'lastSummary']);
  if (run) {
    $('state').textContent = `Syncing: ${run.stage}`;
  } else if (lastError) {
    $('state').textContent = `Last sync failed (${fmt(lastFinished)}).`;
  } else {
    const s = lastSummary || {};
    $('state').textContent = lastFinished
      ? `Last sync OK (${fmt(lastFinished)}): ${s.fetched || 0} fetched of ${s.listed || 0}.`
      : 'No sync yet.';
  }
  $('error').hidden = !lastError;
  $('error').textContent = lastError || '';
  $('login').hidden = !lastError || lastErrorKind !== 'login';
  $('last').textContent = `Last success: ${fmt(lastSuccess)}`;
  // Stays enabled during a run: a click then queues the open chat for a
  // follow-up run.
}

// Also refetch the chat open in the current tab, whose listed update_time
// may not yet show its latest messages.
$('sync').addEventListener('click', async () => {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  const m = tab && tab.url && tab.url.match(/^https:\/\/chatgpt\.com\/(?:g\/[^/]+\/)?c\/([0-9a-f-]{36})/);
  chrome.runtime.sendMessage({type: 'sync-now', force: m ? [m[1]] : []});
});
chrome.storage.onChanged.addListener(render);
render();
