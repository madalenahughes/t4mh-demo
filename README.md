# T4MH live demonstration

A clean snapshot of the working Raspberry Pi presentation system, collected November 17, 2026. It includes two separate demonstrations and the standalone light-control tool. The installer leaves `~/t4mh`, `~/t4mh_demo_20261103`, and `~/t4mh_lighting` intact.

## Commands for the presentation

In a Pi SSH terminal:

```bash
cd ~/t4mh_demo
./demo.sh doctor
./demo.sh music
```

The first demo uses **Three Little Birds** by default: connection/window warm-up, **60 valid seconds of silent baseline**, **60 seconds of fixed music**, then adaptive tempo/playback rate and volume for the rest of the track. Above-baseline RMSSD gradually slows/softens the music; below-baseline speeds/loudens it within limits. Playback ends at track completion, the configured time limit, Ctrl+C, or an error. No automatic simulated-data fallback exists.

After the first demo stops, start the second:

```bash
./demo.sh monitor
```

This starts the combined dashboard plus Polar, Muse, respiration and EmotiBit acquisitions. It uses **polar_record.py, not the music controller**. Sensor console diagnostics and CSVs are saved together in the session folder. The combined dashboard remains visible in the terminal.

**EmotiBit still requires the Mac's EmotiBit Oscilloscope forwarding to the Pi on UDP port 12346.** Keep that connection running. The system is not yet fully independent of the Mac.

Once the sensors are ready (including the Polar monitoring baseline), play fixed music in a second Pi terminal:

```bash
cd ~/t4mh_demo
./demo.sh play
```

Music stays at 1.0× rate and volume command 60. Playback timing is recorded in `playback.json`. It does not read physiology or adapt. Ctrl+C in the monitoring terminal stops that terminal's dashboard/acquisitions; fixed playback runs in its own terminal and stops at track end or Ctrl+C there.

## More options

```bash
./demo.sh music --song sun
./demo.sh play --song sun
./demo.sh music --media /absolute/path/to/song.flac
./demo.sh play --max-music-seconds 10
./demo.sh music --simulate
./demo.sh monitor --sensors muse,emotibit
./demo.sh sensor muse
./demo.sh sensor polar
./demo.sh sensor respiration
./demo.sh sensor emotibit
./demo.sh dashboard
./demo.sh check
```

Simulation is explicitly labeled synthetic and follows the same phase timing. It is for rehearsal, not presented as sensor data. A selected-sensor run leaves unstarted streams marked waiting; it does not invent values.

Stop legacy sensor/dashboard/controller scripts before starting these launchers. The new launchers share resource locks so the music controller and monitoring Polar recorder cannot compete with each other. Legacy scripts do not know about these locks. A busy dashboard port causes a clear error rather than replacing or killing the previous process.

## Optional light

The installer can use the existing lighting environment and private connection settings without copying them into Git:

```bash
./demo.sh light status
./demo.sh light brightness 20
./demo.sh light white 4000 --percent 10
```

Quit amaran Desktop while testing. Brightness and white-temperature commands previously received authenticated confirmation from the light; disconnect cleanup has sometimes warned despite successful control. This tool remains manual and separate from HRV. The controller's sequence state stays in the original private lighting folder.

## What was preserved and changed

- Copied the actual working Pi source, including FAA/FBA, signed RMSSD baseline difference before z-score, Fahrenheit temperature display, respiration logging and Muse stall recovery.
- Kept the two different `polar_hrv.py` APIs in separate `music/` and `monitor/` directories. The music demo and monitoring recorder retain their respective estimators; they are not silently substituted for one another.
- Kept all signal calculations, controller gains, artifact checks and phase durations. The music demo uses a baseline median; the older monitoring estimator uses its baseline mean and retained z calculation. Compare results with that distinction in mind.
- Redirected new session files into this project's `data/` directory and guarded terminal clears when sensor output is redirected. The Muse PSD diagnostic is saved with the session.
- Added launch/resource-lock/process-cleanup code. It sends shutdown signals only to children started by that invocation, allowing log flushing before a forced stop if necessary. A sensor process exiting causes an explicit failure and shutdown of the owned monitoring session.
- Added a fixed-music player for the monitoring demo. The old `run_all_sensors.sh` is deliberately absent because it started the controller instead of the recorder.
- Added tests and reproducible dependency lists. Corrected the old requirements typo `bitsring` to the actual `bitstring` dependency.
- Did not add EEG artifact rejection or change physiological interpretations. The quality labels remain heuristic; LIVE only means a recent publisher message, not necessarily a fresh/clean sample in every field.

