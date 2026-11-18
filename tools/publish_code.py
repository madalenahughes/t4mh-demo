"""Publish an explicit code-only allowlist to an already-created GitHub repository."""
import argparse
from pathlib import Path
import re
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]
ALLOW=['.gitignore','README.md','demo.sh','bootstrap.sh','requirements.txt','SOURCE_VERSIONS.json',
       'music','monitor','lighting','tools','tests','media/README.md']
FORBIDDEN={'.csv','.flac','.wav','.mp3','.m4a','.log','.db','.btsnoop','.pyc'}


def git(*args,capture=False):
    result=subprocess.run(['git','-C',str(ROOT),*args],check=True,
        stdout=subprocess.PIPE if capture else None,text=capture)
    return result.stdout if capture else None


def verify(paths):
    for path in paths:
        p=Path(path)
        if (not any(path==x or (x not in ['.gitignore','README.md','demo.sh','bootstrap.sh','requirements.txt','SOURCE_VERSIONS.json','media/README.md'] and path.startswith(x+'/')) for x in ALLOW)
                or p.suffix.lower() in FORBIDDEN
                or p.name in {'light_config.json','sequence.json','light.lock'}
                or 'private' in p.parts or 'data' in p.parts or 'logs' in p.parts or '__pycache__' in p.parts):
            raise ValueError('Refusing to publish non-code/private/generated path: '+path)
        local=ROOT/p
        if local.is_symlink():raise ValueError('Refusing to publish a symlink: '+path)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--remote',required=True)
    args=parser.parse_args()
    if not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?',args.remote):
        raise ValueError('Use a plain HTTPS GitHub repository URL with no credentials in it.')
    if not (ROOT/'.git').exists():git('init','-b','main')
    # Existing staged files must be safe too; never reset another person's staging.
    verify([x for x in git('diff','--cached','--name-only','-z',capture=True).split('\0') if x])
    existing=subprocess.run(['git','-C',str(ROOT),'remote','get-url','origin'],capture_output=True,text=True)
    if existing.returncode==0 and existing.stdout.strip()!=args.remote:
        raise ValueError('This folder already has a different origin. Nothing was pushed.')
    if existing.returncode!=0:git('remote','add','origin',args.remote)
    git('add','--',*ALLOW)
    verify([x for x in git('ls-files','-z',capture=True).split('\0') if x])
    change=subprocess.run(['git','-C',str(ROOT),'diff','--cached','--quiet'])
    if change.returncode==1:git('commit','-m','Preserve working T4MH presentation demos')
    elif change.returncode!=0:raise ValueError('Could not inspect staged changes.')
    git('push','-u','origin','HEAD:main')
    print('Code pushed. Local music, sensor recordings and light keys were excluded.')

if __name__=='__main__':
    try:main()
    except (ValueError,subprocess.CalledProcessError) as err:raise SystemExit(str(err))
