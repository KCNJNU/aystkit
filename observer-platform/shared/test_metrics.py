"""Bounded read-only odometry evidence shared between collector and cloud."""
from __future__ import annotations
import json
import math
import time
import uuid

MAX_METRIC_SAMPLES = 32
MAX_METRICS_BYTES = 131072
MAX_SAMPLE_SECONDS = 2.0
MAX_SPEED_MPS = 3.0
SAMPLE_FIELDS = frozenset({'id','run_id','revision','start','end','distance_m','valid_seconds'})
STATUS_FIELDS = frozenset({'applied_revision','run_id','state','odom_status','last_sample_timestamp','rejected_samples','dropped_samples'})

def canonical_uuid(value):
    try:
        if not isinstance(value,str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError,TypeError,AttributeError) as exc:
        raise ValueError('invalid test identifier') from exc
    return value

def integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**63-1:
        raise ValueError('invalid test integer')
    return value

def number(value,minimum=0,maximum=253402300799):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError('invalid test number')
    return value

def validate_test_metrics_payload(payload,now=None):
    now=time.time() if now is None else now
    if not isinstance(payload,dict) or set(payload) != {'samples','status'}:
        raise ValueError('invalid test metrics fields')
    samples,status=payload['samples'],payload['status']
    if not isinstance(samples,list) or len(samples)>MAX_METRIC_SAMPLES:
        raise ValueError('invalid test metric batch')
    result=[]
    for sample in samples:
        if not isinstance(sample,dict) or set(sample)!=SAMPLE_FIELDS:
            raise ValueError('invalid test sample fields')
        canonical_uuid(sample['id']);canonical_uuid(sample['run_id']);integer(sample['revision'],1)
        start=number(sample['start'],minimum=1,maximum=now+300)
        end=number(sample['end'],minimum=1,maximum=now+300)
        span=end-start
        if not 0 < span <= MAX_SAMPLE_SECONDS+1e-6:
            raise ValueError('invalid test sample interval')
        distance=number(sample['distance_m'],maximum=MAX_SPEED_MPS*span+.02+1e-6)
        valid=number(sample['valid_seconds'],maximum=span+1e-6)
        result.append(dict(sample,valid_seconds=min(span,valid)))
    if len({s['id'] for s in result}) != len(result):
        raise ValueError('duplicate test sample identifier')
    if not isinstance(status,dict) or set(status)!=STATUS_FIELDS:
        raise ValueError('invalid test source status')
    integer(status['applied_revision']);integer(status['rejected_samples']);integer(status['dropped_samples'])
    if status['state'] not in ('idle','running','paused') or status['odom_status'] not in ('WAITING_UPSTREAM','AVAILABLE','STALE','ERROR'):
        raise ValueError('invalid test source state')
    if status['run_id'] is not None:canonical_uuid(status['run_id'])
    if status['state']=='idle' and status['run_id'] is not None or status['state']!='idle' and (status['run_id'] is None or status['applied_revision']==0):
        raise ValueError('inconsistent test source state')
    if status['last_sample_timestamp'] is not None:
        number(status['last_sample_timestamp'],minimum=1,maximum=now+300)
    value={'samples':result,'status':dict(status)}
    if len(json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode())>MAX_METRICS_BYTES:
        raise ValueError('test metrics too large')
    return value
