"""Test preserved rendering semantics and the cloud-only UI boundary without ROS."""
import json
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess

import pytest

FRONTEND = Path(__file__).resolve().parents[1] / "cloud" / "frontend"


def test_dashboard_assets_and_existing_pages_plus_fault_logs():
    html = (FRONTEND / "index.html").read_text()
    assert re.findall(r'data-page-name="([^"]+)"', html) == [
        "overview", "system", "robot", "media", "tests", "logs"]
    for asset in re.findall(r'(?:src|href)="/([^"#]+)"', html):
        if asset == 'sensor-library/':
            assert (FRONTEND / asset / 'index.html').is_file()
            continue
        if asset in ("users", "coverage", "dbprog", "password"):  # Authenticated application pages.
            assert f'href="/{asset}"' in html
            continue
        assert (FRONTEND / asset).is_file(), asset
    assert 'id="cloud-last-seen"' in html
    assert 'id="cloud-last-observation"' in html
    assert 'id="cloud-source-id"' in html


def test_browser_has_no_robot_network_or_device_credential_dependency():
    scripts = "\n".join(path.read_text() for path in FRONTEND.glob("*.js"))
    assert not re.search(r"(?:https?|wss?)://(?:localhost|127\.0\.0\.1|192\.168\.|10\.48\.)", scripts)
    assert "DEVICE_TOKEN" not in scripts
    assert "Authorization" not in scripts
    assert "?token=" not in scripts
    assert "localStorage.setItem(\"token\"" not in scripts
    assert scripts.count("new WebSocket(") == 1
    assert 'fetch("/api/login"' in scripts
    assert 'fetch("/api/session"' in scripts
    assert 'fetch("/api/logout"' in scripts


def test_number_field_and_history_semantics():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the pure-JS renderer regression")
    script = r'''
      const fs=require('fs'),vm=require('vm'); global.window={};
      vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
      vm.runInThisContext(fs.readFileSync(process.argv[2],'utf8'));
      const f=window.ObserverFormat;
      const field=f.field({debug:{fields:{speed:{value:0,state:'AVAILABLE',type:'ACTUAL'}}}},'speed');
      const missing=f.field({speed:42},'speed');
      const h=new window.ObserverCharts.HistoryStore();
      h.record('cpu',1,0,true); h.record('cpu',2,17,false); h.record('cpu',2,NaN,true);
      console.log(JSON.stringify({zero:f.formatValue(0),nil:f.formatValue(null),nan:f.formatValue(NaN),
        infinity:f.formatValue(Infinity),negativeZero:f.formatValue(-0),falseValue:f.formatValue(false),
        field,missing,history:h.get('cpu',2,60),waiting:f.displayStatus('WAITING_UPSTREAM'),
        stale:f.displayStatus('STALE')}));
    '''
    result = subprocess.run([node, "-e", script, str(FRONTEND / "formatters.js"), str(FRONTEND / "charts.js")],
                            check=True, capture_output=True, text=True)
    values = json.loads(result.stdout)
    assert values["zero"] == values["negativeZero"] == "0"
    assert values["nil"] == values["nan"] == values["infinity"] == "—"
    assert values["falseValue"] == "否"
    assert values["field"]["value"] == 0 and values["field"]["type"] == "ACTUAL"
    assert values["missing"]["value"] is None and values["missing"]["state"] == "WAITING_UPSTREAM"
    assert values["history"] == [{"t": 1, "v": 0}, {"t": 2, "v": None}]
    assert values["waiting"] == "等待数据" and values["stale"] == "数据过期"


def test_javascript_syntax():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for JS syntax checking")
    for path in FRONTEND.glob("*.js"):
        subprocess.run([node, "--check", str(path)], check=True, capture_output=True, text=True)


def test_readable_navigation_and_density_defaults():
    html = (FRONTEND / "index.html").read_text()
    script = (FRONTEND / "app.js").read_text()
    css = (FRONTEND / "app.css").read_text()
    nav = re.findall(r'<button[^>]*data-page="[^"]+"[^>]*>', html)
    assert len(nav) == 6
    assert all('aria-label="' in button and 'data-mobile-label="' in button for button in nav)
    assert 'data-view="compact"' in html
    assert 'readStringPreference("observer.view", "compact"' in script
    assert 'setAttribute("aria-current", "page")' in script
    assert 'setAttribute("aria-pressed", String(active))' in script
    assert html.count('class="cloud-detail"') == 4
    # Compact is the default, but mobile viewers must still reach diagnostics.
    assert not re.search(r'\.view-toggle\s*\{[^}]*display:\s*none', css)


