"""Transfer finalized source files over existing SSH; no source database listener."""
from __future__ import annotations
from datetime import datetime, timezone
from contextlib import contextmanager
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import stat
import threading
import time
from uuid import UUID
from zoneinfo import ZoneInfo

from .archive_core import atomic_json, spool_size
from .archive_policy import (allowed_archive_manifest, allowed_manifest_path, camera_only_enabled,
                             training_only_enabled, training_manifest_allowed)
from .archive_upload import (ArchiveUploadClient, ArchiveUploadConfig, ArchiveUploadError,
                             MAX_FILE_BYTES, valid_file_size)

MAX_MANIFEST = 256 * 1024
MAX_FILE = MAX_FILE_BYTES  # Same exact-integer wire bound as the warehouse uploader.
BLOCK = 128 * 1024


def source_descriptor(descriptor, robot_id):
    """Validate a frozen source manifest before trusting a local cached ACK.

    No files are opened here. The same path may refer to a repaired or replaced
    recording, so a local pathname alone is never sufficient source evidence.
    """
    from .archive_upload import entry_name
    try:
        if not isinstance(descriptor, dict):
            raise ValueError()
        checksum = descriptor.get('sha256')
        encoded = descriptor.get('manifest')
        if (not isinstance(checksum, str) or re.fullmatch(r'[a-f0-9]{64}', checksum) is None
                or not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_MANIFEST + 2) // 3)):
            raise ValueError()
        raw = base64.b64decode(encoded, validate=True)
        if not 0 < len(raw) <= MAX_MANIFEST or hashlib.sha256(raw).hexdigest() != checksum:
            raise ValueError()
        value = json.loads(raw)
        if (not isinstance(value, dict) or type(value.get('schemaVersion')) is not int
                or value['schemaVersion'] != 1 or not isinstance(value.get('metadata'), dict)
                or value['metadata'].get('scope') != 'garage'):
            raise ValueError()
        identity = str(UUID(value.get('robotId', '')))
        expected = str(UUID(robot_id))
        if identity != expected:
            raise ArchiveUploadError('ARCHIVE_ROBOT_IDENTITY_MISMATCH')
        parts = [entry_name(value[name]) for name in ('day', 'sensor', 'label', 'id')]
        if descriptor.get('path') != '/'.join(parts):
            raise ValueError()
        if datetime.strptime(parts[0], '%Y-%m-%d').strftime('%Y-%m-%d') != parts[0]:
            raise ValueError()
        started = datetime.fromisoformat(value['startedAt'].replace('Z', '+00:00'))
        ended = datetime.fromisoformat(value['endAt'].replace('Z', '+00:00'))
        if (started.tzinfo is None or ended.tzinfo is None or ended < started
                or started.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d') != parts[0]):
            raise ValueError()
        files = value.get('files')
        if (not isinstance(files, list) or not 1 <= len(files) <= 4096
                or any(not isinstance(name, str) for name in files)
                or len(set(files)) != len(files)):
            raise ValueError()
        for name in files:
            path = Path(name)
            if not path.parts or path.is_absolute() or str(path) != name or '\\' in name:
                raise ValueError()
            for part in path.parts:
                entry_name(part)
        return raw, value
    except ArchiveUploadError as error:
        if error.code == 'ARCHIVE_ROBOT_IDENTITY_MISMATCH':
            raise
        raise ArchiveUploadError('ARCHIVE_SOURCE_SNAPSHOT_INVALID') from None
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise ArchiveUploadError('ARCHIVE_SOURCE_SNAPSHOT_INVALID') from None


def transfer_pause(received, started, rate, *, clock=time.monotonic, wait):
    """Pace finished-file transfer; leave the sensor network room to operate."""
    delay = max(0, received / rate - (clock() - started)) if rate else 0
    return wait(delay) if delay else False

# This fixed command accepts only paths rooted in the source's archive days/.
# No database credentials, listening socket, port forwarding or live image stream.
REMOTE_FILES = r'''
import base64,hashlib,json,os,re,stat,sys
from pathlib import Path
from uuid import uuid4
root=Path.home()/'.local/share/robot-native-archive'
days=root/'days'
MAX_FILE=(1<<53)-1
def valid_file_size(value):
 return type(value) is int and 0<value<=MAX_FILE
def emit(value):
 sys.stdout.buffer.write(json.dumps(value).encode()+b'\n');sys.stdout.buffer.flush()
def safe(relative):
 path=Path(relative)
 if path.is_absolute() or str(path)!=relative or not path.parts or any(p in ('.','..') or '\\' in p or len(p.encode())>200 or any(ord(c)<32 for c in p) for p in path.parts):raise ValueError('path')
 current=days
 if root.is_symlink() or days.is_symlink():raise ValueError('root link')
 for part in path.parts:
  current=current/part
  if current.is_symlink():raise ValueError('link')
 return current
def digest(path):
 h=hashlib.sha256();size=0
 with path.open('rb') as f:
  for data in iter(lambda:f.read(131072),b''):h.update(data);size+=len(data)
 return h.hexdigest(),size
def transfer_header(stream,name,value):
 # New sealed videos already contain the verified output digest. Reuse it only
 # when the metadata exactly names this sole video and its actual file size.
 # The receiver still hashes every streamed byte before publishing the copy.
 before=os.fstat(stream.fileno())
 if (not stat.S_ISREG(before.st_mode) or before.st_uid!=os.geteuid()
     or before.st_nlink!=1 or not valid_file_size(before.st_size)):raise ValueError('file identity or size')
 metadata=value.get('metadata');metadata=metadata if isinstance(metadata,dict) else {}
 known=metadata.get('videoSha256');declared=metadata.get('videoBytes')
 if (name=='video.mkv' and value.get('files')==['video.mkv'] and metadata.get('videoReady') is True
     and isinstance(known,str) and re.fullmatch(r'[a-f0-9]{64}',known)
     and valid_file_size(declared) and declared==before.st_size):
  return before,known,declared
 # Historical/non-video files retain streamed hashing, then rewind this same
 # pinned descriptor; do not reopen a path between verification and transfer.
 h=hashlib.sha256();size=0
 for data in iter(lambda:stream.read(131072),b''):h.update(data);size+=len(data)
 after=os.fstat(stream.fileno())
 if size!=before.st_size or fingerprint(before)!=fingerprint(after):raise ValueError('source changed')
 stream.seek(0)
 return before,h.hexdigest(),size

def source_files(value):
 metadata=value.get('metadata',{})
 if not isinstance(metadata,dict):return []
 video=metadata.get('videoReady') is True;bag=metadata.get('bagCompressionReady') is True
 if not video and not bag:return []
 rows=metadata.get('sourceFiles');outputs=value.get('files')
 valid=outputs==['video.mkv'] if video and not bag else (bag and not video and isinstance(outputs,list) and len(outputs)>=2 and 'compressed-bag/metadata.yaml' in outputs and len(set(outputs))==len(outputs) and all(isinstance(n,str) and re.fullmatch(r'compressed-bag/(?:metadata\.yaml|[A-Za-z0-9_-]+\.db3\.zst)',n) for n in outputs))
 if not valid or not isinstance(rows,list) or not 1<=len(rows)<=4096:raise ValueError('source files')
 result=[];seen=set()
 for row in rows:
  if (not isinstance(row,dict) or set(row)!={'relativePath','sha256','size'}
      or not isinstance(row.get('relativePath'),str)
      or not re.fullmatch(r'(?:frames\.mjpg|index\.jsonl|bag/(?:metadata\.yaml|[A-Za-z0-9_-]+\.db3))' if video else r'bag/(?:metadata\.yaml|[A-Za-z0-9_-]+\.db3)',row['relativePath'])
      or row['relativePath'] in seen or not isinstance(row.get('sha256'),str)
      or not re.fullmatch(r'[a-f0-9]{64}',row['sha256'])
      or type(row.get('size')) is not int or row['size']<0):raise ValueError('source files')
  seen.add(row['relativePath']);result.append(dict(row))
 return result
def parent(directory,name):
 relative=Path(name)
 if not relative.parts or str(relative)!=name or relative.is_absolute() or '\\' in name or any(p in ('.','..') or any(ord(c)<32 for c in p) for p in relative.parts):raise ValueError('file path')
 descriptor=None
 try:
  descriptor=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
  for part in relative.parts[:-1]:
   following=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
   os.close(descriptor);descriptor=following
  return descriptor,relative.name
 except BaseException:
  if descriptor is not None:os.close(descriptor)
  raise
def fingerprint(info):return info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns
def verified(directory,row,missing_ok=False):
 descriptor=fd=None
 try:
  descriptor,name=parent(directory,row['relativePath'])
  fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=descriptor);before=os.fstat(fd)
  if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.geteuid() or before.st_nlink!=1:raise ValueError('file identity')
  h=hashlib.sha256();size=0
  with os.fdopen(fd,'rb') as f:
   fd=None
   for data in iter(lambda:f.read(131072),b''):h.update(data);size+=len(data)
   after=os.fstat(f.fileno())
  if (h.hexdigest(),size)!=(row.get('sha256'),row.get('size')) or fingerprint(before)!=fingerprint(after):raise ValueError('ack or source mismatch')
  return fingerprint(after)
 except FileNotFoundError:
  if missing_ok:return None
  raise
 finally:
  if fd is not None:os.close(fd)
  if descriptor is not None:os.close(descriptor)
def unlink_verified(directory,name,expected):
 if expected is None:return
 descriptor=None
 try:
  descriptor,leaf=parent(directory,name);info=os.stat(leaf,dir_fd=descriptor,follow_symlinks=False)
  if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or fingerprint(info)!=expected:raise ValueError('source changed before prune')
  os.unlink(leaf,dir_fd=descriptor);os.fsync(descriptor)
 finally:
  if descriptor is not None:os.close(descriptor)
def manifest(relative):
 directory=safe(relative);p=directory/'archive.json'
 if p.is_symlink() or not p.is_file() or p.stat().st_size>262144:raise ValueError('manifest')
 raw=p.read_bytes();value=json.loads(raw)
 if value.get('schemaVersion')!=1 or value.get('metadata',{}).get('scope')!='garage' or not isinstance(value.get('files'),list) or not 1<=len(value['files'])<=4096:raise ValueError('manifest scope')
 if list(Path(relative).parts)!=[value['day'],value['sensor'],value['label'],value['id']]:raise ValueError('manifest path')
 for name in value['files']:
  target=safe(relative+'/'+name)
  if directory not in target.parents:raise ValueError('file path')
 return directory,raw,value
request=json.loads(sys.stdin.buffer.read(1048577));op=request.get('op')
camera_only=request.get('cameraOnly',False)
if type(camera_only) is not bool:raise ValueError('archive policy')
training_only=request.get('trainingOnly',False)
if type(training_only) is not bool:raise ValueError('archive policy')
if camera_only or training_only:
 sys.path.insert(0,str(Path.home()/'.local/share/robot-cloud'))
 from collector.archive_policy import allowed_archive_manifest as _allowed_archive_manifest,training_manifest_allowed
 def allowed_archive_manifest(value,camera_only=False):return _allowed_archive_manifest(value,camera_only,training_only) and (not training_only or training_manifest_allowed(value))
else:
 def allowed_archive_manifest(value,camera_only=False):return True
if op in ('list','snapshot'):
 result=[]
 invalid_segments=0
 limit=4096 if op=='snapshot' else 16
 truncated=False
 if days.exists():
  for p in days.rglob('archive.json'):
   if (p.parent/'uploaded.json').exists():continue
   relative=str(p.parent.relative_to(days))
   try:
    directory,raw,value=manifest(relative)
    if not allowed_archive_manifest(value,camera_only):continue
    if (directory/'uploaded.json').exists():continue
    if len(result)>=limit:truncated=True;break
    result.append(dict(path=relative,manifest=base64.b64encode(raw).decode(),sha256=hashlib.sha256(raw).hexdigest()))
   except (OSError,ValueError,KeyError,TypeError,AttributeError,RecursionError):
    invalid_segments+=1
 emit(dict(segments=result,truncated=truncated,invalidSegments=invalid_segments) if op=='snapshot' else result)
elif op=='read':
 directory,raw,value=manifest(request['segment']);name=request['file']
 if not allowed_archive_manifest(value,camera_only):raise ValueError('archive policy')
 if name not in value['files']:raise ValueError('undeclared file')
 p=safe(request['segment']+'/'+name)
 fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 with os.fdopen(fd,'rb') as f:
  before,sha,size=transfer_header(f,name,value)
  emit(dict(size=size,sha256=sha))
  for data in iter(lambda:f.read(131072),b''):sys.stdout.buffer.write(data)
  sys.stdout.buffer.flush()
  after=os.fstat(f.fileno())
  if fingerprint(before)!=fingerprint(after) or fingerprint(before)!=fingerprint(os.stat(p,follow_symlinks=False)):raise ValueError('source changed')
elif op=='ack':
 directory,raw,value=manifest(request['segment']);ack=request['ack']
 if not allowed_archive_manifest(value,camera_only):raise ValueError('archive policy')
 if ack.get('state')!='complete' or ack.get('manifestSha256')!=hashlib.sha256(raw).hexdigest() or ack.get('segmentId')!=value['id']:raise ValueError('ack')
 rows={row['relativePath']:row for row in ack['files']}
 if set(rows)!=set(value['files']):raise ValueError('ack files')
 prune=request.get('prune') is True
 sources=source_files(value) if prune else []
 marker=directory/'local-copy-pruned.json';resume=False
 if marker.exists():
  if marker.is_symlink():raise ValueError('marker link')
  old=json.loads(marker.read_text())
  if old.get('state') not in ('pruning','pruned'):raise ValueError('prune checkpoint state')
  if old.get('files')!=ack['files']:raise ValueError('prune checkpoint mismatch')
  if prune and sources and old.get('sourceFiles')!=sources:raise ValueError('source prune checkpoint mismatch')
  resume=old.get('state') in ('pruning','pruned')
  if old.get('state')=='pruned':emit(dict(acknowledged=True,pruned=True));sys.exit(0)
 checked={name:verified(directory,row,resume) for name,row in rows.items()}
 for row in sources:checked[row['relativePath']]=verified(directory,row,True)
 def write(path,value):
  if path.is_symlink():raise ValueError('state link')
  tmp=path.with_name(path.name+'.ssh-tmp-'+uuid4().hex);created=False
  try:
   fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);created=True
   with os.fdopen(fd,'w') as f:json.dump(value,f);f.flush();os.fsync(f.fileno())
   os.replace(tmp,path)
   parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
   try:os.fsync(parent)
   finally:os.close(parent)
  finally:
   if created:tmp.unlink(missing_ok=True)
 if prune:
  metadata=value.get('metadata',{})
  cache_needed=(value.get('files')==['video.mkv'] and metadata.get('videoReady') is True
   and (metadata.get('source')=='native-camera-jpeg' or (metadata.get('source')=='native-ros-cdr' and value.get('sensor') in ('Berxel深度','Berxel彩色')))
   and isinstance(metadata.get('captureSessionId'),str) and re.fullmatch(r'[a-f0-9]{32}',metadata['captureSessionId'])
   and isinstance(metadata.get('clockDomain'),str) and re.fullmatch(r'linux-boot:[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',metadata['clockDomain'])
   and metadata.get('clockSource')=='CLOCK_REALTIME'
   and metadata.get('timestampSemantics')==('host_read_complete' if metadata.get('source')=='native-camera-jpeg' else 'host_subscription_receive'))
  if cache_needed:
   sys.path.insert(0,str(Path.home()/'.local/share/robot-cloud'))
   from collector.archive_frame_index_cache import preserve_frame_time_index
   preserve_frame_time_index(directory,value)
 write(directory/'uploaded.json',ack)
 if prune:
  state=dict(state='pruning',files=ack['files'])
  if sources:state['sourceFiles']=sources
  write(marker,state)
  for name,expected in checked.items():unlink_verified(directory,name,expected)
  state['state']='pruned';write(marker,state)
 emit(dict(acknowledged=True,pruned=prune))
else:raise ValueError('operation')
'''


class _IdleReadWatchdog:
    """Bound read stalls without imposing a total-size or total-time ceiling.

    One timer samples the latest deadline. Progress only updates that deadline,
    so a fast transfer does not create a thread for every received block.
    Expiration, progress and cancellation share a lock; a cancelled late timer
    cannot kill the process after its owner has finished with it.
    """
    def __init__(self, process, seconds=180, *, clock=time.monotonic, timer_factory=None):
        self.process, self.seconds, self.clock = process, seconds, clock
        self.timer_factory = timer_factory or threading.Timer
        self.lock = threading.Lock()
        self.closed = False
        self.deadline = clock() + seconds
        self.timer = None
        self._schedule(seconds)

    def _schedule(self, delay):
        self.timer = self.timer_factory(delay, self._check)
        self.timer.daemon = True
        self.timer.start()

    def _check(self):
        with self.lock:
            if self.closed:
                return
            remaining = self.deadline - self.clock()
            if remaining > 0:
                self._schedule(remaining)
                return
            self.closed = True
            try:
                self.process.kill()
            except ProcessLookupError:
                pass

    def touch(self):
        with self.lock:
            if self.closed:
                return False
            self.deadline = self.clock() + self.seconds
            return True

    def cancel(self):
        with self.lock:
            self.closed = True
            if self.timer is not None:
                self.timer.cancel()


class FilePuller:
    def __init__(self, options, target, root, config, *, stopping=None, popen=subprocess.Popen,
                 min_free_bytes=10 * 1024**3, max_spool_bytes=20 * 1024**3, retain_seconds=300, camera_only=None,
                 bytes_per_second=None, training_only=None):
        self.options, self.target, self.root = options, target, Path(root)
        self.config, self.popen = config, popen
        if camera_only is not None and type(camera_only) is not bool:
            raise ValueError('camera_only must be a boolean')
        self.camera_only = camera_only_enabled() if camera_only is None else camera_only
        if training_only is not None and type(training_only) is not bool:
            raise ValueError('training_only must be a boolean')
        self.training_only = training_only_enabled() if training_only is None else training_only
        self.stopping = stopping or threading.Event()
        self.bytes_per_second = int(os.environ.get('ARCHIVE_TRANSFER_BYTES_PER_SECOND', '0')) if bytes_per_second is None else bytes_per_second
        if type(self.bytes_per_second) is not int or self.bytes_per_second < 0:
            raise ValueError('archive transfer rate must be a nonnegative integer')
        self.min_free_bytes, self.max_spool_bytes, self.retain_seconds = min_free_bytes, max_spool_bytes, retain_seconds
        self.client = ArchiveUploadClient(config)
        self.lock = threading.Lock()
        self.snapshot_invalid_segments = 0
        self.status = dict(mode='ssh_file_pull', pulledSegments=0, lastError=None, sourceCopiesPruned=0,
                           localAckMetadataErrors=0)

    def snapshot(self):
        with self.lock:return dict(self.status)

    def _safe_directory(self, *parts):
        current=self.root
        for part in (None,*parts):
            if part is not None:current=current/part
            if current.is_symlink() or (current.exists() and not current.is_dir()):
                raise ValueError('local directory link or non-directory')
        return current

    def _process(self, request):
        process = self.popen(self.options + [self.target, shlex.join([
            '/usr/bin/nice', '-n', '15', '/usr/bin/ionice', '-c', '3',
            '/usr/bin/python3', '-c', REMOTE_FILES])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        process.stdin.write(json.dumps({**request, 'cameraOnly': self.camera_only,
                                       'trainingOnly': self.training_only}).encode());process.stdin.close()
        if request.get('op') == 'read':
            timer = _IdleReadWatchdog(process)
        else:
            # Listings/acknowledgements remain short bounded metadata calls.
            timer=threading.Timer(180,process.kill);timer.daemon=True;timer.start()
        return process,timer

    @staticmethod
    def _finish(process, timer):
        try:
            if process.wait(timeout=5):raise ValueError('source transfer failed')
        finally:timer.cancel()

    def _json(self, request):
        process,timer=self._process(request)
        try:
            data=process.stdout.read(8 * 1024**2 + 1)
            if len(data)>8 * 1024**2:raise ValueError('source listing too large')
            self._finish(process,timer)
            return json.loads(data)
        finally:
            timer.cancel()
            if process.poll() is None:process.kill();process.wait()

    def _receive(self, segment, relative, destination):
        process,timer=self._process(dict(op='read',segment=segment,file=relative))
        temporary=destination.with_name(destination.name+'.part')
        try:
            raw=process.stdout.readline(16385)
            if len(raw)>16384 or not raw.endswith(b'\n'):raise ValueError('source header')
            header=json.loads(raw);size=header['size']
            if not valid_file_size(size):raise ValueError('source size')
            if not timer.touch():raise ValueError('source transfer timed out')
            if destination.is_symlink() or temporary.is_symlink():raise ValueError('local link')
            if destination.is_file():
                try:
                    existing,existing_size,_=self.client._digest(destination)
                    if existing_size==size and existing==header['sha256']:
                        process.kill();process.wait();return
                except ArchiveUploadError:
                    pass  # A derived incomplete staging copy can be re-fetched.
            partial_size=temporary.stat().st_size if temporary.exists() else 0
            if (shutil.disk_usage(self.root).free-max(0,size-partial_size)<=self.min_free_bytes
                    or spool_size(self.root)-partial_size+size>self.max_spool_bytes):raise OSError('archive spool full')
            digest=hashlib.sha256();received=0
            started=time.monotonic()
            destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            if destination.is_symlink() or temporary.is_symlink():raise ValueError('local link')
            with temporary.open('wb') as out:
                os.chmod(temporary,0o600)
                while received<size:
                    if self.stopping.is_set():raise InterruptedError('archive stopping')
                    data=process.stdout.read(min(BLOCK,size-received))
                    if not data:raise ValueError('truncated source file')
                    if not timer.touch():raise ValueError('source transfer timed out')
                    out.write(data);digest.update(data);received+=len(data)
                    if transfer_pause(received, started, self.bytes_per_second, wait=self.stopping.wait):
                        raise InterruptedError('archive stopping')
                out.flush();os.fsync(out.fileno())
            if process.stdout.read(1) or digest.hexdigest()!=header['sha256']:raise ValueError('source checksum')
            self._finish(process,timer)
            os.replace(temporary,destination)
            descriptor=os.open(destination.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
        finally:
            timer.cancel()
            if process.poll() is None:process.kill();process.wait()

    @contextmanager
    def _transfer_lock(self):
        """Share one staging writer between the daemon and foreground uploader."""
        self._safe_directory()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.root / '.source-pull.lock',
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1):
                raise ValueError('source transfer lock invalid')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True
        finally:
            os.close(descriptor)

    def sealed_snapshot(self):
        """Freeze source work without changing its gate or acknowledging files."""
        value = self._json(dict(op='snapshot'))
        invalid = value.get('invalidSegments', 0) if isinstance(value, dict) else None
        if (not isinstance(value, dict) or not isinstance(value.get('segments'), list)
                or len(value['segments']) > 4096 or value.get('truncated') is not False):
            raise ValueError('source snapshot exceeds bounded scope')
        if type(invalid) is not int or not 0 <= invalid <= 2**31 - 1:
            raise ValueError('source snapshot diagnostics invalid')
        self.snapshot_invalid_segments = invalid
        return value['segments']

    def _pull(self, descriptor):
        with self._transfer_lock() as acquired:
            if not acquired:
                return False
            self._pull_unlocked(descriptor)
            return True

    def _pull_unlocked(self, descriptor):
        raw=base64.b64decode(descriptor['manifest'],validate=True)
        if len(raw)>MAX_MANIFEST or hashlib.sha256(raw).hexdigest()!=descriptor['sha256']:raise ValueError('manifest checksum')
        value=json.loads(raw)
        if (not allowed_archive_manifest(value, self.camera_only, self.training_only)
                or self.training_only and not training_manifest_allowed(value)):return
        parts=[value['day'],value['sensor'],value['label'],value['id']]
        from .archive_upload import entry_name
        for part in parts:entry_name(part)
        relative='/'.join(parts)
        if relative!=descriptor['path'] or value.get('robotId')!=self.config.robot_id or value.get('metadata',{}).get('scope')!='garage':raise ValueError('source scope')
        target=self._safe_directory('days',*parts)
        if target.exists():
            path=target/'archive.json'
            if path.is_symlink() or not path.is_file() or path.stat().st_size>MAX_MANIFEST:
                raise ValueError('existing segment manifest invalid')
            with path.open('rb') as stream:existing=stream.read(MAX_MANIFEST+1)
            if len(existing)>MAX_MANIFEST or hashlib.sha256(existing).hexdigest()!=descriptor['sha256']:raise ValueError('segment collision')
            return
        staging=self.root/'.incoming'/value['id']
        current=self.root
        for part in ('.incoming',value['id']):
            current=current/part
            if current.is_symlink():raise ValueError('staging link')
            current.mkdir(exist_ok=True,mode=0o700)
        for filename in value['files']:
            path=Path(filename)
            if str(path)!=filename or path.is_absolute() or any(p in ('.','..') for p in path.parts) or '\\' in filename:raise ValueError('file path')
            for part in path.parts:entry_name(part)
            current=staging
            for part in path.parts:
                current=current/part
                if current.is_symlink():raise ValueError('file link')
            self._receive(relative,filename,staging/path)
        manifest_path=staging/'archive.json'
        if manifest_path.is_symlink():raise ValueError('manifest link')
        with manifest_path.open('wb') as out:
            os.chmod(manifest_path,0o600);out.write(raw);out.flush();os.fsync(out.fileno())
        self.client._manifest(staging)
        atomic_json(staging/'.source-pull.json',dict(path=relative,manifestSha256=descriptor['sha256']))
        current=self.root/'days';current.mkdir(exist_ok=True,mode=0o700)
        if current.is_symlink():raise ValueError('days link')
        for part in parts[:-1]:
            current=current/part
            if current.is_symlink():raise ValueError('target link')
            current.mkdir(exist_ok=True,mode=0o700)
        staging.rename(target)
        descriptor=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
        with self.lock:self.status['pulledSegments']+=1

    def _local_acknowledgement(self, source):
        """Validate one local checkpoint before any remote call is attempted."""
        segment=source.parent
        self._safe_directory(*segment.relative_to(self.root).parts)
        if not allowed_manifest_path(segment/'archive.json', self.camera_only):return None
        ack_path=segment/'uploaded.json';done=segment/'source-ack.json'
        if not ack_path.is_file() or ack_path.is_symlink() or source.is_symlink():return None
        if done.is_symlink():raise ValueError('source acknowledgement link')
        if done.exists():
            checkpoint=json.loads(done.read_text())
            if not isinstance(checkpoint,dict):raise ValueError('local acknowledgement checkpoint')
            if checkpoint.get('pruned') is True:return None
        descriptor=json.loads(source.read_text());ack=json.loads(ack_path.read_text())
        if not isinstance(descriptor,dict) or not isinstance(ack,dict):raise ValueError('local acknowledgement object')
        if ack.get('state')!='complete' or ack.get('manifestSha256')!=descriptor['manifestSha256']:raise ValueError('local ack mismatch')
        relative=descriptor['path']
        if not isinstance(relative,str) or relative!=str(segment.relative_to(self.root/'days')):raise ValueError('local source path')
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(ack['completedAt'])).total_seconds()
        manifest_path=segment/'archive.json'
        if manifest_path.is_symlink():raise ValueError('local manifest link')
        raw=manifest_path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=ack['manifestSha256']:raise ValueError('local manifest changed')
        manifest=json.loads(raw)
        if not isinstance(manifest,dict):raise ValueError('local manifest object')
        identity=manifest.get('robotId');configured=self.config.robot_id
        if (not isinstance(identity,str) or not isinstance(configured,str)
                or str(UUID(identity))!=identity or str(UUID(configured))!=configured
                or identity!=configured):raise ValueError('local source robot identity')
        if ack.get('segmentId')!=manifest.get('id') or manifest.get('id')!=segment.name:raise ValueError('local segment identity')
        declared=manifest.get('files')
        if (not isinstance(declared,list) or not declared or any(not isinstance(name,str) for name in declared)
                or len(set(declared))!=len(declared)):raise ValueError('local manifest files')
        metadata=manifest.get('metadata',{})
        compact=isinstance(metadata,dict) and metadata.get('archiveProfile')=='compact-v1' and (metadata.get('videoReady') is True or metadata.get('bagCompressionReady') is True)
        # Prepare API arguments while still inside local metadata validation.
        # A missing/bad row must not escape later and block unrelated segments.
        checks=[]
        if not isinstance(ack.get('files'),list) or not ack['files']:raise ValueError('local acknowledgement files')
        names=set()
        for row in ack['files']:
            if (not isinstance(row,dict) or not isinstance(row.get('sha256'),str)
                    or len(row['sha256'])!=64 or any(c not in '0123456789abcdef' for c in row['sha256'])
                    or not valid_file_size(row.get('size'))):raise ValueError('local acknowledgement file')
            relative_name=row['relativePath']
            if not isinstance(relative_name,str):raise ValueError('local acknowledgement path')
            name=Path(relative_name)
            if (not name.parts or str(name)!=relative_name or name.is_absolute() or '\\' in relative_name
                    or any(part in ('.','..') or any(ord(c)<32 for c in part) for part in name.parts)
                    or relative_name in names):raise ValueError('local acknowledgement path')
            names.add(relative_name)
            folder='/'.join((ack['folderPath'],*name.parts[:-1]))
            checks.append((row['robotFileId'],row,ack['datasetId'],folder,name.name))
        if names!=set(declared):raise ValueError('local acknowledgement file set')
        return done,relative,ack,age>=(0 if compact else self.retain_seconds),checks

    def _acknowledge(self):
        days=self._safe_directory('days')
        with self.lock:
            self.status['localAckMetadataErrors']=0
            if self.status['lastError']=='local_ack_metadata_invalid':self.status['lastError']=None
        for source in days.rglob('.source-pull.json'):
            if self.stopping.is_set():return
            try:
                prepared=self._local_acknowledgement(source)
            except (OSError,ValueError,KeyError,TypeError):
                with self.lock:
                    self.status['localAckMetadataErrors']+=1
                    self.status['lastError']='local_ack_metadata_invalid'
                continue
            if prepared is None:continue
            done,relative,ack,eligible,checks=prepared
            prune=False
            if eligible:
                try:
                    for arguments in checks:self.client._confirmed_file(*arguments)
                    prune=True
                except ArchiveUploadError:pass
            # Transport/source verification failures retain the global backoff;
            # only malformed local checkpoint files are isolated above.
            result=self._json(dict(op='ack',segment=relative,ack=ack,prune=prune))
            if result.get('acknowledged') is not True:raise ValueError('source ack rejected')
            atomic_json(done,result)
            if result.get('pruned'):
                with self.lock:self.status['sourceCopiesPruned']+=1

    def once(self):
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self._acknowledge()
        descriptors=self._json(dict(op='list'))
        if not isinstance(descriptors,list) or len(descriptors)>16:raise ValueError('source list')
        for descriptor in descriptors:
            if self.stopping.is_set():return
            self._pull(descriptor)

    def run(self):
        while not self.stopping.is_set():
            try:
                self.once()
                with self.lock:
                    self.status['lastError']='local_ack_metadata_invalid' if self.status['localAckMetadataErrors'] else None
            except Exception as error:
                with self.lock:self.status['lastError']=type(error).__name__
            self.stopping.wait(5)
