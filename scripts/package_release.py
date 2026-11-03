"""Build the repository ZIP and the Claude skill ZIP from audited, allowlisted files only.

No Git history, host paths, timestamps or file owners are included. Both archives use a fixed
date and normal file permissions so they reveal nothing about the machine that built them.
"""
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import zipfile
from audit_release import ROOT, audit, files

FIXED_DATE=(2026,1,1,0,0,0)
SKILL_DIR='.claude/skills/alien-search/'


def _add(z,arcname,data):
    info=zipfile.ZipInfo(arcname,date_time=FIXED_DATE)
    info.create_system=3; info.external_attr=0o100644<<16; info.compress_type=zipfile.ZIP_DEFLATED
    z.writestr(info,data)


def build(out_dir):
    stale=subprocess.run([sys.executable,str(ROOT/'scripts'/'build_starters.py'),'--check'],capture_output=True,text=True)
    if stale.returncode: raise SystemExit(stale.stdout.strip() or 'Generated starter files are out of date')
    count,problems,_=audit()
    if problems: raise SystemExit('Release audit failed; run scripts/audit_release.py')
    out_dir=pathlib.Path(out_dir)
    repo=out_dir/'Alien-Search.zip'; skill=out_dir/'Alien-Search-Skill.zip'
    listed=sorted(files())
    with zipfile.ZipFile(repo,'w') as z:
        for name,path in listed: _add(z,'alien-search/'+name,path.read_bytes())
    # Claude skill upload format: a ZIP whose root is the skill folder, named like the skill.
    with zipfile.ZipFile(skill,'w') as z:
        for name,path in listed:
            if name.startswith(SKILL_DIR): _add(z,'alien-search/'+name[len(SKILL_DIR):],path.read_bytes())
    digest=lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    return {'repository_zip':repo.name,'files':count,'repository_sha256':digest(repo),'skill_zip':skill.name,'skill_sha256':digest(skill),'folder':'system temporary folder' if out_dir==pathlib.Path(tempfile.gettempdir()) else 'chosen folder'}


if __name__=='__main__':
    target=pathlib.Path(sys.argv[1]) if len(sys.argv)>1 else pathlib.Path(tempfile.gettempdir())
    print(json.dumps(build(target)))
