import json
from pathlib import Path
import subprocess
import tempfile
import traceback
import unittest
from unittest import mock

from side_lane.adapters import claude
from side_lane.qualification import qualify_claude
from side_lane.redaction import MARKER, redact_provider_secret

SECRET = 'sk-synthetic-9aBcDeF0123456789qRsTuVwX'


class RedactionTests(unittest.TestCase):
    def test_known_fragments_and_masks(self):
        cases = [SECRET, SECRET[:13], SECRET[-12:], SECRET[13:25],
                 SECRET[:13] + '...', '...' + SECRET[-12:],
                 SECRET[:4] + '…' + SECRET[-4:], SECRET[:4] + '****',
                 '****' + SECRET[-4:]]
        for shown in cases:
            with self.subTest(shown=shown):
                output = redact_provider_secret('key "' + shown + '" rejected', SECRET)
                self.assertEqual(output, 'key "' + MARKER + '" rejected')
                self.assertEqual(redact_provider_secret(output, SECRET), output)

    def test_preserves_unrelated_diagnostics_and_json(self):
        raw = json.dumps({'model': 'glm-5.3', 'usage': {'input_tokens': 42},
                          'error': 'Invalid key: ' + SECRET[:13]})
        result = json.loads(redact_provider_secret(raw, SECRET))
        self.assertEqual(result['model'], 'glm-5.3')
        self.assertEqual(result['usage'], {'input_tokens': 42})
        self.assertEqual(result['error'], 'Invalid key: ' + MARKER)
        self.assertEqual(redact_provider_secret('timeout after 1800 seconds', SECRET),
                         'timeout after 1800 seconds')
        self.assertEqual(redact_provider_secret(None, SECRET), '')
        self.assertEqual(redact_provider_secret('unrelated', None), 'unrelated')
        self.assertEqual(redact_provider_secret(SECRET[:13].encode(), SECRET), MARKER)

    def test_every_contiguous_truncation_of_eight_or_more_characters(self):
        for start in range(len(SECRET)):
            for stop in range(start + 8, len(SECRET) + 1):
                self.assertEqual(redact_provider_secret(SECRET[start:stop], SECRET), MARKER)

    def directories(self, root):
        repo, lane = root / 'repo', root / 'lane'
        for path in (repo, lane):
            path.mkdir()
            (path / '.git').write_text('fixture')
        return repo, lane

    def launch(self, root, runner, **kwargs):
        repo, lane = self.directories(root)
        return claude.launch(executable='claude', repo=repo, worktree=lane,
            provider='glm', model='glm-5.3',
            provider_config={'gateway': 'direct-zai', 'auth_method': 'provider-key',
                             'billable': True, 'base_url': 'https://api.z.ai/api/anthropic'},
            model_config={'runtime_model': 'glm-5.3', 'protocol': 'anthropic-compatible'},
            prompt='fixture', secret=SECRET, runner=runner, **kwargs)

    def test_failed_and_timed_out_launches_redact_both_streams(self):
        for code in (7, 124):
            with tempfile.TemporaryDirectory() as directory:
                runner = mock.Mock(return_value=subprocess.CompletedProcess(
                    [], code, json.dumps({'error': SECRET[:13]}), SECRET[-12:]))
                result = self.launch(Path(directory), runner)
                self.assertEqual(result.returncode, code)
                self.assertNotIn(SECRET[:13], result.stdout)
                self.assertEqual(result.stderr, MARKER)
                self.assertEqual(json.loads(result.stdout)['error'], MARKER)

    def test_exception_traceback_does_not_disclose_fragment(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = mock.Mock(side_effect=OSError('bad key ' + SECRET[:13]))
            try:
                self.launch(Path(directory), runner)
            except claude.ClaudeAdapterError:
                rendered = traceback.format_exc()
            else:
                self.fail('expected launch error')
            self.assertNotIn(SECRET[:13], rendered)
            self.assertIn(MARKER, rendered)

    def test_mcp_readiness_error_is_redacted_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = mock.Mock()
            readiness = mock.Mock(return_value=subprocess.CompletedProcess(
                [], 1, 'Status: bad key ' + SECRET[:13], ''))
            with self.assertRaises(claude.ClaudeAdapterError) as caught:
                self.launch(Path(directory), worker, capabilities=('playwright',),
                            readiness_runner=readiness)
            self.assertNotIn(SECRET[:13], str(caught.exception))
            worker.assert_not_called()

    def test_mcp_readiness_subprocess_exception_is_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = mock.Mock()
            readiness = mock.Mock(side_effect=subprocess.SubprocessError(SECRET[:13]))
            try:
                self.launch(Path(directory), worker, capabilities=('playwright',),
                            readiness_runner=readiness)
            except claude.ClaudeAdapterError:
                rendered = traceback.format_exc()
            else:
                self.fail('expected readiness error')
            self.assertNotIn(SECRET[:13], rendered)
            self.assertIn(MARKER, rendered)
            worker.assert_not_called()

    @mock.patch('side_lane.qualification.check_auth_overrides')
    def test_qualification_redacts_before_excerpting_and_parsing(self, _check):
        with tempfile.TemporaryDirectory() as directory:
            repo, lane = self.directories(Path(directory))
            model, endpoint = 'kimi-k2.7-code', 'https://api.moonshot.cn/anthropic'
            args = dict(executable='claude', repo=repo, worktree=lane, provider='kimi',
                model=model, endpoint=endpoint, secret=SECRET, prompt='fixture', approved=True,
                transport_probe=dict(provider='kimi', requested_model=model, resolved_model=model,
                                     endpoint=endpoint, http_status=200, ready=True))
            runner = mock.Mock(return_value=subprocess.CompletedProcess(
                [], 124, 'x ' * 995 + SECRET[:13], SECRET[-12:]))
            result = qualify_claude(**args, runner=runner)
            self.assertNotIn(SECRET[:8], json.dumps(result))
            self.assertNotIn(SECRET[-12:], json.dumps(result))
            with self.assertRaises(ValueError) as caught:
                qualify_claude(**args, runner=mock.Mock(side_effect=OSError(SECRET[:13])))
            self.assertNotIn(SECRET[:13], str(caught.exception))
