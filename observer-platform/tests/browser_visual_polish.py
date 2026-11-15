"""Loopback-only Firefox layout/interaction acceptance with artificial fixtures.

Serves existing frontend assets unchanged; only the local response injects
instrumentation. No robot, ECS, camera, credentials or production login is used.
"""
import asyncio
import base64
from collections import Counter
import json
import math
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

from aiohttp import web
import websockets

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
SERVER = "http://127.0.0.1:19088"
DRIVER = "http://127.0.0.1:19090"
STATE = {"mode": "available", "ws_total": 0, "ws_active": 0, "ws_max": 0, "sequence": 0}
REQUESTS = Counter()
KEYS = ["usbFront", "usbLeft", "usbHub3", "usbHub2"]
THERMAL_NAMES = ["acpitz", "INT3400 Thermal", "TCPU", "TCPU_PCI", "x86_pkg_temp", "unknown_fixture_sensor"]
NETWORK_NAMES = ["Meta", "enp2s0", "enp3s0", "lo", "wlxec1ac3074c64", "unknown_fixture_net"]
BOOTSTRAP = r'''
window.__testErrors=[];window.__testCalls=[];window.__testHistory={};
addEventListener('error',e=>__testErrors.push(e.message));
addEventListener('unhandledrejection',e=>__testErrors.push(String(e.reason)));
const originalError=console.error.bind(console);
console.error=(...args)=>{__testErrors.push(args.map(String).join(' '));originalError(...args)};
const originalArc=CanvasRenderingContext2D.prototype.arc;
CanvasRenderingContext2D.prototype.arc=function(...args){
 if(this.canvas.dataset.chartSeries)__testCalls.push({series:this.canvas.dataset.chartSeries,radius:args[2]});
 if(__testCalls.length>1000)__testCalls.splice(0,__testCalls.length-1000);
 return originalArc.apply(this,args);
};
document.title='ISOLATED UI FIXTURE · NOT ROBOT DATA';
'''
CHART_HOOK = r'''
const originalRecord=ObserverCharts.HistoryStore.prototype.record;
ObserverCharts.HistoryStore.prototype.record=function(name,time,value,usable){
 const result=originalRecord.call(this,name,time,value,usable);
 const points=this.get(name,time,300);
 __testHistory[name]={count:this.pointCount(name),last:points.at(-1),value,usable,
  values:points.map(p=>p.v)};
 return result;
};
'''


