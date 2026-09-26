const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');
const file = require('node:path').join(__dirname, '../web/sfdc24-lead-capture.html');
const html = fs.readFileSync(file, 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function attributes(tag) {
  return Object.fromEntries([...tag.matchAll(/\s([\w-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)]
    .map(([, name, double, single, bare]) => [name, double ?? single ?? bare ?? '']));
}
const tags = [...html.matchAll(/<(?:input|button)\b[^>]*>/gi)].map(([tag]) => attributes(tag));
const defaults = Object.fromEntries(tags.filter(a => a.name && a.name !== 'rel').map(a => [a.name, a.value || '']));

function harness(href) {
  const fields = Object.fromEntries(Object.entries(defaults).map(([k,v]) => [k,{value:v}]));
  Object.assign(fields, {first_name:{value:' SFDC24 '},last_name:{value:' Test '},email:{value:' verify@example.invalid '},company:{value:' Synthetic & Co '}});
  const handlers = {}, windowHandlers = {};
  let valid = true, done = false;
  const buttonText = {textContent: ''};
  const elements = {
    leadForm: {elements:{namedItem:n=>fields[n]},querySelector:()=>({value:'Supplier'}),
      addEventListener:(n,f)=>handlers[n]=f,reportValidity:()=>valid,
      reset(){for (const [key, value] of Object.entries(defaults)) fields[key].value = value;}},
    description: {value:'<script>not executable</script> & café',focus(){}},
    salesforceDescription:fields.description,
    payloadView:{textContent:''},
    submitBtn:{disabled:true,querySelector:()=>buttonText},
    doneState:{classList:{add(){done=true;},remove(){done=false;}}},
    againBtn:{addEventListener:(n,f)=>handlers.again=f},
  };
  const window = {location:new URL(href),addEventListener:(n,f)=>windowHandlers[n]=f,
    history:{replaceState(a,b,url){window.location=new URL(url);}}};
  vm.runInNewContext(script,{document:{getElementById:n=>elements[n]},window,URL,URLSearchParams});
  return {fields,elements,handlers,windowHandlers,window,isDone:()=>done,setValid:v=>valid=v};
}

test('semantic markup keeps the selected org, email constraint and initially disabled submit', () => {
  assert.ok(html.startsWith('<!doctype html>'));
  assert.ok(!html.includes('novalidate'));
  assert.ok(!html.includes('fakeLeadId'));
  const email = tags.find(a => a.id === 'email');
  assert.equal(email.type, 'email');
  assert.equal(email.maxlength, '80');
  assert.ok(Object.hasOwn(tags.find(a => a.id === 'submitBtn'), 'disabled'));
  assert.ok(Object.hasOwn(attributes('<button disabled class="submit" id="submitBtn">'), 'disabled'));
  assert.equal(defaults.oid, '00Dbm00000wK2ibEAC');
  assert.equal(defaults.lead_source, 'sfdc24.com');
  assert.equal(defaults.retURL, '');
});

test('preview, public and nested routes return to their own host and path without private URL data', () => {
  for (const href of ['https://sfdc24-intake-omnistudio.astronautwannabe.chatgpt.site/',
    'https://www.sfdc24.com/intake/?preview=private#session',
    'http://localhost:3000/nested/form.html?submitted=1&token=private#details']) {
    const h = harness(href);
    const current = new URL(href);
    const expected = current.origin + current.pathname + '?submitted=1';
    assert.equal(h.fields.retURL.value, expected);
    assert.equal(new URLSearchParams(h.elements.payloadView.textContent).get('retURL'), expected);
    assert.equal(h.elements.submitBtn.disabled, false);
  }
});

test('submitted page resets and restores the return URL for a second request', () => {
  const h = harness('https://www.sfdc24.com/intake/?submitted=1');
  assert.equal(h.isDone(), true);
  assert.equal(h.elements.leadForm.hidden, true);
  h.handlers.again();
  assert.equal(h.isDone(), false);
  assert.equal(h.elements.leadForm.hidden, false);
  assert.equal(h.window.location.search, '');
  assert.equal(h.fields.retURL.value, 'https://www.sfdc24.com/intake/?submitted=1');
});

test('invalid input is blocked; valid input uses native POST with literal relationship text', () => {
  const h = harness('https://www.sfdc24.com/intake/');
  let prevented = false;
  h.setValid(false);
  h.handlers.submit({preventDefault(){prevented=true;}});
  assert.equal(prevented,true);
  assert.equal(h.elements.submitBtn.disabled,false);
  h.setValid(true);
  prevented=false;
  h.handlers.submit({preventDefault(){prevented=true;}});
  assert.equal(prevented,false);
  assert.equal(h.fields.email.value,'verify@example.invalid');
  assert.equal(h.fields.description.value,'[SFDC24 intake] Relationship: Supplier\n\n<script>not executable</script> & café');
  const payload = new URLSearchParams(h.elements.payloadView.textContent);
  assert.equal(payload.get('description'),h.fields.description.value);
  assert.equal(payload.get('company'),'Synthetic & Co');
  assert.equal(payload.get('oid'),defaults.oid);
  const posted = new Map([['rel','Supplier'],['description',h.fields.description.value]]);
  h.handlers.formdata({formData:posted});
  assert.equal(posted.has('rel'),false);
  assert.ok(posted.has('description'));
  assert.equal(h.elements.submitBtn.disabled,true);
  h.windowHandlers.pageshow();
  assert.equal(h.elements.submitBtn.disabled,false);
});

test('local file previews cannot submit with an unusable return URL', () => {
  const h = harness('file:///tmp/intake.html');
  assert.equal(h.elements.submitBtn.disabled,true);
  assert.equal(h.fields.retURL.value,'');
});
