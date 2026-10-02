"""Pure launcher regressions; the real ephemeral CLI test verifies cleanup."""
from pathlib import Path
import json
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


class HarnessRuntimeTests(unittest.TestCase):
    def setUp(self):
        harness = runpy.run_path(str(ROOT/'scripts'/'godot_smoke.py'))
        self.runtime = harness['service_runtime']
        self.start = harness['start_service']
        self.globals = self.start.__globals__

    def test_windows_venv_uses_native_interpreter_and_preserves_venv(self):
        interpreter = SimpleNamespace(executable='venv/Scripts/python.exe',
            _base_executable='native/python.exe',prefix='venv',base_prefix='native')
        inherited = {'UNCHANGED':'value'}
        windows = SimpleNamespace(name='nt',environ=inherited)
        with patch.dict(self.runtime.__globals__,{'sys':interpreter,'os':windows}):
            executable, environment = self.runtime()
        self.assertEqual('native/python.exe',executable)
        self.assertEqual('venv/Scripts/python.exe',environment['__PYVENV_LAUNCHER__'])
        self.assertEqual('value',environment['UNCHANGED'])
        self.assertEqual({'UNCHANGED':'value'},inherited)

    def test_nonwindows_or_nonvenv_keeps_original_process_model(self):
        for name,prefix,base_prefix in (('posix','venv','native'),('nt','native','native')):
            interpreter = SimpleNamespace(executable='original/python',prefix=prefix,base_prefix=base_prefix)
            with self.subTest(name=name),patch.dict(self.runtime.__globals__,{
                    'sys':interpreter,'os':SimpleNamespace(name=name,environ={})}):
                executable, environment = self.runtime()
            self.assertEqual('original/python',executable)
            self.assertNotIn('__PYVENV_LAUNCHER__',environment)

    def test_service_launch_tracks_native_pid_and_keeps_disposable_database(self):
        process = Mock(pid=1234)
        process.stdout.readline.return_value = json.dumps({'pid':1234,'url':'http://127.0.0.1:43210'})
        process.poll.return_value = None
        response = Mock(status=200)
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=None)
        with patch.dict(self.globals,{'service_runtime':lambda:('native/python.exe',{
                    '__PYVENV_LAUNCHER__':'venv/Scripts/python.exe'})}), \
                patch.object(self.globals['subprocess'],'Popen',return_value=process) as launcher, \
                patch.dict(self.globals,{'urlopen':Mock(return_value=context)}):
            result = self.start(ROOT/'disposable'/'synthetic.sqlite3')
        self.assertIs(process,result)
        args,kwargs = launcher.call_args
        self.assertEqual('native/python.exe',args[0][0])
        self.assertEqual('venv/Scripts/python.exe',kwargs['env']['__PYVENV_LAUNCHER__'])
        self.assertEqual(str(ROOT/'disposable'),kwargs['env']['ARIADNE_DATA_DIR'])
        self.assertIn('--port',args[0])
        self.assertEqual('0',args[0][args[0].index('--port')+1])

    def test_mismatched_service_pid_still_rejected_before_endpoint_access(self):
        process = Mock(pid=1234)
        process.stdout.readline.return_value = json.dumps({'pid':5678,'url':'http://127.0.0.1:43210'})
        endpoint = Mock()
        stop = Mock()
        with patch.dict(self.globals,{'service_runtime':lambda:('native/python.exe',{}),
                'urlopen':endpoint,'stop_service':stop}), \
                patch.object(self.globals['subprocess'],'Popen',return_value=process):
            with self.assertRaisesRegex(RuntimeError,'Disposable service did not start'):
                self.start(ROOT/'disposable'/'synthetic.sqlite3')
        endpoint.assert_not_called()
        stop.assert_called_once_with(process)


if __name__ == '__main__':
    unittest.main()