def snapshot(sequence):
    phase = sequence / 6
    status = {"available": "AVAILABLE", "waiting": "WAITING_UPSTREAM", "stale": "STALE"}.get(STATE["mode"], "AVAILABLE")
    waiting = STATE["mode"] == "waiting"
    now = time.time()

    def metric(value):
        return {"status": status, "value": None if waiting else value, "source": "ISOLATED_FIXTURE"}

    def field(value, unit=""):
        return {"state": status, "value": None if waiting else value, "type": "ACTUAL", "ageMs": 12,
                "unit": unit, "source": "ISOLATED_FIXTURE"}

    values = {"busVoltage": field(24.6 + .2 * math.sin(phase), "V"),
              "actualVxMps": field(.3 + .1 * math.sin(phase), "m/s"),
              "actualWzRadps": field(.08 * math.sin(phase), "rad/s"),
              "cmdLinear": field(.34, "m/s"), "cmdAngular": field(.06, "rad/s"),
              "leftWheelMps": field(.3 + .08 * math.sin(phase), "m/s"),
              "rightWheelMps": field(.32 + .07 * math.cos(phase), "m/s")}
    return {"meta": {"timestamp": now, "robotId": "ISOLATED_BROWSER_FIXTURE", "received_timestamp": now},
            "cloud": {"status": status, "online": status == "AVAILABLE", "robotId": "ISOLATED_BROWSER_FIXTURE",
                      "sourceId": "TEST_ONLY", "lastSeen": now, "lastObservation": now, "ageS": .04},
            "rk3588": {"status": status, "hostname": "TEST_ONLY_IPC", "uptimeS": 9840,
                       "cpu": metric(35 + 10 * math.sin(phase)), "ram": {"usage": metric(44 + 2 * math.cos(phase)),
                                                                          "usedBytes": 3_560_000_000, "totalBytes": 8_000_000_000},
                       "disk": {"usage": metric(27), "totalBytes": 100_000_000_000},
                       "primaryThermal": {"status": status, "temperatureC": 46 + math.sin(phase), "zone": "TCPU"},
                       "thermalZones": [{"label": name, "zone": f"thermal_zone{index}", "temperatureC": 40 + index, "status": status}
                                        for index, name in enumerate(THERMAL_NAMES)],
                       "ips": ["TEST_ONLY"], "loadAverage": [1.1, .7, .5],
                       "network": [{"name": name, "up": True, "speedMbps": 1000, "addresses": ["TEST_ONLY", "fe80::1"],
                                    "rxBytesPerSec": None if waiting else 220_000 + 45000 * math.sin(phase),
                                    "txBytesPerSec": None if waiting else 45_000 + 16000 * math.cos(phase)} for name in NETWORK_NAMES],
                       "npu": {"status": "UNAVAILABLE", "utilization": {"status": "UNAVAILABLE", "value": None},
                               "frequencyHz": {"status": "UNAVAILABLE", "value": None},
                               "temperatureC": {"status": "UNAVAILABLE", "value": None}}},
            "ros2": {"status": status, "domainId": "0", "rmwImplementation": "rmw_fastrtps_cpp", "topics": {}},
            "robot": {"status": status, "source": "ISOLATED_FIXTURE", "telemetry": {"status": status, "hz": 20 + math.sin(phase),
                                                                                     "ageS": .06, "publisherCount": 1},
                      "data": {"chassisAlive": True, "debug": {"fields": values, "lastTelemetryPage": "0x13"}}},
            "devices": {"sensors": {"bluesea": {"label": "蓝海二维雷达", "status": status, "value": {"rayCount": None if waiting else 1000 + sequence % 30}},
                                     "lidar": {"label": "MID360 点云", "status": status, "value": {"points": None if waiting else 12000 + sequence * 20}},
                                     "imu": {"label": "雷达 IMU", "status": status, "value": {"frameId": "TEST_ONLY_IMU", "state": "RUNNING", "encoding": "bgr8", "driverFlag": False}},
                                     "odometry": {"label": "FAST-LIO 里程计", "status": status, "value": {"xM": .5}}},
                        "visionDocking": {}, "cameras": {}},
            "pages": {key: {"status": status, "hz": 20 + index + math.sin(phase)}
                      for index, key in enumerate(["0x00", "0x10", "0x11", "0x12", "0x13"])}, "errors": {}}


def native_status():
    stale = STATE["mode"] != "available"
    n = STATE["sequence"]
    return {"enabled": True, "connected": True, "cameras": {
        key: {"width": 640, "height": 480 if index < 2 else 360, "requested_fps": 10,
              "encoded_fps": 9.8 + .1 * math.sin(n / 5 + index), "error": "", "video_error": "",
              "frame_age_ms": 10000 if stale else 30, "frame_count": 1000 + n * 6,
              "encoded_frame_count": 500 + n * 6, "session_id": "ISOLATED_TEST", "owner": "SHARED_PREVIEW",
              "codec": "avc1.424016", "video_bytes": 200000 + n * 24000}
        for index, key in enumerate(KEYS)}}


def visuals():
    status = "AVAILABLE" if STATE["mode"] == "available" else "STALE"
    phase = STATE["sequence"] / 10
    scan = [[(4 + .3 * math.sin(6 * a + phase)) * math.cos(a),
             (4 + .3 * math.sin(6 * a + phase)) * math.sin(a), 0]
            for a in [index * math.tau / 420 for index in range(420)]]
    # Clearly artificial fixture of walls and columns, never a live robot scan.
    cloud = [[x / 8, y / 8, 1.2 + .3 * math.sin(x / 5 + phase)]
             for x in range(-30, 31) for y in range(-22, 23) if abs(x) > 27 or abs(y) > 19 or (x % 16 == 0 and y % 10 == 0)]
    return {key: {"status": status, "source": "ISOLATED_FIXTURE", "topic": "/fixture/" + key,
                  "frameId": "TEST_ONLY_XYZ", "points": scan if key in ["bluesea", "scan"] else cloud,
                  "totalPoints": 1200 if key in ["bluesea", "scan"] else 16000, "hz": 10, "ageS": .03,
                  "kind": "scan" if key in ["bluesea", "scan"] else "cloud"}
            for key in ["bluesea", "scan", "lidar", "registered", "depthPoints"]}


