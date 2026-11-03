"""Heuristic privacy scan of the release files. Never prints matched values.

Scans every allowlisted release file and, in a Git checkout, every file Git tracks (a tracked file
missing from release-files.json is itself a finding, because it would be published on GitHub
even though the packager leaves it out). With --history it also scans every earlier version of
every file, every commit message, every commit's author and committer email, and whether commit
times carry a time-zone offset (which hints at where the author lives); run that before
making a repository public. Only a full clone covers the whole history.

Optional: list your own identifiers (names, handles, email addresses, folder names), one per line,
in private_input/identity-terms.txt. That folder is git-ignored and never packaged; the audit
reports where a term appears without printing the term.

The rules look for known formats. They cannot recognise every possible secret, such as a key with
no recognisable prefix; your private list and GitHub's secret scanning cover more.
"""
import ipaddress
import json
import pathlib
import re
import subprocess
import sys

ROOT=pathlib.Path(__file__).resolve().parents[1]
PRIVATE_TERMS=ROOT/'private_input'/'identity-terms.txt'
PRIVATE_DIRS={'.git','.runtime','.cache','.state','private_input','node_modules','__pycache__','downloads','extracted','imports','reports','objects'}
RULES={
    'private cloud or chat link':r'https?://(?:drive|docs)\.google\.com/[^\s)"<>]+|https?://(?:www\.)?dropbox\.com/(?:s|scl)/\S+|https?://1drv\.ms/\S+|https?://onedrive\.live\.com/\S+|https?://\S*box\.com/s/\S+|https?://(?:www\.)?icloud\.com/\S+|https://(?:chatgpt\.com|chat\.openai\.com)/(?:c|s|share|g)/\S+|https://claude\.ai/(?:chat|share|project|code|public)/\S+',
    'credential-like token':r'\b(?:sk-proj-|sk-ant-|sk-|sk_live_|rk_live_|ghp_|gho_|ghs_|ghu_|ghr_|github_pat_|glpat-|hf_|npm_|ya29\.|AIza|xox[abprs]-|AKIA|ASIA)[A-Za-z0-9_-]{15,}|-----BEGIN (?:RSA |OPENSSH |EC |DSA |ENCRYPTED )?PRIVATE KEY-----',
    'signed URL or key parameter':r'[?&](?:X-Amz-Signature|X-Goog-Signature|sig|access_token|refresh_token|api_key|apikey|key)=[A-Za-z0-9%_-]{8,}',
    'local identifying path':r'/(?:ho' + r'me)/[^/\s]+/|/(?:Us' + r'ers)/[^/\s]+/|[A-Za-z]:\\(?:Us' + r'ers)\\[^\\\s]+\\',
    'email address':r'(?<![:\w/])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',
}
# Generic public contact addresses that are part of the documented guidance, not anyone's identity.
ALLOWED_EMAILS={'info@archive.org','catalog_api@nara.gov'}


def files():
    names=json.loads((ROOT/'release-files.json').read_text(encoding='utf-8'))
    if len(names)!=len(set(names)): raise ValueError('Duplicate release path')
    root=ROOT.resolve()  # Windows can name the same folder by a short or a long path
    for name in names:
        path=ROOT/name
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError('Invalid release path: '+name)
        if any(p in PRIVATE_DIRS for p in pathlib.PurePosixPath(name).parts):
            raise ValueError('Private directory in release: '+name)
        yield name,path


def private_terms():
    if not PRIVATE_TERMS.exists(): return []
    terms=[t.strip() for t in PRIVATE_TERMS.read_text(encoding='utf-8').splitlines()]
    return [t.casefold() for t in terms if len(t)>=3 and not t.startswith('#')]


IPV4=re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')
IPV6=re.compile(r'(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:.])')
DOCUMENTATION_V6=ipaddress.ip_network('2001:db8::/32')


def scan_text(data,terms):
    """Labels of the rules a text breaks. Matched values are never returned."""
    found=[]
    for label,pattern in RULES.items():
        for match in re.finditer(pattern,data,re.I):
            if label=='email address' and match.group(0).lower() in ALLOWED_EMAILS: continue
            found.append(label); break
    for value in IPV4.findall(data):
        try: addr=ipaddress.ip_address(value)
        except ValueError: continue
        if not addr.is_loopback: found.append('non-loopback IP literal'); break
    for value in IPV6.findall(data):
        if sum(1 for group in value.split(':') if group)<3: continue  # skips slices such as [::2]
        try: addr=ipaddress.IPv6Address(value)
        except ValueError: continue
        if not (addr.is_loopback or addr in DOCUMENTATION_V6): found.append('IPv6 address'); break
    folded=data.casefold()
    if any(term in folded for term in terms): found.append('private identity term')
    return found


