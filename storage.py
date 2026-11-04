"""User-owned storage: JSON files or SQLite, plus provider-neutral JSON export.
No network calls. Filesystem permissions are best effort on non-POSIX systems.
"""
import argparse
import contextlib
import hashlib
import json
import os
import pathlib
import re
import secrets
import sqlite3
import stat
import sys
import time
from typing import Protocol

PRIVATE_OPEN = getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)  # never follow links; never let Windows translate line endings in files written byte for byte
SCHEMA='alien-search-storage/1'
LEGACY_SCHEMA=re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*-storage/1')  # exports from earlier editions import unchanged

def default_data_dir(system=None,environ=None):
    """Per-user state location: %LOCALAPPDATA%\\alien-search on Windows, ~/.local/share/alien-search elsewhere."""
    system=system or os.name; environ=os.environ if environ is None else environ
    if system=='nt':
        base=environ.get('LOCALAPPDATA')
        return (pathlib.Path(base) if base else pathlib.Path.home()/'AppData'/'Local')/'alien-search'
    return pathlib.Path.home()/'.local'/'share'/'alien-search'

def private_dir(path,tighten_existing=True):
    """Create an owner-only directory.

    tighten_existing=False is used for a directory the user chose: if it already exists its
    permissions are left alone (it may be a shared folder such as Documents), but a
    world-writable existing directory is refused on POSIX.
    """
    p=pathlib.Path(path).expanduser()
    if p.is_symlink(): raise ValueError('Private directory must not be a symbolic link')
    existed=p.exists()
    if existed and not p.is_dir(): raise ValueError('Private state path exists and is not a directory: '+str(p))
    p.mkdir(mode=0o700,parents=True,exist_ok=True)
    if not existed or tighten_existing:
        p.chmod(0o700)
    elif os.name=='posix' and p.stat().st_mode&0o002:
        raise ValueError('Refusing a world-writable existing directory for private state: '+str(p))
    return p

