const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

let heartbeat = 0;
let consumed = 0;
const stages = [];
const noopEvent = { addListener() {} };
const context = vm.createContext({
  console,
  chrome: {
    runtime: { getManifest: () => ({version: '0.7.0'}), onStartup: noopEvent,
      onInstalled: noopEvent, onMessage: noopEvent },
    alarms: { onAlarm: noopEvent },
    storage: { local: { set: async value => {
      if (value.run) { heartbeat++; stages.push(value.run.stage); }
    } } },
  },
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../extension/background.js'), 'utf8'), context);
const page = (prefix, count) => Array.from({length: count}, (_, i) => ({id: `${prefix}-${i}`}));
context.mockApi = async url => {
  assert.ok(heartbeat > consumed, 'discovery must touch an extension API before each page request');
  consumed = heartbeat;
  if (url.includes('/conversations?offset=0') && url.includes('is_archived=false')) return {items: page('active', 100)};
  if (url.includes('/conversations?offset=100')) return {items: page('older', 1)};
  if (url.includes('is_archived=true')) return {items: []};
  if (url.includes('/snorlax/sidebar')) return {items: [{gizmo: {gizmo: {id: 'project'}}}], cursor: null};
  if (url.includes('/gizmos/project/conversations?cursor=0')) return {items: page('project', 100), cursor: 'next'};
  if (url.includes('/gizmos/project/conversations?cursor=next')) return {items: page('project-older', 1), cursor: null};
  throw new Error('Unexpected fixture request: ' + url);
};
vm.runInContext('api = mockApi', context);
(async () => {
  const listing = await vm.runInContext("listConversations('test-token')", context);
  assert.equal(listing.length, 202);
  assert.equal(consumed, 6);
  assert.ok(stages.some(s => s.includes('archived')));
  assert.ok(stages.some(s => s.includes('project chats')));
  console.log('Discovery retains its heartbeat across active, archived and project pagination.');
})().catch(error => { console.error(error); process.exitCode = 1; });
