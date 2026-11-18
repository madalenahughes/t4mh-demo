"""Fixed playback for the monitoring demonstration; no physiological input."""
import argparse
import json
import math
import os
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'music'))
from audio_engine import AudioEngine
from controller import AudioParams


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--media',required=True)
    parser.add_argument('--max-music-seconds',type=float,default=600)
    args=parser.parse_args()
    if not math.isfinite(args.max_music_seconds) or args.max_music_seconds<=0:
        parser.error('Music limit must be positive and finite.')
    engine=AudioEngine(args.media)
    record=dict(media=Path(args.media).name,mode='fixed',tempo=1.0,volume=60,
                requested_unix_s=time.time(),status='starting')
    out=Path(os.environ['T4MH_DATA_DIR'])/'playback.json'
    try:
        out.write_text(json.dumps(record,indent=2))
        engine.start()
        startup=time.monotonic()+5
        while not engine.player.is_playing():
            if engine.failed or engine.finished or time.monotonic()>startup:
                raise RuntimeError('Fixed music could not start.')
            time.sleep(.1)
        engine.set_params(AudioParams())
        record.update(status='playing',playback_unix_s=time.time())
        out.write_text(json.dumps(record,indent=2))
        deadline=time.monotonic()+args.max_music_seconds
        while not engine.finished:
            if engine.failed:raise RuntimeError('VLC reported a playback error.')
            if time.monotonic()>=deadline:
                record['status']='time_limit';break
            time.sleep(.1)
        else:record['status']='complete'
    except KeyboardInterrupt:
        record['status']='interrupted'
    except Exception:
        record['status']='error';raise
    finally:
        engine.stop()
        record['stopped_unix_s']=time.time()
        out.write_text(json.dumps(record,indent=2))
        print('Fixed playback stopped; '+str(out))


if __name__=='__main__':main()