async def handle(request):
    REQUESTS[request.path] += 1
    if request.path == "/__test/state":
        return web.json_response({**STATE, "requests": dict(REQUESTS)})
    if request.path == "/__test/mode":
        value = await request.json()
        if value.get("mode") not in ["available", "stale", "waiting", "silent"]:
            raise web.HTTPBadRequest()
        STATE["mode"] = value["mode"]
        return web.json_response({"mode": STATE["mode"]})
    if request.path == "/api/session":
        return web.json_response({"authenticated": True, "username": "ISOLATED TEST"})
    if request.path == "/native-camera/status":
        return web.json_response(native_status())
    if request.path == "/visuals.json":
        return web.json_response(visuals())
    if request.path == "/ws":
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        STATE["ws_total"] += 1
        STATE["ws_active"] += 1
        STATE["ws_max"] = max(STATE["ws_active"], STATE["ws_max"])
        try:
            while not socket.closed:
                if STATE["mode"] != "silent":
                    STATE["sequence"] += 1
                    await socket.send_json(snapshot(STATE["sequence"]))
                await asyncio.sleep(.6)
        finally:
            STATE["ws_active"] -= 1
        return socket
    if request.path == "/__test/bootstrap.js":
        return web.Response(text=BOOTSTRAP, content_type="application/javascript")
    if request.path == "/__test/chart-hook.js":
        return web.Response(text=CHART_HOOK, content_type="application/javascript")
    if request.path.endswith("video.mp4"):
        raise web.HTTPServiceUnavailable(text="No camera fixture is recorded or served")
    path = ROOT / "cloud/frontend" / ("index.html" if request.path == "/" else request.path.lstrip("/"))
    if path.parent != ROOT / "cloud/frontend" or not path.is_file():
        raise web.HTTPNotFound()
    if path.name == "index.html":
        html = path.read_text().replace('<script src="/cloud.js">', '<script src="/__test/bootstrap.js"></script><script src="/cloud.js">')
        html = html.replace('<script src="/charts.js"></script>', '<script src="/charts.js"></script><script src="/__test/chart-hook.js"></script>')
        return web.Response(text=html, content_type="text/html", headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self' ws://127.0.0.1:19088"})
    return web.FileResponse(path)


def serve():
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    web.run_app(app, host="127.0.0.1", port=19088, access_log=None)


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(url, method="GET", data=None, timeout=15):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"}, method=method)
    with OPENER.open(req, timeout=timeout) as response:
        return json.load(response)


def verify():
    ARTIFACTS.mkdir(exist_ok=True)
    processes = []
    session = None
    # Snap's private /tmp and hidden home-directory rules require its own common directory.
    profile = Path.home() / "snap/firefox/common/observer-ui-polish-test"
    profile.mkdir(parents=True, exist_ok=True)
    logs = []
    result = {"scope": "Loopback Firefox 158 with artificial fixtures; no production auth or camera stream", "screenshots": []}
    try:
        for command, name in [([sys.executable, __file__, "--serve"], "fixture-server"),
                              (["/snap/bin/geckodriver", "--host", "127.0.0.1", "--port", "19090", "--profile-root", str(profile)], "geckodriver")]:
            log = (ARTIFACTS / (name + ".log")).open("w")
            logs.append(log)
            processes.append(subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                request(SERVER + "/__test/state")
                request(DRIVER + "/status")
                break
            except (OSError, ValueError):
                time.sleep(.2)
        created = request(DRIVER + "/session", "POST", {"capabilities": {"alwaysMatch": {
            "browserName": "firefox", "webSocketUrl": True,
            "moz:firefoxOptions": {"binary": "/snap/firefox/current/usr/lib/firefox/firefox", "args": ["-headless"]}}}}, timeout=55)["value"]
        session = created["sessionId"]

        async def resize_viewport(width, height):
            # BiDi sizes the CSS content viewport directly, avoiding Firefox's
            # 500px minimum outer-window width and browser-chrome height.
            async with websockets.connect(created["capabilities"]["webSocketUrl"], proxy=None) as ws:
                async def command(identifier, method, params):
                    await ws.send(json.dumps({"id": identifier, "method": method, "params": params}))
                    while True:
                        response = json.loads(await ws.recv())
                        if response.get("id") == identifier:
                            if response.get("type") == "error":
                                raise RuntimeError(response)
                            return response["result"]
                tree = await command(1, "browsingContext.getTree", {})
                await command(2, "browsingContext.setViewport", {"context": tree["contexts"][0]["context"],
                              "viewport": {"width": width, "height": height}, "devicePixelRatio": 1})

        def call(endpoint, data=None, method="POST"):
            return request(DRIVER + f"/session/{session}" + endpoint, method, data)["value"]

        def evaluate(script):
            return call("/execute/sync", {"script": script, "args": []})

        def wait(script, timeout=12):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                value = evaluate("return (" + script + ")")
                if value:
                    return value
                time.sleep(.2)
            raise AssertionError("Timed out: " + script)

        def screenshot(name):
            data = call("/screenshot", method="GET")
            (ARTIFACTS / name).write_bytes(base64.b64decode(data))
            result["screenshots"].append(name)

        def layout():
            return evaluate("return {width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth,overflow:[...document.querySelectorAll('.page.active *')].filter(e=>{let r=e.getBoundingClientRect();return r.width&&r.right>innerWidth+2}).map(e=>({tag:e.tagName,id:e.id,class:e.className,right:e.getBoundingClientRect().right})).slice(0,10)}")

        call("/window/rect", {"width": 1920, "height": 1080})
        call("/url", {"url": SERVER + "/#overview"})
        asyncio.run(resize_viewport(1920, 1080))
        wait("window.RKObserverDashboard&&RKObserverDashboard.getHistoryCount('networkRx')>=6")
        wait("__testHistory.cameraFrontFps&&__testHistory.cameraFrontFps.last.v!==null")
        observed = evaluate("return Object.fromEntries(['networkRx','networkTx','cameraFrontFps','cameraRearFps','cameraLeftFps','cameraRightFps','scanPoints','cloudPoints'].map(k=>[k,__testHistory[k]]))")
        for key, history in observed.items():
            assert history["count"] >= 6, (key, history)
            assert len(set(value for value in history["values"] if value is not None)) > 1, (key, history)
        result["liveHistory"] = {key: {"count": value["count"], "last": value["last"]} for key, value in observed.items()}
        assert not any(path.endswith("video.mp4") for path in request(SERVER + "/__test/state")["requests"])
        result["overviewDoesNotOpenVideo"] = True
        result["layouts"] = {}
        translated_text = {
            "overview": ("处理器使用率", "神经网络处理器 NPU"),
            "system/performance": ("处理器负载", "运行时长"),
            "system/environment": ("英特尔平台温控（INT3400 Thermal）", "代理虚拟网卡（Meta）", "地址（IPv4）", "地址（IPv6）"),
            "robot/sensors": ("雷达惯性测量单元（IMU）", "激光惯性里程计（FAST-LIO）"),
            "robot/motion": ("指令值", "实际值"),
            "stm32": ("底盘协议数据页", "底盘数据页频率趋势", "数据页 0x10"),
            "ros": ("机器人通信 ROS2", "通信发现状态 DDS", "话题类型检查"),
            "npu": ("神经网络处理器利用率", "神经网络处理器频率"),
            "media/scans": ("蓝海二维雷达", "二维雷达扫描"),
            "media/clouds": ("激光惯性配准点云（FAST-LIO）", "深度相机点云")
        }
        for size, width, height in [("desktop", 1920, 1080), ("mobile", 390, 844)]:
            asyncio.run(resize_viewport(width, height))
            for route, expected_text in translated_text.items():
                evaluate(f"RKObserverDashboard.openPage('{route}');window.scrollTo(0,0)")
                wait(f"RKObserverDashboard.getRoute()==='{route}'")
                if route.startswith("media/"):
                    key = "bluesea" if route.endswith("scans") else "lidar"
                    wait(f"document.getElementById('media-empty-{key}').hidden&&document.getElementById('media-{key}').width>0")
                    a = evaluate(f"return document.getElementById('media-{key}').toDataURL()")
                    time.sleep(1.4)
                    b = evaluate(f"return document.getElementById('media-{key}').toDataURL()")
                    assert a != b, "Artificial measured fixture does not update: " + route
                else:
                    time.sleep(.5)
                text = evaluate("return document.querySelector('.page.active').innerText")
                for expected in expected_text:
                    assert expected in text, (size, route, expected, text)
                if route == "system/environment":
                    for density in ("compact", "detailed"):
                        evaluate(f"document.querySelector('[data-view-mode={density}]').click()")
                        thermal = evaluate("return [...document.querySelectorAll('#thermal-zones .thermal-item-head strong')].map(e=>({text:e.innerText,title:e.title}))")
                        network = evaluate("return [...document.querySelectorAll('#network-interfaces h3')].map(e=>e.innerText)")
                        for raw, row in zip(THERMAL_NAMES, thermal):
                            assert raw in row["text"] and raw in row["title"], (raw, row)
                            assert re.search(r"[\u3400-\u9fff]", row["text"]) and re.search(r"[\u3400-\u9fff]", row["title"]), row
                        for raw, text in zip(NETWORK_NAMES, network):
                            assert raw in text and re.search(r"[\u3400-\u9fff]", text), (raw, text)
                        assert len(thermal) == len(THERMAL_NAMES) and len(network) == len(NETWORK_NAMES)
                        page_text = evaluate("return document.querySelector('.page.active').innerText")
                        assert "兆比特/秒" in page_text and "KiB/秒" in page_text, page_text
                        result.setdefault("environmentChinese", {})[size + "/" + density] = {"thermal": thermal, "network": network}
                    evaluate("document.querySelector('[data-view-mode=compact]').click()")
                if route == "robot/sensors":
                    evaluate("document.querySelector('[data-view-mode=detailed]').click()")
                    summary = evaluate("return document.getElementById('sensor-health').innerText")
                    assert "坐标系：TEST_ONLY_IMU" in summary and "状态：运行中" in summary, summary
                    assert "彩色图像（bgr8）" in summary and "其他字段（driverFlag）：否" in summary, summary
                    assert "frameId：" not in summary and "[object Object]" not in summary, summary
                    evaluate("document.querySelector('[data-view-mode=compact]').click()")
                check = layout()
                assert (check["width"], check["height"]) == (width, height), check
                assert check["scrollWidth"] <= check["width"] + 2, (size, route, check)
                assert not check["overflow"], (size, route, check)
                result["layouts"][size + "/" + route] = check
                screenshot(f"{size}-{route.replace('/', '-')}.png")
                if route == "overview":
                    evaluate("document.querySelector('.telemetry-grid').scrollIntoView({block:'start'})")
                    screenshot(f"{size}-overview-trends.png")
        result["chineseTextAcrossRoutes"] = True
        state = request(SERVER + "/__test/state")
        assert state["ws_total"] == state["ws_max"] == 1, state
        result["oneWebSocketAcrossRoutes"] = True
        evaluate("RKObserverDashboard.openPage('overview');window.scrollTo(0,0)")
        for mode in ["stale", "waiting"]:
            request(SERVER + "/__test/mode", "POST", {"mode": mode})
            wait("__testHistory.networkRx.last.v===null&&__testHistory.cameraFrontFps.last.v===null&&__testHistory.scanPoints.last.v===null")
            evaluate("__testCalls.length=0;ObserverCharts.drawAll(new ObserverCharts.HistoryStore(),0,60)")
            # Let the actual app draw the next incoming null-tailed history.
            time.sleep(.9)
            markers = evaluate("return __testCalls.filter(p=>p.radius===6)")
            assert not markers, (mode, markers)
        result["waitingAndStaleNullTailNoCurrentMarkers"] = True
        request(SERVER + "/__test/mode", "POST", {"mode": "available"})
        wait("__testHistory.networkRx.last.v!==null")
        request(SERVER + "/__test/mode", "POST", {"mode": "silent"})
        time.sleep(.7)
        counts = evaluate("return ['networkRx','cameraFrontFps','scanPoints'].map(k=>RKObserverDashboard.getHistoryCount(k))")
        time.sleep(3.8)
        evaluate("__testCalls.length=0")
        time.sleep(1.1)
        after = evaluate("return ['networkRx','cameraFrontFps','scanPoints'].map(k=>RKObserverDashboard.getHistoryCount(k))")
        result["silentDisconnect"] = {"before": counts, "after": after,
            "cloudText": evaluate("return document.getElementById('cloud-robot-status').textContent"),
            "markers": evaluate("return __testCalls.filter(p=>p.radius===6)")}
        assert counts == after, result["silentDisconnect"]
        assert "中断" in result["silentDisconnect"]["cloudText"]
        assert not result["silentDisconnect"]["markers"], result["silentDisconnect"]
        result["errors"] = evaluate("return __testErrors")
        assert not result["errors"], result["errors"]
        result["pass"] = True
    finally:
        (ARTIFACTS / "browser-visual-report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        if session:
            try:
                request(DRIVER + "/session/" + session, "DELETE")
            except OSError:
                pass
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
        for log in logs:
            log.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    serve() if "--serve" in sys.argv else verify()
