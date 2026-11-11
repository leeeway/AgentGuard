'use strict';
// Cooperative path checks, deliberately limited to documented fs entry points.
const fs = require('node:fs');
const path = require('node:path');
const { fileURLToPath } = require('node:url');
const os = require('node:os');

function install(policy, record) {
  const realpath = fs.realpathSync.native.bind(fs.realpathSync);
  const root = realpath(policy.root);
  const escape = text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const rules = policy.rules.map(rule => ({absolute: rule.absolute,
    regex: new RegExp('^(?:' + (rule.home ? escape(os.homedir() + '/') : '') + rule.body + ')(?![\\s\\S])', 'su')}));

  function canonical(candidate) {
    try { return realpath(candidate); }
    catch (error) {
      if (error.code !== 'ENOENT') throw error;
      const parent = path.dirname(candidate);
      if (parent === candidate) throw error;
      return path.join(canonical(parent), path.basename(candidate));
    }
  }

  function check(input, event) {
    // Descriptor and FileHandle operations are outside this path policy.
    if (input instanceof URL) input = fileURLToPath(input);
    if (Buffer.isBuffer(input)) {
      const text = input.toString('utf8');
      if (!Buffer.from(text).equals(input)) throw new TypeError('AgentGuard: file path must be UTF-8');
      input = text;
    }
    if (typeof input !== 'string') return;
    if (input.includes('\0')) throw new TypeError('AgentGuard: invalid file path');
    const raw = path.isAbsolute(input) ? input : process.cwd() + '/' + input;
    const names = {absolute: new Set(), relative: new Set()};
    // Resolve the raw path before lexical normalization, preserving symlink/..
    // semantics. For new files, resolve the nearest existing ancestor.
    for (let candidate of [path.resolve(raw), canonical(raw)]) {
      for (;;) {
        names.absolute.add(candidate);
        const relative = path.relative(root, candidate);
        if (relative !== '..' && !relative.startsWith('../') && !path.isAbsolute(relative)) {
          names.relative.add(relative || '.');
        }
        const parent = path.dirname(candidate);
        if (parent === candidate) break;
        candidate = parent;
      }
    }
    const matched = rules.findIndex(rule => [...(rule.absolute ? names.absolute : names.relative)].some(name => rule.regex.test(name)));
    if (matched !== -1) {
      record(event, 'blocked', [matched + 1]);
      const error = new Error(`AgentGuard: file access blocked by privacy rule ${matched + 1}`);
      error.code = 'AGENTGUARD_PROTECTED_FILE';
      throw error;
    }
  }

  for (const name of ['openSync', 'readFileSync', 'writeFileSync', 'appendFileSync', 'createReadStream', 'createWriteStream']) {
    const original = fs[name];
    fs[name] = function (...args) {
      check(args[0], `fs.${name}`);
      return Reflect.apply(original, this, args);
    };
  }
  for (const name of ['open', 'readFile', 'writeFile', 'appendFile']) {
    const original = fs[name];
    fs[name] = function (...args) {
      try { check(args[0], `fs.${name}`); }
      catch (error) {
        const callback = args[args.length - 1];
        if (typeof callback !== 'function') throw error;
        queueMicrotask(() => callback(error));
        return;
      }
      return Reflect.apply(original, this, args);
    };
    const promiseOriginal = fs.promises[name];
    fs.promises[name] = async function (...args) {
      check(args[0], `fs.promises.${name}`);
      return Reflect.apply(promiseOriginal, this, args);
    };
  }
}
module.exports = { install };
