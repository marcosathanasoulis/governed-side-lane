import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from side_lane.qualification import qualify_claude, check_auth_overrides, bounded_process

class QualificationTests(unittest.TestCase):
    def test_no_authority_or_mismatched_probe_never_launches(self):
        runner=mock.Mock()
        args=dict(executable='claude',repo=Path('/unused'),worktree=Path('/unused'),
                  provider='kimi',model='kimi-k2.7-code',endpoint='https://api.moonshot.cn/anthropic',
                  secret='fake',transport_probe={},prompt='fixture',runner=runner)
        with self.assertRaisesRegex(ValueError,'authority'): qualify_claude(**args)
        with self.assertRaisesRegex(ValueError,'probe'): qualify_claude(**args,approved=True)
        runner.assert_not_called()

    def test_saved_auth_conflict_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'settings.json';p.write_text(json.dumps({'env':{'ANTHROPIC_AUTH_TOKEN':'fake'}}))
            with self.assertRaisesRegex(ValueError,'override'): check_auth_overrides([p])

    @mock.patch('side_lane.qualification.check_auth_overrides')
    def test_trial_is_local_bounded_redacted_and_does_not_activate(self, check):
        with tempfile.TemporaryDirectory() as d:
            repo=Path(d)/'repo';lane=Path(d)/'lane'
            for p in (repo,lane): p.mkdir();(p/'.git').write_text('fixture')
            model='kimi-k2.7-code';endpoint='https://api.moonshot.cn/anthropic'
            probe=dict(provider='kimi',requested_model=model,resolved_model=model,endpoint=endpoint,http_status=200,ready=True)
            runner=mock.Mock(return_value=subprocess.CompletedProcess([],0,'{"result":"fake-secret"}','fake-secret'))
            result=qualify_claude(executable='claude',repo=repo,worktree=lane,provider='kimi',model=model,
                endpoint=endpoint,secret='fake-secret',transport_probe=probe,prompt='fixture',approved=True,runner=runner)
            argv=runner.call_args.args[0];kwargs=runner.call_args.kwargs
            self.assertNotIn('fake-secret',str(argv));self.assertNotIn('fake-secret',json.dumps(result))
            self.assertEqual(kwargs['cwd'],lane);self.assertEqual(kwargs['timeout'],180)
            settings=json.loads(argv[argv.index('--settings')+1])
            self.assertFalse(settings['sandbox']['enabled'])
            self.assertEqual(settings['env']['ANTHROPIC_BASE_URL'],endpoint)
            self.assertEqual(kwargs['env']['ANTHROPIC_AUTH_TOKEN'],'fake-secret')
            self.assertFalse(result['activated'])

class LifecycleTests(unittest.TestCase):
    @mock.patch('side_lane.qualification.os.killpg')
    @mock.patch('side_lane.qualification.subprocess.Popen')
    def test_timeout_stops_group_and_preserves_output(self, popen, kill):
        process=popen.return_value;process.pid=123
        process.communicate.side_effect=[subprocess.TimeoutExpired('fake',1),('partial','diagnostic')]
        result=bounded_process(['fake'],timeout=1)
        self.assertEqual(result.returncode,124)
        self.assertEqual(result.stdout,'partial')
        self.assertIn('diagnostic',result.stderr)
        kill.assert_called_once()
