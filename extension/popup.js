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
  $('sync').disabled = Boolean(run);
}

$('sync').addEventListener('click', () => {
  chrome.runtime.sendMessage({type: 'sync-now'});
});
chrome.storage.onChanged.addListener(render);
render();
