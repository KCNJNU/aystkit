#!/usr/bin/env python3
"""Generate private robot enrollment files or validate a cloud configuration merge."""
from __future__ import annotations
import argparse,ipaddress,json,os,re,secrets,shlex,stat,sys
from pathlib import Path
from uuid import UUID,uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from cloud.backend.security import Config
from collector.robot_profile import literal_environment
from shared.protocol import IDENTIFIER,MEDIA_KEYS


def private_json(path,data):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:json.dump(data,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())


def generate(args):
    if not IDENTIFIER.fullmatch(args.observer_id) or len(args.observer_id)>32:raise ValueError('invalid observer ID')
    warehouse_id=str(UUID(args.warehouse_id))
    if not args.name.strip() or not 0<=args.domain<=101:raise ValueError('invalid robot name/domain')
    host=args.jetson_host
    if (not isinstance(host,str) or host.lower() in ('auto','localhost') or host.lower().endswith('.localhost') or len(host)>253
            or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?',host)
            or any(not label or len(label)>63 or label.startswith('-') or label.endswith('-') for label in host.split('.'))):
        raise ValueError('explicit Jetson hostname or IPv4 required')
    try:address=ipaddress.ip_address(host)
    except ValueError:
        if re.fullmatch(r'[0-9.]+',host):raise ValueError('invalid Jetson IPv4') from None
        address=None
    if address is not None and (address.version!=4 or address.is_loopback or address.is_unspecified or address.is_multicast):
        raise ValueError('Jetson must be a remote IPv4 source or hostname')
    args.output.mkdir(mode=0o700,parents=True,exist_ok=False)
    token=secrets.token_urlsafe(48);home=Path.home();control=home/'.local/state/robot-cloud/robots'/args.observer_id/'garage-control'
    profile={'schemaVersion':1,'observerId':args.observer_id,'warehouseId':warehouse_id,
             'name':args.name,'rosDomainId':args.domain,'machineId':'','jetsonHost':host,
             'cloudSocketDir':'/var/lib/robot-video-tunnel/preview/robots/'+args.observer_id}
    private_json(args.output/'robot.json.template',profile)
    private_json(args.output/'cloud-entry.json',{'observerId':args.observer_id,'name':args.name,'warehouseId':warehouse_id,
        'nativeVideoSocket':'/run/native-preview/robots/'+args.observer_id+'/jetson.sock',
        'devices':[{'robot_id':args.observer_id,'source_id':'ipc','role':'primary','token':token,'media_keys':sorted(MEDIA_KEYS)}]})
    private_json(args.output/'archive-upload.json',{'robotId':warehouse_id,'email':'','password':'',
        'apiUrl':'http://127.0.0.1:13000/api','datasetPrefix':'地库_'+args.observer_id+'_','uploadBytesPerSecond':1048576})
    # Install this marker alongside archive.env before starting a new vehicle.
    # Its absence means legacy local-warehouse mode to the startup entry point.
    private_json(args.output/'warehouse-cloud.json',{'schemaVersion':1,'mode':'cloud',
        'apiUrl':'http://127.0.0.1:13000/api','publicUrl':'https://aystkit.cn/dbprog/',
        'unit':'robot-warehouse-cloud-api-tunnel.service'})
    files={
        'collector.env':{'ROBOT_ID':args.observer_id,'SOURCE_ID':'ipc','DEVICE_TOKEN':token,'CLOUD_URL':'https://aystkit.cn',
                         'ROS_DOMAIN_ID':str(args.domain),'COLLECTOR_BERXEL_TRANSPORT':'bridge'},
        'archive.env':{'ARCHIVE_ROBOT_ID':warehouse_id,'ARCHIVE_ROOT':str(home/'.local/share/robot-native-archive'/args.observer_id),
                       'ARCHIVE_CONTROL_DIR':str(control),'ARCHIVE_UPLOAD_CONFIG':str(home/'.config/robot-cloud/archive-upload.json'),
                       'ROS_DOMAIN_ID':str(args.domain),'ARCHIVE_CAMERA_ONLY':'0','ARCHIVE_TRAINING_ONLY':'1',
                       'ARCHIVE_ROS_SENSORS':'雷达MID360,二维雷达scan,二维雷达蓝海,定位与TF',
                       'ARCHIVE_DEPTH_VIDEO_MODE':'lossless','ARCHIVE_VIDEO_FPS':'10',
                       'ARCHIVE_CAMERA_SEGMENT_BYTES':'134217728','ARCHIVE_MIN_FREE_BYTES':'10737418240',
                       'ARCHIVE_MAX_SPOOL_BYTES':'21474836480','ARCHIVE_SOURCE_HOST':host,
                       'ARCHIVE_SOURCE_HOST_KEY_ALIAS':host,'ARCHIVE_SOURCE_USER':'nvidia',
                       'ARCHIVE_SOURCE_KEY':'','ARCHIVE_SOURCE_KNOWN_HOSTS':''},
        'robot-runtime.env':{'ROS_DOMAIN_ID':str(args.domain)},
        'warehouse-upload-tunnel.env.template':{'WAREHOUSE_CLOUD_HOST':'8.140.19.17',
            'WAREHOUSE_CLOUD_USER':'','WAREHOUSE_CLOUD_KEY':'','WAREHOUSE_CLOUD_KNOWN_HOSTS':'',
            'WAREHOUSE_CLOUD_BIND_INTERFACE':'auto','WAREHOUSE_CLOUD_LEGACY_API_ALIAS':'false'},
        'video-tunnel.env.template':{'VIDEO_SOURCE_HOST':host,'VIDEO_SOURCE_HOST_KEY_ALIAS':host,'VIDEO_SOURCE_USER':'nvidia','VIDEO_SOURCE_KEY':'',
            'VIDEO_SOURCE_KNOWN_HOSTS':'','VIDEO_CLOUD_HOST':'8.140.19.17','VIDEO_CLOUD_USER':'',
            'VIDEO_CLOUD_KEY':'','VIDEO_CLOUD_KNOWN_HOSTS':'','VIDEO_CLOUD_BIND_INTERFACE':'auto',
            'VIDEO_CLOUD_SOCKET_DIR':profile['cloudSocketDir']}}
    for name,values in files.items():
        fd=os.open(args.output/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(''.join(k+'='+shlex.quote(v)+'\n' for k,v in values.items()))
    print(json.dumps({'generated':True,'directory':str(args.output),'observerId':args.observer_id,'warehouseId':warehouse_id,
        'credentialsPrinted':False,'servicesStarted':False,'requiresUploadAccountAndHardwareConfiguration':True},ensure_ascii=False))


def merge(args):
    for path in (args.env_file,args.entry):
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or info.st_size>65536 or path.is_symlink():raise ValueError('private files required')
    before=args.env_file.read_bytes();values=literal_environment(args.env_file);entry=json.loads(args.entry.read_text())
    robot=entry['observerId'];credentials=json.loads(values['DEVICE_CREDENTIALS_JSON']);profiles=json.loads(values.get('ROBOT_FLEET_JSON','{}'))
    incoming=entry['devices']
    if not incoming or any(d.get('robot_id')!=robot for d in incoming):raise ValueError('device identity mismatch')
    existing=[d for d in credentials if d['robot_id']==robot]
    if existing and existing!=incoming:raise ValueError('refusing credential replacement; use a separate rotation workflow')
    if not existing:credentials.extend(incoming)
    profile={k:entry[k] for k in ('name','warehouseId','nativeVideoSocket') if k in entry}
    if robot in profiles and profiles[robot]!=profile:raise ValueError('refusing to rebind an existing robot')
    profiles[robot]=profile
    Config(robot_id=values['ROBOT_ID'],device_credentials=credentials,viewer_username=values['VIEWER_USERNAME'],
        viewer_password_hash=values['VIEWER_PASSWORD_HASH'],session_secret=values['SESSION_SECRET'],
        public_origin=values['PUBLIC_ORIGIN'],data_dir=values.get('DATA_DIR','/data'),fleet=profiles).validate()
    changes={'DEVICE_CREDENTIALS_JSON':json.dumps(credentials,separators=(',',':')),
             'ROBOT_FLEET_JSON':json.dumps(profiles,ensure_ascii=False,separators=(',',':'))}
    lines=[];found=set()
    for line in before.decode().splitlines():
        key=line.partition('=')[0]
        if key in changes:
            if key in found:raise ValueError('duplicate identity environment key')
            found.add(key);lines.append(key+'='+shlex.quote(changes[key]))
        else:lines.append(line)
    lines.extend(k+'='+shlex.quote(v) for k,v in changes.items() if k not in found)
    after=('\n'.join(lines)+'\n').encode()
    if args.apply and after!=before:
        if args.env_file.read_bytes()!=before:raise ValueError('configuration changed during validation')
        backup=args.env_file.with_name(args.env_file.name+'.before-fleet-'+uuid4().hex)
        fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as f:f.write(before);f.flush();os.fsync(f.fileno())
        temporary=args.env_file.with_name(args.env_file.name+'.fleet-'+uuid4().hex)
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as f:f.write(after);f.flush();os.fsync(f.fileno())
        os.replace(temporary,args.env_file)
    print(json.dumps({'validated':True,'applied':args.apply,'robotCount':len({d['robot_id'] for d in credentials}),
        'observerId':robot,'credentialsPrinted':False,'servicesRestarted':False},ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    new=sub.add_parser('generate');new.add_argument('--observer-id',required=True);new.add_argument('--warehouse-id',required=True)
    new.add_argument('--name',required=True);new.add_argument('--domain',type=int,required=True)
    new.add_argument('--jetson-host',required=True,help='This vehicle\'s independently configured Jetson hostname or IPv4')
    new.add_argument('--output',type=Path,required=True)
    cloud=sub.add_parser('cloud');cloud.add_argument('--env-file',type=Path,required=True);cloud.add_argument('--entry',type=Path,required=True);cloud.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    try:
        generate(args) if args.action=='generate' else merge(args)
        return 0
    except (OSError,ValueError,TypeError,KeyError):
        print('接入配置未通过校验；请核对唯一身份、凭据归属、私有文件权限和机器人配置。凭据未输出。',file=sys.stderr)
        return 2

if __name__=='__main__':raise SystemExit(main())
