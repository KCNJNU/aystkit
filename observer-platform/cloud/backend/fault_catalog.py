"""Conservative Chinese interpretations of recorded evidence, never diagnoses."""
from __future__ import annotations

import json
import re

CATEGORIES = (
    ('lidar_3d', '三维激光雷达'),
    ('lidar_2d', '二维激光雷达'),
    ('imu', '惯性传感器（IMU）'),
    ('camera', '普通相机'),
    ('depth_camera', '深度相机'),
    ('lidar_imu', '雷达与惯性融合'),
    ('navigation', '定位与导航'),
    ('chassis', '底盘'),
    ('host', '工控机'),
    ('network', '网络与数据链路'),
    ('system', '系统与其他'),
)
CATEGORY_LABELS = dict(CATEGORIES)
CATEGORY_IDS = frozenset(CATEGORY_LABELS)
_IMU = re.compile(r'(?<![a-z])imu(?![a-z])', re.I)

# Short, stable text describes what the evidence establishes. Root causes
# remain conditional; suggestions never relax collision or motion safeguards.
_MEANINGS = {
    'TIMESTAMP_ROLLBACK': (
        '采样时间戳回退',
        '后收到的数据帧，其采样时间早于前一帧，数据时间顺序发生了回退。',
        '可能使传感器融合丢弃数据或重置缓冲，影响定位连续性。',
        '核对设备与驱动的时间戳来源，关联同一时刻的重启、时钟同步和原始日志。'),
    'TIMESTAMP_GAP': (
        '采样时间出现断档',
        '相邻数据帧的采样时间间隔超过了当前监测阈值。',
        '算法可用的数据可能不连续；这条记录本身不能区分停采、传输延迟和处理积压。',
        '对照数据帧率、网络与温度记录，检查采集和处理链路是否同时出现中断。'),
    'LIDAR_POINT_TIME_WRAP': (
        '激光点时间回绕',
        '同一帧激光点的相对时间从较大数值跳回较小数值，符合数值回绕特征。',
        '点时间异常可能影响点云去畸变及激光惯性定位。',
        '检查原始点时间、雷达驱动时间转换及 FAST-LIO 的同时间段日志。'),
    'LIDAR_POINT_TIME_NONMONOTONIC': (
        '激光点时间倒序',
        '抽样检查发现同一帧中后一个激光点的相对时间小于前一个点。',
        '点序或时间转换异常可能影响点云处理，尚不能仅凭此记录确定硬件故障。',
        '核对原始点序和 offset_time，关联驱动版本、时间转换与定位日志。'),
    'SENSOR_TIME_SYNC_REPORTED': (
        '传感器时间未对齐',
        '来源节点报告参与处理的传感器时间未同步，当前数据不能按预期时间对齐。',
        '雷达与惯性数据融合可能等待、丢帧或不稳定。',
        '核对雷达和 IMU 的采样时间基准、驱动同步配置与接收时间差。'),
    'DEVICE_DATA_INTERRUPTED': (
        '设备数据中断',
        '此前收到过数据，但在设定的超时时间内没有再次收到新数据。',
        '使用该数据的观测或算法可能暂停；主动停采也会触发此记录。',
        '先确认设备是否被主动停止，再检查供电、连接、发布节点和实际接收帧率。'),
    'CAMERA_PREVIEW_INTERRUPTED': (
        '相机预览中断',
        '此前可用的相机预览不再按时更新，预览链路进入异常或过期状态。',
        '网页画面可能卡住或变黑，不等于相机硬件已经损坏。',
        '比较本地画面与网页状态，检查共享采集进程、编码和上传连接。'),
    'NAVIGATION_TIMEOUT': (
        '导航执行超时',
        '导航、路径跟随或转向动作在规定时间内未能完成。',
        '本次目标可能中止或进入恢复流程；原因需结合定位、障碍和实际运动反馈判断。',
        '核对目标误差、定位连续性、局部代价地图和底盘实际反馈。'),
    'NAVIGATION_ABORTED': (
        '导航动作失败',
        '动作服务器将该目标标记为失败终态，确认本次动作未成功完成。',
        '当前导航目标已经终止；终态本身不提供完整失败原因。',
        '查看同一目标前后的控制器、规划器和定位日志，关联具体失败信息。'),
    'NAVIGATION_FAILURE': (
        '导航执行受阻',
        '来源节点报告本次规划或控制未能继续完成。',
        '小车可能停止、重试或中止目标；应结合具体原始报错判断所在环节。',
        '关联有效路径、可执行轨迹、目标位置、定位状态与障碍记录。'),
    'LOCALIZATION_WARNING': (
        '定位数据异常',
        '来源节点报告定位丢失、位姿不可信、跳变或里程计数据无效。',
        '当前位置可能无法可靠用于规划与控制，需结合原始日志确认具体状态。',
        '检查雷达和 IMU 时间连续性、定位匹配结果、坐标变换与重定位状态。'),
    'CONTROLLER_OSCILLATION': (
        '控制器反复转向',
        '来源日志明确报告控制振荡或转向方向反复变化。',
        '可能导致反复修正、难以收敛或执行超时；本条不确定是哪项控制误差引起。',
        '对比目标航向误差、里程计运动和控制器输出，核对实际运动反馈及控制配置。'),
    'OBSTACLE_STOP_REPORTED': (
        '障碍触发停车',
        '来源节点报告障碍、碰撞风险或路径阻塞触发了停车或阻止继续运动。',
        '本次运动可能被安全逻辑阻止；日志不能自动判定障碍是真实物体还是地图误标。',
        '结合现场画面、雷达点云和代价地图核对触发区域及传感器安装参数。'),
    'ROS_DIAGNOSTIC_STALE': (
        '设备诊断状态过期',
        '诊断节点报告相关状态已过期，当前诊断数据不能代表设备的最新状态。',
        '设备健康状态暂时不可信，是否同时影响实际数据需另行核对。',
        '检查诊断与实际数据是否仍在发布，并对照节点、连接和采集时间。'),
    'ROS_DIAGNOSTIC_ERROR': (
        '节点诊断错误',
        '诊断节点明确上报了错误状态，具体检查项目保留在原始诊断详情中。',
        '相关设备或算法可能无法正常工作，影响范围以该节点的诊断项目为准。',
        '展开原始记录，核对诊断项、硬件标识和同时间段的实际数据。'),
    'HOST_TEMPERATURE_HIGH': (
        '工控机温度偏高',
        '记录到的工控机温度达到当前配置的监测阈值。',
        '高温可能影响运行稳定性；这条记录不确认已经降频，也不确认它导致了数据中断。',
        '核对散热、负载与温度趋势，并比较故障发生时的网络和数据帧率。'),
    'COLLECTOR_CONNECTION_LOST': (
        '观测采集心跳超时',
        '服务器在设定时间内没有收到工控机采集端的新心跳。',
        '网页显示的数据可能不再实时；这不等于所有传感器都停止工作。',
        '检查工控机采集服务和上云连接，结合本地设备状态区分网络与采集问题。'),
    'FAULT_MONITOR_LIMITED': (
        '部分故障监测不可用',
        '故障监测器无法读取或解析某类证据，相关检查能力暂时受限。',
        '对应故障可能无法完整记录；这条信息本身不代表相关传感器已经故障。',
        '查看原始详情中的缺失类型或读取原因，核对消息类型、日志权限和采集环境。'),
    'FAULT_LOG_OVERFLOW': (
        '离线故障队列已满',
        '待上传的故障日志超过本地队列容量，部分较早记录已被移除。',
        '该时间段的故障历史可能不完整，队列中的保留记录仍会继续补传。',
        '检查上云连接和故障产生速率，并查看原始详情中的丢失次数。'),
    'TEST_METRICS_OVERFLOW': (
        '测试里程队列已满',
        '待上传测试里程达到本地队列容量，部分新里程样本未能保存。',
        '相关测试的移动距离可能记录不全，手动测试计时与里程有效时间需分别查看。',
        '检查采集上云连接与补传积压，结合每日测试页面的缺失标识核对记录范围。'),
    'NODE_TIMEOUT': (
        '节点操作超时',
        '节点等待的数据、服务或操作在设定时间内没有完成。',
        '该项操作可能失败或需要重试，是否影响整机需结合来源节点判断。',
        '核对该节点依赖的数据和服务是否可用，再关联同时间段的其他报错。'),
}


