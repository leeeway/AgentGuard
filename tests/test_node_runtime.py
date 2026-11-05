from pathlib import Path
import shutil
import unittest

from click.testing import CliRunner
from agentguard.cli import main


@unittest.skipUnless(shutil.which('node'), 'Node.js is not installed')
class NodeHooks(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.runner.invoke(main, ['init'])
        Path('.agentguard/config.toml').write_text(
            '[safety]\ndangerous_commands = "block"\nblocked_patterns = ["AG_TEST_FORBIDDEN"]\n'
        )

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def execute(self, source, suffix='cjs', flags=None):
        path = 'worker.' + suffix
        Path(path).write_text(source)
        return self.runner.invoke(main, ['run'] + (flags if flags is not None else ['--node-hooks']) + ['--', shutil.which('node'), path])

    def test_all_entry_points_block(self):
        for method in ('spawn', 'spawnSync', 'exec', 'execSync', 'execFile', 'execFileSync', 'fork'):
            with self.subTest(method=method):
                result = self.execute(f"""const cp = require('node:child_process');
try {{ cp.{method}('AG_TEST_FORBIDDEN'); throw new Error('not blocked'); }}
catch (e) {{ if (e.code !== 'AGENTGUARD_BLOCKED') throw e; }}
""")
                self.assertEqual(result.exit_code, 0, result.output)

    def test_esm_and_promisify(self):
        result = self.execute("""import {exec, spawnSync} from 'node:child_process';
import {promisify} from 'node:util';
const result = await promisify(exec)('echo allowed');
if (result.stdout.trim() !== 'allowed') throw new Error('bad output');
try { await promisify(exec)('echo AG_TEST_FORBIDDEN'); throw new Error('not blocked'); }
catch (e) { if (e.code !== 'AGENTGUARD_BLOCKED') throw e; }
try { spawnSync('AG_TEST_FORBIDDEN'); throw new Error('not blocked'); }
catch (e) { if (e.code !== 'AGENTGUARD_BLOCKED') throw e; }
""", suffix='mjs')
        self.assertEqual(result.exit_code, 0, result.output)

    def test_inheritance(self):
        Path('child.cjs').write_text("""try { require('node:child_process').execSync('echo AG_TEST_FORBIDDEN'); process.exit(9); }
catch (e) { if (e.code !== 'AGENTGUARD_BLOCKED') throw e; }
""")
        result = self.execute("require('node:child_process').execFileSync(process.execPath, ['child.cjs'], {stdio: 'inherit'});")
        self.assertEqual(result.exit_code, 0, result.output)

    def test_warn_and_opt_in(self):
        source = "require('node:child_process').execSync('echo AG_TEST_FORBIDDEN > marker');"
        result = self.execute(source, flags=[])
        self.assertEqual(result.exit_code, 0, result.output)
        Path('marker').unlink()
        config = Path('.agentguard/config.toml')
        config.write_text(config.read_text().replace('"block"', '"warn"'))
        result = self.execute(source, flags=['--python-hooks', '--node-hooks'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(Path('marker').exists())

    def test_arguments_and_ask_blocked(self):
        config = Path('.agentguard/config.toml')
        config.write_text(config.read_text().replace('"block"', '"ask"'))
        result = self.execute("""const cp = require('node:child_process');
try { cp.execFileSync(process.execPath, ['-e', 'console.log("AG_TEST_FORBIDDEN")']); throw new Error('not blocked'); }
catch (e) { if (e.code !== 'AGENTGUARD_BLOCKED') throw e; }
""")
        self.assertEqual(result.exit_code, 0, result.output)

    def test_promisify_preserves_failure_output(self):
        result = self.execute("""const {promisify} = require('node:util');
const {execFile} = require('node:child_process');
(async () => {
  const promise = promisify(execFile)(process.execPath, ['-e', 'console.log("output"); process.exit(7)']);
  if (!promise.child) throw new Error('missing child');
  try { await promise; throw new Error('unexpected success'); }
  catch (e) { if (e.code !== 7 || e.stdout.trim() !== 'output') throw e; }
})().catch(() => process.exit(1));
""")
        self.assertEqual(result.exit_code, 0, result.output)

    def test_invalid_js_regex_fails_closed(self):
        Path('.agentguard/config.toml').write_text('[safety]\nblocked_patterns = ["(?i)forbidden"]\n')
        result = self.execute("require('node:fs').writeFileSync('marker', 'bad');")
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(Path('marker').exists())