def test_nested_navigation_exposes_accessible_fixed_routes_without_new_pages():
    class Elements(HTMLParser):
        def __init__(self):
            super().__init__()
            self.items = []

        def handle_starttag(self, tag, attrs):
            self.items.append((tag, dict(attrs)))

    parser = Elements()
    parser.feed((FRONTEND / "index.html").read_text())
    children = {
        "system": ("performance", "environment", "stm32", "ros"),
        "robot": ("motion", "actuators", "sensors", "docking"),
        "media": ("cameras", "scans", "clouds", "maps"),
    }
    expected_routes = {f"{parent}/{child}" for parent, names in children.items() for child in names}
    buttons = [attrs for tag, attrs in parser.items if tag == "button" and "data-route" in attrs]
    assert {button["data-route"] for button in buttons} == expected_routes
    assert len(buttons) == 12
    subpages = [attrs for _, attrs in parser.items if "data-subpage" in attrs]
    assert {item["data-subpage"] for item in subpages} == {route.split("/")[1] for route in expected_routes}
    assert len(subpages) == 12
    assert all("subpage" in item.get("class", "").split() for item in subpages)
    for name in ("stm32", "ros"):
        section = next(item for item in subpages if item.get("id") == f"page-{name}")
        assert section["data-subpage"] == name
        assert "data-page-name" not in section
    assert len({attrs["id"] for _, attrs in parser.items if "id" in attrs}) == len(
        [attrs for _, attrs in parser.items if "id" in attrs]), "Duplicated IDs break persistent canvases"
    for parent, names in children.items():
        parent_button = next(attrs for tag, attrs in parser.items if tag == "button" and attrs.get("data-page") == parent)
        assert parent_button["aria-controls"] == f"subnav-{parent}"
        assert parent_button["aria-expanded"] in ("true", "false")
        menu = next(attrs for _, attrs in parser.items if attrs.get("id") == f"subnav-{parent}")
        assert "subnav" in menu.get("class", "").split()
        picker = next(attrs for tag, attrs in parser.items if tag == "select" and attrs.get("data-subpage-picker") == parent)
        assert "subpage-picker" in picker.get("class", "").split()
        assert picker.get("aria-label") or picker.get("id"), "Mobile navigation needs an accessible name"
    app = (FRONTEND / "app.js").read_text()
    assert "getRoute" in app and "popstate" in app and "hashchange" in app


def test_login_presentation_keeps_auth_contract_and_local_assets():
    html = (FRONTEND / "login.html").read_text()
    assert 'id="login-form" action="/api/login" method="post"' in html
    for element_id in ("username", "password", "login-submit", "login-message"):
        assert f'id="{element_id}"' in html
    assert 'autocomplete="current-password"' in html
    for asset in re.findall(r'(?:src|href)="([^"#]+)"', html):
        if asset == "/register":  # Registration is served by the application router.
            assert 'href="/register"' in html
            continue
        assert asset.startswith("/") and (FRONTEND / asset[1:]).is_file()
    css = "\n".join(path.read_text() for path in FRONTEND.glob("*.css"))
    assert "prefers-reduced-motion" in css
    assert "focus-visible" in css
    assert "@import" not in css
    assert not re.search(r'url\([\s\"\x27]*(?:https?:)?//', css)


def test_login_and_registration_require_password_without_length_policy():
    class Inputs(HTMLParser):
        def __init__(self):
            super().__init__()
            self.fields = []

        def handle_starttag(self, tag, attrs):
            if tag == "input":
                self.fields.append(dict(attrs))

    for filename, expected in (("login.html", 1), ("register.html", 2)):
        parser = Inputs()
        parser.feed((FRONTEND / filename).read_text())
        fields = [field for field in parser.fields if field.get("type") == "password"]
        assert len(fields) == expected
        for field in fields:
            assert "required" in field
            assert "minlength" not in field and "maxlength" not in field
        account = next(field for field in parser.fields if field.get("name") == "username")
        assert "maxlength" in account, "Username validation is independent of passwords"
    source = (FRONTEND / "access.js").read_text()
    assert 'confirm.value !== password.value' in source
    assert "12–512" not in source