# --- shared safe-file helpers (used by every module that writes private state) ---------------------
def atomic_write(path,data,mode=0o600):
    """Replace path with data as one step: a unique temporary file beside it is written, flushed and renamed.

    A crash or full disk leaves the previous file untouched, two processes never share a temporary
    name, and the new file is created owner-only (best effort on non-POSIX systems).
    """
    p=pathlib.Path(path)
    if p.is_symlink(): raise ValueError('Refusing to replace a symbolic link: '+p.name)
    tmp=p.with_name('.'+p.name+'.'+secrets.token_hex(6)+'.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|PRIVATE_OPEN,mode)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        for attempt in range(10):
            try: os.replace(tmp,p); break
            except PermissionError:
                # Windows refuses to replace a file another program has open for a moment; wait briefly.
                if os.name!='nt' or attempt==9: raise
                time.sleep(0.05)
    except BaseException:
        with contextlib.suppress(OSError): os.unlink(tmp)
        raise

def write_json(path,value,indent=None):
    """Write JSON atomically and owner-only. Values that are not valid JSON (NaN, infinity) are refused before anything is written."""
    atomic_write(path,json.dumps(value,ensure_ascii=False,allow_nan=False,indent=indent).encode('utf-8'))

def load_json(path,default=None,expect=None,keep_corrupt=False):
    """Read a JSON state file, returning default when it is missing or damaged.

    Caches are simply rebuilt (keep_corrupt=False). Records worth keeping, such as download and
    import indexes, are moved aside as NAME.corrupt-TIMESTAMP so nothing is silently lost.
    """
    p=pathlib.Path(path)
    try:
        value=json.loads(p.read_bytes())
        if expect is None or isinstance(value,expect): return value
    except FileNotFoundError:
        return default
    except (ValueError,UnicodeDecodeError):
        pass
    if keep_corrupt:
        with contextlib.suppress(OSError): os.replace(p,p.with_name(p.name+'.corrupt-'+time.strftime('%Y%m%d%H%M%S')))
    return default

@contextlib.contextmanager
def file_lock(path,timeout=30.0):
    """Hold an exclusive lock for path across threads and separate processes (standard library only).

    The lock lives in a hidden .NAME.lock file beside path, which is left in place: deleting a lock
    file while another process waits on it would let two writers in. POSIX uses flock and Windows
    uses msvcrt.locking; both lock per open handle, so two threads in one process also exclude each
    other. Raises TimeoutError if the lock is not free within timeout seconds.
    """
    p=pathlib.Path(path)
    lock=p.with_name('.'+p.name+'.lock')
    if lock.is_symlink(): raise ValueError('Refusing a symbolic lock file: '+lock.name)
    fd=os.open(lock,os.O_RDWR|os.O_CREAT|PRIVATE_OPEN,0o600)
    deadline=time.monotonic()+timeout
    try:
        if os.name=='nt':
            import msvcrt
            def take(): os.lseek(fd,0,os.SEEK_SET); msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
            def give(): os.lseek(fd,0,os.SEEK_SET); msvcrt.locking(fd,msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            def take(): fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            def give(): fcntl.flock(fd,fcntl.LOCK_UN)
        while True:
            try: take(); break
            except OSError:
                if time.monotonic()>=deadline: raise TimeoutError('Another Alien Search process is still updating '+p.name) from None
                time.sleep(0.02)
        try: yield
        finally:
            with contextlib.suppress(OSError): give()
    finally:
        os.close(fd)

def update_json(path,change,default=None,expect=None,keep_corrupt=False,indent=None):
    """Read, change and atomically rewrite a JSON state file as one locked step.

    atomic_write alone stops half-written files; this also stops two writers (threads or separate
    CLI processes) from each reading the old value and one silently discarding the other's update.
    change(value) returns the new value. Returns what was written.
    """
    with file_lock(path):
        value=change(load_json(path,default,expect=expect,keep_corrupt=keep_corrupt))
        write_json(path,value,indent=indent)
        return value

def file_sha256(path):
    digest=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda: f.read(1<<20),b''): digest.update(block)
    return digest.hexdigest()

def store_object(objects,digest,data=None,src=None):
    """Keep content under its SHA-256 name. An existing file is reused only if it still hashes to that name.

    Give either data (bytes) or src (a finished temporary file, which is moved into place or removed).
    A damaged or altered stored copy is replaced, so one bad file cannot spoil later fingerprint matches.
    """
    dest=pathlib.Path(objects)/digest
    if dest.is_symlink(): raise ValueError('Refusing a symbolic link in the object store')
    if dest.is_file() and file_sha256(dest)==digest:
        if src is not None: pathlib.Path(src).unlink(missing_ok=True)
        return dest
    if src is not None:
        os.replace(src,dest)
        with contextlib.suppress(OSError): os.chmod(dest,0o600)
    else:
        atomic_write(dest,data)
    return dest

def _read_token(p):
    fd=os.open(p,os.O_RDONLY|PRIVATE_OPEN)
    with os.fdopen(fd,'r',encoding='ascii',errors='replace') as f:
        st=os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size>256: raise ValueError('Invalid token file')
        if os.name=='posix' and (st.st_uid!=os.getuid() or st.st_mode&0o077): raise ValueError('Token file must be owner-only')
        token=f.read().strip()
    if not re.fullmatch('[a-f0-9]{64}',token):
        raise ValueError('Invalid generated token; delete '+str(p)+' and start the server again to regenerate')
    return token

def _write_new(path,token):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|PRIVATE_OPEN,0o600)
    with os.fdopen(fd,'w',encoding='ascii') as f:
        f.write(token+'\n'); f.flush(); os.fsync(f.fileno())

