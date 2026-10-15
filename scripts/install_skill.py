import argparse,os,shutil
from pathlib import Path
p=argparse.ArgumentParser(description="Install the bundled Skill without overwriting existing files")
p.add_argument('--dest',type=Path,default=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))/'skills')
a=p.parse_args()
source=Path(__file__).resolve().parents[1]/'skills/codex-image-queue'
target=a.dest.expanduser().resolve()/'codex-image-queue'
if target.exists():p.error(f'Already exists: {target}; review and back it up before replacing')
shutil.copytree(source,target,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
print(f'Installed: {target}')
print('Reload skills or start a new conversation, then invoke $codex-image-queue.')
