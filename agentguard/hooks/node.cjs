'use strict';
// Cooperative guards: application code can replace these wrappers.
const cp = require('node:child_process');
const { syncBuiltinESMExports } = require('node:module');
const policy = JSON.parse(process.env.AGENTGUARD_POLICY || '{}');
const mode = policy.dangerous_commands || 'block';
if (!['block', 'ask', 'warn'].includes(mode)) {
  throw new Error('AgentGuard: invalid runtime mode');
}
// Invalid JavaScript patterns abort startup rather than silently disabling checks.
const patterns = (policy.blocked_patterns || []).map(pattern => new RegExp(pattern));
for (const name of ['spawn', 'spawnSync', 'exec', 'execSync', 'execFile', 'execFileSync', 'fork']) {
  const original = cp[name];
  const guarded = function (...args) {
    const command = [args[0], ...(Array.isArray(args[1]) ? args[1] : [])].join(' ');
    const rule = patterns.findIndex(pattern => pattern.test(command));
    if (rule !== -1) {
      const message = `AgentGuard: ${mode === 'warn' ? 'warning' : 'blocked'} child_process.${name} by rule ${rule + 1}`;
      process.stderr.write(message + '\n');
      if (mode !== 'warn') {
        const error = new Error(message);
        error.code = 'AGENTGUARD_BLOCKED';
        throw error;
      }
    }
    return Reflect.apply(original, this, args);
  };
  // Preserve Node's custom promisify behavior without bypassing the guard.
  const { promisify } = require('node:util');
  if (original[promisify.custom]) {
    guarded[promisify.custom] = (...args) => {
      let child;
      const promise = new Promise((resolve, reject) => {
        child = guarded(...args, (error, stdout, stderr) => {
          if (error) {
            error.stdout = stdout;
            error.stderr = stderr;
            reject(error);
          } else {
            resolve({ stdout, stderr });
          }
        });
      });
      promise.child = child;
      return promise;
    };
  }
  cp[name] = guarded;
}
syncBuiltinESMExports();
