"""Independent image-only sidecar. Run on Jetson; never imports robot control code."""
import argparse
import json
import math
import os
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections import deque
from contextlib import contextmanager

INPUTS = {'fisheye': '/fisheye_front/image_raw', 'color': '/berxel/color/image_raw',
          'depth': '/berxel/depth/image_raw'}
HEALTH_TOPIC = '/observer_camera/status'
BERXEL_KEYS = ('color', 'depth')
PROBE_PROFILE_ENV = ('RMW_IMPLEMENTATION', 'FASTRTPS_DEFAULT_PROFILES_FILE',
                     'FASTDDS_DEFAULT_PROFILES_FILE')
HEALTH_WORKER_TIMEOUT = 18
HEALTH_WORKER_ENV = 'BERXEL_HEALTH_PROBE_WORKER'
PROBE_UDP_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<profiles xmlns="http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles">
  <transport_descriptors>
    <transport_descriptor>
      <transport_id>berxel_frame_probe_udp</transport_id>
      <type>UDPv4</type>
    </transport_descriptor>
  </transport_descriptors>
  <participant profile_name="berxel_frame_probe_udp_only" is_default_profile="true">
    <rtps>
      <userTransports><transport_id>berxel_frame_probe_udp</transport_id></userTransports>
      <useBuiltinTransports>false</useBuiltinTransports>
    </rtps>
  </participant>
