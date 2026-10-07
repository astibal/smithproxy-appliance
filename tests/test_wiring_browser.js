// Run with node; exercise the submit handler with named-form-property shadowing.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

async function testSave(action) {
  const handlers = {};
  const button = {disabled: false};
  const output = {};
  const form = {
    action: {name: 'action', value: 'addressing'},
    getAttribute: name => name === 'action' ? action : null,
    querySelector: selector => selector === 'button' ? button : output,
    elements: {addresses: {addEventListener() {}}},
    closest: () => ({addEventListener() {}}),
    addEventListener: (event, handler) => { handlers[event] = handler; },
  };
  const requests = [];
  vm.runInNewContext(fs.readFileSync('console/static/wiring.js', 'utf8'), {
    document: {
      querySelector: () => ({addEventListener() {}}),
      querySelectorAll: () => [form], addEventListener() {},
    },
    window: {}, location: {href: '/network/wiring'},
    FormData: class { constructor(value) { assert.equal(value, form); } },
    fetch: async (url, options) => {
      requests.push({url, options});
      return {ok: true, json: async () => ({task_id: 'test-task'})};
    },
  });
  await handlers.submit({preventDefault() {}});
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/network/wiring');
  assert.equal(requests[0].options.method, 'POST');
  assert.equal(button.disabled, false);
  assert.match(output.textContent, /test-task/);
}

(async () => {
  await testSave('/network/wiring');
  await testSave(null);
  console.log('Wiring submit regression tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