def _text(value, limit=2048):
    return value[:limit] if isinstance(value, str) else ''


def _details(value):
    if isinstance(value, str):
        if len(value) > 16_384:
            return {}
        try:
            value = json.loads(value)
        except (TypeError, ValueError, RecursionError):
            return {}
    return value if isinstance(value, dict) else {}


def _inputs(code, component, message, details):
    code, component, message = _text(code,64), _text(component,160), _text(message)
    details = _details(details)
    if code == 'REPEATED_FAULT_SUMMARY':
        original = _text(details.get('original_code'),64)
        if original and original != 'REPEATED_FAULT_SUMMARY':
            code = original
            message = _text(details.get('original_message')) or message
    return code, component, message, details


def _has_imu(text):
    return bool(_IMU.search(text))


def _device_identity(identity):
    """Only direct source identifiers are used, never unrelated context."""
    name = identity.lower()
    if 'bluesea' in name or '蓝海' in name:
        return 'lidar_2d', '蓝海二维雷达'
    if any(t in name for t in ('stl19p','stl-19p','ldlidar')):
        return 'lidar_2d', 'STL-19P 二维雷达'
    if any(t in name for t in ('berxel','p100r','depthpoints')) or name.strip() in ('color','depth'):
        return 'depth_camera', 'P100R 深度相机'
    if any(t in name for t in ('depth_camera','depth-camera','深度相机')):
        return 'depth_camera', '深度相机（设备待确认）'
    if any(t in name for t in ('usbleft','usb_left')) or re.search(r'(?<![a-z0-9])camera1(?![a-z0-9])',name):
        return 'camera', '前视相机'
    if any(t in name for t in ('usbfront','usb_front','usbrear','usb_rear')) or re.search(r'(?<![a-z0-9])camera2(?![a-z0-9])',name):
        return 'camera', '后视相机'
    if any(t in name for t in ('usbhub2','usb_hub2')) or re.search(r'(?<![a-z0-9])camera4(?![a-z0-9])',name):
        return 'camera', '左视相机'
    if any(t in name for t in ('usbhub3','usb_hub3','usbright','usb_right')) or re.search(r'(?<![a-z0-9])camera3(?![a-z0-9])',name):
        return 'camera', '右视相机'
    if 'usbhub' in name:
        return 'camera', '扩展相机（方向待确认）'
    if 'fisheye' in name or '鱼眼' in name:
        return 'camera', '鱼眼相机'
    if _has_imu(name):
        return 'imu', '惯性传感器（IMU）'
    if any(t in name for t in ('mid360','mid-360','livox')):
        return 'lidar_3d', 'MID360 三维激光雷达'
    if any(t in name for t in ('fastlio','fast_lio','fast-lio')):
        return 'lidar_imu', '激光惯性定位（FAST-LIO）'
    if any(t in name.split() for t in ('scan','/scan')) or 'laserscan' in name:
        return 'lidar_2d', '二维扫描（设备待确认）'
    if any(t in name for t in ('camera','/dev/video','相机')):
        return 'camera', '普通相机（设备待确认）'
    return None


