"""Private SSH file link: sync the garage choice and pull finalized original files.

The camera records locally on its source. This connection carries finished-file
uploads, control acknowledgements and status; it never transports raw live video.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import threading

from .archive_core import GarageGate, atomic_json
from .archive_upload import ArchiveUploadConfig
from .archive_pull import FilePuller

# Executed on the trusted source host by REMOTE_SYNC. This probe only reads
# bounded metadata; neither HTTP path opens a video stream or a camera handle.
REMOTE_CAPTURE_PROBE = r"""
import http.client,math,socket,threading,time,os,stat
from pathlib import Path
from datetime import datetime,timezone

def finite(value,maximum=86400):
 try:return value if type(value) in (int,float) and math.isfinite(value) and 0<=value<=maximum else None
 except OverflowError:return None

def frame_count(value):
 return value if type(value) is int and 0<=value<=2**53-1 else None

def frame_state(frames,age,error=None):
 # Missing/malformed telemetry is uncertainty, not evidence of stopped capture.
 if error not in (None,''):
  return 'not_capturing' if isinstance(error,str) and len(error)<=256 else 'unknown'
 if frames==0 or (age is not None and age>5):return 'not_capturing'
 if frames is not None and frames>0 and age is not None:return 'capturing'
 return 'unknown'

def camera_http_status():
 connection=http.client.HTTPConnection('127.0.0.1',8081,timeout=.8)
 sock=None
 expired=threading.Event()
 def expire():
  expired.set()
  if sock is not None:
   try:sock.shutdown(socket.SHUT_RDWR)
   except OSError:pass
 timer=threading.Timer(1.25,expire);timer.daemon=True;timer.start()
 try:
  connection.connect();sock=connection.sock
  connection.request('GET','/status',headers={'Accept':'application/json','Accept-Encoding':'identity','Connection':'close'})
  response=connection.getresponse()
  if response.status!=200 or response.getheader('Content-Type','').split(';',1)[0].strip().lower()!='application/json':return None
  length=response.getheader('Content-Length')
  if length is not None and (not length.isascii() or not length.isdigit() or int(length)>65536):return None
  body=response.read(65537)
  if len(body)>65536 or expired.is_set():return None
  result=json.loads(body)
  return result if isinstance(result,dict) and isinstance(result.get('cameras'),list) and len(result['cameras'])<=16 else None
 except (OSError,http.client.HTTPException,ValueError,UnicodeError,RecursionError,OverflowError):return None
 finally:
  timer.cancel();connection.close()

def depth_capture_status():
 # A bounded, local metadata file written from actual original Image callbacks.
 # No DDS participant, stream, device handle, or save switch is opened here.
 path=Path.home()/'.local/state/robot-cloud/garage-control/berxel-capture.json'
 try:
  for current in (path.parent,*path.parent.parents):
   info=current.lstat()
   if not stat.S_ISDIR(info.st_mode):return None
  parent=path.parent.stat()
  if parent.st_uid!=os.getuid() or stat.S_IMODE(parent.st_mode)!=0o700:return None
  fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
  with os.fdopen(fd,'rb') as stream:
   info=os.fstat(stream.fileno())
   if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1
       or stat.S_IMODE(info.st_mode)!=0o600 or not 1<=info.st_size<=16384):return None
   raw=stream.read(16385)
  if len(raw)>16384:return None
  report=json.loads(raw)
  if not isinstance(report,dict) or report.get('schemaVersion')!=1:return None
  if report.get('bootId')!=Path('/proc/sys/kernel/random/boot_id').read_text().strip():return None
  pid=report.get('pid')
  if type(pid) is not int or pid<1:return None
  process=Path('/proc')/str(pid)
  if process.stat().st_uid!=os.getuid():return None
  args=(process/'cmdline').read_bytes().decode().split('\0')
  if not args or Path(args[0]).name not in ('python3','python3.10','python3.11','python3.12'):return None
  operands=args[1:]
  while operands and operands[0] in ('-B','-u','-s','-E','-I','-S','-q'):operands=operands[1:]
  expected=Path.home()/'.local/share/rk3588-observer-camera/jetson_camera_encoder.py'
  if not operands or operands[0]!=str(expected):return None
  return report
 except (OSError,ValueError,UnicodeError,RecursionError,OverflowError):return None