def _git(*args):
    return subprocess.run(['git','-C',str(ROOT),*args],capture_output=True,check=True).stdout


def tracked_files():
    """Files Git tracks in this checkout, or [] outside a Git checkout."""
    if not (ROOT/'.git').exists(): return []
    try: out=_git('ls-files','-z')
    except (OSError,subprocess.CalledProcessError): return []
    return sorted(n for n in out.decode('utf-8','replace').split('\0') if n)


def audit():
    problems=[];count=0;terms=private_terms();listed=set()
    for name,path in files():
        count+=1; listed.add(name)
        problems+=[(name,label) for label in scan_text(path.read_text(encoding='utf-8'),terms)]
    for name in tracked_files():
        if name in listed: continue
        problems.append((name,'tracked by Git but not in release-files.json'))
        path=ROOT/name
        if path.is_file() and not path.is_symlink():
            try: problems+=[(name,label) for label in scan_text(path.read_text(encoding='utf-8'),terms)]
            except UnicodeDecodeError: pass  # binary file: already reported above
    return count,problems,len(terms)


NO_REPLY=re.compile(r'(?:\d+\+)?[A-Za-z0-9-]+@users\.noreply\.github\.com|no-?reply@[A-Za-z0-9.-]+',re.I)


def no_reply(email):
    """GitHub's private commit address, or a service's plain noreply or no-reply address. A personal
    address that merely contains the word "noreply" somewhere is not accepted."""
    return bool(NO_REPLY.fullmatch(email.strip()))


def utc_time(stamp):
    """Commit times carry the committer's time-zone offset, which hints at where they live."""
    return stamp.endswith(('+00:00','Z'))


def history_audit():
    """Every file version and commit message reachable from HEAD, and every commit's emails."""
    terms=private_terms();problems=[];blobs={}
    commits=_git('log','--format=%H%x00%ae%x00%ce%x00%aI%x00%cI%x00%B%x1e','HEAD').decode('utf-8','replace').split('\x1e')
    count=0
    for record in commits:
        record=record.strip('\n')
        if not record: continue
        sha,author,committer,authored,committed,message=record.split('\0',5); count+=1; short=sha[:7]
        for role,email in (('author',author),('committer',committer)):
            if not no_reply(email): problems.append(('commit '+short,role+' email is not a no-reply address'))
        for role,stamp in (('author',authored),('committer',committed)):
            if not utc_time(stamp): problems.append(('commit '+short,role+' time records a time-zone offset'))
        problems+=[('commit '+short+' message',label) for label in scan_text(message,terms)]
        for line in _git('ls-tree','-r',sha).decode('utf-8','replace').splitlines():
            meta,_,path=line.partition('\t'); kind,obj=meta.split()[1:3]
            if kind=='blob': blobs.setdefault(obj,path)
    for obj,path in blobs.items():
        data=_git('cat-file','blob',obj).decode('utf-8','replace')
        problems+=[(path+' (version '+obj[:7]+')',label) for label in scan_text(data,terms)]
    complete=_git('rev-parse','--is-shallow-repository').strip()!=b'true'
    return {'commits_scanned':count,'file_versions_scanned':len(blobs),'history_complete':complete,
            'private_terms_checked':len(terms),'findings':[{'where':w,'rule':r} for w,r in dict.fromkeys(problems)]}


if __name__=='__main__':
    if '--history' in sys.argv[1:]:
        try: report=history_audit()
        except (OSError,subprocess.CalledProcessError):
            print(json.dumps({'error':'--history needs Git and a Git clone of the repository; clone it without --depth to cover the whole history'}))
            sys.exit(2)
        print(json.dumps(report))
        sys.exit(bool(report['findings']) or not report['history_complete'])
    count,problems,terms=audit()
    print(json.dumps({'files_scanned':count,'private_terms_checked':terms,'findings':[{'file':f,'rule':r} for f,r in problems]}))
    sys.exit(bool(problems))
