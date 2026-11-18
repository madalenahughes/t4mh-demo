#!/usr/bin/env python3
"""Read recorded sessions; export PNG figures and an offline HTML report."""
from __future__ import annotations
import argparse
import csv
from datetime import datetime
import html
import json
import math
import os
from pathlib import Path
import statistics
import sys

ROOT=Path(__file__).resolve().parents[1]
COLORS=['#58DBE5','#FFD166','#D5A3FF','#FF8A80','#8FE1A2','#B0C5D8']


def number(value):
    try:
        result=float(value)
        return result if math.isfinite(result) else math.nan
    except (ValueError,TypeError):return math.nan


def read_rows(path):
    """Read only complete records from a snapshot; never change source files."""
    rows=[];bad=0
    with path.open(newline='',encoding='utf-8-sig') as file:
        reader=csv.DictReader(file)
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                bad+=1;continue
            rows.append(row)
    return rows,bad


def select_session(mode, session=None, root=ROOT):
    if session:
        path=Path(session).expanduser()
        if not path.is_absolute():path=Path.cwd()/path
        if not path.exists():raise ValueError('Session not found: '+str(path))
        return path.resolve()
    candidates=[p for p in (root/'data').glob(mode+'_*') if p.is_dir()]
    if not candidates:raise ValueError('No recorded %s session found under %s. Run the demo first.'%(mode,root/'data'))
    # Session names contain creation timestamps. Plot modification times must not change selection.
    return max(candidates,key=lambda p:p.name).resolve()


def series(rows,time_key,value_key,origin=0):
    pairs=[]
    for row in rows:
        x=number(row.get(time_key))
        if math.isfinite(x):pairs.append((x-origin,number(row.get(value_key))))
    pairs.sort(key=lambda p:p[0])
    return ([p[0] for p in pairs],[p[1] for p in pairs])


def break_gaps(x,y,minimum=3.0):
    """Insert missing points for long gaps; retain every recorded value and timestamp."""
    if len(x)<2:return x,y
    differences=[b-a for a,b in zip(x,x[1:]) if b>a]
    typical=statistics.median(differences) if differences else 0
    threshold=max(minimum,3*typical)
    xx=[];yy=[]
    for i,(a,b) in enumerate(zip(x,y)):
        if i and a-x[i-1]>threshold:
            xx.append((a+x[i-1])/2);yy.append(math.nan)
        xx.append(a);yy.append(b)
    return xx,yy


def music_data(path):
    files=[path] if path.is_file() else sorted(path.glob('session_*_samples.csv'))
    if len(files)!=1:raise ValueError('Expected one music samples CSV; pass its exact path with --session.')
    rows,bad=read_rows(files[0])
    rows=[r for r in rows if math.isfinite(number(r.get('t_rel_s')))]
    rows.sort(key=lambda r:number(r['t_rel_s']))
    if not rows:raise ValueError('This session has no usable recorded samples yet. Stop the demo and check its log.')
    required={'rmssd_ms','baseline_ms','tempo','volume','phase'}
    if not required.issubset(rows[0]):raise ValueError('Music CSV is missing the current RMSSD/controller fields.')
    synthetic=any(r.get('source','').lower() in ('simulation','synthetic') for r in rows) or 'simulation' in files[0].name.lower()
    phases=[]
    start=number(rows[0]['t_rel_s']);current=rows[0]['phase']
    for row in rows[1:]:
        t=number(row['t_rel_s'])
        if row['phase']!=current:
            phases.append((current,start,t));current=row['phase'];start=t
    phases.append((current,start,number(rows[-1]['t_rel_s'])))
    return dict(files=files,rows=rows,bad_rows=bad,synthetic=synthetic,phases=phases)


def monitor_data(path):
    if not path.is_dir():raise ValueError('For monitoring, --session must be a directory containing sensor CSVs.')
    groups={};files=[];bad=0;times=[]
    for sensor in ['polar','muse','respiration','emotibit']:
        rows=[]
        for file in sorted(path.glob(sensor+'_*.csv')):
            records,dropped=read_rows(file);bad+=dropped;files.append(file)
            rows.extend(records)
        valid=[r for r in rows if math.isfinite(number(r.get('unix_time_s')))]
        valid.sort(key=lambda r:number(r['unix_time_s']))
        groups[sensor]=valid
        times.extend(number(r['unix_time_s']) for r in valid)
    if not times:raise ValueError('No timestamped monitoring samples found in '+str(path))
    origin=min(times);end=max(times)
    return dict(files=files,groups=groups,bad_rows=bad,origin=origin,end=end)


