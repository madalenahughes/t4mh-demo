#!/usr/bin/env python3
"""Own only this launcher's children; keep monitoring and control separate."""
from __future__ import annotations
import argparse
from contextlib import ExitStack
from datetime import datetime
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
SENSOR_FILES={'polar':'polar_record.py','muse':'muse_notify.py','emotibit':'emotibit.py','respiration':'respiration.py'}
TRACKS={'birds':'three_little_birds.flac','sun':'here_comes_the_sun.flac'}


def make_environment(data_dir):
    env=os.environ.copy()
    env['T4MH_DATA_DIR']=str(data_dir)
    env['T4MH_MUSIC_LOG_DIR']=str(data_dir)
    env.setdefault('MPLBACKEND','Agg')
    env['PYTHONUNBUFFERED']='1'
    return env


def session_directory(mode):
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    path=ROOT/'data'/(mode+'_'+stamp)
    path.mkdir(parents=True,exist_ok=False)
    return path


class ResourceLocks:
    def __init__(self, names, directory=None):
        self.names=sorted(set(names));self.handles=[]
        self.directory=Path(directory or os.environ.get('T4MH_LOCK_DIR',str(Path.home()/'.cache/t4mh-demo/locks')))

    def __enter__(self):
        self.directory.mkdir(parents=True,exist_ok=True)
        try:
            for name in self.names:
                fd=os.open(str(self.directory/(name+'.lock')),os.O_RDWR|os.O_CREAT,0o600)
                file=os.fdopen(fd,'r+')
                try:fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    file.close()
                    raise RuntimeError('The %s resource is already in use by another demo launcher. Stop it first.'%name)
                self.handles.append(file)
            return self
        except BaseException:
            self.__exit__(None,None,None)
            raise

    def __exit__(self,*_):
        for file in reversed(self.handles):file.close()
        self.handles.clear()


def check_dashboard_port():
    probe=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    try:
        probe.bind(('127.0.0.1',15000))
    except OSError as err:
        raise RuntimeError('Dashboard port 15000 is busy. Stop the old integration.py/dashboard first.') from err
    finally:probe.close()


