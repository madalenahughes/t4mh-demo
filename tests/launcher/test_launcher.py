import contextlib
import importlib.util
import io
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('demo_launcher',ROOT/'tools/demo.py')
demo=importlib.util.module_from_spec(spec);spec.loader.exec_module(demo)


class LauncherTests(unittest.TestCase):
    def test_monitor_uses_recorder_not_controller(self):
        specs=demo.commands_for_monitor(list(demo.SENSOR_FILES),Path('/tmp/logs'))
        joined=' '.join(' '.join(x[1]) for x in specs)
        self.assertIn('polar_record.py',joined)
        self.assertNotIn('polar_run.py',joined)
        self.assertEqual(specs[0][0],'dashboard')
        self.assertTrue(all(x[2] is not None for x in specs[1:]))

    def test_runtime_logs_are_separate_from_source(self):
        env=demo.make_environment(Path('/tmp/session'))
        self.assertEqual(env['T4MH_DATA_DIR'],'/tmp/session')
        self.assertEqual(env['T4MH_MUSIC_LOG_DIR'],'/tmp/session')
        self.assertEqual(env['MPLBACKEND'],'Agg')

    def test_shared_lock_prevents_controller_and_polar_recorder_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            with demo.ResourceLocks(['polar','audio'],td):
                with self.assertRaisesRegex(RuntimeError,'already in use'):
                    with demo.ResourceLocks(['polar','dashboard'],td):pass
                with demo.ResourceLocks(['muse'],td):pass
            with demo.ResourceLocks(['polar'],td):pass

    def test_lock_failure_releases_already_acquired_resources(self):
        with tempfile.TemporaryDirectory() as td:
            with demo.ResourceLocks(['polar'],td):
                with self.assertRaises(RuntimeError):
                    with demo.ResourceLocks(['audio','polar'],td):pass
                with demo.ResourceLocks(['audio'],td):pass

    def test_song_default_and_missing_media(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'media').mkdir();song=root/'media/three_little_birds.flac';song.write_bytes(b'fixture')
            args=type('Args',(),{'media':None,'song':'birds'})()
            with patch.object(demo,'ROOT',root):
                self.assertEqual(demo.media_path(args),song.resolve())
                song.unlink()
                with self.assertRaisesRegex(RuntimeError,'missing'):demo.media_path(args)

    def test_each_launch_has_unique_session_directory(self):
        with tempfile.TemporaryDirectory() as td, patch.object(demo,'ROOT',Path(td)):
            a=demo.session_directory('monitor');b=demo.session_directory('monitor')
            self.assertNotEqual(a,b)
            self.assertTrue(a.is_dir() and b.is_dir())

    def test_child_failure_stops_only_owned_children(self):
        # The unrelated process must survive. The owned sibling writes a marker on SIGINT.
        with tempfile.TemporaryDirectory() as td:
            marker=Path(td)/'stopped'
            owned=Path(td)/'owned.py'
            owned.write_text('import signal,time\nfrom pathlib import Path\ndef stop(*a):\n Path('+repr(str(marker))+').write_text("flushed")\n raise SystemExit(0)\nsignal.signal(signal.SIGINT,stop)\nwhile True:time.sleep(.01)\n')
            unrelated=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True)
            try:
                specs=[('owned',[sys.executable,str(owned)],Path(td)/'owned.log'),
                       ('broken',[sys.executable,'-c','import time;time.sleep(.3);raise SystemExit(7)'],Path(td)/'broken.log')]
                with contextlib.redirect_stdout(io.StringIO()),self.assertRaisesRegex(RuntimeError,'broken stopped'):
                    demo.supervise(specs,os.environ.copy(),td)
                self.assertEqual(marker.read_text(),'flushed')
                self.assertIsNone(unrelated.poll())
            finally:
                os.killpg(unrelated.pid,signal.SIGTERM);unrelated.wait()

    def test_single_playback_returns_success_at_track_end(self):
        specs=[('play',[sys.executable,'-c','pass'],None)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(demo.supervise(specs,os.environ.copy(),ROOT,finite=True),0)

    def test_clear_commands_are_guarded_when_redirected(self):
        import ast
        for name in ['integration.py','processor.py','emotibit.py']:
            tree=ast.parse((ROOT/'monitor'/name).read_text())
            parents={child:parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            for node in ast.walk(tree):
                if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='system' and node.args and isinstance(node.args[0],ast.Constant) and node.args[0].value=='clear':
                    parent=parents[node]
                    while parent in parents and not isinstance(parent,ast.If):parent=parents[parent]
                    self.assertIsInstance(parent,ast.If,name)
                    self.assertIn('isatty',ast.unparse(parent.test),name)

    def test_split_polar_streams_keep_distinct_interfaces(self):
        import ast
        music=ast.parse((ROOT/'music/polar_hrv.py').read_text())
        monitor=ast.parse((ROOT/'monitor/polar_hrv.py').read_text())
        self.assertIn('rmssd_stream',[x.name for x in music.body if isinstance(x,ast.AsyncFunctionDef)])
        self.assertIn('rmssd_z_stream',[x.name for x in monitor.body if isinstance(x,ast.AsyncFunctionDef)])


class PublishTests(unittest.TestCase):
    def load_publisher(self):
        spec=importlib.util.spec_from_file_location('publisher',ROOT/'tools/publish_code.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def test_rejects_music_recordings_and_private_settings(self):
        module=self.load_publisher()
        for name in ['media/three_little_birds.flac','data/session/muse.csv','lighting/light_config.json','private/key.json','monitor/foo.csv','tools/__pycache__/foo.pyc']:
            with self.assertRaises(ValueError):module.verify([name])

    def test_accepts_only_source_documentation_and_tests(self):
        module=self.load_publisher()
        module.verify(['README.md','demo.sh','monitor/polar_record.py','music/polar_hrv.py','media/README.md','lighting/vendor/amaran_protocol/LICENSE'])
        with self.assertRaises(ValueError):module.verify(['unexpected.txt'])


class FixedPlaybackTests(unittest.TestCase):
    def exercise(self, fail=False):
        from unittest.mock import MagicMock
        import json,types
        engine=MagicMock();engine.failed=fail;engine.finished=False
        engine.player.is_playing.return_value=True
        def finish(params):
            self.assertEqual(params.tempo,1.0);self.assertEqual(params.volume,60)
            if not fail:engine.finished=True
        engine.set_params.side_effect=finish
        with tempfile.TemporaryDirectory() as td,patch.dict(sys.modules,{'audio_engine':types.SimpleNamespace(AudioEngine=lambda path:engine)}):
            spec=importlib.util.spec_from_file_location('fixed_player',ROOT/'tools/play_fixed.py')
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            with patch.dict(os.environ,{'T4MH_DATA_DIR':td}),patch.object(sys,'argv',['play_fixed','--media','fixture.flac']):
                if fail:
                    with self.assertRaisesRegex(RuntimeError,'playback error'):module.main()
                else:module.main()
            data=json.loads((Path(td)/'playback.json').read_text())
        engine.stop.assert_called_once()
        self.assertEqual(data['status'],'error' if fail else 'complete')
        self.assertEqual(data['mode'],'fixed')
        self.assertIn('playback_unix_s',data)
        self.assertIn('stopped_unix_s',data)

    def test_fixed_player_never_adapts_and_saves_timing(self):self.exercise()
    def test_fixed_player_stops_audio_and_saves_error(self):self.exercise(True)


if __name__=='__main__':unittest.main()

