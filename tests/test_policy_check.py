import json
from pathlib import Path
import unittest
from unittest.mock import patch

from click.testing import CliRunner
from agentguard.cli import main


class PolicyCheck(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.context = self.runner.isolated_filesystem()
        self.context.__enter__()
        self.runner.invoke(main, ['init'])

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def policy(self, mode='block'):
        Path('.agentguard/config.toml').write_text(
            f'[safety]\ndangerous_commands = "{mode}"\nblocked_patterns = ["FORBIDDEN", "TOKEN"]\n'
        )

    def test_blocked_check_has_no_execution_or_snapshot(self):
        with patch('agentguard.cli.subprocess.run') as execute:
            result = self.runner.invoke(main, ['check', '--json', '--', 'rm', '-rf', 'keep'])
        execute.assert_not_called()
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(json.loads(result.output)['decision'], 'blocked')
        self.assertEqual(sorted(p.name for p in Path('.agentguard').iterdir()), ['config.toml'])

    def test_modes_and_multiple_matches_hide_command_arguments(self):
        for mode, decision, code in [('block', 'blocked', 3), ('ask', 'confirmation-required', 4), ('warn', 'warning', 0)]:
            with self.subTest(mode=mode):
                self.policy(mode)
                result = self.runner.invoke(main, ['check', '--json', '--', 'echo', 'FORBIDDEN_TOKEN_secret'])
                self.assertEqual(result.exit_code, code)
                self.assertEqual(json.loads(result.output)['decision'], decision)
                self.assertEqual(json.loads(result.output)['matched_rules'], [1, 2])
                self.assertNotIn('secret', result.output)

    def test_allowed_and_empty_policy_are_explicit(self):
        self.policy()
        result = self.runner.invoke(main, ['check', '--json', '--', 'python', '--version'])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(json.loads(result.output)['decision'], 'allowed')
        Path('.agentguard/config.toml').write_text('')
        result = self.runner.invoke(main, ['check', '--json', '--', 'rm', '-rf', 'anything'])
        self.assertEqual(json.loads(result.output)['rule_count'], 0)

    def test_invalid_config_prevents_launch_with_clear_error(self):
        cases = [('safety = []', 'TOML table'), ('snapshots = 3', 'TOML table'),
                 ('[safety]\nblocked_patterns = "bad"', 'array of strings'),
                 ('[safety]\nblocked_patterns = [1]', 'array of strings'),
                 ('[safety]\nblocked_patterns = ["["]', 'Invalid safety rule 1'),
                 ('[snapshots]\nmax_history = true', 'positive integer')]
        for config, message in cases:
            with self.subTest(config=config):
                Path('.agentguard/config.toml').write_text(config)
                with patch('agentguard.cli.subprocess.run') as execute:
                    result = self.runner.invoke(main, ['run', '--', 'echo', 'test'])
                execute.assert_not_called()
                self.assertEqual(result.exit_code, 1)
                self.assertIn(message, result.output)

    def test_run_and_check_report_same_rules(self):
        self.policy()
        checked = self.runner.invoke(main, ['check', '--json', '--', 'echo', 'FORBIDDEN'])
        launched = self.runner.invoke(main, ['run', '--', 'echo', 'FORBIDDEN'])
        self.assertEqual(json.loads(checked.output)['matched_rules'], [1])
        self.assertIn('blocked by safety rules: 1', launched.output)