def load_or_create_token(path):
    """Return this installation's token, creating it atomically on first use.

    The token is fully written to a private temporary file and then hard-linked into place,
    so an interrupted first start cannot leave an empty token file behind. If two processes
    race, the loser reads the winner's token.
    """
    p=pathlib.Path(path)
    try: return _read_token(p)
    except FileNotFoundError: pass
    except ValueError:
        # An empty token file can only come from an interrupted first start on a filesystem
        # without hard links; replace it. Any other invalid token is reported, not overwritten.
        if p.is_symlink() or not p.is_file() or p.stat().st_size!=0: raise
        p.unlink()
    token=secrets.token_hex(32)
    temp=p.with_name(p.name+'.'+secrets.token_hex(8)+'.tmp')
    try:
        _write_new(temp,token)
        try: os.link(temp,p)
        except FileExistsError: return _read_token(p)
        except OSError:
            # Filesystem without hard links: fall back to exclusive creation.
            try: _write_new(p,token)
            except FileExistsError: return _read_token(p)
    finally:
        with contextlib.suppress(FileNotFoundError): temp.unlink()
    return token

WINDOWS_RESERVED=re.compile(r'(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])')

def check_key(key):
    """Record IDs must be usable as file names on Windows, macOS and Linux alike."""
    if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',key):
        raise ValueError('Use a 1..100 character record ID with letters, digits, underscore or hyphen')
    if WINDOWS_RESERVED.fullmatch(key):
        raise ValueError('"'+key+'" is a reserved device name on Windows; choose another record ID')
    return key

def check_new_keys(keys,existing):
    """Refuse IDs that differ only in capitals from each other or from stored IDs.

    Windows and macOS usually treat Case and case as the same file, so such pairs would
    overwrite each other there even if they work on Linux.
    """
    seen={k.casefold():k for k in existing}
    for k in keys:
        check_key(k)
        other=seen.get(k.casefold())
        if other is not None and other!=k:
            raise ValueError('Record IDs "'+other+'" and "'+k+'" differ only in capitals; use one spelling')
        seen[k.casefold()]=k

def read_json_file(path):
    """Read JSON saved as UTF-8 (with or without BOM) or UTF-16/32 with BOM.

    Windows PowerShell 5.1 writes UTF-16 for `>` redirection; json.loads(bytes) detects that.
    """
    try: return json.loads(pathlib.Path(path).read_bytes())
    except UnicodeDecodeError:
        raise ValueError('File is not UTF-8 or UTF-16 JSON; re-create it with "export --output FILE"') from None

def print_json(value,stream=None):
    """Print JSON: indented in a terminal, compact when read by a program or AI agent (fewer tokens).

    ALIEN_SEARCH_PRETTY=1 forces indentation. Non-ASCII is escaped when the stream cannot encode UTF-8.
    """
    stream=stream or sys.stdout
    encoding=(getattr(stream,'encoding',None) or '').lower().replace('-','').replace('_','')
    pretty=os.environ.get('ALIEN_SEARCH_PRETTY')=='1' or (hasattr(stream,'isatty') and stream.isatty())
    layout={'indent':2} if pretty else {'separators':(',',':')}
    print(json.dumps(value,ensure_ascii=encoding not in ('utf8','utf8sig'),**layout),file=stream)

class Store(Protocol):
    def put(self,key:str,value:dict)->None: ...
    def get(self,key:str)->dict: ...  # raises KeyError when the record does not exist
    def export(self)->dict: ...

def _encode_records(records):
    """JSON-encode every record before any write, naming the first record that cannot be saved."""
    out={}
    for k,v in records.items():
        try: out[k]=json.dumps(v,ensure_ascii=False,allow_nan=False)
        except (TypeError,ValueError) as e: raise ValueError('Record "'+str(k)[:100]+'" cannot be saved as JSON ('+str(e)+'); nothing was imported') from None
    return out

class JsonStore:
    def __init__(self,path): self.path=private_dir(path)
    def keys(self):
        return [p.stem for p in self.path.glob('*.json') if not p.name.startswith('.')]
    def put(self,key,value):
        self.put_many({key:value})
    def put_many(self,records):
        """Validate every record first, then write each one atomically. Nothing is written if any record is invalid."""
        check_new_keys(list(records),self.keys())
        for k,data in _encode_records(records).items(): atomic_write(self.path/(k+'.json'),(data+'\n').encode('utf-8'))
    def get(self,key):
        path=self.path/(check_key(key)+'.json')
        if path.is_symlink(): raise ValueError('Symbolic record file refused')
        try: text=path.read_text(encoding='utf-8')
        except FileNotFoundError: raise KeyError(key) from None
        return json.loads(text)
    def export(self):
        return {'schema':SCHEMA,'records':{k:self.get(k) for k in sorted(self.keys())}}

