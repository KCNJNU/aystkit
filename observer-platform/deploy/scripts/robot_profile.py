#!/usr/bin/env python3
"""Validate/bind per-robot configuration without starting devices or reading secrets aloud."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from collector.robot_profile import validate_current_profile


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check','bind'])
    parser.add_argument('--profile', type=Path, default=Path.home()/'.config/robot-cloud/robot.json')
    parser.add_argument('--template', type=Path, help='Public per-robot template with IDs and ROS domain; no credentials')
    args=parser.parse_args()
    try:
        if args.action=='bind':
            if not args.template:parser.error('bind requires --template')
            profile=json.loads(args.template.read_text())
            if profile.get('machineId'):raise ValueError('模板仍含旧机器绑定；请使用新车模板，保留旧车配置')
            profile['machineId']=Path('/etc/machine-id').read_text().strip()
            args.profile.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            # Exclusive creation never overwrites an existing vehicle identity.
            fd=os.open(args.profile,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            try:
                with os.fdopen(fd,'w') as stream:json.dump(profile,stream,ensure_ascii=False,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
                validate_current_profile(environ=dict(os.environ,ROBOT_PROFILE_FILE=str(args.profile)))
            except BaseException:
                args.profile.unlink();raise
        result=validate_current_profile(environ=dict(os.environ,ROBOT_PROFILE_FILE=str(args.profile)))
        if not result:raise ValueError('尚未配置本车身份')
        print(json.dumps({'ok':True,'observerId':result['observerId'],'warehouseId':result['warehouseId'],
            'name':result.get('name'),'rosDomainId':result['rosDomainId'],'machineBound':True,
            'deviceOrServiceStarted':False,'pendingArchivesModified':False},ensure_ascii=False))
        return 0
    except (OSError,ValueError,TypeError,KeyError):
        print('机器人接入检查未通过：请核对本机绑定、两套机器人 ID、上传配置与 ROS 域。未启动设备，未更改旧队列。',file=sys.stderr)
        return 2

if __name__=='__main__':raise SystemExit(main())