def playback_intervals(path,origin,end,explicit=None):
    files=[Path(explicit).expanduser()] if explicit else sorted(path.parent.glob('play_*/playback.json'))
    intervals=[]
    for file in files:
        try:record=json.loads(file.read_text())
        except (OSError,ValueError):continue
        start=number(record.get('playback_unix_s'));stop=number(record.get('stopped_unix_s'))
        if not math.isfinite(start) or not math.isfinite(stop) or stop<start:continue
        if stop<origin or start>end:continue
        intervals.append((max(start,origin)-origin,min(stop,end)-origin,record.get('media','fixed music')))
    return intervals


def emotibit_tag(rows,tag,origin, fahrenheit=False):
    selected=[r for r in rows if r.get('tag')==tag]
    x,y=series(selected,'unix_time_s','value',origin)
    if fahrenheit:y=[v*9/5+32 if math.isfinite(v) else math.nan for v in y]
    return x,y


def plotting():
    os.environ.setdefault('MPLCONFIGDIR',str(ROOT/'.cache/matplotlib'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'figure.facecolor':'#071421','axes.facecolor':'#102231',
        'text.color':'#F2F5F7','axes.labelcolor':'#E7EEF3','xtick.color':'#C5D0D9','ytick.color':'#C5D0D9',
        'axes.edgecolor':'#617587','grid.color':'#617587','font.size':11,
        'axes.titlesize':13,'axes.labelsize':10,'legend.fontsize':9,'savefig.facecolor':'#071421'})
    return plt


def draw_series(ax,x,y,label,color,*,step=False,linestyle='-'):
    if not any(math.isfinite(v) for v in y):return False
    xx,yy=break_gaps(x,y)
    ax.plot(xx,yy,label=label,color=color,linewidth=1.3,linestyle=linestyle,
            drawstyle='steps-post' if step else 'default')
    return True


def finish_axis(ax,title,ylabel,xlim):
    ax.set_title(title,loc='left',fontweight='bold',pad=9)
    ax.set_ylabel(ylabel)
    ax.grid(alpha=.25)
    ax.set_xlim(*xlim)
    handles,labels=ax.get_legend_handles_labels()
    if handles:ax.legend(loc='best',facecolor='#102231',edgecolor='#617587',labelcolor='#F2F5F7')
    else:ax.text(.5,.5,'Not recorded / no usable samples',ha='center',va='center',transform=ax.transAxes,color='#B0C5D8')


def save_figure(plt,fig,path,title,footnote):
    fig.suptitle(title,fontsize=19,fontweight='bold',x=.06,ha='left',y=.99)
    fig.text(.06,.012,footnote,fontsize=9,color='#B0C5D8',va='bottom')
    fig.tight_layout(rect=(.025,.045,.995,.95))
    fig.savefig(path,dpi=150)
    plt.close(fig)


def render_music(path,out,data,synthetic=False):
    plt=plotting();rows=data['rows'];t=[number(r['t_rel_s']) for r in rows]
    fig,axes=plt.subplots(3,1,figsize=(15,9),sharex=True)
    phase_colors={'baseline':'#7F9CB1','static':'#62A8E5','adaptive':'#AA84D8'}
    phase_names={'baseline':'Silent baseline','static':'Fixed music','adaptive':'Adaptive music'}
    for ax in axes:
        for phase,start,end in data['phases']:
            ax.axvspan(start,end,color=phase_colors.get(phase,'#7F9CB1'),alpha=.13)
    for phase,start,end in data['phases']:
        axes[0].text((start+end)/2,1.15,phase_names.get(phase,phase),ha='center',transform=axes[0].get_xaxis_transform(),fontsize=10,color='#CFDCE5')
    draw_series(axes[0],t,[number(r.get('rmssd_ms')) for r in rows],'RMSSD',COLORS[0])
    draw_series(axes[0],t,[number(r.get('baseline_ms')) for r in rows],'Fixed baseline',COLORS[1],linestyle='--')
    draw_series(axes[1],t,[number(r.get('tempo')) for r in rows],'Commanded playback rate',COLORS[1],step=True)
    axes[1].axhline(1,color='#BAC9D5',linewidth=.7,linestyle=':')
    draw_series(axes[2],t,[number(r.get('volume')) for r in rows],'Commanded volume',COLORS[2],step=True)
    limits=(min(t),max(max(t),min(t)+1))
    for ax,title,unit in zip(axes,['Heart rate variability','Playback rate','Volume'],['RMSSD (ms)','Rate (×)','Volume command (%)']):
        finish_axis(ax,title,unit,limits)
    axes[-1].set_xlabel('Seconds from first usable RMSSD estimate (initial RR warm-up excluded)')
    label='SYNTHETIC REHEARSAL — ' if data['synthetic'] or synthetic else ''
    output=out/'hrv_music.png'
    save_figure(plt,fig,output,label+'HRV and music commands',
        'Recorded estimates and commands; gaps are left visible. Playback commands are not measurements of acoustic output or evidence of causation.')
    return [('HRV and music settings',output.name)]


