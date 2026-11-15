"""Bounded, read-only standard ROS message summaries (never store image/cloud data)."""
# key: Chinese name, topic, standard message type, freshness timeout (None = latched/event)
SENSOR_TOPICS = {
    'lidar': ('MID360 点云', '/livox/points', 'sensor_msgs/msg/PointCloud2', 2),
    'imu': ('雷达 IMU', '/livox/imu', 'sensor_msgs/msg/Imu', 2),
    'odometry': ('FAST-LIO 里程计', '/Odometry', 'nav_msgs/msg/Odometry', 2),
    'registered': ('配准点云', '/cloud_registered', 'sensor_msgs/msg/PointCloud2', 2),
    'map': ('静态地图', '/map', 'nav_msgs/msg/OccupancyGrid', None),
    'localCostmap': ('局部代价地图', '/local_costmap/costmap', 'nav_msgs/msg/OccupancyGrid', 5),
    'globalCostmap': ('全局代价地图', '/global_costmap/costmap', 'nav_msgs/msg/OccupancyGrid', 5),
    'plan': ('导航路径（按需）', '/plan', 'nav_msgs/msg/Path', None),
    'scan': ('二维扫描（可选）', '/scan', 'sensor_msgs/msg/LaserScan', 2),
    'localization': ('RTAB-Map 定位（可选）', '/localization_pose', 'geometry_msgs/msg/PoseWithCovarianceStamped', 2),
    'amcl': ('AMCL 定位（可选）', '/amcl_pose', 'geometry_msgs/msg/PoseWithCovarianceStamped', 2),
    'fisheye': ('鱼眼相机', '/fisheye_front/image_raw', 'sensor_msgs/msg/Image', 2),
    'color': ('深度相机彩色图', '/berxel/color/image_raw', 'sensor_msgs/msg/Image', 2),
    'depth': ('深度相机深度图', '/berxel/depth/image_raw', 'sensor_msgs/msg/Image', 2),
    'depthPoints': ('深度相机点云', '/berxel/points', 'sensor_msgs/msg/PointCloud2', 2),
    'previewDocking': ('对接预览状态', '/aruco_docking_preview/state', 'std_msgs/msg/String', 2),
    'diagnostics': ('节点诊断', '/diagnostics', 'diagnostic_msgs/msg/DiagnosticArray', 3),
}


def summarize(message):
    """Only small metadata; no COMMAND fields or derived chassis wheel feedback."""
    result = {}
    if hasattr(message, 'header'):
        result['frameId'] = message.header.frame_id
    if hasattr(message, 'width'):
        result.update(width=message.width, height=message.height)
    if hasattr(message, 'encoding'):
        result['encoding'] = message.encoding
    if hasattr(message, 'point_step'):
        result['points'] = message.width * message.height
    if hasattr(message, 'info'):
        result.update(width=message.info.width, height=message.info.height,
                      resolutionM=message.info.resolution)
    if hasattr(message, 'pose'):
        p = message.pose.pose.position
        result.update(xM=p.x, yM=p.y, zM=p.z)
    if hasattr(message, 'twist'):
        result.update(vxMps=message.twist.twist.linear.x,
                      wzRadps=message.twist.twist.angular.z)
    if hasattr(message, 'angular_velocity'):
        result.update(angularVelocity=[message.angular_velocity.x, message.angular_velocity.y,
                                       message.angular_velocity.z],
                      acceleration=[message.linear_acceleration.x, message.linear_acceleration.y,
                                    message.linear_acceleration.z])
    if hasattr(message, 'poses'):
        result['poseCount'] = len(message.poses)
    if hasattr(message, 'ranges'):
        result['rayCount'] = len(message.ranges)
    if hasattr(message, 'status'):
        result['diagnostics'] = [{'name': s.name,
                                 'level': int.from_bytes(s.level, 'little') if isinstance(s.level, bytes) else int(s.level),
                                 'message': s.message}
                                 for s in message.status[:16]]
    if isinstance(getattr(message, 'data', None), str):
        result['state'] = message.data[:256]
    return result


