from fnmatch import fnmatchcase
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import unittest
from click.testing import CliRunner
from agentguard.cli import main
from agentguard import core
from agentguard.privacy import node_rules, FilePolicy


@unittest.skipUnless(shutil.which('node'), 'Node.js is not installed')
class NodeProtectedFiles(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.runner.invoke(main, ['init'])
        Path('.env').write_text('PRIVATE_TEST_SECRET')
        Path('public').write_text('public')

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def execute(self, code, suffix='cjs', flags=(), protected=True, env=None):
        Path('worker.' + suffix).write_text(code)
        return self.runner.invoke(main, ['run', '--node-hooks'] + (['--protect-files'] if protected else []) + list(flags) + ['--', shutil.which('node'), 'worker.' + suffix], env=env)

    def configure(self, patterns):
        Path('.agentguard/config.toml').write_text('[privacy]\nprotected_files = ' + json.dumps(patterns))

    def test_sync_operations_and_path_types(self):
        result = self.execute("""const fs = require('node:fs');
const {pathToFileURL} = require('node:url'); const path = require('node:path');
for (const file of ['.env', Buffer.from('.env'), pathToFileURL(path.resolve('.env'))]) {
  for (const [method, args] of [['openSync', [file, 'r']], ['readFileSync', [file]],
       ['writeFileSync', [file, 'bad']], ['appendFileSync', [file, 'bad']],
       ['createReadStream', [file]], ['createWriteStream', [file]]]) {
    try { fs[method](...args); throw Error('not blocked'); }
    catch(e) { if (e.code !== 'AGENTGUARD_PROTECTED_FILE' || e.message.includes('.env')) throw e; }
  }
}
if (fs.readFileSync('public', 'utf8') !== 'public') throw Error('public broken');
fs.writeFileSync('output', 'allowed');
""")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(Path('.env').read_text(), 'PRIVATE_TEST_SECRET')
        logs = self.runner.invoke(main, ['audit', '--json'])
        events = [e for e in json.loads(logs.output) if e['event'].startswith('fs.')]
        self.assertEqual(len(events), 18)
        self.assertTrue(all(e['decision'] == 'blocked' for e in events))
        self.assertNotIn('PRIVATE_TEST_SECRET', logs.output)
        self.assertNotIn('.env', logs.output)

    def test_callbacks_promisify_and_promises(self):
        result = self.execute("""const fs = require('node:fs'); const {promisify} = require('node:util');
(async () => {
  for (const [method,args] of [['open',['.env','r']],['readFile',['.env']],
          ['writeFile',['.env','bad']],['appendFile',['.env','bad']]]) {
    let returned = false;
    const promise = new Promise((resolve,reject) => {
      fs[method](...args, e => {
        if (!returned || e.code !== 'AGENTGUARD_PROTECTED_FILE') reject(Error('bad callback'));
        else resolve();
      }); returned = true;
    });
    await promise;
    try { await fs.promises[method](...args); throw Error('promise allowed'); }
    catch(e) { if (e.code !== 'AGENTGUARD_PROTECTED_FILE') throw e; }
    try { await promisify(fs[method])(...args); throw Error('promisify allowed'); }
    catch(e) { if (e.code !== 'AGENTGUARD_PROTECTED_FILE') throw e; }
  }
  if ((await fs.promises.readFile('public','utf8')) !== 'public') throw Error('public broken');
})().catch(e => { console.error(e); process.exitCode=1; });
""")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(Path('.env').read_text(), 'PRIVATE_TEST_SECRET')

    def test_esm_bindings(self):
        result = self.execute("""import {readFileSync} from 'node:fs';
import {readFile} from 'node:fs/promises';
try { readFileSync('.env'); throw Error('sync allowed'); }
catch(e) { if(e.code !== 'AGENTGUARD_PROTECTED_FILE') throw e; }
try { await readFile('.env'); throw Error('promise allowed'); }
catch(e) { if(e.code !== 'AGENTGUARD_PROTECTED_FILE') throw e; }
""", suffix='mjs')
        self.assertEqual(result.exit_code, 0, result.output)

    def test_symlink_parent_and_new_file_under_protected_directory(self):
        Path('vault/nested').mkdir(parents=True)
        Path('vault/token').write_text('secret')
        Path('alias').symlink_to('vault/nested', target_is_directory=True)
        self.configure(['vault'])
        # The unnormalized path reaches vault/token, not workspace/token.
        self.assertEqual(FilePolicy(Path.cwd(), ['vault']).match('alias/../token'), 1)
        result = self.execute("""const fs=require('node:fs');
if (fs.readFileSync('.env', 'utf8') !== 'PRIVATE_TEST_SECRET') throw Error('policy was not replaced');
for (const file of ['alias/../token', 'alias/new']) {
  try { fs.writeFileSync(file,'bad'); throw Error('alias allowed'); }
  catch(e) { if(e.code !== 'AGENTGUARD_PROTECTED_FILE') throw e; }
}
process.chdir('vault');
try { fs.readFileSync('token'); throw Error('chdir allowed'); }
catch(e) { if(e.code !== 'AGENTGUARD_PROTECTED_FILE') throw e; }
""")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(Path('vault/token').read_text(), 'secret')
        self.assertFalse(Path('vault/nested/new').exists())

    def test_home_rule_escapes_regex_characters(self):
        home = Path('home+[test]').resolve()
        (home / '.ssh').mkdir(parents=True)
        (home / '.ssh/key').write_text('test key')
        result = self.execute("""const fs=require('node:fs'),os=require('node:os');
try { fs.readFileSync(os.homedir()+'/.ssh/key'); throw Error('key allowed'); }
catch(e) { if(e.code !== 'AGENTGUARD_PROTECTED_FILE') throw e; }
""", env={'HOME': str(home)})
        self.assertEqual(result.exit_code, 0, result.output)

    def test_cross_runtime_child_and_verification_inherit_policy(self):
        Path('child.cjs').write_text("""try { require('node:fs').readFileSync('.env'); throw Error('allowed'); }
catch(e) { if(e.code !== 'AGENTGUARD_PROTECTED_FILE') throw e; }
""")
        Path('child.py').write_text("""from pathlib import Path
try: Path('.env').read_text()
except PermissionError: pass
else: raise AssertionError('allowed')
""")
        code = "const cp=require('node:child_process'); cp.execFileSync(process.execPath,['child.cjs']); cp.execFileSync(" + json.dumps(sys.executable) + ",['child.py']);"
        verify = shlex.join([shutil.which('node'), 'child.cjs'])
        result = self.execute(code, flags=['--python-hooks', '--verify', verify])
        self.assertEqual(result.exit_code, 0, result.output)
        events = json.loads(self.runner.invoke(main, ['audit', '--json']).output)
        self.assertTrue(any(e['event'] == 'file.open' for e in events))
        self.assertEqual(sum(e['event'] == 'fs.readFileSync' for e in events), 2)

    def test_opt_in_and_rollback_on_denial(self):
        result = self.execute("require('node:fs').readFileSync('.env');", protected=False)
        self.assertEqual(result.exit_code, 0)
        result = self.execute("const fs=require('node:fs'); fs.writeFileSync('created','x'); fs.readFileSync('.env');", flags=['--rollback-on-failure'])
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(Path('created').exists())
        self.assertTrue(core.records(Path.cwd())[-1]['undone'])

    def test_audit_can_write_when_its_directory_is_protected(self):
        self.configure(['.env', '.agentguard'])
        result = self.execute("""try { require('node:fs').readFileSync('.env'); throw Error('allowed'); }
catch(e) { if(e.code !== 'AGENTGUARD_PROTECTED_FILE') throw e; }
""")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('fs.readFileSync', self.runner.invoke(main, ['audit']).output)

    def test_glob_translation_agrees_with_python(self):
        patterns = ['.env*', '**/.env*', '*.key', 'a?b', '[!a-z]*', '[[]name[]]', 'a*b*c', 'literal.+$', '[z-a]', 'line?end', '*', '[]a]', '[!]]', '[-a]', '[&~|]', 'a b', '?.key', '*.😀', '*-*']
        names = ['.env', 'dir/.env.local', 'nested/a.key', 'a/b', 'aab', 'a1b2c', '[name]', 'literal.+$', 'abc', 'line\nend', 'line\nend\n', '', ']a', ']', '-', '&', '~', '|', 'a b', '😀.key', 'file.😀', 'a-b']
        payload = {'rules': node_rules(patterns), 'names': names}
        script = "const p=JSON.parse(process.argv[1]); console.log(JSON.stringify(p.rules.map(r=>{const re=new RegExp('^(?:'+r.body+')(?![\\\\s\\\\S])','su'); return p.names.map(n=>re.test(n));})));"
        result = subprocess.run([shutil.which('node'), '-e', script, json.dumps(payload)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [[fnmatchcase(n, p) for n in names] for p in patterns])