def capture_status(recorder,fetch=camera_http_status,now=None,depth=None,monotonic_now=None):
 now=time.time() if now is None else now
 identities={'camera1':'usbLeft','camera2':'usbFront','camera3':'usbHub3','camera4':'usbHub2'}
 rows={key:dict(state='unknown',frames=None,ageS=None) for key in (*identities,'berxelColor','berxelDepth')}
 try:status=fetch()
 except Exception:status=None
 if isinstance(status,dict) and isinstance(status.get('cameras'),list):
  usage=status.get('cameraUsage')
  usage=usage if isinstance(usage,dict) else {}
  seen=set()
  for item in status['cameras']:
   if not isinstance(item,dict) or not isinstance(item.get('id'),str) or item['id'] not in identities:continue
   key=item['id']
   if key in seen:
    rows[key]=dict(state='unknown',frames=None,ageS=None);continue
   seen.add(key)
   frames=frame_count(item.get('frames'));age=finite(item.get('frame_age_ms'),86400000)
   age=None if age is None else age/1000
   state=frame_state(frames,age,item.get('error'))
   rows[key]=dict(state=state,frames=frames,ageS=age)
  for key,usage_key in identities.items():
   if key in seen:continue
   owner=usage.get(usage_key)
   owner=owner if isinstance(owner,dict) else {}
   checked=finite(owner.get('checkedAgeMs'),86400000)
   rows[key]['state']='not_capturing' if owner.get('owner')=='NONE' and type(owner.get('connected')) is bool and checked is not None and checked<6000 else 'unknown'
 # The depth-camera preview exposes a ROS health topic, not an HTTP endpoint.
 # Reuse only fresh original-image receipt evidence from the source recorder;
 # a disabled archive tells us nothing about whether the depth camera runs.
 if isinstance(recorder,dict):
  try:elapsed=now-datetime.fromisoformat(recorder.get('updatedAt','').replace('Z','+00:00')).timestamp()
  except (ValueError,TypeError,AttributeError):elapsed=None
  ros=recorder.get('ros');ros=ros if isinstance(ros,dict) else {}
  topics=ros.get('topics');topics=topics if isinstance(topics,dict) else {}
  if elapsed is not None and -5<=elapsed<20 and recorder.get('state') not in ('stopped','stop_failed'):
   for key,topic in (('berxelColor','/berxel/color/image_raw'),('berxelDepth','/berxel/depth/image_raw')):
    item=topics.get(topic)
    if not isinstance(item,dict) or item.get('state')=='DISABLED':continue
    frames=frame_count(item.get('received'));age=finite(item.get('ageS'))
    age=None if age is None else age+max(0,elapsed)
    state=frame_state(frames,age)
    rows[key]=dict(state=state,frames=frames,ageS=age)
 # Independent source evidence works even while archival subscriptions are off.
 # It proves original Image callbacks; it never claims those frames were saved.
 if isinstance(depth,dict) and type(depth.get('schemaVersion')) is int and depth['schemaVersion']==1:
  current=time.monotonic() if monotonic_now is None else monotonic_now
  stamp=finite(depth.get('reportedMonotonic'),1e12)
  elapsed=None if stamp is None else current-stamp
  channels=depth.get('channels')
  if elapsed is not None and 0<=elapsed<=5 and isinstance(channels,dict):
   for key,channel,topic in (('berxelColor','color','/berxel/color/image_raw'),('berxelDepth','depth','/berxel/depth/image_raw')):
    item=channels.get(channel)
    if not isinstance(item,dict) or item.get('topic')!=topic:continue
    frames=frame_count(item.get('receivedFrames'));age=finite(item.get('sourceAgeS'))
    age=None if age is None else age+elapsed
    rows[key]=dict(state=frame_state(frames,age),frames=frames,ageS=age)
 return dict(updatedAt=datetime.fromtimestamp(now,timezone.utc).isoformat(),cameras=rows)
