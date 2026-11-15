# 分层验收

## 已开发代码与本地测试

- 协议严格版本/身份/类型/时间戳/序号、真实0/null/NaN/Inf、缺失Page。
- 独立ROS订阅、硬件指标、限频媒体、队列键数/字节上限、退避/重连。
- 设备与用户身份隔离、密码hash、HttpOnly/Secure/SameSite、WS Origin、媒体鉴权。
- 云进程健康与机器人在线分离、旧值STALE、断线恢复、数据库检查点。
- 7页复用、三桌面分辨率/手机、单WS、切页/精简详细不清history、console。

实际命令和结果以test_report.md为准，不把文件存在当测试通过。

## 真实机器人数据（现场条件）

原数据源由操作者启动；Collector仅观察，不启动驱动、串口或控制。
先 `python -m collector.main --audit-seconds 10` 查看真实发布者、类型、QoS、计数。
确认STM32 Topic确实有真实消息，再核对原值与云端DTO；未收到字段仍null，COMMAND与ACTUAL分开。
逐项验证相机/点云/地图frameId、实际大小/频率、上游停止时过期、恢复时更新。
上游不存在标WAITING_UPSTREAM，不往生产链路注入fixture。

## 阿里云公网

ECS/域名/HTTPS已部署；最新分层实测及仍待验证的真实设备数据见 [实际部署状态](deployment_status.md)。以下保留验收要求，不将接口健康等同于收到设备数据。
HTTP公网IP能打开初始健康入口；HTTPS域名能登录七页；有效证书；WSS鉴权及广播；手机异网访问；未登录不能取数据。
机器人断网而网页可用、旧数据过期；公网只开放80/443/受限22，无DDS/8765/数据库/控制接口。
验证systemd Collector与Docker恢复，但不把云失败绑定到机器人主控启动。

## 状态矩阵

ALREADY_AVAILABLE表示已存在且已审计；ADAPTER_REQUIRED表示已知接口需要转换；WAITING_UPSTREAM表示没有现场真实数据；NOT_IMPLEMENTED表示功能本身未实现（例如地图位姿叠图、长期数据库历史）。
部署配置占位、缺SSH、缺证书、缺公网实测必须分别报告，不互相冒充通过。