def _classify(code, component, message, details):
    # Source/component takes precedence over mentions inside an error sentence.
    # Temperature/network context attached to every fault is deliberately absent.
    identifiers = [component]
    for key in ('topic','hardware_id','log_file'):
        if isinstance(details.get(key),str):
            identifiers.append(details[key][:256])
    identity = ' '.join(identifiers)
    name, lower = identity.lower(), message.lower()
    if code.startswith('IMU_'):
        return 'imu', '惯性传感器（IMU）'
    if code.startswith('LIDAR_') or code == 'POINT_TIME_WRAP_REPORTED':
        physical = _device_identity(identity)
        return physical if physical and physical[0] in ('lidar_2d','lidar_3d') else ('lidar_3d','三维激光雷达（设备待确认）')
    if code == 'HOST_TEMPERATURE_HIGH':
        return 'host', '工控机温度监测'
    if code.startswith('COLLECTOR_CONNECTION_'):
        return 'network', '工控机上云采集链路'
    if code in ('FAULT_LOG_OVERFLOW','TEST_METRICS_OVERFLOW'):
        return 'system', '故障日志记录模块' if code=='FAULT_LOG_OVERFLOW' else '测试里程记录模块'
    if code in ('NAVIGATION_TIMEOUT','NAVIGATION_ABORTED','NAVIGATION_FAILURE','LOCALIZATION_WARNING',
                'CONTROLLER_OSCILLATION','OBSTACLE_STOP_REPORTED'):
        return 'navigation', '定位与导航'
    if code == 'SENSOR_TIME_SYNC_REPORTED' and (
            (_has_imu(name+' '+lower) and any(t in name+' '+lower for t in ('lidar','livox','雷达')))
            or any(t in name for t in ('fastlio','fast_lio','fast-lio'))):
        return 'lidar_imu', '雷达与 IMU 时间同步'
    if code in ('SENSOR_TIME_ROLLBACK_REPORTED','SENSOR_TIME_GAP_REPORTED'):
        if _has_imu(lower) and not any(t in lower for t in ('lidar','雷达')):
            return 'imu','惯性传感器（IMU）'
        if any(t in lower for t in ('lidar loop back','lidar timestamp','雷达时间')):
            return 'lidar_3d','三维激光雷达'
    if component.lower().strip() in ('color','depth','depthpoints') or any(t in name for t in ('/observer_camera/color/','/observer_camera/depth/')):
        return 'depth_camera','P100R 深度相机'
    if component.lower().strip() in ('/odometry','odometry'):
        return 'lidar_imu','激光惯性里程计（FAST-LIO）'
    if any(t in name for t in ('planner','controller','localization','rtabmap','costmap','navigation',
                                'nav2','behavior','follow_path','collision_monitor','coverage','odometry')):
        return 'navigation', '定位与导航'
    physical = _device_identity(identity)
    if physical:
        return physical
    if any(t in name for t in ('chassis','stm32','底盘')):
        return 'chassis','底盘反馈与串口'
    if any(t in name for t in ('thermal','cpu','x86_pkg','system_metrics','工控机')):
        return 'host','工控机'
    if any(t in name for t in ('network','uplink','cloud/collector','transport','网络')):
        return 'network','网络与数据链路'
    # An explicit model/topic in an otherwise unidentified source can identify
    # a sensor. Generic references like "lidar obstacle" cannot.
    if re.search(r'/livox/imu\b',lower):
        return 'imu','惯性传感器（IMU）'
    for token in ('mid360','mid-360','/livox/lidar','p100r','berxel','bluesea','stl19p','stl-19p'):
        if token in lower:
            found = _device_identity(token)
            if found:
                return found
    if code.startswith('CAMERA_PREVIEW_'):
        return 'camera','相机预览（设备待确认）'
    return 'system','其他系统组件'


