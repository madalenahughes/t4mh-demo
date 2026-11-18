import csv
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('plot_sessions',ROOT/'tools/plot_sessions.py')
plots=importlib.util.module_from_spec(spec);spec.loader.exec_module(plots)


def write_csv(path,fields,rows):
    with path.open('w',newline='') as file:
        writer=csv.DictWriter(file,fieldnames=fields);writer.writeheader();writer.writerows(rows)


def music_fixture(path,synthetic=False):
    fields=['t_rel_s','music_t_s','phase','source','rmssd_ms','baseline_ms','delta_rmssd_ms','rejected_rr','tempo','pitch','volume']
    rows=[]
    for t in range(181):
        phase='baseline' if t<60 else 'static' if t<120 else 'adaptive'
        value=30+4*math.sin(t/12)
        rows.append(dict(t_rel_s=t,music_t_s='' if t<60 else t-60,phase=phase,source='simulation' if synthetic else 'polar',rmssd_ms=value,
                         baseline_ms='' if t<60 else 30,delta_rmssd_ms='' if t<60 else value-30,rejected_rr=0,
                         tempo=1 if t<120 else 1-(t-120)*.001,pitch=-2,volume=60 if t<120 else 60-(t-120)*.05))
    write_csv(path,fields,rows)


def monitor_fixture(path,origin=1000):
    write_csv(path/'muse_fixture.csv',['unix_time_s','elapsed_s','faa','faa_smoothed','fba','fba_smoothed','AF7_alpha_power','AF8_alpha_power','AF7_beta_power','AF8_beta_power'],
        [dict(unix_time_s=origin+t,elapsed_s=t,faa=math.sin(t/10),faa_smoothed=.5*math.sin(t/10),fba=.3*math.cos(t/10),fba_smoothed=.2*math.cos(t/10),AF7_alpha_power=10,AF8_alpha_power=15,AF7_beta_power=20,AF8_beta_power=25) for t in range(91)])
    write_csv(path/'polar_fixture.csv',['unix_time_s','elapsed_s','rmssd_ms','baseline_rmssd_ms','delta_rmssd_ms','rmssd_z'],
        [dict(unix_time_s=origin+t,elapsed_s=t,rmssd_ms=30+t*.1,baseline_rmssd_ms=30,delta_rmssd_ms=t*.1,rmssd_z=t*.02) for t in range(60,91)])
    write_csv(path/'respiration_fixture.csv',['unix_time_s','elapsed_s','force_n','respiration_rate_bpm','breaths_detected'],
        [dict(unix_time_s=origin+t,elapsed_s=t,force_n=2+math.sin(t),respiration_rate_bpm=12,breaths_detected=5) for t in range(91)])
    rows=[]
    for t in range(91):
        for tag in ['EA','T1','TH','AX','AY','AZ','GX','GY','GZ','MX','MY','MZ']:
            rows.append(dict(unix_time_s=origin+t,elapsed_s=t,tag=tag,sample_index=0,value=30 if tag in ['T1','TH'] else math.sin(t/6)))
    write_csv(path/'emotibit_fixture.csv',['unix_time_s','elapsed_s','tag','sample_index','value'],rows)


