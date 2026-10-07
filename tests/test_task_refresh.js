const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('console/static/app.js', 'utf8');
const start = source.indexOf('    const observeTasks = tasks => {');
const end = source.indexOf("    document.addEventListener('visibilitychange', refreshView);", start);
const ctx = {previous: null, snapshotKey: 'test', refreshPending: false,
  refreshTimer: null, scheduled: 0, refreshView() {}, clearTimeout() {},
  setTimeout() { ctx.scheduled++; }, sessionStorage: {setItem() {}}};
vm.createContext(ctx);
vm.runInContext(source.slice(start, end) + '\nglobalThis.observe = observeTasks;', ctx);
ctx.observe([{task_id: 'old', state: 'succeeded'}]);
assert.equal(ctx.scheduled, 0, 'historical tasks must not reload');
ctx.observe([{task_id: 'a', state: 'running'}]);
ctx.observe([{task_id: 'a', state: 'succeeded'}]);
assert.equal(ctx.scheduled, 1);
ctx.observe([{task_id: 'a', state: 'succeeded'}]);
assert.equal(ctx.scheduled, 1, 'no reload loop');
ctx.observe([{task_id: 'b', state: 'failed'}]);
assert.equal(ctx.scheduled, 2, 'fast tasks and failures refresh too');
console.log('Task completion refresh regression tests passed');