## Files and environments

`music/` contains `polar_run.py`, the adaptive `polar_hrv.py`, `controller.py` and `audio_engine.py`. `monitor/` contains the dashboard and individual acquisitions. `lighting/` contains the separate light tool and its MIT-licensed protocol helpers. `tools/` contains the launchers/checks; `tests/` contains nonhardware regressions.

The presentation installer links `.venv` to the existing working `~/t4mh/venv` and optionally `.venv-lighting` to the existing lighting environment. It does not reinstall packages or modify those environments. **Keep the original folders while these links are in use.** On a new machine, use `bash bootstrap.sh` with Python 3.13 and install native VLC separately. The optional light tool has its own requirements.

Every launch creates a timestamped directory under `data/`. Music logs include RMSSD, baseline, signed change, phase and commanded tempo/volume; monitoring CSVs preserve each sensor's current schema. Fixed playback and monitoring can be matched later by Unix timestamps, with transport/timing limitations noted. This is not hardware-level synchronization.

Git ignores recordings, logs, media, environments and private light settings. The source archive and local songs are distinct from the code repository. Keep personal recordings and connection keys out of uploads. `SOURCE_VERSIONS.json` records the captured source hashes and runtime versions; it contains no music or connection keys.

## GitHub

Create an empty **private** repository (for example `madalenahughes/t4mh-demo`). Do not initialize it with a README. Then, from this folder:

```bash
python3 tools/publish_code.py --remote https://github.com/madalenahughes/t4mh-demo.git
```

The publisher stages only the code/documentation/test directories listed in the script, checks staged paths for media/data/private files, makes a commit if needed, and pushes without force. GitHub authentication may need completing in your terminal. It does not create the GitHub repository or make it public.

Nonhardware tests verify calculations, log fields, module separation and launcher cleanup. They cannot establish live BLE reliability, sound output or physiological benefit; rehearse the two real demos after installation.


## Session graphs after each demonstration

Stop the recording with Ctrl+C (or let the music demo finish), then on the Pi:

```bash
cd ~/t4mh_demo
./demo.sh graph music
./demo.sh graph monitor
```

Each command selects the latest matching session directory by its timestamped name, reads its CSVs without changing them, and saves PNGs plus an offline `index.html` report in that session's `plots/` folder. No synthetic fallback or new physiological filtering is applied. If the newest session is empty, it reports that instead of silently showing an older participant's session.

The music figure aligns RMSSD/baseline with commanded playback rate and volume, shading the logged phases. The initial RR warm-up is excluded from its recorded time axis. The monitoring report includes a combined overview and detailed HRV, EEG, respiration and EmotiBit figures. Monitoring panels share one Unix-time origin: a later-starting signal is not shifted back to zero. Missing streams remain blank. Long gaps are disconnected. Temperature is displayed in Fahrenheit from the logged Celsius values.

Completed fixed-playback logs that overlap the monitor's recorded interval provide blue music shading. A specific playback log or older session can be chosen:

```bash
./demo.sh graph monitor --session data/monitor_TIMESTAMP
./demo.sh graph monitor --session data/monitor_TIMESTAMP --playback data/play_TIMESTAMP/playback.json
./demo.sh graph music --session data/music_TIMESTAMP
```

For an SSH-only Pi, run this on the Mac to generate, download and open the latest report (after installing the plotting update):

```bash
bash /private/tmp/t4mh-review-session.sh music
bash /private/tmp/t4mh-review-session.sh monitor
```

The Mac wrapper downloads only the generated report/figures into `~/Downloads/t4mh-session-graphs/`, then opens the local HTML file. A plain browser is enough; no server or internet is required to view the downloaded report. Generated figures and recordings stay under ignored `data/`, not in GitHub.

These are recorded processed features, not raw EEG voltage waveforms. EmotiBit packet samples share arrival timestamps; different sensors have different processing/transport delays. Alignment is useful context, not evidence of precise synchronization or causation. Quality labels in acquisition remain heuristic; the plots do not turn noisy measurements into clean ones.
