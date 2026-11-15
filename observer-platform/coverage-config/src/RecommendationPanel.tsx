import type { RecommendationResult } from './optimizer';
import { recommendationIsApplicable, recommendationIsProtected, recommendationIsSymmetric } from './recommendation-state';
import { formatArea, formatLength } from './units';

const labels: Record<string, string> = { camera3: '左视相机', camera4: '右视相机' };
const ids = Object.keys(labels);
const percent = (value: number) => value.toFixed(2);
const angle = (value: number) => Number(value.toFixed(1)).toLocaleString('zh-CN', { useGrouping: false });
const signed = (value: number) => `${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value).toLocaleString('zh-CN')}`;
const signedLength = (value: number) => `${value > 1e-8 ? '+' : value < -1e-8 ? '−' : ''}${formatLength(Math.abs(value) / 100)}`;
const nearRegions = [
  { key: 'left', label: '左侧近场' },
  { key: 'right', label: '右侧近场' },
  { key: 'leftRear', label: '左后近场' },
  { key: 'rightRear', label: '右后近场' },
  { key: 'rear', label: '后方近场' },
] as const;

export default function RecommendationPanel({ phase, progress, error, result, applied, canUndo, preview, onPreview, onApply, onUndo }: {
  phase: 'idle' | 'running' | 'ready' | 'error'; progress: number; error: string;
  result: RecommendationResult | null; applied: boolean; canUndo: boolean; preview: boolean;
  onPreview: (value: boolean) => void; onApply: () => void; onUndo: () => void;
}) {
  if (phase === 'running') return <section className="recommendation-card" data-testid="recommend-panel" data-status="running" aria-live="polite"><div className="recommend-heading"><strong>计算中</strong><span>{Math.round(progress * 100)}%</span></div><progress data-testid="recommend-progress" max="1" value={progress} aria-label="左右相机推荐计算进度" /></section>;
  if (phase === 'error') return <section className="recommendation-card recommendation-error" data-testid="recommend-panel" data-status="error" role="status">{error || '计算未完成，请重试'}{canUndo && <div className="recommend-actions"><button type="button" className="button" data-testid="recommend-undo" onClick={onUndo}>撤销上次</button></div>}</section>;
  if (!result) return null;
  if (result.status === 'no-cameras' || result.status === 'no-area') return <section className="recommendation-card" data-testid="recommend-panel" data-status={result.status} role="status"><span data-testid="recommend-reason">{result.reason || (result.status === 'no-cameras' ? '请同时启用左右相机' : '当前切片没有可分析区域')}</span></section>;
  const hasChanges = result.status === 'improved' && result.movedIds.length > 0;
  const totalGain = result.after.overallCovered - result.before.overallCovered;
  const delta = result.commonDeltaCm;
  const nearBefore = result.before.nearField, nearAfter = result.after.nearField;
  const nearGain = nearBefore && nearAfter ? nearAfter.covered - nearBefore.covered : null;
  const coverageProtected = recommendationIsProtected(result);
  const symmetric = recommendationIsSymmetric(result.config);
  const improved = hasChanges && recommendationIsApplicable(result);
  const currentOnly = !improved && !applied;
  const beamMidpoint = result.beamMidpointXCm, boxCenter = result.recommendedBoxCenterXCm;
  const beforeDistance = result.beforeCenterDistanceCm, afterDistance = result.afterCenterDistanceCm;
  const centering = typeof beamMidpoint === 'number' && Number.isFinite(beamMidpoint) && typeof boxCenter === 'number' && Number.isFinite(boxCenter)
    && typeof beforeDistance === 'number' && Number.isFinite(beforeDistance) && typeof afterDistance === 'number' && Number.isFinite(afterDistance)
    ? { beamMidpoint, boxCenter, beforeDistance, afterDistance } : null;
  const centerOffset = centering ? centering.boxCenter - centering.beamMidpoint : 0;
  const gainText = hasChanges && !coverageProtected ? '未通过覆盖保护' : hasChanges && !symmetric ? '未满足对称约束' : !improved ? '保留当前布局' : centering ? `偏移 ${signedLength(centerOffset)} cm` : '已计算';
  const leftAlignment = result.alignmentOffsetsCm?.camera3 ?? 0, rightAlignment = result.alignmentOffsetsCm?.camera4 ?? 0;
  const hasAlignment = Math.abs(leftAlignment) > 1e-8 || Math.abs(rightAlignment) > 1e-8;
  return <section className="recommendation-card" data-testid="recommend-panel" data-status={applied ? 'applied' : result.status} data-symmetric={symmetric} data-protected={coverageProtected} data-policy={result.coverageProtection ?? 'all-regions'}>
    <div className="recommend-heading"><strong>{applied ? '已应用左右推荐' : currentOnly ? '未找到满足约束的推荐' : '左右相机推荐'}</strong><span data-testid="recommend-center-gain" className={improved ? 'recommend-gain' : ''}>{gainText}</span></div>
    {!currentOnly && centering && <div className="recommend-centering" data-testid="recommend-center-summary"><div className="recommend-center-values"><div><span>横梁中点 X</span><b data-testid="recommend-beam-midpoint" data-value-cm={centering.beamMidpoint}>{formatLength(centering.beamMidpoint / 100)} <small>cm</small></b></div><div><span>推荐两盒中心 X</span><b data-testid="recommend-box-center" data-value-cm={centering.boxCenter}>{formatLength(centering.boxCenter / 100)} <small>cm</small></b></div></div><div className="recommend-center-distance"><span>距横梁中点</span><span data-testid="recommend-center-distance-before" data-value-cm={centering.beforeDistance}>{formatLength(centering.beforeDistance / 100)} cm</span><i>→</i><b data-testid="recommend-center-distance-after" data-value-cm={centering.afterDistance}>{formatLength(centering.afterDistance / 100)} cm</b></div><div className="recommend-center-distance"><span>中点偏移 X</span><b data-testid="recommend-center-offset" data-value-cm={centerOffset}>{signedLength(centerOffset)} cm</b></div></div>}
    <div className="recommend-near-heading"><span>两侧及侧后近场</span><span className={nearGain !== null && nearGain < 0 ? 'is-decrease' : ''} data-testid="recommend-near-gain">{nearGain === null ? '—' : `近场 ${signed(nearGain)} 格`}</span></div>
    {nearBefore && nearAfter && <>
      <div className="recommend-metrics recommend-secondary-metrics">
        <div><span>近场覆盖率</span><p><span data-testid="recommend-near-before">{percent(nearBefore.percent)}%</span><i>→</i><b className={nearGain !== null && nearGain < 0 ? 'is-decrease' : ''} data-testid="recommend-near-after">{percent(nearAfter.percent)}%</b></p></div>
        <div className="recommend-blind-metric"><span>近场盲区 · cm²</span><p><span data-testid="recommend-near-blind-before">{formatArea(nearBefore.blindArea)}</span><i>→</i><b className={nearAfter.blindArea > nearBefore.blindArea ? 'is-decrease' : ''} data-testid="recommend-near-blind-after">{formatArea(nearAfter.blindArea)}</b></p></div>
      </div>
      <div className="recommend-near-regions"><table aria-label="两侧和侧后近场分区覆盖"><thead><tr><th>分区</th><th>当前覆盖</th><th>推荐覆盖</th></tr></thead><tbody>{nearRegions.map(({ key, label }) => {
        const before = nearBefore[key], after = nearAfter[key];
        return <tr key={key}><th>{label}</th><td data-testid={`recommend-near-${key}-before`} data-covered={before.covered} data-total={before.totalCells}><span>{percent(before.percent)}%</span><small>{before.covered} / {before.totalCells} 格</small></td><td className={after.covered < before.covered ? 'is-decrease' : 'is-maintained'} data-testid={`recommend-near-${key}-after`} data-covered={after.covered} data-total={after.totalCells}><span>{percent(after.percent)}%</span><small>{after.covered} / {after.totalCells} 格</small></td></tr>;
      })}</tbody></table></div>
    </>}
    <div className="recommend-overall-heading"><span>整体统计</span><span className={totalGain < 0 ? 'is-decrease' : ''} data-testid="recommend-overall-change">{`整体 ${signed(totalGain)} 格`}</span></div>
    <div className="recommend-metrics recommend-secondary-metrics">
      <div><span>整体覆盖率</span><p><span data-testid="recommend-overall-before">{percent(result.before.overallPercent)}%</span><i>→</i><b data-testid="recommend-overall-after">{percent(result.after.overallPercent)}%</b></p></div>
      <div className="recommend-blind-metric"><span>整体盲区 · cm²</span><p><span data-testid="recommend-blind-before">{formatArea(result.before.overallBlindArea)}</span><i>→</i><b data-testid="recommend-blind-after">{formatArea(result.after.overallBlindArea)}</b></p></div>
    </div>
    <div className="recommend-camera-metric"><span>相机联合覆盖率</span><span data-testid="recommend-camera-before">{percent(result.before.cameraPercent)}%</span><i>→</i><b data-testid="recommend-camera-after">{percent(result.after.cameraPercent)}%</b></div>
    {!improved && result.reason && <p className="recommend-reason" data-testid="recommend-reason">{result.reason}</p>}
    {!currentOnly && <><div className="recommend-table-scroll" tabIndex={0} aria-label="左右相机推荐位置与角度"><table className="recommend-positions" aria-label="左右相机推荐坐标及角度，位置单位厘米，角度单位度"><thead><tr><th>推荐相机</th><th>X 前后<br />cm</th><th>Y 左右<br />cm</th><th>Z 高度<br />cm</th><th>偏航<br />°</th><th>俯角<br />°</th></tr></thead><tbody>{ids.map(id => {
      const sensor = result.config.sensors.find(item => item.id === id);
      if (!sensor) return null;
      return <tr key={id} data-testid={`recommend-row-${id}`} className={result.movedIds.includes(id) ? 'is-moved' : ''}><th><span style={{ color: sensor.color }}>{labels[id]}</span>{!sensor.enabled && <small>{result.movedIds.includes(id) ? '随盒移动' : '未启用'}</small>}</th>{(['x', 'y', 'z'] as const).map(axis => <td key={axis} data-axis={axis} data-value={sensor[axis]}>{formatLength(sensor[axis])}</td>)}{(['yaw', 'pitch'] as const).map(axis => <td key={axis} data-axis={axis} data-value={sensor[axis]}>{angle(sensor[axis])}</td>)}</tr>;
    })}</tbody></table></div>
    {hasAlignment && <div className="recommend-alignment" data-testid="recommend-alignment">{typeof result.commonReferenceXCm === 'number' && <p><span>对齐参考 X：</span><b data-testid="recommend-common-reference" data-value-cm={result.commonReferenceXCm}>{formatLength(result.commonReferenceXCm / 100)} cm</b></p>}<p><span data-testid="recommend-alignment-left" data-delta-cm={leftAlignment}>左盒校正：{signedLength(leftAlignment)} cm</span> / <span data-testid="recommend-alignment-right" data-delta-cm={rightAlignment}>右盒校正：{signedLength(rightAlignment)} cm</span></p><p data-testid="recommend-total-moves" data-left-cm={leftAlignment + (delta ?? 0)} data-right-cm={rightAlignment + (delta ?? 0)}>总位移：左 {signedLength(leftAlignment + (delta ?? 0))} cm / 右 {signedLength(rightAlignment + (delta ?? 0))} cm</p></div>}
    <div className="recommend-meta"><span data-testid="recommend-common-delta" data-delta-cm={delta ?? 0}>共同位移：{signedLength(delta ?? 0)} cm</span></div></>}
    <div className="recommend-meta"><span data-testid="recommend-evaluations">{result.evaluations.toLocaleString('zh-CN')} 次评估</span></div>
    <div className="recommend-actions">{!applied && <label><input type="checkbox" data-testid="recommend-preview" checked={preview} disabled={!improved} onChange={event => onPreview(event.target.checked)} />位置与朝向预览</label>}<div>{canUndo && <button type="button" className="button" data-testid="recommend-undo" onClick={onUndo}>{applied ? '撤销应用' : '撤销上次'}</button>}{!applied && <button type="button" className="button primary" data-testid="recommend-apply" disabled={!improved} onClick={onApply}>应用到布局</button>}</div></div>
  </section>;
}