</profiles>
'''


@contextmanager
def probe_udp_transport():
    """Only the short-lived diagnostic participant avoids shared-memory transport.

    Domain/discovery settings stay inherited. Existing RMW/profile choices are
    overridden for this diagnostic and restored even if ROS init or cleanup fails.
    The normal encoder never enters this context.
    """
    previous = {key: os.environ.get(key) for key in PROBE_PROFILE_ENV}
    with tempfile.TemporaryDirectory(prefix='berxel-frame-probe-') as directory:
        profile = os.path.join(directory, 'udp-only.xml')
        descriptor = os.open(profile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as output:
            output.write(PROBE_UDP_XML)
        try:
            os.environ.update(RMW_IMPLEMENTATION='rmw_fastrtps_cpp',
                FASTRTPS_DEFAULT_PROFILES_FILE=profile, FASTDDS_DEFAULT_PROFILES_FILE=profile)
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def launch_health_probe(timeout=15.0):
    """Stdlib-only parent: prepare transport before the child imports ROS/OpenCV."""
    try:
        with probe_udp_transport():
            environment = dict(os.environ, **{HEALTH_WORKER_ENV: '1'})
            completed = subprocess.run([sys.executable, '-B', os.path.abspath(__file__),
                '--health-probe-worker', '--health-timeout', str(timeout)],
                env=environment, capture_output=True, text=True, check=False,
                timeout=HEALTH_WORKER_TIMEOUT)
            if not completed.stdout.strip() or len(completed.stdout) > 32768:
                raise ValueError('worker returned no bounded result')
            result = json.loads(completed.stdout.strip().splitlines()[-1])
            if (not isinstance(result, dict) or type(result.get('ok')) is not bool
                    or result.get('state') not in ('ready', 'timeout', 'cancelled', 'failed')
                    or (result['ok'] and (completed.returncode or result.get('dataVerified') is not True))):
                raise ValueError('invalid worker result')
            return dict(result, probeTransport='udp-only', probeIsolation='fresh-worker')
    except subprocess.TimeoutExpired:
        return dict(ok=False, dataVerified=False, state='timeout',
            errorCode='BERXEL_FRAME_PROBE_WORKER_TIMEOUT', timeoutSeconds=HEALTH_WORKER_TIMEOUT,
            probeTransport='udp-only', probeIsolation='fresh-worker',
            error='深度相机真实帧检查超时：独立 ROS 检查进程未在18秒内完成观察和退出，已结束该检查进程；'
                  '尚未确认连续出图，未重启现有相机驱动。')
    except KeyboardInterrupt:
        return dict(ok=False, dataVerified=False, state='cancelled',
                    probeTransport='udp-only', probeIsolation='fresh-worker', error='深度帧检查已取消')
    except Exception as error:
        return dict(ok=False, dataVerified=False, state='failed',
            errorCode='BERXEL_FRAME_PROBE_WORKER_FAILED', probeTransport='udp-only',
            probeIsolation='fresh-worker', error='独立深度帧检查进程未返回有效结果：' + type(error).__name__)


def print_probe_result(result):
    print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
    return 0 if result['ok'] else 130 if result['state'] == 'cancelled' else 1


# This dispatch must precede cv2/rclpy imports. Fast DDS can initialize its
# process-wide participant factory while those modules are imported.
if __name__ == '__main__' and '--health-probe' in sys.argv[1:]:
    parser = argparse.ArgumentParser(description='只读检查连续彩色／深度帧，不启动相机或编码器')
    parser.add_argument('--health-probe', action='store_true')
    parser.add_argument('--health-timeout', type=float, default=15.0)
    arguments = parser.parse_args()
    if not math.isfinite(arguments.health_timeout) or not 4.0 <= arguments.health_timeout <= 15.0:
        parser.error('--health-timeout must be between 4 and 15 seconds')
    raise SystemExit(print_probe_result(launch_health_probe(arguments.health_timeout)))

if __name__ == '__main__' and '--health-probe-worker' in sys.argv[1:]:
    profile = os.environ.get('FASTRTPS_DEFAULT_PROFILES_FILE', '')
    try:
        info = os.stat(profile)
        if (os.environ.get(HEALTH_WORKER_ENV) != '1'
                or os.environ.get('RMW_IMPLEMENTATION') != 'rmw_fastrtps_cpp'
                or profile != os.environ.get('FASTDDS_DEFAULT_PROFILES_FILE')
                or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid() or os.path.islink(profile)):
            raise ValueError('worker profile was not prepared before imports')
    except (OSError, ValueError):
        raise SystemExit(print_probe_result(dict(ok=False, dataVerified=False, state='failed',
            errorCode='BERXEL_FRAME_PROBE_ENVIRONMENT', error='深度帧检查缺少导入前准备的私有 UDP 环境')))

import cv2
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import Image, CompressedImage, PointCloud2
from std_msgs.msg import String

from camera_codec import image_to_jpeg
from point_codec import cloud_points


class FrameReadiness:
    """Require live input and encoded output across a bounded, continuous window."""
    def __init__(self, clock=None, *, minimum_seconds=3.0,
                 minimum_reports=4, max_frame_age=1.0, max_report_gap=2.5):
        self.clock = clock or time.monotonic
        self.minimum_seconds, self.minimum_reports = minimum_seconds, minimum_reports
        self.max_frame_age, self.max_report_gap = max_frame_age, max_report_gap
        self.samples = deque(maxlen=32)
        self.last_reason = '等待深度相机真实帧状态'
        self.last_channels = {}
        self.result = None

    def reject(self, reason):
        self.samples.clear()
        self.result = None
        self.last_reason = reason
        return None

    @staticmethod
    def number(value):
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)

    def observe(self, report, *, now=None):
        now = self.clock() if now is None else now
        if not self.number(now) or not isinstance(report, dict):
            return self.reject('深度帧状态格式错误')
        channels, identities = {}, []
        for key in BERXEL_KEYS:
            row = report.get(key)
            if not isinstance(row, dict):
                return self.reject('深度帧状态缺少彩色或深度通道')
            required = ('sessionId', 'reportSequence', 'reportedMonotonic',
                        'receivedFrames', 'publishedFrames', 'sourceAgeS', 'previewAgeS')
            if any(name not in row for name in required):
                return self.reject('预览服务尚未提供帧计数与帧龄，请更新预览服务')
            session, sequence, reported = row['sessionId'], row['reportSequence'], row['reportedMonotonic']
            if (not isinstance(session, str) or not 1 <= len(session) <= 128
                    or type(sequence) is not int or sequence < 1 or not self.number(reported)
                    or reported > now or now - reported > self.max_report_gap):
                return self.reject('深度帧状态已过期或会话信息错误')
            received, published = row['receivedFrames'], row['publishedFrames']
            if (type(received) is not int or type(published) is not int
                    or received < 1 or published < 1 or published > received):
                return self.reject(key + ' 尚未同时收到并输出有效帧')
            if (not self.number(row['sourceAgeS'])
                    or row['sourceAgeS'] + now - reported > self.max_frame_age):
                return self.reject(key + ' 原始帧已过期')
            if (not self.number(row['previewAgeS'])
                    or row['previewAgeS'] + now - reported > self.max_frame_age):
                return self.reject(key + ' 预览帧已过期')
            if row.get('error'):
                return self.reject(key + ' 当前存在预览编码错误')
            if any(type(row.get(name)) is not int or row[name] < 1
                   for name in ('width', 'height', 'previewWidth', 'previewHeight')):
                return self.reject(key + ' 尚未输出有效尺寸的图像')
            channels[key] = {name: row[name] for name in
                             ('receivedFrames', 'publishedFrames', 'sourceAgeS', 'previewAgeS')}
            identities.append((session, sequence, reported))
        if identities[0] != identities[1]:
            return self.reject('彩色与深度帧状态来自不同会话或采样时刻')
        session, sequence, _ = identities[0]
        sample = dict(at=now, session=session, sequence=sequence, channels=channels)
        self.last_channels = channels
        if self.samples:
            previous = self.samples[-1]
            if session != previous['session']:
                self.reject('深度预览进程会话已变化，重新观察连续帧')
            elif now <= previous['at'] or now - previous['at'] > self.max_report_gap:
                self.reject('深度帧状态中断，重新观察连续帧')
            elif sequence <= previous['sequence']:
                self.reject('深度帧状态序号未增长，重新观察连续帧')
            elif any(channels[key][name] <= previous['channels'][key][name]
                     for key in BERXEL_KEYS for name in ('receivedFrames', 'publishedFrames')):
                self.reject('彩色或深度的原始帧／预览帧未连续增长')
        self.samples.append(sample)
        first = self.samples[0]
        duration = now - first['at']
        if len(self.samples) < self.minimum_reports or duration < self.minimum_seconds:
            if len(self.samples) > 1:
                self.last_reason = '正在观察彩色与深度的连续原始帧及预览帧'
            return None
        self.result = dict(ok=True, dataVerified=True, state='ready',
            sampleWindowSeconds=round(duration, 3), reports=len(self.samples),
            channels={key: dict(receivedFrames=channels[key]['receivedFrames'],
                publishedFrames=channels[key]['publishedFrames'],
                receivedDelta=channels[key]['receivedFrames'] - first['channels'][key]['receivedFrames'],
                publishedDelta=channels[key]['publishedFrames'] - first['channels'][key]['publishedFrames'],
                sourceAgeS=channels[key]['sourceAgeS'], previewAgeS=channels[key]['previewAgeS'])
                for key in BERXEL_KEYS})
        return self.result


def probe_frame_health(timeout=15.0):
    """In-process test harness; the CLI uses a fresh worker before ROS imports."""
    try:
        with probe_udp_transport():
            result = _probe_frame_health(timeout)
        return dict(result, probeTransport='udp-only')
    except KeyboardInterrupt:
        return dict(ok=False, dataVerified=False, state='cancelled',
                    probeTransport='udp-only', error='深度帧检查已取消')
    except Exception as error:
        return dict(ok=False, dataVerified=False, state='failed', probeTransport='udp-only',
                    error='深度帧检查或诊断节点清理无法完成：' + type(error).__name__)


def _probe_frame_health(timeout):
    """Short-lived status subscription only; no image or device subscription."""
    verdict = FrameReadiness()
    node = None
    initialized = False
    try:
        rclpy.init(args=[])
        initialized = True
        node = Node('observer_berxel_frame_probe_' + str(os.getpid()),
                    enable_rosout=False, start_parameter_services=False)
        def receive(message):
            try:
                if not isinstance(message.data, str) or len(message.data) > 16384:
                    raise ValueError('status bound')
                verdict.observe(json.loads(message.data))
            except (ValueError, TypeError, json.JSONDecodeError):
                verdict.reject('无法读取深度帧状态')
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE)
        subscription = node.create_subscription(String, HEALTH_TOPIC, receive, qos)
        deadline = time.monotonic() + timeout
        while rclpy.ok() and time.monotonic() < deadline and verdict.result is None:
            rclpy.spin_once(node, timeout_sec=min(.2, max(0.0, deadline - time.monotonic())))
        if verdict.result is not None:
            return verdict.result
        return dict(ok=False, dataVerified=False, state='timeout',
                    error='深度相机进程运行状态不代表有图像；' + verdict.last_reason,
                    channels=verdict.last_channels)
    except KeyboardInterrupt:
        return dict(ok=False, dataVerified=False, state='cancelled', error='深度帧检查已取消')
    except Exception as error:
        return dict(ok=False, dataVerified=False, state='failed',
                    error='深度帧检查无法完成：' + type(error).__name__)
    finally:
        try:
            if node is not None:
                node.destroy_node()
        finally:
            if initialized and rclpy.ok():
                rclpy.shutdown()


def load_preview_profile(environ=None):
    """Only observer color/depth previews are configurable; defaults stay legacy."""
    environ = os.environ if environ is None else environ

    def bounded(name, default, cast, lower, upper):
        name = 'BERXEL_PREVIEW_' + name
        raw = environ.get(name)
        if raw is None:
            return default
        try:
            value = cast(raw)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f'{name} must be between {lower} and {upper}') from None
        if not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError(f'{name} must be between {lower} and {upper}')
        return value

    return {
        'max_edge': bounded('MAX_EDGE', None, int, 160, 960),
        'jpeg_quality': bounded('JPEG_QUALITY', 70, int, 20, 85),
        'image_fps': bounded('IMAGE_FPS', 5.0, float, 0.5, 10),
        'cloud_points': bounded('CLOUD_POINTS', 3000, int, 100, 3000),
        'cloud_fps': bounded('CLOUD_FPS', 2.0, float, 0.5, 2),
    }


class CameraEncoder(Node):
    def __init__(self):
        self.profile = load_preview_profile()
        super().__init__('observer_camera_encoder', enable_rosout=False, start_parameter_services=False)
        cv2.setNumThreads(1)
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
        self.latest, self.received, self.errors, self.times = {}, {}, {}, {k: deque(maxlen=300) for k in INPUTS}
        self.health_session, self.health_sequence = uuid.uuid4().hex, 0
        self.received_frames = {key: 0 for key in INPUTS}
        self.published_frames = {key: 0 for key in INPUTS}
        self.last_received, self.last_published = {}, {}
        self.outputs = {k: self.create_publisher(CompressedImage, '/observer_camera/' + k + '/compressed', qos)
                        for k in INPUTS}
        self.health = self.create_publisher(String, HEALTH_TOPIC, 1)
        self.cloud_output = self.create_publisher(String, '/observer_camera/depth_points', qos)
        self.latest_cloud = None
        self.cloud_subscription = self.create_subscription(PointCloud2, '/berxel/points', self.receive_cloud, qos)
        self.subscriptions_ = [self.create_subscription(Image, topic, lambda msg, key=k: self.receive(key, msg), qos)
                               for k, topic in INPUTS.items()]
        self.create_timer(0.2, lambda: self.encode_latest(('fisheye',)))
        self.create_timer(1.0 / self.profile['image_fps'],
                          lambda: self.encode_latest(('color', 'depth')))
        self.create_timer(1.0, self.publish_health)
        self.create_timer(1.0 / self.profile['cloud_fps'], self.publish_cloud)

    def receive_cloud(self, message):
        self.latest_cloud = message

    def publish_cloud(self):
        message, self.latest_cloud = self.latest_cloud, None
        if message is None:
            return
        try:
            output = String()
            output.data = json.dumps({'points': cloud_points(message, limit=self.profile['cloud_points']),
                                      'totalPoints': message.width * message.height,
                                      'frameId': message.header.frame_id},
                                     allow_nan=False, separators=(',', ':'))
            self.cloud_output.publish(output)
        except (ValueError, TypeError):
            pass

    def receive(self, key, message):
        now = time.monotonic()
        self.latest[key] = message  # One frame per camera; newer frames replace older ones.
        self.received.setdefault(key, {}).update(width=message.width, height=message.height,
                                                encoding=message.encoding)
        self.times[key].append(now)
        self.received_frames[key] += 1
        self.last_received[key] = now

    def encode_latest(self, keys=INPUTS):
        for key in keys:
            message = self.latest.pop(key, None)
            if message is None:
                continue
            try:
                options = ({} if key == 'fisheye' else
                           {'max_edge': self.profile['max_edge'],
                            'jpeg_quality': self.profile['jpeg_quality'],
                            'annotate_depth': key == 'depth'})
                jpeg, width, height = image_to_jpeg(message, depth=key == 'depth', **options)
                output = CompressedImage()
                output.header = message.header
                output.format = 'jpeg'
                output.data = jpeg
                self.outputs[key].publish(output)
                self.published_frames[key] += 1
                self.last_published[key] = time.monotonic()
                self.received[key].update(previewWidth=width, previewHeight=height)
                self.errors.pop(key, None)
            except Exception as exc:
                self.errors[key] = str(exc)

    def publish_health(self):
        now = time.monotonic()
        self.health_sequence += 1
        report = {}
        for key, topic in INPUTS.items():
            times = self.times[key]
            while times and times[0] < now - 5:
                times.popleft()
            hz = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 else None
            report[key] = dict(self.received.get(key, {}), sourceTopic=topic, sourceHz=hz,
                               error=self.errors.get(key), sourcePublisherCount=self.count_publishers(topic),
                               sessionId=self.health_session, reportSequence=self.health_sequence,
                               reportedMonotonic=now, receivedFrames=self.received_frames[key],
                               publishedFrames=self.published_frames[key],
                               sourceAgeS=None if key not in self.last_received else max(0.0, now-self.last_received[key]),
                               previewAgeS=None if key not in self.last_published else max(0.0, now-self.last_published[key]))
        output = String()
        output.data = json.dumps(report, allow_nan=False)
        self.health.publish(output)
        try:
            from capture_health import write_capture_health
            write_capture_health(report)
        except (OSError, ValueError, ImportError):
            # A diagnostic write must never interrupt source/preview processing.
            pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--health-probe', action='store_true', help='只读检查连续彩色／深度帧，不启动相机或编码器')
    parser.add_argument('--health-probe-worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--health-timeout', type=float, default=15.0)
    args, ros_args = parser.parse_known_args()
    if not math.isfinite(args.health_timeout) or not 4.0 <= args.health_timeout <= 30.0:
        parser.error('--health-timeout must be between 4 and 30 seconds')
    if args.health_probe_worker:
        if ros_args:
            parser.error('health probe uses the existing configured ROS environment; unexpected arguments')
        if args.health_timeout > 15.0:
            parser.error('--health-timeout must be at most 15 seconds for the bounded worker')
        result = dict(_probe_frame_health(args.health_timeout), probeTransport='udp-only')
        raise SystemExit(print_probe_result(result))
    rclpy.init(args=ros_args)
    node = CameraEncoder()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
