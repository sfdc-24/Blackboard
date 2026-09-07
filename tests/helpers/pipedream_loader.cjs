const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');

module.exports = function load(name, env = {}, fetch = () => { throw new Error('unexpected network access'); }) {
  const source = fs.readFileSync(path.join(__dirname, '../../scripts', name), 'utf8')
    .replace(/^import crypto from "crypto";\r?\n/m, '')
    .replace('export default defineComponent(', 'globalThis.component = defineComponent(');
  const context = vm.createContext({ crypto, process: { env }, fetch, AbortSignal,
    Buffer, defineComponent: value => value });
  vm.runInContext(source, context, { filename: name });
  return context;
};