class DataTests(unittest.TestCase):
    def test_invalid_numbers_remain_missing_not_zero(self):
        for x in ['',None,'nan','inf','invalid']:
            self.assertTrue(math.isnan(plots.number(x)))
        self.assertEqual(plots.number('0'),0)

    def test_late_polar_start_keeps_common_time_origin(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);monitor_fixture(p)
            data=plots.monitor_data(p)
            self.assertEqual(data['origin'],1000)
            x,y=plots.series(data['groups']['polar'],'unix_time_s','rmssd_ms',data['origin'])
            self.assertEqual(x[0],60)
            self.assertEqual(y[0],36)

    def test_long_gaps_are_disconnected_without_changing_samples(self):
        x,y=plots.break_gaps([0,1,2,20,21],[1,2,3,4,5])
        self.assertTrue(math.isnan(y[3]))
        self.assertEqual([v for v in y if math.isfinite(v)],[1,2,3,4,5])
        self.assertEqual([v for v in x if v!=11],[0,1,2,20,21])

    def test_emotibit_converts_temperature_and_preserves_packet_order(self):
        rows=[{'tag':'T1','unix_time_s':'1000','value':'0'},{'tag':'T1','unix_time_s':'1000','value':'37'}]
        x,y=plots.emotibit_tag(rows,'T1',1000,True)
        self.assertEqual(x,[0,0]);self.assertAlmostEqual(y[0],32);self.assertAlmostEqual(y[1],98.6)

    def test_latest_selection_uses_session_name_not_plot_modification(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);data=root/'data';data.mkdir()
            a=data/'music_20261117_100000';b=data/'music_20261117_110000';a.mkdir();b.mkdir()
            os.utime(a,(2000000000,2000000000))
            self.assertEqual(plots.select_session('music',root=root),b.resolve())

    def test_no_session_does_not_make_up_data(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError,'No recorded'):plots.select_session('music',root=Path(td))

    def test_empty_latest_music_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);write_csv(p/'session_empty_samples.csv',['t_rel_s','rmssd_ms'],[])
            with self.assertRaisesRegex(ValueError,'no usable'):plots.music_data(p)

    def test_synthetic_music_is_labeled_and_phases_follow_record(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);music_fixture(p/'session_fixture_samples.csv',True)
            data=plots.music_data(p)
            self.assertTrue(data['synthetic'])
            self.assertEqual([x[0] for x in data['phases']],['baseline','static','adaptive'])
            self.assertEqual(data['phases'][1][1],60)
            self.assertEqual(data['phases'][2][1],120)

    def test_only_overlapping_completed_playback_is_shaded(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);p=root/'monitor_1';p.mkdir()
            for name,start,stop in [('play_1',1010,1030),('play_2',2000,2100),('play_3',1020,None)]:
                q=root/name;q.mkdir();(q/'playback.json').write_text(json.dumps(dict(playback_unix_s=start,stopped_unix_s=stop,media='fixture.flac')))
            self.assertEqual(plots.playback_intervals(p,1000,1090),[(10,30,'fixture.flac')])

    def test_missing_fba_is_not_fabricated(self):
        rows=[{'unix_time_s':'1000','faa':'1'}]
        x,y=plots.series(rows,'unix_time_s','fba',1000)
        self.assertEqual(x,[0]);self.assertTrue(math.isnan(y[0]))

    def test_incomplete_csv_row_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'a.csv';p.write_text('a,b,c\n1,2,3\n4,5\n')
            rows,bad=plots.read_rows(p);self.assertEqual(len(rows),1);self.assertEqual(bad,1)


class DispatchTests(unittest.TestCase):
    def test_graph_command_does_not_launch_devices_or_create_session(self):
        from unittest.mock import patch
        spec=importlib.util.spec_from_file_location('graph_dispatch',ROOT/'tools/demo.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch.object(module.subprocess,'call',return_value=0) as call,patch.object(module,'session_directory',side_effect=AssertionError('must not create sensor session')):
            self.assertEqual(module.main(['graph','music']),0)
            self.assertTrue(str(call.call_args.args[0][1]).endswith('plot_sessions.py'))
            self.assertEqual(call.call_args.args[0][2:],["music"])


class RenderTests(unittest.TestCase):
    def test_music_report_and_png_are_created_without_source_changes(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);source=p/'session_fixture_samples.csv';music_fixture(source,True)
            before=hashlib.sha256(source.read_bytes()).hexdigest()
            out=p/'plots';out.mkdir();data=plots.music_data(p)
            files=plots.render_music(p,out,data)
            meta=dict(synthetic=True,bad_rows=0,sources=[source.name])
            plots.write_report(out,'music',p,files,meta)
            self.assertGreater((out/'hrv_music.png').stat().st_size,1000)
            self.assertIn('SYNTHETIC DATA',(out/'index.html').read_text())
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),before)

    def test_monitor_report_has_overview_and_details_with_missing_streams(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);monitor_fixture(p)
            # Omit a whole stream and FBA is still handled as unavailable, not zero.
            (p/'respiration_fixture.csv').unlink()
            before={x.name:x.read_bytes() for x in p.glob('*.csv')}
            data=plots.monitor_data(p);out=p/'plots';out.mkdir()
            files=plots.render_monitor(p,out,data,[(10,30,'fixture')],True)
            self.assertEqual(len(files),5)
            for _,name in files:self.assertGreater((out/name).stat().st_size,1000)
            for name,contents in before.items():self.assertEqual((p/name).read_bytes(),contents)

if __name__=='__main__':unittest.main()
