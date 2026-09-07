// Run the pinned package's real formatters with fake project methods only.
const assert = require('node:assert/strict');
const { test } = require('node:test');

test('clasp 3.4.1 emits the deployment JSON schema consumed by staging checks', async () => {
  const { command } = await import('../tooling/clasp/node_modules/@google/clasp/build/src/commands/list-deployments.js');
  let actualScript;
  command.setOptionValue('json', true);
  command.setOptionValue('clasp', {
    withScriptId: id => { actualScript = id; },
    project: { listDeployments: async () => ({ results: [
      { deploymentId: 'fixture', deploymentConfig: { versionNumber: 12, description: 'fixture' } },
    ] }) },
  });
  const output = [];
  const original = console.log;
  console.log = value => output.push(value);
  try { await command.parseAsync(['staging-fixture'], { from: 'user' }); }
  finally { console.log = original; }
  assert.equal(actualScript, 'staging-fixture');
  assert.deepEqual(JSON.parse(output.join('')), [
    { deploymentId: 'fixture', versionNumber: 12, description: 'fixture' },
  ]);
});

test('clasp 3.4.1 emits the create-version JSON schema consumed by staging checks', async () => {
  const { command } = await import('../tooling/clasp/node_modules/@google/clasp/build/src/commands/create-version.js');
  command.setOptionValue('json', true);
  command.setOptionValue('clasp', {
    project: { version: async description => {
      assert.equal(description, 'offline fixture');
      return 12;
    } },
  });
  const output = [];
  const original = console.log;
  console.log = value => output.push(value);
  try { await command.parseAsync(['offline fixture'], { from: 'user' }); }
  finally { console.log = original; }
  assert.deepEqual(JSON.parse(output.join('')), { versionNumber: 12 });
});