def stop_children(children, grace=8):
    # Every child has its own process group. No name-based/global process killing.
    for child in children:
        if child.poll() is None:
            try:os.killpg(child.pid,signal.SIGINT)
            except ProcessLookupError:pass
    deadline=time.monotonic()+grace
    while any(p.poll() is None for p in children) and time.monotonic()<deadline:
        time.sleep(.05)
    for child in children:
        if child.poll() is None:
            try:os.killpg(child.pid,signal.SIGTERM)
            except ProcessLookupError:pass
    for child in children:
        try:child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:os.killpg(child.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            child.wait()


def supervise(specs, env, cwd, *, finite=False):
    """Specs are (name, argv, console_path_or_None); keep dashboard in foreground."""
    children=[]
    old_handlers={}
    def interrupted(signum,frame):raise KeyboardInterrupt
    for sig in (signal.SIGINT,signal.SIGTERM):
        old_handlers[sig]=signal.signal(sig,interrupted)
    try:
        with ExitStack() as stack:
            for name,command,log_path in specs:
                output=None if log_path is None else stack.enter_context(open(log_path,'a',buffering=1))
                child=subprocess.Popen(command,cwd=cwd,env=env,stdout=output,stderr=subprocess.STDOUT if output is not None else None,
                                       start_new_session=True)
                children.append(child)
                print('Started '+name,flush=True)
            while True:
                for (name,_,log_path),child in zip(specs,children):
                    status=child.poll()
                    if status is not None:
                        if finite and len(children)==1:
                            return status
                        suffix='' if log_path is None else ' See '+str(log_path)
                        raise RuntimeError('%s stopped (exit %s).%s'%(name,status,suffix))
                time.sleep(.15)
    except KeyboardInterrupt:
        print('\nStopping this session and saving its logs...',flush=True)
        return 130
    finally:
        # Children retain their own log descriptors; allow them to flush before exiting.
        stop_children(children)
        for sig,handler in old_handlers.items():signal.signal(sig,handler)


def media_path(args):
    path=Path(args.media).expanduser() if args.media else ROOT/'media'/TRACKS[args.song]
    if not path.is_absolute():path=Path.cwd()/path
    if not path.is_file():
        raise RuntimeError('Audio file missing: %s. Songs are local and deliberately excluded from GitHub.'%path)
    return path.resolve()


def sensor_names(text):
    names=[x.strip() for x in text.split(',') if x.strip()]
    if not names or len(names)!=len(set(names)) or any(x not in SENSOR_FILES for x in names):
        raise argparse.ArgumentTypeError('Choose distinct names from polar,muse,emotibit,respiration.')
    return names


def commands_for_monitor(names, data_dir):
    specs=[('dashboard',[sys.executable,'-u',str(ROOT/'monitor/integration.py')],None)]
    for name in names:
        specs.append((name,[sys.executable,'-u',str(ROOT/'monitor'/SENSOR_FILES[name])],data_dir/(name+'_console.log')))
    return specs


def doctor():
    missing=[]
    for module in ['bleak','numpy','scipy','matplotlib','bitstring','godirect','vlc']:
        ok=importlib.util.find_spec(module) is not None
        print(('%-14s '%module)+('available' if ok else 'MISSING'))
        if not ok:missing.append(module)
    if 'vlc' not in missing:
        try:
            import vlc
            print('Native VLC:    '+vlc.libvlc_get_version().decode())
        except Exception:missing.append('native VLC');print('Native VLC:    MISSING')
    for name,track in TRACKS.items():
        p=ROOT/'media'/track
        print('Track %-7s '%name+('available' if p.is_file() else 'not copied'))
    print('EmotiBit still needs Oscilloscope forwarding to Pi UDP port 12346.')
    print('Stop legacy sensor processes before using the clean launchers.')
    try:check_dashboard_port();print('Dashboard port: free')
    except RuntimeError as err:print(str(err))
    return 1 if missing else 0


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='mode',required=True)
    for mode in ['music','play']:
        p=sub.add_parser(mode,help='HRV-adaptive music' if mode=='music' else 'Fixed music; no sensor or controller')
        p.add_argument('--song',choices=TRACKS,default='birds')
        p.add_argument('--media')
        if mode=='music':p.add_argument('--simulate',action='store_true',help='Explicitly labeled synthetic rehearsal')
        p.add_argument('--max-music-seconds',type=float,default=600)
    p=sub.add_parser('monitor',help='Monitoring-only dashboard plus selected sensor acquisitions')
    p.add_argument('--sensors',type=sensor_names,default=list(SENSOR_FILES))
    p=sub.add_parser('sensor',help='Run one sensor acquisition in the foreground')
    p.add_argument('name',choices=SENSOR_FILES)
    sub.add_parser('dashboard',help='Display messages from separately started sensor processes')
    p=sub.add_parser('light',help='Standalone light control using the existing private settings')
    p.add_argument('arguments',nargs=argparse.REMAINDER)
    p=sub.add_parser('graph',help='Create PNG plots and an offline report from a recorded session')
    p.add_argument('arguments',nargs=argparse.REMAINDER)
    sub.add_parser('doctor',help='Nonhardware environment check')
    sub.add_parser('check',help='Run nonhardware regression tests')
    args=parser.parse_args(argv)
    if args.mode=='graph':return subprocess.call([sys.executable,str(ROOT/'tools/plot_sessions.py'),*args.arguments])
    if args.mode=='doctor':return doctor()
    if args.mode=='check':return subprocess.call([sys.executable,str(ROOT/'tools/check.py')])
    if args.mode=='light':
        python=ROOT/'.venv-lighting/bin/python'
        config=ROOT/'private/light_config.json'
        if not python.exists() or not config.exists():
            raise RuntimeError('Optional light setup is missing; the install script can link the existing private settings.')
        return subprocess.call([str(python),'-u',str(ROOT/'lighting/light.py'),'--config',str(config),*(args.arguments or ['status'])])
    names=[]
    if args.mode=='music':names=['polar','audio']
    elif args.mode=='play':names=['audio']
    elif args.mode=='monitor':names=[*args.sensors,'dashboard']
    elif args.mode=='dashboard':names=['dashboard']
    elif args.mode=='sensor':names=[args.name]
    with ResourceLocks(names):
        if args.mode in ('monitor','dashboard'):check_dashboard_port()
        # Validate local media before starting any device connection.
        media=media_path(args) if args.mode in ('music','play') else None
        data_dir=session_directory(args.mode)
        env=make_environment(data_dir)
        print('Session files: '+str(data_dir),flush=True)
        if args.mode=='monitor':
            print('Monitoring only: no music controller. In another terminal use ./demo.sh play when ready.',flush=True)
            print('Sensor diagnostics are saved beside the CSVs. Ctrl+C stops only this session.',flush=True)
            return supervise(commands_for_monitor(args.sensors,data_dir),env,ROOT)
        if args.mode=='dashboard':
            command=[sys.executable,'-u',str(ROOT/'monitor/integration.py')]
        elif args.mode=='sensor':
            command=[sys.executable,'-u',str(ROOT/'monitor'/SENSOR_FILES[args.name])]
        elif args.mode=='music':
            command=[sys.executable,'-u',str(ROOT/'music/polar_run.py'),'--media',str(media),
                     '--max-music-seconds',str(args.max_music_seconds)]
            if args.simulate:command.append('--simulate')
        else:
            command=[sys.executable,'-u',str(ROOT/'tools/play_fixed.py'),'--media',str(media),
                     '--max-music-seconds',str(args.max_music_seconds)]
        return supervise([(args.mode,command,None)],env,ROOT,finite=True)


if __name__=='__main__':
    try:raise SystemExit(main())
    except RuntimeError as err:raise SystemExit(str(err))