"""

REMOTE_SYNC = '''
import json,os,sys
from pathlib import Path
from uuid import uuid4
root=Path.home()/'.local/state/robot-cloud/garage-control'
if root.is_symlink() or (root.exists() and not root.is_dir()):raise ValueError('control directory')
root.mkdir(parents=True,exist_ok=True,mode=0o700)
gate=json.loads(sys.stdin.buffer.read(16385))
if gate.get('scope')!='garage' or type(gate.get('enabled')) is not bool: raise ValueError('gate')
if (root/'garage.json').is_symlink():raise ValueError('gate link')
tmp=root/('garage.json.link-tmp-'+uuid4().hex);created=False
try:
 fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);created=True
 with os.fdopen(fd,'w') as f:json.dump(gate,f);f.flush();os.fsync(f.fileno())
 os.replace(tmp,root/'garage.json')
 parent=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(parent)
 finally:os.close(parent)
finally:
 if created:tmp.unlink(missing_ok=True)
def read(name):
 try:
  p=root/name
  if p.is_symlink() or p.stat().st_size>262144:return None
  return json.loads(p.read_text())
 except (OSError,ValueError):return None
''' + REMOTE_CAPTURE_PROBE + '''
recorder=read('status.json')
capture=capture_status(recorder,depth=depth_capture_status())
# A file receipt is not proof that the source's effective gate is still open.
# Probe the same deployed gate implementation used by both source recorders,
# after the bounded health probe, so an expired lease cannot be acknowledged.
effective=None
try:
 sys.path.insert(0,str(Path.home()/'.local/share/robot-cloud'))
 from collector.archive_core import GarageGate
 effective=GarageGate(root/'garage.json').snapshot()
except (ImportError,OSError,ValueError):pass
print(json.dumps(dict(gate=gate,effectiveGate=effective,
 gateObservedAt=datetime.now(timezone.utc).isoformat(),recorder=recorder,
 camera=read('camera-status.json'),capture=capture),allow_nan=False))
'''


def ssh_options(environ):
    host = environ.get('ARCHIVE_SOURCE_HOST', 'nvidia-desktop.local')
    user = environ.get('ARCHIVE_SOURCE_USER', 'nvidia')
    alias = environ.get('ARCHIVE_SOURCE_HOST_KEY_ALIAS', '192.168.1.50')
    for value in (host, user, alias):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]*', value):
            raise ValueError('source identity must be a host or account name')
    key = environ.get('ARCHIVE_SOURCE_KEY', str(Path.home() / '.ssh/id_ed25519'))
    known = environ.get('ARCHIVE_SOURCE_KNOWN_HOSTS', str(Path.home() / '.ssh/known_hosts_jetson_video'))
    if any(not Path(path).is_absolute() for path in (key, known)):
        raise ValueError('source identity paths must be absolute')
    return ['/usr/bin/ssh', '-4', '-F', '/dev/null', '-T', '-o', 'BatchMode=yes',
        '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
        '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no',
        '-o', 'ClearAllForwardings=yes',
        '-o', 'ForwardAgent=no', '-o', 'ForwardX11=no', '-o', 'HostKeyAlgorithms=ssh-ed25519',
        '-o', 'HostKeyAlias=' + alias, '-o', 'UserKnownHostsFile=' + known,
        '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2',
        '-i', key], user + '@' + host


def sync_gate(options, target, control, runner=subprocess.run):
    control = Path(control)
    gate = GarageGate(control / 'garage.json').snapshot()
    gate = dict(schemaVersion=1, scope='garage', enabled=GarageGate._enabled(gate),
                updatedAt=gate.get('updatedAt'), expiresAt=gate.get('expiresAt'),
                **({'desktopSession': gate['desktopSession']} if gate.get('desktopSession') else {}))
    result = runner(options + [target, shlex.join(['/usr/bin/python3', '-c', REMOTE_SYNC])],
                    input=json.dumps(gate).encode(), capture_output=True, timeout=15)
    now = datetime.now(timezone.utc).isoformat()
    if result.returncode:
        reason = 'source_authentication_failed' if b'Permission denied' in result.stderr else 'source_unreachable'
        report = dict(updatedAt=now, connected=False, error=reason, gateAcknowledged=False)
    else:
        if len(result.stdout) > 600 * 1024:
            raise ValueError('source status too large')
        remote = json.loads(result.stdout)
        ack = remote.get('gate', {})
        effective = remote.get('effectiveGate')
        applied = (isinstance(effective, dict)
            and GarageGate._enabled(effective) == gate['enabled']
            and (not gate['enabled'] or (effective.get('updatedAt') == gate['updatedAt']
                and effective.get('desktopSession') == gate.get('desktopSession')
                and effective.get('expiresAt') == gate.get('expiresAt'))))
        recorder = remote.get('recorder')
        recorder_store = recorder.get('store') if isinstance(recorder, dict) else None
        report = dict(updatedAt=now, connected=True, error=None,
            gateAcknowledged=isinstance(ack, dict) and applied
                and ack.get('enabled') == gate['enabled'] and ack.get('updatedAt') == gate['updatedAt']
                and ack.get('desktopSession') == gate.get('desktopSession')
                and ack.get('expiresAt') == gate.get('expiresAt'),
            gateEnabled=GarageGate._enabled(effective) if isinstance(effective, dict) else None,
            gateRevision=ack.get('updatedAt') if isinstance(ack, dict) else None,
            gateObservedAt=remote.get('gateObservedAt'),
            recorderGateEnabled=recorder_store.get('enabled') if isinstance(recorder_store, dict) else None,
            recorder=recorder, camera=remote.get('camera'), capture=remote.get('capture'))
    atomic_json(control / 'source.json', report)
    return report


def main():
    # This daemon can run independently of the desktop capture entry point.
    # Reject a cloned/mismatched identity before any remote garage-gate write.
    from .robot_profile import validate_current_profile
    validate_current_profile()
    control = Path(os.environ['ARCHIVE_CONTROL_DIR'])
    options, target = ssh_options(os.environ)
    stopping = threading.Event()
    for item in (signal.SIGTERM, signal.SIGINT):
        signal.signal(item, lambda *_: stopping.set())
    config = ArchiveUploadConfig.from_environment(os.environ.get('ARCHIVE_UPLOAD_CONFIG'))
    puller = FilePuller(options, target, os.environ['ARCHIVE_ROOT'], config, stopping=stopping,
        min_free_bytes=int(os.environ.get('ARCHIVE_MIN_FREE_BYTES', 10 * 1024**3)),
        max_spool_bytes=int(os.environ.get('ARCHIVE_MAX_SPOOL_BYTES', 20 * 1024**3)))
    worker = threading.Thread(target=puller.run, name='archive-source-file-pull', daemon=True)
    worker.start()
    try:
        while not stopping.is_set():
            try:
                report = sync_gate(options, target, control)
                report['fileTransfer'] = puller.snapshot()
                atomic_json(control / 'source.json', report)
            except (OSError, ValueError, subprocess.SubprocessError):
                atomic_json(control / 'source.json', dict(updatedAt=datetime.now(timezone.utc).isoformat(),
                    connected=False, gateAcknowledged=False, error='source_unreachable', fileTransfer=puller.snapshot()))
            stopping.wait(5)
    finally:
        stopping.set()
        worker.join(timeout=3)


if __name__ == '__main__':
    main()
