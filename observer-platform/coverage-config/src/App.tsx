import { useEffect, useMemo, useRef, useState } from 'react';
import type { ChangeEvent, CSSProperties, ReactNode } from 'react';
import { DEFAULT_CONFIG, sanitizeConfig } from './model';
import type { Analysis, Chassis, Config, Sensor, FrameSide, SupportBox, SupportId } from './model';
import { computeCoverage } from './geometry';
import { frameRails } from './frame';
import { SUPPORT_IDS, supportIdForSensor, supportGeometry, supportForSensor, moveSensorWithinSupport, moveSupportAlongRail, isBelowBeamSupport, DEFAULT_BEAM_GAP } from './mounting';
import { BASELINE_STORAGE_KEY, compareCoverage, computeBaselineCoverage, loadBaseline } from './comparison';
import type { ComparisonValue } from './comparison';
import Scene from './Scene';
import RecommendationPanel from './RecommendationPanel';
import { useCameraRecommendation } from './useCameraRecommendation';
import type { RecommendationResult } from './optimizer';
import { applySideRecommendation, recommendationIsApplicable } from './recommendation-state';
import { toCentimeters, fromCentimeters, formatLength, formatArea, quantizeCentimeters, centimeterBounds } from './units';

const STORAGE_KEY = 'observer.sensor-coverage.v1';
const directions = [{ value: 0, label: '朝前' }, { value: 180, label: '朝后' }, { value: 90, label: '朝左' }, { value: -90, label: '朝右' }];
const inputNumber = (n: number) => String(Number(n.toFixed(6)));
const number = (n: number, digits = 2) => n.toLocaleString('zh-CN', { maximumFractionDigits: digits, minimumFractionDigits: digits });
function Icon({ name, size = 18 }: { name: string; size?: number }) {
  const paths: Record<string, ReactNode> = {
    sensors: <><circle cx="12" cy="12" r="3" /><path d="M6.4 6.4a8 8 0 0 0 0 11.2M17.6 6.4a8 8 0 0 1 0 11.2M3.5 3.5a12 12 0 0 0 0 17M20.5 3.5a12 12 0 0 1 0 17" /></>,
    arrow: <path d="m14 6-6 6 6 6M8 12h12" />,
    down: <path d="m7 10 5 5 5-5" />,
    reset: <><path d="M3 11a9 9 0 1 1 2.6 7M3 4v7h7" /></>,
    download: <><path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5" /></>,
    upload: <><path d="M12 15V3m-5 5 5-5 5 5M4 16v5h16v-5" /></>,
    camera: <><rect x="3" y="6" width="15" height="13" rx="3" /><path d="m18 10 4-2v9l-4-2M7 6l2-3h5l2 3" /></>,
    lidar: <><ellipse cx="12" cy="7" rx="7" ry="3" /><path d="M5 7v10c0 4 14 4 14 0V7M8 13h8" /></>,
    cube: <><path d="m12 2 9 5v10l-9 5-9-5V7l9-5Zm0 10v10M3 7l9 5 9-5M8 4l9 5" /></>,
    target: <><circle cx="12" cy="12" r="8" /><circle cx="12" cy="12" r="3" /><path d="M12 1v4m0 14v4M1 12h4m14 0h4" /></>,
    layers: <><path d="m12 3 10 6-10 6L2 9l10-6ZM2 14l10 6 10-6M2 19l10 6 10-6" /></>,
    check: <path d="m5 12 4 4L19 6" />,
    move: <><path d="M12 3v18M3 12h18m-12-6 3-3 3 3m-6 12 3 3 3-3M6 9l-3 3 3 3m12-6 3 3-3 3" /></>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.target}</svg>;
}
function loadConfig(): Config {
  try { return sanitizeConfig(JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null') || DEFAULT_CONFIG); }
  catch { return sanitizeConfig(DEFAULT_CONFIG); }
}
function useSession() {
  const [approved, setApproved] = useState(false);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false, busy = false;
    const controller = new AbortController();
    async function check() {
      if (busy || cancelled) return;
      busy = true;
      try {
        const response = await fetch('/api/session', { credentials: 'same-origin', cache: 'no-store', signal: controller.signal });
        if (cancelled) return;
        if (response.status === 401) { window.location.replace('/login'); return; }
        if (response.status === 403) { window.location.replace('/pending'); return; }
        if (!response.ok) throw new Error('session');
        const value = await response.json();
        if (cancelled) return;
        if (value.approved !== true) { window.location.replace('/pending'); return; }
        setApproved(true); setError('');
      } catch (cause) {
        if (!cancelled && !(cause instanceof DOMException && cause.name === 'AbortError')) setError('暂时无法确认账户状态，请检查连接后重试。');
      } finally { busy = false; }
    }
    void check();
    const interval = window.setInterval(() => { void check(); }, 30_000);
    const visibility = () => { if (!document.hidden) void check(); };
    document.addEventListener('visibilitychange', visibility);
    return () => { cancelled = true; controller.abort(); clearInterval(interval); document.removeEventListener('visibilitychange', visibility); };
  }, [attempt]);
  return { approved, error, retry: () => setAttempt(value => value + 1) };
}
type NumericFieldProps = {
  label: string; value: number; onChange: (value: number) => void; unit?: string; min?: number; max?: number; step?: number; testId?: string; disabled?: boolean;
};
function NumericField({ label, value, onChange, unit, min, max, step = .01, testId, disabled = false }: NumericFieldProps) {
  const isLength = unit === 'cm';
  const display = (next: number) => isLength ? String(quantizeCentimeters(next)) : inputNumber(next);
  const limits = isLength ? centimeterBounds(min, max) : { min, max };
  const [draft, setDraft] = useState(() => display(value));
  const focused = useRef(false), edited = useRef(false);
  useEffect(() => { if (!focused.current) setDraft(display(value)); }, [value, unit]);
  const bounded = (next: number) => Math.min(limits.max ?? Infinity, Math.max(limits.min ?? -Infinity, isLength ? quantizeCentimeters(next) : next));
  return <label className={`numeric-field${disabled ? ' is-locked' : ''}`}><span className="field-label">{label}</span><span className="input-wrap"><input type="number" inputMode="decimal" data-testid={testId} value={draft} min={limits.min} max={limits.max} step={isLength ? Math.max(.1, quantizeCentimeters(step)) : step} disabled={disabled}
    onFocus={() => { focused.current = true; edited.current = false; }}
    onChange={event => { edited.current = true; setDraft(event.target.value); if (event.target.value !== '' && Number.isFinite(event.target.valueAsNumber)) { const result = bounded(event.target.valueAsNumber); if (Math.abs(result - event.target.valueAsNumber) > 1e-9) setDraft(display(result)); onChange(result); } }}
    onBlur={() => { focused.current = false; if (!edited.current || draft.trim() === '') { setDraft(display(value)); return; } const next = draft.trim() === '' ? value : Number(draft); const result = Number.isFinite(next) ? bounded(next) : value; if (result !== value) onChange(result); edited.current = false; setDraft(display(result)); }} />{unit && <span className="input-unit">{unit}</span>}</span></label>;
}
/** Value callbacks stay in meters; only the input's visible value uses centimeters. */
function LengthField({ value, onChange, min, max, step = .01, ...props }: Omit<NumericFieldProps, 'unit'>) {
  return <NumericField {...props} value={toCentimeters(value)} onChange={centimeters => onChange(fromCentimeters(centimeters))} unit="cm" min={min === undefined ? undefined : toCentimeters(min)} max={max === undefined ? undefined : toCentimeters(max)} step={toCentimeters(step)} />;
}
function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (value: boolean) => void; label: string }) {
  return <button type="button" className={`toggle ${checked ? 'is-on' : ''}`} role="switch" aria-checked={checked} aria-label={label} onClick={() => onChange(!checked)}><span /></button>;
}
const sensorLabels: Record<string, { name: string; short: string; detail: string }> = {
  camera1: { name: '后视相机', short: '后视', detail: '500 万像素' },
  camera2: { name: '前相机', short: '前视', detail: '500 万像素' },
  camera3: { name: '左视相机', short: '左视', detail: '200 万像素' },
  camera4: { name: '右视相机', short: '右视', detail: '200 万像素' },
  mid360: { name: '激光雷达', short: '雷达', detail: 'MID360 · 全向扫描' },
  p100r: { name: '深度相机', short: '深度', detail: 'P100R · 彩色与深度' },
  bluesea: { name: '蓝海二维雷达', short: '蓝海', detail: '蓝海 · 二维雷达' },
  stl19p: { name: 'STL-19P', short: '底部雷达', detail: '二维雷达' },
};
function sensorLabel(sensor: Sensor) { return sensorLabels[sensor.id] ?? { name: sensor.name, short: sensor.name, detail: sensor.id }; }
function SensorPicker({ sensors, selected, onSelect, onChange }: { sensors: Sensor[]; selected: string; onSelect: (id: string) => void; onChange: (id: string, change: Partial<Sensor>) => void }) {
  return <div className="sensor-list" aria-label="选择传感器">{sensors.map(sensor => <section key={sensor.id} data-testid={`sensor-${sensor.id}`} className={`sensor-choice ${selected === sensor.id ? 'is-selected' : ''} ${!sensor.enabled ? 'is-disabled' : ''}`} style={{ '--sensor': sensor.color } as CSSProperties}>
    <button className="sensor-select" type="button" aria-pressed={selected === sensor.id} onClick={() => onSelect(sensor.id)}><span className="sensor-symbol"><Icon name={(sensor.kind === 'lidar' || sensor.kind === 'scan') ? 'lidar' : 'camera'} size={19} /></span><span className="sensor-name">{sensorLabel(sensor).name}<small>{sensor.kind === 'camera' ? (sensor.id === 'camera1' || sensor.id === 'camera2' ? '500万' : '200万') : sensor.id === 'bluesea' ? '二维' : sensor.id === 'stl19p' ? '二维' : sensor.id.toUpperCase()}</small></span>{selected === sensor.id && <span className="choice-check"><Icon name="check" size={12} /></span>}</button>
    <div className="choice-calculation"><span>{sensor.enabled ? '计入' : '排除'}</span><Toggle checked={sensor.enabled} onChange={enabled => onChange(sensor.id, { enabled })} label={`${sensor.name}参与覆盖计算`} /></div>
  </section>)}</div>;
}
function LayoutMap({ config, selected, onSelect, onEdit }: { config: Config; selected: string; onSelect: (id: string) => void; onEdit: () => void }) {
  const length = config.chassis.frameLength ?? config.chassis.length;
  const scale = Math.min(108 / Math.max(config.chassis.length, length), 88 / Math.max(config.chassis.width, config.chassis.frameWidth));
  const cx = 174, cy = 116;
  const bodyWidth = config.chassis.width * scale, bodyLength = config.chassis.length * scale;
  const frameWidth = config.chassis.frameWidth * scale, frameLength = length * scale;
  const positions: Record<string, { x: number; y: number; left: boolean }> = {
    camera1: { x: 18, y: 147, left: true }, camera2: { x: 260, y: 37, left: false },
    camera3: { x: 18, y: 37, left: true }, camera4: { x: 260, y: 92, left: false },
    mid360: { x: 18, y: 92, left: true }, p100r: { x: 260, y: 147, left: false },
    bluesea: { x: 18, y: 202, left: true }, stl19p: { x: 260, y: 202, left: false },
  };
  return <section className="layout-overview"><div className="overview-heading"><span><Icon name="target" size={14} />安装布局</span><button type="button" className="overview-edit" data-testid="edit-selected-sensor" onClick={onEdit}>调整参数<Icon name="arrow" size={12} /></button></div><svg className="layout-map" viewBox="0 0 348 234" role="group" aria-label="八台设备的俯视安装布局，点击设备切换编辑">
    <defs><pattern id="layout-grid" width="18" height="18" patternUnits="userSpaceOnUse"><path d="M18 0H0V18" fill="none" stroke="#36526a" strokeOpacity=".16" strokeWidth=".6" /></pattern></defs>
    <rect x="111" y="29" width="126" height="180" rx="12" fill="url(#layout-grid)" />
    <path d={`M${cx} 10v12m-4-4 4-4 4 4`} stroke="#91a9be" fill="none" strokeWidth="1.3" /><text x={cx+10} y="20" className="map-front">车头</text>
    <rect x={cx-bodyWidth/2} y={cy-bodyLength/2} width={bodyWidth} height={bodyLength} rx="12" className="map-chassis" />
    <rect x={cx-frameWidth/2} y={cy-frameLength/2} width={frameWidth} height={frameLength} rx="2" className="map-frame" />
    <path d={`M${cx-5} ${cy}h10m-5-5v10`} className="map-origin" />
    
    {config.sensors.filter(sensor => sensor.id !== selected).concat(config.sensors.filter(sensor => sensor.id === selected)).map(sensor => {
      const point = { x: cx - sensor.y * scale, y: cy - sensor.x * scale };
      const label = positions[sensor.id], angle = sensor.yaw * Math.PI / 180;
      const tip = { x: point.x - Math.sin(angle) * 20, y: point.y - Math.cos(angle) * 20 };
      const active = selected === sensor.id;
      return <g key={sensor.id} data-testid={`layout-map-${sensor.id}`} tabIndex={0} role="button" aria-label={`选择${sensorLabel(sensor).name}`} aria-pressed={active} className={`map-sensor ${active ? 'is-selected' : ''} ${!sensor.enabled ? 'is-disabled' : ''}`} style={{ '--sensor': sensor.color } as CSSProperties} onClick={() => onSelect(sensor.id)} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(sensor.id); } }}>
        <path d={`M${point.x} ${point.y}L${label.left ? 101 : 247} ${label.y}H${label.left ? 92 : 254}`} className="map-leader" />
        <path d={`M${point.x} ${point.y}L${tip.x} ${tip.y}m${Math.sin(angle+.55)*6} ${Math.cos(angle+.55)*6}L${tip.x} ${tip.y}l${Math.sin(angle-.55)*6} ${Math.cos(angle-.55)*6}`} className="map-direction" />
        <circle cx={point.x} cy={point.y} r={active ? 8 : 6} className="map-halo" /><circle cx={point.x} cy={point.y} r="3.5" className="map-point" />
        <rect x={label.x-8} y={label.y-15} width="90" height="31" rx="7" className="map-label-bg" /><text x={label.x} y={label.y+4} className="map-label">{sensorLabel(sensor).name}</text>
      </g>;
    })}
  </svg></section>;
}
const supportNames: Record<SupportId, string> = { front: '前相机 · 深度相机', camera1: '后视相机', camera3: '左视相机', camera4: '右视相机', mid360: 'MID360', bluesea: '蓝海二维雷达', stl19p: 'STL-19P' };
function SupportPanel({ config, supportId, selected, onSelect, onChange }: {
  config: Config; supportId: SupportId; selected: boolean; onSelect: () => void; onChange: (change: Partial<SupportBox>) => void;
}) {
  const box = supportGeometry(config, supportId), layout = box.layout;
  const belowBeam = isBelowBeamSupport(supportId);
  const [expanded, setExpanded] = useState(selected);
  useEffect(() => { if (selected) setExpanded(true); }, [selected]);
  const rails = frameRails(config.chassis).filter(rail => rail.kind === 'profile' && (supportId !== 'front' || rail.id === 'y-mid-front'));
  const axisNames = { x: 'X 前后', y: 'Y 左右', z: 'Z 高度' };
  const corners = Array.from({ length: 8 }, (_, n) => ({ x: n & 1 ? 1 : 0, y: n & 2 ? 1 : 0, z: n & 4 ? 1 : 0 }));
  const project = (p: { x: number; y: number; z: number }) => ({ x: 70 + (p.x - p.y) * 35, y: 57 + (p.x + p.y - 1) * 13 - p.z * 35 });
  const mounted = config.sensors.filter(sensor => supportIdForSensor(sensor.id) === supportId);
  return <details open={expanded} onToggle={event => setExpanded(event.currentTarget.open)} className={`support-panel${selected ? ' is-selected' : ''}`} data-testid="mount-support-editor" data-support-id={supportId}>
    <summary className="support-heading"><span className="support-icon"><Icon name="cube" size={19} /></span><span><strong>安装空间</strong><small>{supportNames[supportId]}</small></span><span className="support-dimensions">{formatLength(layout.sizeX)} × {formatLength(layout.sizeY)} × {formatLength(layout.sizeZ)}<small> cm</small></span><button type="button" className="support-locate" data-testid="select-support" aria-label="选中安装空间" aria-pressed={selected} onClick={event => { event.preventDefault(); onSelect(); setExpanded(true); }}><Icon name="target" size={16} /></button><Icon name="down" size={13} /></summary>
    <div className="support-body"><div className="support-mini"><svg viewBox="0 0 145 90" aria-label="安装空间与盒内设备点">{corners.flatMap((p, index) => [1, 2, 4].filter(mask => !(index & mask)).map(mask => { const a = project(p), b = project(corners[index | mask]); return <path key={`${index}-${mask}`} d={`M${a.x} ${a.y}L${b.x} ${b.y}`} className="support-mini-edge" />; }))}{mounted.map(sensor => { const point = project(sensor.inSupport ?? { x: .5, y: .5, z: .5 }); return <g key={sensor.id}><circle cx={point.x} cy={point.y} r="6.5" fill={sensor.color} opacity=".16" /><circle cx={point.x} cy={point.y} r="3.5" fill={sensor.color} /></g>; })}<text x="113" y="76">X</text><text x="13" y="76">Y</text><text x="76" y="13">Z</text></svg><div><span>{layout.style === 'pedestal' ? (belowBeam ? '倒挂塑料底座' : '凸起塑料底座') : '塑料安装支架'}</span><small>中心 {formatLength(box.center.x)} / {formatLength(box.center.y)} / {formatLength(box.center.z)} cm</small></div></div>
      <div className="support-section-title">空间尺寸</div><div className="field-grid three">{(['x', 'y', 'z'] as const).map(axis => <LengthField key={axis} label={{ x: '长 · X', y: '宽 · Y', z: '高 · Z' }[axis]} testId={`support-size-${axis}`} value={layout[`size${axis.toUpperCase()}` as 'sizeX' | 'sizeY' | 'sizeZ']} min={.04} max={supportId === 'front' && axis === 'y' ? config.chassis.frameWidth : 1.5} step={.005} onChange={value => onChange({ [`size${axis.toUpperCase()}`]: value })} />)}</div>
      <div className="support-section-title">{belowBeam ? '横梁下挂位置' : '相对横梁偏置'}</div><div className="field-grid three">{(['x', 'y', 'z'] as const).map(axis => belowBeam && axis === 'z' ? <LengthField key={axis} label="距横梁下表面" testId="support-beam-gap" value={layout.beamGap ?? DEFAULT_BEAM_GAP} min={0} max={.5} step={.001} onChange={beamGap => onChange({ beamGap })} /> : <LengthField key={axis} label={axisNames[axis]} testId={`support-offset-${axis}`} value={layout[`offset${axis.toUpperCase()}` as 'offsetX' | 'offsetY' | 'offsetZ']} min={-.5} max={.5} step={.005} onChange={value => onChange({ [`offset${axis.toUpperCase()}`]: value })} />)}</div>
      <details className="support-structure"><summary>支架与安装杆件<Icon name="down" size={12} /></summary><div className="field-grid"><LengthField label="塑料板厚度" testId="support-thickness" value={layout.thickness} min={.001} max={Math.min(.1, layout.sizeX, layout.sizeY, layout.sizeZ)} step={.001} onChange={thickness => onChange({ thickness })} /><label className="numeric-field"><span className="field-label">支架外形</span><select data-testid="support-style" value={layout.style} onChange={event => onChange({ style: event.target.value as SupportBox['style'] })}><option value="bracket">塑料支架</option><option value="pedestal">{belowBeam ? '倒挂底座' : '凸起底座'}</option></select></label></div><label className="numeric-field mount-rail"><span className="field-label">固定杆件</span><select data-testid="support-rail" value={layout.rail} disabled={supportId === 'front'} onChange={event => onChange({ rail: event.target.value })}>{rails.map(rail => <option key={rail.id} value={rail.id}>{rail.label}</option>)}</select></label><label className="rail-position"><span><span>支架沿梁位置</span><b>{number(layout.t * 100, 0)}<small>%</small></b></span><input type="range" data-testid="support-position" aria-label="支架沿梁位置" min="0" max="1" step=".001" value={layout.t} onChange={event => onChange({ t: Number(event.target.value) })} /><span className="rail-endpoints"><span>{formatLength(box.rail.start[box.rail.axis])} cm</span><span>{formatLength(box.rail.end[box.rail.axis])} cm</span></span></label></details>
    </div>
  </details>;
}
function OpticsPreview({ sensor }: { sensor: Sensor }) {
  const half = sensor.hfov / 2 * Math.PI / 180, radius = 32;
  const x = 73, y = 44, edgeX = Math.sin(half) * radius, edgeY = Math.cos(half) * radius;
  const cone = sensor.hfov >= 359 ? '' : `M${x} ${y}L${x-edgeX} ${y-edgeY}A${radius} ${radius} 0 ${sensor.hfov > 180 ? 1 : 0} 1 ${x+edgeX} ${y-edgeY}Z`;
  const pitch = sensor.pitch * Math.PI / 180, vfov = sensor.vfov * Math.PI / 180;
  const center = pitch - (sensor.verticalOffset ?? 0) * Math.PI / 180;
  const sx = 217, sy = 43, r = 30;
  const top = { x: sx + Math.cos(center-vfov/2)*r, y: sy + Math.sin(center-vfov/2)*r };
  const bottom = { x: sx + Math.cos(center+vfov/2)*r, y: sy + Math.sin(center+vfov/2)*r };
  return <div className="optics-preview" style={{ '--sensor': sensor.color } as CSSProperties}><svg viewBox="0 0 318 95" role="img" aria-label={`水平视野${number(sensor.hfov, 1)}度，垂直视野${number(sensor.vfov, 1)}度，向下倾斜${number(sensor.pitch, 1)}度，视场示意`}>
    <path d="M24 44H122M73 9V77M177 74H299M186 43H287" stroke="#34485e" strokeWidth=".7" strokeDasharray="3 4" />
    {sensor.hfov >= 359 ? <circle cx={x} cy={y} r={radius} className="optics-cone" /> : <path d={cone} className="optics-cone" />}
    <circle cx={x} cy={y} r="3" className="optics-origin" /><path d="m69 39 4-6 4 6" className="optics-line" />
    <text x="73" y="88" textAnchor="middle">水平 {number(sensor.hfov, 1)}°</text>
    <rect x="203" y="37" width="14" height="12" rx="3" fill="#243b50" stroke="#668398" />
    <path d={`M${sx} ${sy}L${top.x} ${top.y}A${r} ${r} 0 0 1 ${bottom.x} ${bottom.y}Z`} className="optics-cone" />
    <path d={`M${sx} ${sy}L${sx+Math.cos(pitch)*r} ${sy+Math.sin(pitch)*r}`} className="optics-line" />
    <text x="243" y="88" textAnchor="middle">{sensor.pitch >= 0 ? '下倾' : '上仰'} {number(Math.abs(sensor.pitch), 1)}°</text>
  </svg><span className="optics-separator" /></div>;
}
function SensorPanel({ sensor, config, onChange, onSelectSupport, coverage, area }: {
  sensor: Sensor; config: Config; onChange: (changes: Partial<Sensor>) => void; onSelectSupport: () => void; coverage: number; area: number;
}) {
  const box = supportForSensor(config, sensor.id);
  const yaw = ((sensor.yaw + 180) % 360 + 360) % 360 - 180;
  const preset = directions.find(item => Math.abs((((item.value + 180) % 360 + 360) % 360 - 180) - yaw) < .01);
  return <section data-testid={`sensor-${sensor.id}-editor`} className={`sensor-card is-open ${!sensor.enabled ? 'is-disabled' : ''}`} style={{ '--sensor': sensor.color } as CSSProperties}>
    <div className="editor-heading"><span className="sensor-symbol"><Icon name={(sensor.kind === 'lidar' || sensor.kind === 'scan') ? 'lidar' : 'camera'} size={21} /></span><div><h3>{sensorLabel(sensor).name}</h3><p>{sensorLabel(sensor).detail}</p></div><span className={`calculation-badge ${sensor.enabled ? '' : 'is-off'}`}>{sensor.enabled ? '参与计算' : '已排除'}</span></div>
    <div className="sensor-body"><div className="sensor-coverage"><span>切片面积</span><b>{formatArea(coverage * area)}<small> cm²</small></b><span className="coverage-track"><i style={{ width: `${Math.min(100, coverage * 100)}%` }} /></span></div>
      <section className="parameter-section"><div className="field-heading"><span className="parameter-icon"><Icon name="move" size={15} /></span>盒内位置 <span className="position-unit">cm</span><button type="button" className="mount-badge" data-testid="edit-sensor-support" onClick={onSelectSupport}>{box.id === 'front' ? '共用安装空间' : '调整安装空间'}<Icon name="cube" size={12} /></button></div><div className="field-grid three">{(['x', 'y', 'z'] as const).map(axis => <LengthField key={axis} label={{ x: 'X 前后', y: 'Y 左右', z: 'Z 高度' }[axis]} testId={`sensor-${sensor.id}-${axis}`} value={sensor[axis]} onChange={value => onChange({ [axis]: value })} min={box.min[axis]} max={box.max[axis]} step={.005} />)}</div><div className="box-point-sliders">{(['x', 'y', 'z'] as const).map(axis => <label key={axis}><span>{axis.toUpperCase()}</span><input type="range" data-testid={`sensor-${sensor.id}-local-${axis}`} aria-label={`${sensorLabel(sensor).name}盒内${axis.toUpperCase()}位置`} min="0" max="1" step=".001" value={sensor.inSupport?.[axis] ?? .5} onChange={event => onChange({ [axis]: box.min[axis] + Number(event.target.value) * (box.max[axis] - box.min[axis]) })} /><small>{number((sensor.inSupport?.[axis] ?? .5) * 100, 0)}%</small></label>)}</div></section>
      <section className="parameter-section"><div className="field-heading"><span className="parameter-icon"><Icon name="target" size={15} /></span>朝向与倾角</div>
      <div className="direction-presets" role="group" aria-label={`${sensorLabel(sensor).name}朝向快捷设置`}>{directions.map(item => <button type="button" key={item.value} data-testid={`sensor-${sensor.id}-yaw-${item.value}`} aria-label={`${sensorLabel(sensor).name}${item.label}`} aria-pressed={preset?.value === item.value} className={preset?.value === item.value ? 'active' : ''} onClick={() => onChange({ yaw: item.value })}><span style={{ transform: `rotate(${-item.value}deg)` }}>↑</span>{item.label.replace('朝', '')}</button>)}</div>
      <div className="field-grid"><NumericField label="偏航角 · Yaw" value={sensor.yaw} onChange={yaw => onChange({ yaw })} unit="°" min={-180} max={180} step={1} /><NumericField label="向下倾角 · Pitch" value={sensor.pitch} onChange={pitch => onChange({ pitch })} unit="°" min={-90} max={90} step={1} /></div>
      <OpticsPreview sensor={sensor} /></section>
      <section className="parameter-section"><div className="field-heading"><span className="parameter-icon"><Icon name="camera" size={15} /></span>视野与量程</div><div className="field-grid"><NumericField label="水平视野" value={sensor.hfov} onChange={hfov => onChange({ hfov })} unit="°" min={1} max={sensor.kind === 'lidar' || sensor.kind === 'scan' ? 360 : 179} step={1} /><NumericField label="垂直视野" value={sensor.vfov} onChange={vfov => onChange({ vfov })} unit="°" min={sensor.kind === 'scan' ? 0 : 1} max={sensor.kind === 'scan' ? 0 : 179} step={1} disabled={sensor.kind === 'scan'} /><LengthField label="最小距离" value={sensor.minRange} onChange={minRange => onChange({ minRange: Math.min(minRange, sensor.maxRange - .01) })} min={0} max={Math.min(29.99, Math.max(0, sensor.maxRange - .01))} step={.05} /><LengthField label={sensor.kind === 'camera' ? '查看距离' : '最大距离'} value={sensor.maxRange} onChange={maxRange => onChange({ maxRange: Math.max(maxRange, sensor.minRange + .01) })} min={Math.max(.01, sensor.minRange + .01)} max={30} step={.1} /></div>
      {sensor.kind === 'lidar' && <NumericField label="垂直中心偏置" value={sensor.verticalOffset ?? 0} onChange={verticalOffset => onChange({ verticalOffset })} unit="°" min={-(90 - sensor.vfov / 2)} max={90 - sensor.vfov / 2} step={.5} />}</section>
    </div>
  </section>;
}
function Fold({ title, icon, children }: { title: string; icon: string; children: ReactNode }) {
  return <details className="settings-fold"><summary><Icon name={icon} size={17} /><span>{title}</span><Icon name="down" size={15} /></summary><div className="settings-body">{children}</div></details>;
}
function ComparisonDetail({ value, id, unit, improve, digits = 1 }: { value: ComparisonValue | null; id: string; unit: string; improve?: 'increase' | 'decrease'; digits?: number }) {
  if (!value) return <span className="metric-comparison unavailable" data-testid={`${id}-delta`}>当前区域无有效网格</span>;
  const areaUnit = unit === 'cm²';
  const delta = areaUnit ? value.delta * 10_000 : value.delta;
  const baseline = areaUnit ? value.baseline * 10_000 : value.baseline;
  const rounded = Math.abs(delta) < .5 * 10 ** -digits ? 0 : delta;
  const direction = rounded > 0 ? '↑' : rounded < 0 ? '↓' : '';
  const tone = !rounded || !improve ? 'neutral' : (improve === 'increase' ? rounded > 0 : rounded < 0) ? 'better' : 'worse';
  return <span className="metric-comparison"><span data-testid={`${id}-baseline`}>基准 {number(baseline, digits)}{areaUnit ? ' cm²' : '%'}</span><b className={tone} data-testid={`${id}-delta`} aria-label={`较基准${rounded > 0 ? '增加' : rounded < 0 ? '减少' : '变化'} ${number(Math.abs(rounded), digits)} ${unit}`}>{direction}{number(Math.abs(rounded), digits)}<em>{unit}</em></b></span>;
}
export default function App() {
  const session = useSession();
  const [config, setConfig] = useState<Config>(loadConfig);
  const [interactionRevision, setInteractionRevision] = useState(0);
  const invalidateInteractions = () => setInteractionRevision(value => value + 1);
  const [baseline, setBaseline] = useState<Config>(() => {
    try { return loadBaseline(localStorage.getItem(BASELINE_STORAGE_KEY), config); }
    catch { return sanitizeConfig(config); }
  });
  const configKey = JSON.stringify(config);
  const recommendation = useCameraRecommendation(config, session.approved);
  const [previewRecommendation, setPreviewRecommendation] = useState(false);
  const [appliedRecommendation, setAppliedRecommendation] = useState<{ before: Config; result: RecommendationResult; afterKey: string } | null>(null);
  const appliedCurrent = appliedRecommendation?.afterKey === configKey;
  const freshRecommendation = recommendation.sourceKey === configKey ? recommendation.result : null;
  const visibleRecommendation = freshRecommendation ?? (appliedCurrent ? appliedRecommendation!.result : null);
  const activeCameras = config.sensors.some(sensor => sensor.enabled && (sensor.id === 'camera3' || sensor.id === 'camera4'));
  useEffect(() => {
    setPreviewRecommendation(false);
    setAppliedRecommendation(value => value?.afterKey === configKey ? value : null);
  }, [configKey]);
  const [dragEnabled, setDragEnabled] = useState(true), [dragging, setDragging] = useState(false);
  const configToSave = useRef(config);
  const saveTimer = useRef<number | null>(null);
  const [selected, setSelected] = useState('camera1');
  const [view, setView] = useState({ mode: 'perspective' as 'perspective' | 'top' | 'mounting', nonce: 0 });
  const [showVolumes, setShowVolumes] = useState(true), [showLabels, setShowLabels] = useState(false), [showOverlap, setShowOverlap] = useState(true);
  const [volumeScope, setVolumeScope] = useState<'selected' | 'all'>('selected');
  const [errorMessage, setErrorMessage] = useState('');
  const fileInput = useRef<HTMLInputElement>(null);
  const panelScroll = useRef<HTMLDivElement>(null);
  const coverage = useMemo(() => computeCoverage(config), [config]);
  const enabled = config.sensors.filter(sensor => sensor.enabled).length;
  const conditionKey = JSON.stringify({ analysis: config.analysis, chassis: config.chassis });
  const baselineCoverage = useMemo(() => computeBaselineCoverage(baseline, config), [baseline, conditionKey]);
  const comparison = compareCoverage(coverage, baselineCoverage);
  const selectedSensor = config.sensors.find(sensor => sensor.id === selected);
  const selectedSupport = selected.startsWith('support:') && SUPPORT_IDS.includes(selected.slice(8) as SupportId) ? selected.slice(8) as SupportId : null;
  const activeSupportId = selectedSupport ?? supportIdForSensor(selectedSensor?.id ?? 'camera1');
  const activeSupport = supportGeometry(config, activeSupportId);
  function editSensor(id: string) {
    setSelected(id);
    window.requestAnimationFrame(() => {
      const scroller = panelScroll.current;
      const editor = scroller?.querySelector<HTMLElement>(id.startsWith('support:') ? '[data-testid="mount-support-editor"]' : `[data-testid="sensor-${id}-editor"]`);
      if (!scroller || !editor) return;
      // Scroll this panel only: selecting a device must never move the mobile page.
      const top = scroller.scrollTop + editor.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 2;
      scroller.scrollTo({ top: Math.max(0, top), behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
    });
  }
  function startRecommendation() {
    setPreviewRecommendation(false);
    recommendation.start();
    window.requestAnimationFrame(() => panelScroll.current?.scrollTo({ top: 0, behavior: 'auto' }));
  }
  function applyRecommendation() {
    if (!freshRecommendation || freshRecommendation.status !== 'improved' || !freshRecommendation.movedIds.length || recommendation.status !== 'ready') return;
    if (!recommendationIsApplicable(freshRecommendation)) { setErrorMessage('推荐未满足左右对称或覆盖保护条件，请重新计算'); return; }
    let next: Config;
    try { next = applySideRecommendation(config, freshRecommendation.config); }
    catch { recommendation.cancel(); setErrorMessage('推荐结果已失效，请重新计算'); return; }
    setAppliedRecommendation({ before: config, result: freshRecommendation, afterKey: JSON.stringify(next) });
    setPreviewRecommendation(false);
    invalidateInteractions();
    setConfig(next);
    setErrorMessage('');
  }
  function undoRecommendation() {
    if (!appliedRecommendation || appliedRecommendation.afterKey !== configKey) return;
    recommendation.cancel();
    invalidateInteractions();
    setConfig(appliedRecommendation.before);
    setAppliedRecommendation(null);
    setPreviewRecommendation(false);
    setErrorMessage('');
  }
  function saveCurrentConfig() {
    if (saveTimer.current !== null) { clearTimeout(saveTimer.current); saveTimer.current = null; }
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(configToSave.current)); } catch { /* Browser storage is optional. */ }
  }
  useEffect(() => {
    configToSave.current = config;
    if (saveTimer.current === null) saveTimer.current = window.setTimeout(saveCurrentConfig, 200);
  }, [config]);
  useEffect(() => {
    window.addEventListener('pagehide', saveCurrentConfig);
    return () => { window.removeEventListener('pagehide', saveCurrentConfig); saveCurrentConfig(); };
  }, []);
  useEffect(() => {
    try { localStorage.setItem(BASELINE_STORAGE_KEY, JSON.stringify({ version: 1, config: baseline })); } catch { /* Comparison remains available without browser storage. */ }
  }, [baseline]);
  function dragStateChanged(active: boolean) {
    setDragging(active);
    if (!active) window.setTimeout(saveCurrentConfig, 0);
  }
  useEffect(() => { if (!errorMessage) return; const timer = window.setTimeout(() => setErrorMessage(''), 5000); return () => clearTimeout(timer); }, [errorMessage]);
  function updateSensor(id: string, change: Partial<Sensor>, fromScene = false) {
    if (!fromScene) invalidateInteractions();
    setConfig(value => {
      const original = value.sensors.find(sensor => sensor.id === id);
      if (!original) return value;
      let next = { ...value, sensors: value.sensors.map(sensor => sensor.id === id ? { ...sensor, ...change } : sensor) };
      if (['x', 'y', 'z'].some(axis => Object.prototype.hasOwnProperty.call(change, axis))) {
        next = moveSensorWithinSupport(next, id, { x: change.x ?? original.x, y: change.y ?? original.y, z: change.z ?? original.z });
      }
      return sanitizeConfig(next);
    });
  }
  function updateSupport(id: SupportId, change: Partial<SupportBox>) {
    invalidateInteractions();
    setConfig(value => sanitizeConfig({ ...value, supports: { ...value.supports, [id]: { ...value.supports?.[id], ...change } } }));
  }
  function moveSupport(id: SupportId, center: { x: number; y: number; z: number }) {
    setConfig(value => sanitizeConfig(moveSupportAlongRail(value, id, center)));
  }
  function restoreMove(id: string, snapshot: { inSupport?: { x: number; y: number; z: number }; t?: number }) {
    setConfig(value => {
      if (id.startsWith('support:') && typeof snapshot.t === 'number') {
        const key = id.slice(8) as SupportId;
        if (!SUPPORT_IDS.includes(key)) return value;
        return sanitizeConfig({ ...value, supports: { ...value.supports, [key]: { ...value.supports?.[key], t: snapshot.t } } });
      }
      if (!snapshot.inSupport) return value;
      return sanitizeConfig({ ...value, sensors: value.sensors.map(sensor => sensor.id === id ? { ...sensor, inSupport: { ...snapshot.inSupport! } } : sensor) });
    });
  }
  function editSupport(id: SupportId) {
    editSensor(`support:${id}`);
    setView(value => ({ mode: 'mounting', nonce: value.nonce + 1 }));
  }
  function updateAnalysis(change: Partial<Analysis>) { invalidateInteractions(); setConfig(value => sanitizeConfig({ ...value, analysis: { ...value.analysis, ...change } })); }
  function updateChassis(change: Partial<Chassis>) { invalidateInteractions(); setConfig(value => sanitizeConfig({ ...value, chassis: { ...value.chassis, ...change } })); }
  function updateMiddleHeight(side: FrameSide, height: number) {
    invalidateInteractions();
    setConfig(value => {
      const previous = value.chassis;
      const fallback = previous.frameMidHeight ?? previous.frameHeight / 2;
      const heights = { front: fallback, rear: fallback, left: fallback, right: fallback, ...previous.frameMidHeights, [side]: height };
      return sanitizeConfig({ ...value, chassis: { ...previous, frameMidHeights: heights } });
    });
  }

  function exportConfig() {
    const url = URL.createObjectURL(new Blob([JSON.stringify({ version: 1, config }, null, 2)], { type: 'application/json' }));
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = '传感器覆盖配置.json'; anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000); setErrorMessage('');
  }
  async function importConfig(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]; event.target.value = ''; if (!file) return;
    try {
      if (file.size > 131072) throw new Error('文件不能超过 128 KB');
      let raw: unknown; try { raw = JSON.parse(await file.text()); } catch { throw new Error('文件不是有效的 JSON'); }
      if (!raw || typeof raw !== 'object' || !('version' in raw) || raw.version !== 1 || !('config' in raw) || !raw.config || typeof raw.config !== 'object' || !('sensors' in raw.config) || !Array.isArray(raw.config.sensors) || !('analysis' in raw.config) || !('chassis' in raw.config)) throw new Error('请选择本页面导出的版本 1 配置文件');
      invalidateInteractions(); setConfig(sanitizeConfig(raw.config)); setErrorMessage('');
    } catch (error) { setErrorMessage(`导入失败：${error instanceof Error ? error.message.slice(0, 100) : '无法读取配置文件'}`); }
  }
  if (!session.approved) return <main className="session-gate"><div className="brand-mark"><Icon name="sensors" size={30} /></div><h1>传感器覆盖与盲区</h1><p>{session.error || '正在连接观测系统…'}</p>{session.error && <button className="button primary" onClick={session.retry}>重新连接</button>}<a href="/">返回观测</a></main>;
  return <div className="app-shell"><header className="topbar"><a className="back-link" href="/#robot/sensors"><Icon name="arrow" size={19} /><span>实时观测</span></a><span className="header-divider" /><div className="page-title"><Icon name="sensors" size={24} /><div><h1>传感器覆盖与盲区</h1></div></div><div className="header-actions"><button className="button icon-button" aria-label="导入配置" onClick={() => fileInput.current?.click()}><Icon name="upload" /><span>导入</span></button><button className="button" aria-label="导出配置" onClick={exportConfig}><Icon name="download" /><span>导出配置</span></button><input ref={fileInput} type="file" accept=".json,application/json" hidden onChange={event => { void importConfig(event); }} /></div></header>
    {session.error && <div className="session-warning" role="status">{session.error}<button onClick={session.retry}>重试</button></div>}
    <main className="workspace"><aside className="config-panel" aria-label="传感器参数配置"><div className="panel-title"><div><h2>传感器配置</h2></div><b><span className="enabled-count">{enabled}</span><span> / {config.sensors.length}</span></b></div><SensorPicker sensors={config.sensors} selected={selected} onSelect={editSensor} onChange={updateSensor} /><div className="recommend-launch"><button type="button" className="button recommend-start" data-testid="recommend-start" disabled={recommendation.status === 'running' || !activeCameras} onClick={startRecommendation}><Icon name="target" size={15} />{recommendation.status === 'running' ? '计算中…' : '推荐左右相机位置与角度'}</button>{recommendation.status === 'running' && <button type="button" className="button" data-testid="recommend-cancel" onClick={recommendation.cancel}>取消</button>}</div><div className="panel-scroll" ref={panelScroll}><RecommendationPanel phase={recommendation.status} progress={recommendation.progress} error={recommendation.error} result={visibleRecommendation} applied={!freshRecommendation && appliedCurrent} canUndo={appliedCurrent} preview={previewRecommendation} onPreview={setPreviewRecommendation} onApply={applyRecommendation} onUndo={undoRecommendation} /><details className="layout-fold" data-testid="layout-fold"><summary><Icon name="target" size={15} />安装俯视图<Icon name="down" size={14} /></summary><LayoutMap config={config} selected={selected} onSelect={editSensor} onEdit={() => editSensor(selected)} /></details><SupportPanel key={`support-${activeSupportId}`} config={config} supportId={activeSupportId} selected={selectedSupport !== null} onSelect={() => editSupport(activeSupportId)} onChange={change => updateSupport(activeSupportId, change)} />{selectedSensor && <SensorPanel key={`sensor-${selectedSensor.id}`} sensor={selectedSensor} config={config} onSelectSupport={() => editSupport(activeSupportId)} onChange={change => updateSensor(selectedSensor.id, change)} coverage={(coverage.perSensor[selectedSensor.id] || 0) / Math.max(1, coverage.total)} area={coverage.area} />}
      <div className="additional-settings"><Fold title="分析区域与统计" icon="layers"><div className="field-grid"><LengthField label="区域长度 · X" testId="analysis-length" value={config.analysis.length} onChange={length => updateAnalysis({ length })} min={.5} max={12} step={.5} /><LengthField label="区域宽度 · Y" testId="analysis-width" value={config.analysis.width} onChange={width => updateAnalysis({ width })} min={.5} max={12} step={.5} /><LengthField label="高度切片" testId="analysis-height" value={config.analysis.height} onChange={height => updateAnalysis({ height })} min={0} max={4} step={.05} /><LengthField label="网格精度" testId="analysis-resolution" value={config.analysis.resolution} onChange={resolution => updateAnalysis({ resolution })} min={.04} max={.2} step={.02} /></div><label className="check-field"><input type="checkbox" checked={config.analysis.occlusion} onChange={event => updateAnalysis({ occlusion: event.target.checked })} /><span>计算底盘实体遮挡</span></label></Fold><Fold title="底盘与铝合金架" icon="cube"><div className="field-grid">
        <LengthField label="底盘长度" testId="chassis-length" value={config.chassis.length} onChange={length => updateChassis({ length })} min={.2} max={4} />
        <LengthField label="底盘宽度" testId="chassis-width" value={config.chassis.width} onChange={width => updateChassis({ width })} min={.2} max={3} />
        <LengthField label="底盘高度" testId="chassis-height" value={config.chassis.height} onChange={height => updateChassis({ height })} min={.05} max={2} />
        <LengthField label="框架长度" testId="chassis-frame-length" value={config.chassis.frameLength ?? config.chassis.length} onChange={frameLength => updateChassis({ frameLength })} min={.2} max={4} />
        <LengthField label="框架高度" testId="chassis-frame-height" value={config.chassis.frameHeight} onChange={frameHeight => updateChassis({ frameHeight })} min={.1} max={3} />
        <LengthField label="框架宽度" testId="chassis-frame-width" value={config.chassis.frameWidth} onChange={frameWidth => updateChassis({ frameWidth })} min={.1} max={3} />
      </div><div className="field-heading separated">中层横梁</div>
      <div className="field-grid">{(['front', 'rear', 'left', 'right'] as const).map(side => <LengthField key={side} label={{ front: '前侧中梁', rear: '后侧中梁', left: '左侧中梁', right: '右侧中梁' }[side]} testId={`chassis-mid-${side}`} value={config.chassis.frameMidHeights?.[side] ?? config.chassis.frameMidHeight ?? config.chassis.frameHeight / 2} onChange={height => updateMiddleHeight(side, height)} min={.05} max={Math.max(.05, config.chassis.frameHeight - .05)} />)}</div>
      </Fold></div>
      </div><div className="panel-footer"><button type="button" onClick={() => { invalidateInteractions(); setConfig(sanitizeConfig(DEFAULT_CONFIG)); setSelected('camera1'); setErrorMessage(''); }}><Icon name="reset" size={13} />恢复默认</button></div></aside>
      <section className="visual-panel" aria-label="三维覆盖与盲区可视化"><div className="visual-toolbar"><div className="view-switch" role="group" aria-label="观察视角"><button data-testid="view-perspective" className={view.mode === 'perspective' ? 'active' : ''} onClick={() => setView(value => ({ mode: 'perspective', nonce: value.nonce + 1 }))}><Icon name="cube" size={15} />三维</button><button data-testid="view-top" className={view.mode === 'top' ? 'active' : ''} onClick={() => setView(value => ({ mode: 'top', nonce: value.nonce + 1 }))}><Icon name="target" size={15} />俯视</button><button data-testid="view-mounting" className={view.mode === 'mounting' ? 'active' : ''} onClick={() => setView(value => ({ mode: 'mounting', nonce: value.nonce + 1 }))}><Icon name="camera" size={15} />近景</button></div><button type="button" data-testid="drag-toggle" className={`button drag-toggle ${dragEnabled ? 'is-active' : ''}`} aria-pressed={dragEnabled} onClick={() => setDragEnabled(value => !value)}><Icon name="move" size={15} />调整位置</button><div className="display-toggles"><label><input data-testid="volume-toggle" type="checkbox" checked={showVolumes} onChange={event => setShowVolumes(event.target.checked)} />视场</label>{showVolumes && <select className="volume-scope" data-testid="volume-scope" aria-label="视场显示范围" value={volumeScope} onChange={event => setVolumeScope(event.target.value as 'selected' | 'all')}><option value="selected">当前设备</option><option value="all">全部设备</option></select>}<label><input data-testid="labels-toggle" type="checkbox" checked={showLabels} onChange={event => setShowLabels(event.target.checked)} />全部标签</label><label><input data-testid="overlap-toggle" type="checkbox" checked={showOverlap} onChange={event => setShowOverlap(event.target.checked)} />重叠</label></div></div>
      <div className="comparison-toolbar" data-testid="comparison-toolbar"><div className="comparison-status"><span className="comparison-icon"><Icon name="layers" size={17} /></span><span className="comparison-label">基准对比</span></div><div className="comparison-actions"><button type="button" data-testid="restore-baseline" onClick={() => { invalidateInteractions(); setConfig(sanitizeConfig(baseline)); setErrorMessage(''); }}><Icon name="reset" size={13} />恢复</button><button type="button" className="set-baseline" data-testid="set-baseline" onClick={() => { setBaseline(sanitizeConfig(config)); setErrorMessage(''); }}><Icon name="target" size={13} />设为基准</button></div></div>
      <div className="scene-wrap"><Scene recommendation={previewRecommendation && freshRecommendation?.status === 'improved' ? freshRecommendation.config : null} config={config} coverage={coverage} selected={selected} view={view} showVolumes={showVolumes} volumeScope={volumeScope} showLabels={showLabels} showOverlap={showOverlap} onSelect={setSelected} dragEnabled={dragEnabled} onMove={(id, position) => updateSensor(id, position, true)} onSupportMove={moveSupport} onRestoreMove={restoreMove} interactionRevision={interactionRevision} onDragState={dragStateChanged} /><div className="slice-caption"><span className="eyebrow">切片 Z</span><div><b>{formatLength(config.analysis.height)}</b><span> cm</span></div><label className="slice-slider"><span>0</span><input aria-label="高度切片滑块" aria-valuetext={`${formatLength(config.analysis.height)} cm`} type="range" min="0" max="400" step="5" value={toCentimeters(config.analysis.height)} onChange={event => updateAnalysis({ height: fromCentimeters(Number(event.target.value)) })} /><span>400 cm</span></label></div><div className={`drag-status ${dragging ? 'is-dragging' : ''}`} data-testid="drag-status">{selectedSensor && <span data-testid="selected-coordinates">{selectedSensor.id === 'mid360' ? 'MID360' : selectedSensor.id === 'p100r' ? 'P100R' : selectedSensor.id} · X {formatLength(selectedSensor.x)} / Y {formatLength(selectedSensor.y)} / Z {formatLength(selectedSensor.z)} cm</span>}{selectedSupport && <span data-testid="selected-coordinates">安装空间 · {formatLength(activeSupport.center.x)} / {formatLength(activeSupport.center.y)} / {formatLength(activeSupport.center.z)} cm</span>}</div>{selectedSensor && <div className="selected-sensor-summary" style={{ '--sensor': selectedSensor.color } as CSSProperties}><div className="summary-title"><span className="summary-point" /><strong>{sensorLabel(selectedSensor).name}</strong><span className="summary-spec">{number(selectedSensor.hfov, 0)}° · {formatLength(selectedSensor.maxRange)} cm</span></div></div>}{selectedSupport && <div className="selected-sensor-summary plate-summary"><div className="summary-title"><span className="summary-point" /><strong>{supportNames[selectedSupport]} · 安装空间</strong><span className="summary-spec">{formatLength(activeSupport.layout.sizeX)} × {formatLength(activeSupport.layout.sizeY)} × {formatLength(activeSupport.layout.sizeZ)} cm</span></div></div>}<div className="scene-compass"><span className="axis-x">X 前</span><span className="axis-y">Y 左</span><span className="axis-z">Z 上</span></div><div className="scene-bottom"><div className="legend"><span className="legend-covered"><i className="covered" />覆盖</span><span className="legend-blind"><i className="blind" />盲区</span>{showOverlap && <span className="legend-overlap"><i className="overlap" />重叠</span>}</div></div></div>
      <div className="metrics" aria-live="polite"><article className="metric coverage-metric"><span className="metric-label"><i className="status-dot cyan" />覆盖率</span><div><strong>{number(coverage.percent, 1)}</strong><span>%</span></div><span className="metric-progress"><i style={{ width: `${Math.min(100, coverage.percent)}%` }} /></span><ComparisonDetail value={comparison.coverage} id="coverage" unit="百分点" improve="increase" /></article><article className="metric blind-metric area-metric"><span className="metric-label"><i className="status-dot red" />盲区</span><div><strong>{formatArea(coverage.blindArea)}</strong><span>cm²</span></div><ComparisonDetail value={comparison.blind} id="blind" unit="cm²" improve="decrease" /></article><article className="metric overlap-metric"><span className="metric-label"><i className="status-dot purple" />重叠率</span><div><strong>{number(coverage.total ? coverage.overlap / coverage.total * 100 : 0, 1)}</strong><span>%</span></div><ComparisonDetail value={comparison.overlap} id="overlap" unit="百分点" /></article><article className="metric area-metric"><span className="metric-label">分析面积</span><div><strong>{formatArea(coverage.area)}</strong><span>cm²</span></div></article></div></section>
    </main>{errorMessage && <div className="toast" role="alert">{errorMessage}<button aria-label="关闭错误" onClick={() => setErrorMessage('')}>×</button></div>}
  </div>;
}