def render_monitor(path,out,data,intervals,synthetic=False):
    plt=plotting();g=data['groups'];origin=data['origin'];duration=max(1,data['end']-origin)
    def field(sensor,key):return series(g[sensor],'unix_time_s',key,origin)
    def tag(key,temp=False):return emotibit_tag(g['emotibit'],key,origin,temp)
    def panel(ax,key):
        if key=='hrv':
            for name,label,color,style in [('rmssd_ms','RMSSD',COLORS[0],'-'),('baseline_rmssd_ms','Baseline',COLORS[1],'--')]:
                draw_series(ax,*field('polar',name),label,color,linestyle=style)
            return 'Heart rate variability','RMSSD (ms)'
        if key=='delta':
            draw_series(ax,*field('polar','delta_rmssd_ms'),'Change from baseline',COLORS[0]);ax.axhline(0,color='#BAC9D5',linewidth=.7)
            return 'Change from baseline','Δ RMSSD (ms)'
        if key=='z':
            draw_series(ax,*field('polar','rmssd_z'),'Retained z-score',COLORS[2]);return 'Normalized baseline change','z-score (SD)'
        if key in ('faa','fba'):
            draw_series(ax,*field('muse',key),'Current',COLORS[0])
            draw_series(ax,*field('muse',key+'_smoothed'),'Logged smoothed',COLORS[1]);ax.axhline(0,color='#BAC9D5',linewidth=.7)
            return key.upper()+' — right minus left log power','Log power ratio'
        if key in ('alpha','beta'):
            for channel,color in [('AF7',COLORS[0]),('AF8',COLORS[1])]:
                draw_series(ax,*field('muse',channel+'_'+key+'_power'),channel,color)
            return 'Frontal '+key+' band power','Logged band power'
        if key=='rate':
            draw_series(ax,*field('respiration','respiration_rate_bpm'),'Breathing rate',COLORS[0]);return 'Breathing rate','Breaths/min'
        if key=='force':
            draw_series(ax,*field('respiration','force_n'),'Belt force',COLORS[1]);return 'Respiration belt force','Force (N)'
        if key=='eda':
            draw_series(ax,*tag('EA'),'EDA / EA',COLORS[0]);return 'Electrodermal activity','Logged EDA units'
        if key=='temperature':
            draw_series(ax,*tag('T1',True),'T1',COLORS[0]);draw_series(ax,*tag('TH',True),'TH',COLORS[1]);return 'Local temperature','Temperature (°F)'
        names={'accel':('A','Acceleration'),'gyro':('G','Angular velocity'),'mag':('M','Magnetic field')}
        prefix,title=names[key]
        for axis,color in zip('XYZ',COLORS):draw_series(ax,*tag(prefix+axis),axis,color)
        return title,'Logged units'
    foot='Shared Unix-time origin; packet/processing arrival times are not hardware-synchronized. Blank panels are missing data; no artifact rejection or imputation added.'
    prefix='SYNTHETIC TEST DATA — ' if synthetic else ''
    plans=[('overview.png','Multisensor overview',5,2,['hrv','faa','fba','rate','force','eda','temperature','accel','gyro','mag']),
           ('hrv.png','HRV baseline comparisons',3,1,['hrv','delta','z']),
           ('eeg.png','Frontal EEG band powers and asymmetry',4,1,['alpha','beta','faa','fba']),
           ('respiration.png','Breathing rate and belt force',2,1,['rate','force']),
           ('emotibit.png','EmotiBit skin and movement signals',5,1,['eda','temperature','accel','gyro','mag'])]
    outputs=[]
    for filename,title,nrows,ncols,keys in plans:
        fig,axes=plt.subplots(nrows,ncols,figsize=(16,3*nrows if ncols==1 else 12),sharex=True,squeeze=False)
        for ax,key in zip(axes.flat,keys):
            for start,stop,_ in intervals:ax.axvspan(start,stop,color='#62A8E5',alpha=.13)
            panel_title,unit=panel(ax,key);finish_axis(ax,panel_title,unit,(0,duration))
        for ax in axes[-1,:]:ax.set_xlabel('Seconds from first logged sensor sample')
        if intervals:footnote=foot+' Blue shading: overlapping fixed playback.'
        else:footnote=foot
        save_figure(plt,fig,out/filename,prefix+title,footnote)
        outputs.append((title,filename))
    return outputs


