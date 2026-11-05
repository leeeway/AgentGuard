from pathlib import Path
import sys
import unittest

from click.testing import CliRunner
from agentguard.cli import main


class RuntimeHooks(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.runner.invoke(main, ['init'])
        # Harmless test command stands in for a forbidden operation.
        Path('.agentguard/config.toml').write_text(
            '[safety]\ndangerous_commands = "block"\nblocked_patterns = ["AG_TEST_FORBIDDEN"]\n'
        )

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def execute(self, source, hooks=True):
        Path('worker.py').write_text(source)
        return self.runner.invoke(main, ['run'] + (['--python-hooks'] if hooks else []) + ['--', sys.executable, 'worker.py'])

    def test_subprocess_blocked_and_safe_command_allowed(self):
        result = self.execute('''import subprocess, sys
subprocess.run([sys.executable, '-c', 'pass'], check=True)
try:
    subprocess.run([sys.executable, '-c', "from pathlib import Path; Path('AG_TEST_FORBIDDEN').touch()"], check=True)
except PermissionError:
    pass
else:
    raise AssertionError('not blocked')
''')
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(Path('AG_TEST_FORBIDDEN').exists())

    def test_os_system_blocked(self):
        result = self.execute("import os\ntry:\n os.system('echo AG_TEST_FORBIDDEN > marker')\nexcept PermissionError:\n pass\nelse:\n raise AssertionError('not blocked')\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(Path('marker').exists())

    def test_warn_allows_and_default_is_opt_in(self):
        source = "import subprocess, sys; subprocess.run([sys.executable, '-c', \"from pathlib import Path; Path('AG_TEST_FORBIDDEN').touch()\"], check=True)"
        result = self.execute(source, hooks=False)
        self.assertEqual(result.exit_code, 0, result.output)
        Path('AG_TEST_FORBIDDEN').unlink()
        config = Path('.agentguard/config.toml')
        config.write_text(config.read_text().replace('"block"', '"warn"'))
        result = self.execute(source)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(Path('AG_TEST_FORBIDDEN').exists())

    def test_inherited_by_python_descendant(self):
        Path('child.py').write_text("import os\ntry:\n os.system('echo AG_TEST_FORBIDDEN > marker')\nexcept PermissionError:\n pass\nelse:\n raise AssertionError('not blocked')\n")
        result = self.execute("import subprocess, sys; subprocess.run([sys.executable, 'child.py'], check=True)")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(Path('marker').exists())

    def test_ask_fails_closed_in_child(self):
        config = Path('.agentguard/config.toml')
        config.write_text(config.read_text().replace('"block"', '"ask"'))
        result = self.execute("import os\ntry:\n os.system('echo AG_TEST_FORBIDDEN > marker')\nexcept PermissionError:\n pass\nelse:\n raise AssertionError('not blocked')\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(Path('marker').exists())
