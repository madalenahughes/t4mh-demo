"""Regression suites run separately to isolate the two Polar stream APIs."""
import os
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]


def main():
    env=os.environ.copy()
    env.pop('T4MH_DATA_DIR',None);env.pop('T4MH_MUSIC_LOG_DIR',None)
    env.setdefault('MPLBACKEND','Agg')
    for suite in ['music','muse','fba','polar','launcher']:
        print('\nChecking '+suite+'...',flush=True)
        completed=subprocess.run([sys.executable,'-m','unittest','discover','-s',str(ROOT/'tests'/suite),'-p','test_*.py','-v'],env=env,cwd=ROOT)
        if completed.returncode:return completed.returncode
    print('\nAll nonhardware checks passed. Live sensors and sound still need rehearsal.')
    return 0
if __name__=='__main__':raise SystemExit(main())