def test_account_deletion_confirmation_permissions_busy_state_and_refresh():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for account management interaction regression")
    script = r'''
      const fs=require('fs'),vm=require('vm'),assert=require('assert');
      const source=fs.readFileSync(process.argv[1],'utf8');
      const drain=async()=>{for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve));};
      class Element {
        constructor(tag='div'){this.tagName=tag;this.dataset={};this.listeners={};this.children=[];this.disabled=false;this.textContent='';}
        append(...children){this.children.push(...children);}
        replaceChildren(fragment){this.children=[...fragment.children];}
        setAttribute(key,value){this[key]=value;}
        addEventListener(key,callback){this.listeners[key]=callback;}
        closest(){return this.tagName==='button'?this:null;}
      }
      const descendants=element=>[element,...element.children.flatMap(descendants)];
      async function setup(role='admin') {
        const elements=new Map();
        const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
        const state={rows:[{id:1,username:'pending_user',status:'pending'},{id:2,username:'approved_user',status:'approved'}],
          calls:[],confirm:true,confirmations:[],mode:'ok',redirects:[]};
        const document={body:{dataset:{accessPage:'users'}},hidden:false,getElementById:get,
          createElement:tag=>new Element(tag),createDocumentFragment:()=>new Element('fragment'),addEventListener(){},
          querySelectorAll:()=>[...elements.values()].flatMap(descendants).filter(el=>el.dataset.approveId||el.dataset.deleteId)};
        const response=(status,value)=>({status,ok:status>=200&&status<300,json:async()=>value});
        const fetch=async(path,options={})=>{
          const method=options.method||'GET';state.calls.push({path,method});
          if(path==='/api/session')return response(200,{authenticated:true,role,username:role==='admin'?'observer':'viewer',approved:true});
          if(method==='GET'&&path==='/api/admin/users')return response(200,{users:state.rows});
          if(method==='DELETE') {
            assert.equal(options.headers['Content-Type'],'application/json');assert.equal(options.body,'{}');
            if(state.mode==='throw')throw new Error('Network unavailable');
            if(state.mode==='forbidden')return response(403,{});
            if(state.mode==='unauthenticated')return response(401,{});
            if(state.mode==='protected')return response(409,{});
            if(state.mode==='mismatch')return response(200,{deleted:true,user:{id:999}});
            const id=Number(path.split('/').pop()),user=state.rows.find(row=>row.id===id);
            const complete=()=>{state.rows=state.rows.filter(row=>row.id!==id);return response(200,{deleted:true,user});};
            if(state.mode==='pending')return await new Promise(resolve=>{state.release=()=>resolve(complete());});
            if(state.mode==='missing'){state.rows=state.rows.filter(row=>row.id!==id);return response(404,{});}
            return complete();
          }
          throw new Error('Unexpected request '+method+' '+path);
        };
        const window={location:{replace:path=>state.redirects.push(path)},addEventListener(){},
          confirm:text=>{state.confirmations.push(text);return state.confirm;}};
        vm.runInNewContext(source,{window,document,fetch,AbortController,setTimeout:()=>1,clearTimeout(){},console});
        await drain();
        state.buttons=()=>['pending','approved'].flatMap(status=>descendants(get(status+'-users'))).filter(el=>el.dataset.deleteId);
        state.click=id=>{
          const button=state.buttons().find(el=>el.dataset.deleteId===String(id));
          assert(button,'Expected delete button for '+id);
          const list=get(id===1?'pending-users':'approved-users');list.listeners.click({target:button});
        };
        state.get=get;return state;
      }
      (async()=>{
        const state=await setup();assert.equal(state.buttons().length,2);
        state.confirm=false;state.click(1);await drain();
        assert.equal(state.calls.filter(call=>call.method==='DELETE').length,0);
        assert(state.confirmations[0].includes('pending_user'));
        state.confirm=true;state.mode='pending';state.click(1);await drain();
        assert(state.buttons().every(button=>button.disabled));assert(state.get('users-refresh').disabled);
        state.click(2);await drain();assert.equal(state.calls.filter(call=>call.method==='DELETE').length,1);
        state.release();await drain();
        assert.equal(state.buttons().length,1);assert.equal(state.buttons()[0].dataset.deleteId,'2');
        assert.equal(state.calls.filter(call=>call.method==='GET'&&call.path==='/api/admin/users').length,2);
        assert.equal(state.get('pending-count').textContent,'0');assert.equal(state.get('access-message').textContent,'已删除账号 pending_user');
        const approved=await setup();approved.click(2);await drain();
        assert.equal(approved.buttons().length,1);assert.equal(approved.buttons()[0].dataset.deleteId,'1');
        assert.equal(approved.get('approved-count').textContent,'0');
        for(const mode of ['forbidden','protected','mismatch','throw']) {
          const failed=await setup();failed.mode=mode;failed.click(2);await drain();
          assert.equal(failed.buttons().length,2,mode);assert(failed.buttons().every(button=>!button.disabled),mode);
          assert.equal(failed.get('access-message').dataset.tone,'error',mode);
        }
        const missing=await setup();missing.mode='missing';missing.click(1);await drain();
        assert.equal(missing.buttons().length,1);assert.equal(missing.get('access-message').textContent,'该账号已不存在');
        const expired=await setup();expired.mode='unauthenticated';expired.click(1);await drain();assert.deepEqual(expired.redirects,['/login']);
        const viewer=await setup('viewer');assert.equal(viewer.buttons().length,0);assert.deepEqual(viewer.redirects,['/']);
        assert(!viewer.calls.some(call=>call.path==='/api/admin/users'));
        console.log(JSON.stringify({confirmation:true,busy:true,refresh:true,permissions:true,failures:true}));
      })().catch(error=>{console.error(error);process.exitCode=1;});
    '''
    result = subprocess.run([node, "-e", script, str(FRONTEND / "access.js")],
                            check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == {
        "confirmation": True, "busy": True, "refresh": True, "permissions": True, "failures": True}


def test_independent_laserscan_cards_keep_truthful_sources_without_recording():
    html = (FRONTEND / "index.html").read_text()
    media = (FRONTEND / "media.js").read_text()
    app = (FRONTEND / "app.js").read_text()
    assert 'id="scan-gallery"' in html
    assert "本地 XY · 未融合 · 非导航地图" in html
    assert 'bluesea: "蓝海二维雷达", scan: "二维雷达扫描"' in media
    assert 'cards(scans, "scan")' in media
    assert 'ObserverRecording' not in media
    # Projection can evolve independently; verify XY/XYZ and bounds through
    # the renderer below instead of pinning the obsolete oblique formula.
    assert '[` · 有效采样 ${(item.points || []).length} 点`]' in media
    assert '[` / 原始 ${F.formatValue(item.totalPoints, 0)} 条距离`, true]' in media
    assert 'F.formatFrequency(item.sourceHz ?? item.hz)' in media
    assert 'F.formatValue(item.previewHz ?? item.hz)' in media
    assert 'hz: camera.previewHz ?? camera.hz' in app
    assert 'item.source || "来源待确认"' in media
    assert 'camera.source || "来源待确认"' in app
    assert 'item.source || "Jetson"' not in media
    assert '（Jetson）' not in app
    assert "远程 Jetson 相机" not in html



def test_spatial_renderer_keeps_scan_planar_and_bounds_real_point_work():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for spatial point validation")
    script = r"""
    const fs=require('fs'),vm=require('vm'),assert=require('assert');
    const source=fs.readFileSync(process.argv[1],'utf8'),labels=[];
    const gradient={addColorStop(){}};
    const ctx=new Proxy({}, {get(target,key){
      if(key==='createRadialGradient'||key==='createLinearGradient')return()=>gradient;
      if(key==='fillText')return text=>labels.push(text);
      return target[key]||(()=>{});
    }});
    const context={MAX_RENDER_POINTS:4000,global:{devicePixelRatio:4},
      F:{formatValue:(value,digits=1)=>Number(value).toFixed(digits)}};
    vm.createContext(context);
    vm.runInContext(source.slice(source.indexOf('  function metricStep('),source.indexOf('  function drawCloud(')),context);
    const raw=Array.from({length:25000},(_,index)=>[Math.sin(index)*3,Math.cos(index)*3,index%50/10]);
    raw[0]=[1e100,1,2];const points=context.validCloudPoints({points:raw});
    assert(points.length>0&&points.length<=4000);assert(points.every(point=>Math.abs(point[0])<=1000000));
    const canvas={clientWidth:820,clientHeight:420,dataset:{},getContext:()=>ctx};
    assert(context.paintSpatial(canvas,points,false,'perspective',{yaw:-.8,elevation:.6,zoom:1}));
    assert.strictEqual(canvas.dataset.dimension,'3');assert.strictEqual(canvas.dataset.view,'perspective');
    assert(canvas.width<=2048&&canvas.height<=1400);
    context.paintSpatial(canvas,[[1,0],[0,2],[-1,0]],true,'top',{yaw:-.8,elevation:.6,zoom:1});
    assert.strictEqual(canvas.dataset.dimension,'2');assert.strictEqual(canvas.dataset.pointCount,'3');
    assert(labels.includes('XY · m'));assert(labels.some(label=>/ m$/.test(label)));
    console.log(JSON.stringify({bounded:true,scanDimension:canvas.dataset.dimension}));
    """
    result = subprocess.run([node, "-e", script, str(FRONTEND / "media.js")],
                            check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == {"bounded": True, "scanDimension": "2"}


def test_http_usb_cameras_are_separate_and_keep_source_metadata():
    media = (FRONTEND / "media.js").read_text()
    assert 'usbLeft: "前视相机", usbFront: "后视相机"' in media
    assert 'usbHub2: "左视相机", usbHub3: "右视相机"' in media
    assert 'const cameras = { ...httpCameras, color:' in media
    assert 'color: "深度相机 · 彩色", depth: "深度相机 · 深度伪彩（中心 Z）"' in media
    assert 'Object.prototype.hasOwnProperty.call(httpCameras, key)' in media
    assert '["来源 Jetson 边缘计算机 · 网页预览", true]' in media, "Sources stay out of compact cards"
    assert '[` · 采集端 ${F.sourceLabel(item.source || "—")}`, true]' in media
    assert '[` · 接口 ${item.topic || "—"}`, true]' in media
    assert 'originParts(item, key)' in media
    assert 'await candidate.decode()' in media
    assert 'nextCameraPoll[key] = now + index * 1000 / Object.keys(httpCameras).length' in media
    assert 'nextCameraPoll[key] = now + index * 1000 / 3' not in media
    assert 'cameraStates[key]?.status !== "AVAILABLE" ? 1000 : 250' in media
    assert 'ObserverRecording' not in media


def test_current_camera_cards_exclude_historical_interfaces_but_keep_protocol():
    from shared.protocol import CAMERA_KEYS

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for camera display inventory regression")
    script = r'''
      const fs=require('fs'),vm=require('vm');
      const source=fs.readFileSync(process.argv[1],'utf8');
      const definitions=source.slice(source.indexOf('  const httpCameras ='),source.indexOf('  const clouds ='));
      const cardsSource=source.slice(source.indexOf('  function cards('),source.indexOf('  document.getElementById("camera-gallery")'));
      vm.runInThisContext(definitions+cardsSource+';console.log(JSON.stringify({http:Object.keys(httpCameras),keys:Object.keys(cameras),html:cards(cameras,"camera")}));');
    '''
    result = subprocess.run([node, "-e", script, str(FRONTEND / "media.js")],
                            check=True, capture_output=True, text=True)
    values = json.loads(result.stdout)
    expected = ["usbLeft", "usbFront", "usbHub2", "usbHub3", "color", "depth"]
    assert values["http"] == expected[:4]
    assert values["keys"] == expected
    assert re.findall(r'id="media-(\w+)"', values["html"]) == expected
    assert values["html"].count('class="panel media-card"') == 6
    for key in ("fisheye", "usbRear", "usbPort21"):
        assert key not in values["html"]
        assert key in CAMERA_KEYS, "UI cleanup must not remove ingestion/API compatibility"


def test_camera_identity_deadline_and_cached_snapshot_boundaries():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the camera freshness regression")
    script = r'''
      const fs=require('fs'),vm=require('vm');
      const source=fs.readFileSync(process.argv[1],'utf8');
      vm.runInThisContext(source.slice(source.indexOf('  function frameIdentity('),source.indexOf('  function freshCamera(')));
      const identity=frameIdentity('test-session:40'), huge=frameIdentity('test-session:9223372036854775807');
      const previous={identity,deadline:2500};
      console.log(JSON.stringify({zero:frameIdentity('test-session:0').counter.toString(),
        huge:huge.counter.toString(),bad:[null,'','test:NaN','test:-1',':3'].map(frameIdentity),
        first:frameDeadline('0.4','2',1000,null,identity),
        repeat:frameDeadline('0.1','2',2000,previous,identity),
        changed:frameDeadline('0.1','2',2000,previous,frameIdentity('test-session:41')),
        invalid:[[null,'2'],['','2'],['0',null],['NaN','2'],['-1','2'],['2','2'],['0','0'],['0','Infinity']]
          .map(([age,ttl])=>frameDeadline(age,ttl,1000,null,identity))}));
    '''
    result = subprocess.run([node, "-e", script, str(FRONTEND / "media.js")],
                            check=True, capture_output=True, text=True)
    values = json.loads(result.stdout)
    assert values["zero"] == "0" and values["huge"] == "9223372036854775807"
    assert values["bad"] == [None] * 5
    assert values["first"] == 2600
    assert values["repeat"] == 2500, "Repeated frame must not renew its first deadline"
    assert values["changed"] == 3900
    assert values["invalid"] == [None] * 8
    app = (FRONTEND / "app.js").read_text()
    media = (FRONTEND / "media.js").read_text()
    assert 'if (ingest && global.ObserverMedia) global.ObserverMedia.update(snapshot)' in app
    assert 'timestamp <= lastSnapshotTimestamp' in media
    assert 'incoming.counter < verified.identity.counter && (freshCamera(key) || !!cameraStale[key])' in media


def test_retained_camera_pixels_never_renew_freshness_or_extend_the_hold():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the bounded retained-frame regression")
    script = r'''
      const fs=require('fs'),vm=require('vm'),source=fs.readFileSync(process.argv[1],'utf8');
      const context={LAST_FRAME_HOLD_MS:10000,cameraFrames:{left:{deadline:2000}},
        cameraStale:{},urls:{left:'blob:decoded-last-frame'}};vm.createContext(context);
      vm.runInContext(source.slice(source.indexOf('  function freshCamera('),source.indexOf('  function acceptCameraSession(')),context);
      const first=context.retainedCameraUntil(undefined,2000,2100);
      const repeated=[2200,5000,11000].map(now=>context.retainedCameraUntil(first,2000,now));
      const fresh=context.freshCamera('left',1900);context.cameraStale.left={until:first};
      console.log(JSON.stringify({first,repeated,expired:context.retainedCameraUntil(first,2000,12000),
        tooLate:context.retainedCameraUntil(undefined,2000,13000),
        early204:context.retainedCameraUntil(undefined,2000,1000),fresh,
        retainedIsNotFresh:context.freshCamera('left',1900),deadline:context.cameraFrames.left.deadline,
        invalid:context.retainedCameraUntil(undefined,NaN,1000)}));
    '''
    result = subprocess.run([node, "-e", script, str(FRONTEND / "media.js")],
                            check=True, capture_output=True, text=True)
    values = json.loads(result.stdout)
    assert values["first"] == 12000 and values["repeated"] == [12000] * 3
    assert values["expired"] is None and values["tooLate"] is None
    assert values["early204"] == 11000, "Early 204 still holds for no more than ten seconds"
    assert values["fresh"] and not values["retainedIsNotFresh"]
    assert values["deadline"] == 2000 and values["invalid"] is None


def test_camera_buffer_grace_is_visual_only_and_has_a_fixed_deadline():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the short-gap camera regression")
    script = r'''
      const fs=require('fs'),vm=require('vm'),source=fs.readFileSync(process.argv[1],'utf8');
      const context={CAMERA_BUFFER_GRACE_MS:3000};vm.createContext(context);
      vm.runInContext(source.slice(source.indexOf('  function cameraBuffering('),source.indexOf('  function acceptCameraSession(')),context);
      console.log(JSON.stringify({phases:[1999,2000,2100,4999,5000,12000].map(now=>context.cameraBuffering(2000,now)),
        invalid:[context.cameraBuffering(NaN,1000),context.cameraBuffering(2000,Infinity)]}));
    '''
    result = subprocess.run([node, "-e", script, str(FRONTEND / "media.js")],
                            check=True, capture_output=True, text=True)
    values = json.loads(result.stdout)
    assert values["phases"] == [False, True, True, True, False, False]
    assert values["invalid"] == [False, False]
    media = (FRONTEND / "media.js").read_text()
    stale = media[media.index('  function showStaleCamera('):media.index('  function discardCameraCandidate(')]
    assert 'overlay.hidden = true' in stale and 'overlay.hidden = false' not in stale
    assert 'overlay.textContent = ""' in stale
    assert 'stateLabel(key, "STALE")' in stale
    assert 'ObserverRecording' not in stale
    assert 'frame.deadline' in stale and 'cameraStale[key] = { until }' in stale
    assert '已跳过无法验证的帧' in media and '已跳过无效图像帧' in media
    assert 'if (["STALE", "WAITING_UPSTREAM", "NO_DATA"].includes(item.status)) discardCameraCandidate' in media
    css = (FRONTEND / "app.css").read_text()
    assert '.media-viewport.is-stale img { opacity: .65; }' in css
    assert '.media-overlay.is-stale.is-buffering' not in css, "Keep original CSS materials"
    assert '.media-viewport img, .media-viewport canvas, .media-viewport video' in css


def test_discarding_camera_candidates_keeps_the_verified_pixels_and_lease():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the invalid-candidate regression")
    script = r'''
      const fs=require('fs'),vm=require('vm'),source=fs.readFileSync(process.argv[1],'utf8');
      const context={urls:{front:'blob:verified'},cameraFrames:{front:{deadline:2000}},isFresh:true,labels:[],stale:[],clears:[],element:{title:'previous warning',removeAttribute(name){delete this[name];}},
        freshCamera(){return context.isFresh;},showStaleCamera(key){context.stale.push(key);},
        clear(key,text){context.clears.push({key,text});},stateLabel(...args){context.labels.push(args);},
        document:{getElementById(){return context.element;}}};vm.createContext(context);
      vm.runInContext(source.slice(source.indexOf('  function discardCameraCandidate('),source.indexOf('  function invalidateView(')),context);
      context.discardCameraCandidate('front','invalid candidate');context.isFresh=false;
      context.discardCameraCandidate('front','expired candidate');context.discardCameraCandidate('missing','no decoded frame');
      console.log(JSON.stringify({labels:context.labels,stale:context.stale,clears:context.clears,
        hasTitle:Object.prototype.hasOwnProperty.call(context.element,'title'),url:context.urls.front,deadline:context.cameraFrames.front.deadline}));
    '''
    result = subprocess.run([node, "-e", script, str(FRONTEND / "media.js")],
                            check=True, capture_output=True, text=True)
    values = json.loads(result.stdout)
    assert values["labels"] == [["front", "WAITING_UPSTREAM", "缓冲中", True]]
    assert values["stale"] == ["front"]
    assert values["clears"] == [{"key": "missing", "text": "no decoded frame"}]
    assert values["url"] == "blob:verified" and values["deadline"] == 2000
    assert values["hasTitle"] is False


def test_camera_validators_are_bounded_opaque_and_only_decoded_frames_poll_fast():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the conditional camera polling regression")
    script = r'''
      const fs=require('fs'),vm=require('vm'),source=fs.readFileSync(process.argv[1],'utf8');
      const context={httpCameras:{usbFront:'front'},cameraEtags:{},urls:{},cameraStates:{color:{status:'AVAILABLE'}}};
      vm.createContext(context);
      vm.runInContext(source.slice(source.indexOf('  function strongFrameEtag('),source.indexOf('  function freshCamera(')),context);
      const tag='"opaque-session-abcdef"';
      const invalid=[null,'','*','W/'+tag,tag+','+tag,'"bad\nvalue"','unquoted','""','"'+ 'x'.repeat(255)+'"'];
      const empty=context.cameraPollInterval('usbFront');context.cameraEtags.usbFront=tag;
      const tagWithoutPixels=context.cameraPollInterval('usbFront');context.urls.usbFront='blob:decoded';
      console.log(JSON.stringify({tag:context.strongFrameEtag(tag),invalid:invalid.map(context.strongFrameEtag),
        max:context.strongFrameEtag('"'+'x'.repeat(254)+'"').length,
        empty,tagWithoutPixels,decoded:context.cameraPollInterval('usbFront'),ros:context.cameraPollInterval('color')}));
    '''
    result = subprocess.run([node, "-e", script, str(FRONTEND / "media.js")],
                            check=True, capture_output=True, text=True)
    values = json.loads(result.stdout)
    assert values["tag"] == '"opaque-session-abcdef"' and values["invalid"] == [None] * 9
    assert values["max"] == 256
    assert values["empty"] == values["tagWithoutPixels"] == 1000
    assert values["decoded"] == values["ros"] == 250


def test_camera_sessions_and_submillisecond_poll_boundaries():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the camera session regression")
    script = r'''
      const fs=require('fs'),vm=require('vm'),source=fs.readFileSync(process.argv[1],'utf8');
      const context={cameraSessions:{},clears:[],clear(key,reason){this;context.clears.push({key,reason});}};
      vm.createContext(context);
      vm.runInContext(source.slice(source.indexOf('  function acceptCameraSession('),source.indexOf('  function cards(')),context);
      const results=[context.acceptCameraSession('usbFront',{session:'A'}),
        context.acceptCameraSession('usbFront',{session:'B'}),
        context.acceptCameraSession('usbFront',{session:'A'}),
        context.acceptCameraSession('usbFront',{session:'B'}),
        context.acceptCameraSession('usbFront',{session:'C'})];
      console.log(JSON.stringify({results,clears:context.clears,session:context.cameraSessions.usbFront.current,
        early:context.cameraPollDue(1000.060,2000.030,1000),
        late:context.cameraPollDue(1000.060,2000.061,1000),
        first:context.cameraPollDue(undefined,1000,1000)}));
    '''
    result = subprocess.run([node, "-e", script, str(FRONTEND / "media.js")],
                            check=True, capture_output=True, text=True)
    values = json.loads(result.stdout)
    assert values["results"] == [True, True, False, True, True]
    assert values["session"] == "C" and len(values["clears"]) == 2
    assert values["early"] == 2000.060  # Retry the remaining .030 ms, not a full second.
    assert values["late"] == 2000.061
    assert values["first"] == 1000
    media = (FRONTEND / "media.js").read_text()
    assert 'nextCameraPoll[key] = started + interval' in media
    assert 'nextCameraPoll[key] = now + interval' not in media
    assert 'if (!acceptCameraSession(key, identity)) { URL.revokeObjectURL(candidateUrl); return; }' in media
    assert media.index('try { await candidate.decode(); }') < media.index('if (!acceptCameraSession(key, identity))')
    assert 'if (incoming && !acceptCameraSession(key, incoming)) return' in media


def test_live_verifier_checks_jpeg_headers_and_frame_identity_without_pixels():
    from deploy.scripts.verify_live import VerificationError, frame_identity, jpeg_dimensions
    from tests.test_protocol import jpeg

    assert jpeg_dimensions(jpeg()) == (1, 1)
    assert frame_identity("isolated-session:12") == ("isolated-session", 12)
    for malformed in (b"", b"not a JPEG", jpeg()[:-2], b"\xff\xd8\xff\xd9"):
        with pytest.raises(VerificationError):
            jpeg_dimensions(malformed)
    for identity in (None, "", "12", ":12", "session:-1", "session:NaN", "session:１２"):
        with pytest.raises(VerificationError):
            frame_identity(identity)


def test_compact_copy_removes_recording_and_moves_diagnostics_to_detail():
    html = (FRONTEND / "index.html").read_text()
    media = (FRONTEND / "media.js").read_text()
    cloud = (FRONTEND / "cloud.js").read_text()
    css = (FRONTEND / "app.css").read_text()
    assert 'class="eyebrow"' not in html
    assert 'class="page-description"' not in html
    assert not re.search(r'<header class="panel-head">(?:(?!</header>).)*<p[ >]', html)
    assert 'body[data-view="compact"] .detail-only' in css
    assert 'class="recording-notice"' not in html and "未下载的视频" not in html
    assert 'class="recording-help"' not in html and "录制说明" not in html
    assert "本地 XY · 未融合" in html and "非导航地图" in html
    assert '[` · 话题 ${item.topic || "—"}`, true]' in media
    assert '[` · 坐标系 ${item.frameId || "—"}`, true]' in media
    assert 'return [[`来源 ${F.sourceLabel(item.source || "来源待确认")}`, true]' in media
    assert 'container.classList.toggle("detail-only", parts.every(([, detail]) => detail))' in media
    assert 'item.reason !== F.displayStatus(item.status)' in media
    assert 'note.hidden = linkFresh && online && status === "AVAILABLE"' in cloud


def test_observation_assets_remove_browser_recording_but_keep_test_exports():
    assets = "\n".join(path.read_text() for pattern in ("*.js", "*.css", "*.html")
                       for path in FRONTEND.glob(pattern))
    for token in ("MediaRecorder", "captureStream", "ObserverRecording", "media-record-",
                  "recording-notice", "recording-help", "开始录制", "停止录制", "下载视频"):
        assert token not in assets, token
    html = (FRONTEND / "index.html").read_text()
    assert 'id="page-tests"' in html
    assert 'data-tests-export="csv"' in html and 'data-tests-export="json"' in html
