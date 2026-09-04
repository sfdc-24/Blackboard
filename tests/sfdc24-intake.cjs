const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const file = require('node:path').join(__dirname, '../web/sfdc24-lead-capture.html');
const html = fs.readFileSync(file, 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
assert.ok(html.startsWith('<!doctype html>'));
assert.ok(!html.includes('novalidate'));
assert.ok(!html.includes('fakeLeadId'));
assert.ok(html.includes('type="email" maxlength="80"'));
assert.ok(html.includes('id="submitBtn" disabled'));
const defaults = {};
for (const [, tag] of html.matchAll(/<(input\b[^>]+)>/g)) {
  const name = tag.match(/\bname="([^"]+)"/)?.[1];
  const value = tag.match(/\bvalue="([^"]*)"/)?.[1] || '';
  if (name && name !== 'rel') defaults[name] = value;
}
assert.equal(defaults.oid, '00Dbm00000wK2ibEAC');
assert.equal(defaults.lead_source, 'sfdc24.com');
assert.equal(new URL(defaults.retURL).pathname, '/');
const fields = Object.fromEntries(Object.entries(defaults).map(([k,v])=>[k,{value:v}]));
Object.assign(fields, {first_name:{value:' SFDC24 '},last_name:{value:' Test '},email:{value:' verify@example.invalid '},company:{value:' Synthetic & Co '}});
let valid = true;
const handlers = {};
const elements = {
  leadForm: {elements:{namedItem:n=>fields[n]},querySelector:()=>({value:'Supplier'}),addEventListener:(n,f)=>handlers[n]=f,reportValidity:()=>valid},
  description: {value:'<script>not executable</script> & café'},
  salesforceDescription:fields.description,
  payloadView:{textContent:''},
  submitBtn:{disabled:true,querySelector:()=>({textContent:''})},
  doneState:{classList:{add(){},remove(){}}},
  againBtn:{addEventListener(){}},
};
const window = {location:{search:''},addEventListener(){}};
vm.runInNewContext(script,{document:{getElementById:n=>elements[n]},window,URL,URLSearchParams});
assert.equal(elements.submitBtn.disabled,false);
let prevented = false;
valid = false;
handlers.submit({preventDefault(){prevented=true;}});
assert.equal(prevented,true,'invalid submissions must be blocked');
assert.equal(elements.submitBtn.disabled,false);
valid = true;
prevented = false;
handlers.submit({preventDefault(){prevented=true;}});
assert.equal(prevented,false,'valid submissions must use native POST');
assert.equal(fields.email.value,'verify@example.invalid');
assert.equal(fields.description.value,'[SFDC24 intake] Relationship: Supplier\n\n<script>not executable</script> & café');
const payload = new URLSearchParams(elements.payloadView.textContent);
assert.equal(payload.get('description'),fields.description.value);
assert.equal(payload.get('company'),'Synthetic & Co');
assert.equal(payload.get('oid'),defaults.oid);
const posted = new Map([['rel','Supplier'],['description',fields.description.value]]);
handlers.formdata({formData:posted});
assert.equal(posted.has('rel'),false);
assert.ok(posted.has('description'));
assert.equal(elements.submitBtn.disabled,true);
console.log('PASS: selected org, form contract, validation guard, relationship mapping, literal text, native submission, and duplicate-click guard.');