def _interpret(code, component, message, category):
    lower = message.lower()
    if code.endswith('TIMESTAMP_ROLLBACK') or code == 'SENSOR_TIME_ROLLBACK_REPORTED':
        return _MEANINGS['TIMESTAMP_ROLLBACK']
    if code.endswith('TIMESTAMP_GAP') or code == 'SENSOR_TIME_GAP_REPORTED':
        return _MEANINGS['TIMESTAMP_GAP']
    if code == 'POINT_TIME_WRAP_REPORTED':
        return _MEANINGS['LIDAR_POINT_TIME_WRAP']
    if category in ('imu','lidar_3d','lidar_2d','lidar_imu'):
        if any(t in lower for t in ('not synced','not synchronized','时间不同步','时间同步失败')):
            return _MEANINGS['SENSOR_TIME_SYNC_REPORTED']
        if any(t in lower for t in ('lidar loop back','imu loop back','timestamp rollback','timestamp backward','时间回退','时间戳回退')):
            return _MEANINGS['TIMESTAMP_ROLLBACK']
    # Specific source text makes generic ROS/navigation errors readable while
    # preserving the original message verbatim in the separate evidence field.
    if any(t in lower for t in ('no valid trajectories','no legal trajectories','all trajectories invalid')):
        return ('局部轨迹不可用','局部控制器没有找到同时满足避障和运动约束的可执行轨迹。',
                '当前运动可能停止或重试，目标执行受到阻碍。',
                '检查局部代价地图、障碍位置、车体轮廓及底盘实际反馈。')
    if any(t in lower for t in ('goal is outside','goal outside','start is outside','start outside')):
        return ('规划位置超出地图','来源规划器报告起点或目标点超出当前地图范围。',
                '无法在当前地图内生成该次有效路径。',
                '核对地图范围、当前定位和目标坐标系，确认起点与目标的位置。')
    if any(t in lower for t in ('failed to create plan','failed to find a valid path','no valid path',
                                'no path found','valid plan could not','planning failed')):
        return ('有效路径规划失败','全局规划器未能生成从当前位置到目标点的有效路径。',
                '本次导航可能无法开始或需要重新规划，尚不能据此判定是定位还是障碍问题。',
                '检查起点和目标是否在地图可通行区域，结合代价地图核对障碍与定位。')
    if 'failed to make progress' in lower or 'progress checker' in lower and 'fail' in lower:
        return ('导航进展不足','控制器在设定时间内没有达到进度检查要求。',
                '本次目标可能被终止；是否因卡住、反复修正或反馈异常需继续核对。',
                '对比实际位移、目标误差、障碍地图与控制器输出。')
    if 'extrapolat' in lower and any(t in lower for t in ('into the future','into future','future extrapolation')):
        return ('坐标变换请求时间超前','请求的变换时刻晚于当前能够取得的最新 TF 坐标变换数据。',
                '所需时刻的变换尚不可用，相关数据转换可能暂时失败；本条不能单独确认是哪一端时钟异常。',
                '比较数据采样时间与最新 TF 时间，检查相关变换的发布频率和时间基准。')
    if 'extrapolat' in lower and any(t in lower for t in ('into the past','into past','past extrapolation')):
        return ('坐标变换请求时间过旧','请求的变换时刻早于当前 TF 缓存中保留的最早坐标变换数据。',
                '该时刻的变换已经不在现有缓存内，相关数据转换可能失败；数据延迟和时间基准均需核对。',
                '比较采样时间与 TF 缓存最早时间，检查处理或传输积压及各节点时间基准。')
    if any(t in lower for t in ('extrapolat','lookuptransform','transform timeout','transform unavailable',
                                'could not transform','cannot transform','failed to transform','invalid frame','tf missing','tf error',
                                'does not exist','not part of the same tree')) and (
            any(t in lower for t in ('transform','frame','tf ','tf:'))):
        return ('坐标变换不可用','所需坐标系变换缺失、过期或时间不匹配，节点无法完成当前数据转换。',
                '定位、点云或规划数据可能无法对齐，具体影响取决于来源节点。',
                '检查相关 TF 坐标链是否持续发布，并比较数据时间戳与变换有效时间。')
    if any(t in lower for t in ('device or resource busy','device is busy','resource busy','ebusy')):
        return ('采集设备被占用','当前进程无法取得正在被其他进程使用的设备资源。',
                '新的采集可能无法启动，已有本地画面可能仍然正常。',
                '检查是否重复启动采集程序，优先复用已有视频流，并核对设备占用情况。')
    if any(t in lower for t in ('bad_alloc','out of memory','cannot allocate memory','memory allocation failed')):
        return ('内存分配失败','节点未能分配当前操作所需的内存。',
                '相关处理可能失败或退出，当前记录不确认是哪项负载占用了内存。',
                '检查工控机内存、进程负载和数据队列，关联故障发生时的资源记录。')
    if any(t in lower for t in ('connection refused','connection reset','network unreachable','no route to host','broken pipe')):
        return ('数据连接失败','节点建立或维持数据连接时收到了连接拒绝、中断或不可达错误。',
                '对应数据链路可能不可用；不据此断言所有设备均已断连。',
                '核对来源与目标服务、网络连通性和连接状态，结合两端日志排查。')
    if any(t in lower for t in ('permission denied','failed to open','cannot open')) and (
            'serial' in lower or '/dev/tty' in lower):
        return ('串口访问失败','节点未能打开或访问所需串口。',
                '相关传感器或底盘反馈可能无法接收，具体设备以来源和端口为准。',
                '核对连接、端口名称、访问权限和是否被其他进程占用。')
    if code in _MEANINGS:
        return _MEANINGS[code]
    if any(t in lower for t in ('timed out','timeout','time out','超时')):
        return _MEANINGS['NAVIGATION_TIMEOUT' if category=='navigation' else 'NODE_TIMEOUT']
    if any(t in lower for t in ('invalid parameter','parameter not set','parameter is not','参数无效')):
        return ('配置参数无效','节点报告某项必需参数缺失或不符合当前要求。',
                '相关功能可能无法初始化或继续执行。',
                '展开原始日志核对参数名称、单位与实际设备配置。')
    if code == 'REPEATED_FAULT_SUMMARY':
        return ('同类故障重复出现','同类故障多次出现，本条是保留的重复计数摘要。',
                '具体影响取决于原始故障；旧摘要不一定保留所有重复事件详情。',
                '查看原始故障代码、首次和最近发生时间，结合相邻记录定位问题。')
    return ('来源节点报告错误','来源组件上报了错误，但当前记录没有匹配到可确认的具体故障类型。',
            '相关功能可能未能正常完成，影响范围和原因需结合该节点的原始报错判断。',
            '展开原始记录，核对具体错误文本、来源组件及同时间段的关联故障。')


def describe_fault(code, component, message, details=None):
    """Derived presentation only; no event fields or persisted rows are changed."""
    code, component, message, details = _inputs(code,component,message,details)
    category, device = _classify(code,component,message,details)
    title, meaning, impact, suggestion = _interpret(code,component,message,category)
    return dict(category=category,category_label=CATEGORY_LABELS[category],device_label=device,
                title=title,meaning=meaning,impact=impact,suggestion=suggestion)


def fault_category(code, component, message, details=None):
    return _classify(*_inputs(code,component,message,details))[0]


def fault_search_text(code, component, message, details=None):
    value = describe_fault(code,component,message,details)
    return ' '.join(value.values())