def write_report(out,mode,path,outputs,metadata):
    title=('SYNTHETIC DATA — ' if metadata['synthetic'] else '')+('HRV music session' if mode=='music' else 'Multisensor session')
    descriptions={
        'hrv_music.png':'RMSSD and its fixed baseline above commanded playback rate and volume. Phase boundaries follow the logged phase field.',
        'overview.png':'Separate panels retain their own units; all use the same elapsed-time origin.',
        'hrv.png':'Absolute RMSSD, signed baseline difference, and retained z-score are shown separately.',
        'eeg.png':'Recorded frontal band powers and current/smoothed asymmetries. These are processed EEG features, not raw voltage waveforms.',
        'respiration.png':'Recorded rate and force; force is not calibrated lung volume.',
        'emotibit.png':'Temperature converts the logged Celsius values to Fahrenheit for these figures. Motion channels remain in their original logged units.'}
    sections=[]
    for label,name in outputs:
        sections.append('<section><h2>'+html.escape(label)+'</h2><p>'+html.escape(descriptions[name])+'</p><a href="'+name+'"><img src="'+name+'" alt="'+html.escape(label)+'"></a></section>')
    notes=['Recorded data only; temporal associations do not establish causation.','Source CSV files were not modified. Long gaps remain disconnected; no new smoothing or artifact rejection was applied.']
    if mode=='monitor':notes.append('Sensor timestamps reflect arrival/processing time. EmotiBit samples in the same packet share an arrival timestamp. These plots do not establish precise sensor synchronization.')
    if metadata['bad_rows']:notes.append(str(metadata['bad_rows'])+' incomplete CSV rows were skipped.')
    files=''.join('<li>'+html.escape(x)+'</li>' for x in metadata['sources'])
    body='<!doctype html><html><head><meta charset="utf-8"><title>'+html.escape(title)+'</title><style>body{background:#071421;color:#edf4f7;font:18px/1.5 system-ui,sans-serif;margin:32px auto;max-width:1500px;padding:0 24px}h1{color:#ffd166}h2{color:#58dbe5;margin-bottom:8px}p,li{color:#c9d7e0}img{width:100%;height:auto}section{margin:44px 0}a{color:#58dbe5}code{overflow-wrap:anywhere}</style></head><body><h1>'+html.escape(title)+'</h1><p>Session: <code>'+html.escape(path.name)+'</code></p><p>Click any figure to open the full-resolution image.</p><ul>'+''.join('<li>'+html.escape(n)+'</li>' for n in notes)+'</ul>'+''.join(sections)+'<h2>Source files</h2><ul>'+files+'</ul></body></html>'
    (out/'index.html').write_text(body)
    (out/'plot_manifest.json').write_text(json.dumps(metadata,indent=2))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['music','monitor'])
    parser.add_argument('--session',help='Specific session directory (or music CSV); default: latest matching session')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--playback',help='Optional playback.json for fixed-music shading')
    parser.add_argument('--json',action='store_true',help='Return machine-readable report paths')
    parser.add_argument('--synthetic-label',action='store_true',help='Mark explicitly synthetic test fixtures')
    args=parser.parse_args(argv)
    path=select_session(args.mode,args.session)
    base=path.parent if path.is_file() else path
    out=args.output.expanduser().resolve() if args.output else base/'plots'
    out.mkdir(parents=True,exist_ok=True)
    if args.mode=='music':
        data=music_data(path);outputs=render_music(path,out,data,args.synthetic_label)
        synthetic=data['synthetic'] or args.synthetic_label
        counts={'music':len(data['rows'])};timing='Seconds from first usable RMSSD estimate; warm-up excluded.'
        extra={}
    else:
        data=monitor_data(path);intervals=playback_intervals(path,data['origin'],data['end'],args.playback)
        outputs=render_monitor(path,out,data,intervals,args.synthetic_label)
        synthetic=args.synthetic_label;counts={k:len(v) for k,v in data['groups'].items()}
        timing='Common Unix arrival/processing-time origin; not hardware-synchronized.'
        extra={'origin_unix_s':data['origin'],'end_unix_s':data['end'],'fixed_playback_intervals_s':intervals}
    metadata=dict(mode=args.mode,session=str(path),output_dir=str(out),index=str(out/'index.html'),
                  sources=[p.name for p in data['files']],rows=counts,bad_rows=data['bad_rows'],
                  synthetic=synthetic,timing=timing,figures=[name for _,name in outputs],**extra)
    write_report(out,args.mode,path,outputs,metadata)
    if args.json:print(json.dumps({'output_dir':str(out),'index':str(out/'index.html'),'session':str(path)}))
    else:print('Saved '+str(out/'index.html')+'\nPNG figures: '+', '.join(name for _,name in outputs))
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError) as error:raise SystemExit(str(error))