class SQLiteStore:
    def __init__(self,path):
        self.path=pathlib.Path(path);private_dir(self.path.parent,tighten_existing=False)
        if self.path.is_symlink(): raise ValueError('Symbolic database refused')
        # Restrict mode at creation, before SQLite writes any content.
        fd=os.open(self.path,os.O_CREAT|os.O_RDWR|PRIVATE_OPEN,0o600);os.close(fd)
        self.path.chmod(0o600)
        with self._db() as db, db: db.execute('CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
    def _db(self):
        # closing() releases the file handle; the inner `with db` commits or rolls back.
        return contextlib.closing(sqlite3.connect(self.path))
    def keys(self):
        with self._db() as db: return [row[0] for row in db.execute('SELECT id FROM evidence')]
    def put(self,key,value):
        self.put_many({key:value})
    def put_many(self,records):
        """Validate every record first, then write them in one transaction: all are saved or none are."""
        check_new_keys(list(records),self.keys())
        rows=list(_encode_records(records).items())
        with self._db() as db, db: db.executemany('INSERT OR REPLACE INTO evidence VALUES (?,?)',rows)
    def get(self,key):
        with self._db() as db: row=db.execute('SELECT data FROM evidence WHERE id=?',(check_key(key),)).fetchone()
        if row is None: raise KeyError(key)
        return json.loads(row[0])
    def export(self):
        with self._db() as db: rows=db.execute('SELECT id,data FROM evidence ORDER BY id').fetchall()
        return {'schema':SCHEMA,'records':{k:json.loads(v) for k,v in rows}}

def write_export(path,value):
    """Write an export as UTF-8 with owner-only permissions.

    The new export replaces any earlier file only once it is complete, so a failure keeps the
    previous export, and a replaced file does not keep looser permissions it had before.
    """
    atomic_write(path,(json.dumps(value,indent=2,ensure_ascii=False,allow_nan=False)+'\n').encode('utf-8'))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir',help='Private state directory (default: ALIEN_SEARCH_DATA_DIR or the per-user default)')
    p.add_argument('--backend',choices=['json','sqlite'],default='json')
    sub=p.add_subparsers(dest='cmd',required=True)
    put=sub.add_parser('put');put.add_argument('key');put.add_argument('--input',required=True)
    get=sub.add_parser('get');get.add_argument('key')
    exp=sub.add_parser('export');exp.add_argument('--output',help='Write UTF-8 JSON to this file instead of standard output')
    imp=sub.add_parser('import');imp.add_argument('--input',required=True)
    a=p.parse_args()
    chosen=a.data_dir or os.environ.get('ALIEN_SEARCH_DATA_DIR')
    try:
        root=private_dir(chosen or default_data_dir(),tighten_existing=not chosen)
        store=JsonStore(root/'evidence') if a.backend=='json' else SQLiteStore(root/'evidence.sqlite3')
        if a.cmd=='put': store.put(a.key,read_json_file(a.input))
        elif a.cmd=='get': print_json(store.get(a.key))
        elif a.cmd=='export':
            if a.output: write_export(a.output,store.export())
            else: print_json(store.export())
        else:
            data=read_json_file(a.input)
            if not isinstance(data,dict) or not LEGACY_SCHEMA.fullmatch(str(data.get('schema'))) or not isinstance(data.get('records'),dict): raise ValueError('Invalid portable export')
            store.put_many(data['records'])  # validated in full before anything is written
            print_json({'imported':len(data['records'])})
    except KeyError as e:
        print('No record with ID '+str(e.args[0]),file=sys.stderr); raise SystemExit(1)
    except (ValueError,OSError) as e:
        print('Error: '+str(e),file=sys.stderr); raise SystemExit(1)
if __name__=='__main__': main()
