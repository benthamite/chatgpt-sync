// Nudges the background worker to sync when a ChatGPT page loads. The sync
// itself runs in the worker, so it isn't affected by tabs being hidden,
// frozen, navigated or closed.
chrome.runtime.sendMessage({type: 'page-loaded'});
