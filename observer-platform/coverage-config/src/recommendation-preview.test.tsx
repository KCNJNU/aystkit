import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { DEFAULT_CONFIG, sanitizeConfig } from './model';
import { sideRecommendationPreview } from './recommendation-preview';
import { supportGeometry } from './mounting';
import RecommendationPanel from './RecommendationPanel';
import type { RecommendationResult } from './optimizer';

describe('side-camera recommendation preview', () => {
  it('keeps pure angle changes visible at the existing optical origin', () => {
    const current = sanitizeConfig(DEFAULT_CONFIG), proposed = structuredClone(current);
    const left = proposed.sensors.find(sensor => sensor.id === 'camera3')!;
    left.yaw += 12; left.pitch += 9;
    const points = sideRecommendationPreview(current, proposed);
    expect(points).toHaveLength(1);
    expect(points[0]).toMatchObject({ id: 'camera3', yaw: left.yaw, pitch: left.pitch, translated: false, rotated: true });
    expect([points[0].x, points[0].y, points[0].z]).toEqual([left.x, left.y, left.z]);
    expect(points[0].current.pitch).toBe(current.sensors.find(sensor => sensor.id === 'camera3')!.pitch);
  });

  it('tracks both translated boxes without previewing unrelated devices', () => {
    const current = sanitizeConfig(DEFAULT_CONFIG), proposed = structuredClone(current);
    for (const id of ['camera3', 'camera4'] as const) {
      const support = proposed.supports?.[id];
      if (!support || support.t === undefined) throw new Error('Missing side-box fixture.');
      support.t += .08;
    }
    proposed.sensors.find(sensor => sensor.id === 'camera2')!.pitch += 10;
    const positions = sideRecommendationPreview(current, sanitizeConfig(proposed));
    expect(positions.map(point => point.id)).toEqual(['camera3', 'camera4']);
    expect(positions.every(point => point.translated && !point.rotated)).toBe(true);
  });

  it('ignores equivalent yaw wraps and absent recommendations', () => {
    const current = sanitizeConfig(DEFAULT_CONFIG), proposed = structuredClone(current);
    current.sensors.find(sensor => sensor.id === 'camera3')!.yaw = 180;
    proposed.sensors.find(sensor => sensor.id === 'camera3')!.yaw = -180;
    expect(sideRecommendationPreview(current, proposed)).toEqual([]);
    expect(sideRecommendationPreview(current, null)).toEqual([]);
  });
});

describe('side-camera recommendation panel', () => {
  const noop = () => {};
  const result = (): RecommendationResult => {
    const config = sanitizeConfig(DEFAULT_CONFIG);
    for (const id of ['camera3', 'camera4']) config.sensors.find(sensor => sensor.id === id)!.pitch = 50;
    const nearField = { rangeMeters: 1, totalCells: 200, covered: 100, percent: 50, blindArea: 4,
      left: { totalCells: 100, covered: 50, percent: 50 }, right: { totalCells: 100, covered: 50, percent: 50 },
      leftRear: { totalCells: 50, covered: 20, percent: 40 }, rightRear: { totalCells: 50, covered: 20, percent: 40 },
      rear: { totalCells: 40, covered: 10, percent: 25 } };
    const afterNear = { ...nearField, covered: 120, percent: 60, blindArea: 3.2,
      left: { totalCells: 100, covered: 60, percent: 60 }, right: { totalCells: 100, covered: 60, percent: 60 },
      leftRear: { totalCells: 50, covered: 30, percent: 60 }, rightRear: { totalCells: 50, covered: 30, percent: 60 },
      rear: { totalCells: 40, covered: 20, percent: 50 } };
    const before = { cameraPercent: 65, overallPercent: 70, cameraBlindArea: 14, overallBlindArea: 12, cameraCovered: 650, overallCovered: 700, totalCells: 1000, nearField };
    return { status: 'improved', config, before, after: { ...before, cameraPercent: 66, cameraBlindArea: 13.6, cameraCovered: 660, overallPercent: 72, overallBlindArea: 11.2, overallCovered: 720, nearField: afterNear },
      movedIds: ['camera3', 'camera4'], rotatedIds: ['camera3', 'camera4'], translatedIds: [], commonDeltaCm: 0,
      alignmentOffsetsCm: { camera3: 0, camera4: 0 }, commonReferenceXCm: 0,
      beamMidpointXCm: 0, recommendedBoxCenterXCm: 0, beforeCenterDistanceCm: 0, afterCenterDistanceCm: 0, coverageProtection: 'overall',
      evaluations: 300, elapsedMs: 100 } as RecommendationResult;
  };
  const render = (value: RecommendationResult) => renderToStaticMarkup(<RecommendationPanel phase="ready" progress={1} error="" result={value} applied={false} canUndo={false} preview={false} onPreview={noop} onApply={noop} onUndo={noop} />);
  const reduceNearCoverage = (value: RecommendationResult) => {
    const before = value.before.nearField!, after = value.after.nearField!;
    after.covered = 90; after.percent = 45; after.blindArea = 4.4;
    for (const key of ['left', 'right', 'leftRear', 'rightRear', 'rear'] as const) {
      after[key].covered = before[key].covered - 5;
      after[key].percent = after[key].covered / after[key].totalCells * 100;
    }
  };

  it('shows only left/right positions and angles, with pure rotation available to apply', () => {
    const value = result();
    value.reason = '此推荐的完整策略说明不应作为提示显示。';
    const markup = render(value);
    expect(markup).not.toContain(value.reason);
    expect(markup.match(/data-testid="recommend-row-/g)).toHaveLength(2);
    expect(markup).toContain('data-testid="recommend-row-camera3"');
    expect(markup).toContain('data-testid="recommend-row-camera4"');
    expect(markup).not.toContain('data-testid="recommend-row-camera2"');
    expect(markup.match(/data-axis="yaw"/g)).toHaveLength(2);
    expect(markup.match(/data-axis="pitch"/g)).toHaveLength(2);
    expect(markup.match(/<button[^>]*data-testid="recommend-apply"[^>]*>/)?.[0]).not.toContain('disabled');
    expect(markup).toContain('位置与朝向预览');
    expect(markup).toContain('data-testid="recommend-common-delta" data-delta-cm="0">共同位移：0 cm');
    expect(markup).toContain('data-symmetric="true"');
    expect(markup).not.toMatch(/\stitle=/);
    expect(markup).toContain('aria-label="左右相机推荐坐标及角度');
    expect(markup).not.toContain('recommend-scope');
    expect(markup).not.toContain('recommend-policy');
    expect(markup).not.toContain('recommend-install-rule');
    expect(markup).not.toContain('recommend-coordinate-note');
    expect(markup).not.toContain('data-testid="recommend-alignment"');
  });

  it.each(['yaw', 'pitch', 'boxX'] as const)('blocks a recommendation that violates symmetric %s', kind => {
    const value = result();
    const right = value.config.sensors.find(sensor => sensor.id === 'camera4')!;
    if (kind === 'yaw') right.yaw = -80;
    else if (kind === 'pitch') right.pitch = 45;
    else {
      const box = value.config.supports?.camera4;
      if (!box || box.t === undefined) throw new Error('Missing side-box fixture.');
      box.t += .01;
      value.config = sanitizeConfig(value.config);
    }
    const markup = render(value);
    expect(markup).toContain('data-symmetric="false"');
    expect(markup).toContain('未满足对称约束');
    expect(markup).not.toContain('data-testid="recommend-row-camera3"');
    expect(markup.match(/<button[^>]*data-testid="recommend-apply"[^>]*>/)?.[0]).toContain('disabled');
    expect(markup.match(/<input[^>]*data-testid="recommend-preview"[^>]*>/)?.[0]).toContain('disabled');
  });

  it('defines symmetry by box X and viewing angles while retaining different optical origins', () => {
    const value = result();
    const right = value.config.sensors.find(sensor => sensor.id === 'camera4')!;
    right.inSupport = { ...right.inSupport!, x: .502, y: .304 };
    value.config = sanitizeConfig(value.config);
    const leftPoint = value.config.sensors.find(sensor => sensor.id === 'camera3')!;
    const rightPoint = value.config.sensors.find(sensor => sensor.id === 'camera4')!;
    expect(leftPoint.x).not.toBe(rightPoint.x);
    const markup = render(value);
    expect(markup).toContain('data-symmetric="true"');
    expect(markup).toContain(`data-axis="x" data-value="${rightPoint.x}"`);
    expect(markup).not.toContain('下表为相机光学原点坐标');
    expect(markup.match(/<button[^>]*data-testid="recommend-apply"[^>]*>/)?.[0]).not.toContain('disabled');
  });

  it('separates initial alignment, common translation, and each original total movement', () => {
    const value = result();
    for (const id of ['camera3', 'camera4'] as const) {
      const box = value.config.supports?.[id];
      if (!box) throw new Error('Missing side-box fixture.');
      box.t = .5 + .04 / (value.config.chassis.frameLength ?? value.config.chassis.length);
    }
    value.config = sanitizeConfig(value.config);
    value.alignmentOffsetsCm = { camera3: 1, camera4: -1 };
    value.commonReferenceXCm = 1;
    value.commonDeltaCm = 3;
    const markup = render(value);
    expect(markup).toContain('data-testid="recommend-common-reference" data-value-cm="1">1 cm');
    expect(markup).toContain('左盒校正：+1 cm');
    expect(markup).toContain('右盒校正：−1 cm');
    expect(markup).toContain('data-testid="recommend-common-delta" data-delta-cm="3">共同位移：+3 cm');
    expect(markup).toContain('data-testid="recommend-total-moves" data-left-cm="4" data-right-cm="2"');
    expect(markup).toContain('总位移：左 +4 cm / 右 +2 cm');
  });

  it('does not describe an alignment-only change as keeping box positions', () => {
    const value = result();
    for (const id of ['camera3', 'camera4'] as const) {
      const box = value.config.supports?.[id];
      if (!box) throw new Error('Missing side-box fixture.');
      box.t = .5 + .01 / (value.config.chassis.frameLength ?? value.config.chassis.length);
    }
    value.config = sanitizeConfig(value.config);
    value.alignmentOffsetsCm = { camera3: 1, camera4: -1 };
    value.commonReferenceXCm = 1;
    const markup = render(value);
    expect(markup).toContain('data-testid="recommend-common-delta" data-delta-cm="0">共同位移：0 cm');
    expect(markup).not.toContain('盒子位置保持');
    expect(markup).toContain('data-testid="recommend-total-moves" data-left-cm="1" data-right-cm="-1"');
  });

  it('presents centering ahead of the actual near-field and overall coverage data', () => {
    const markup = render(result());
    expect(markup).toContain('data-testid="recommend-near-before">50.00%');
    expect(markup).toContain('data-testid="recommend-near-after">60.00%');
    expect(markup).toContain('近场 +20 格');
    expect(markup).toContain('整体 +20 格');
    expect(markup).not.toContain('+-');
    expect(markup).not.toContain('+−');
    expect(markup).toContain('data-testid="recommend-overall-before"');
    expect(markup).toContain('data-testid="recommend-overall-after"');
    expect(markup).toContain('data-testid="recommend-camera-before"');
    expect(markup).toContain('data-testid="recommend-camera-after"');
    expect(markup).toContain('40,000');
    expect(markup).toContain('32,000');
    expect(markup).toContain('120,000');
    expect(markup).toContain('112,000');
    expect(markup).not.toContain('固定框架与其他传感器');
    expect(markup).not.toContain('距底盘外缘 100 cm');
    expect(markup).not.toContain('正前方不计入');
    expect(markup).not.toContain('整体覆盖不下降 → 尽量靠近横梁中点 → 再改善近场');
    expect(markup).not.toContain('整体覆盖率可能下降');
    expect(markup).not.toContain('整体盲区可能增加');
    expect(markup).not.toContain('不悬出梁端');
    expect(markup.indexOf('recommend-box-center')).toBeLessThan(markup.indexOf('recommend-near-before'));
    expect(markup.indexOf('recommend-near-before')).toBeLessThan(markup.indexOf('recommend-overall-before'));
  });

  it('shows the true midpoint, common box center, and before/after distance', () => {
    const value = result();
    value.beforeCenterDistanceCm = 12;
    value.commonReferenceXCm = 12;
    value.commonDeltaCm = -12;
    const markup = render(value);
    expect(markup).toContain('data-testid="recommend-beam-midpoint" data-value-cm="0"');
    expect(markup).toContain('data-testid="recommend-box-center" data-value-cm="0"');
    expect(markup).toContain('data-testid="recommend-center-offset" data-value-cm="0"');
    expect(markup).toContain('data-testid="recommend-center-distance-before" data-value-cm="12">12 cm');
    expect(markup).toContain('data-testid="recommend-center-distance-after" data-value-cm="0">0 cm');
    expect(markup).toContain('data-testid="recommend-center-offset" data-value-cm="0">0 cm');
  });

  it.each([-8, 8])('shows a constrained signed offset of %s cm without claiming exact centering', offset => {
    const value = result();
    for (const id of ['camera3', 'camera4'] as const) {
      const box = value.config.supports?.[id];
      if (!box) throw new Error('Missing side-box fixture.');
      box.t = .5 + offset / 100 / (value.config.chassis.frameLength ?? value.config.chassis.length);
    }
    value.config = sanitizeConfig(value.config);
    value.recommendedBoxCenterXCm = offset; value.afterCenterDistanceCm = Math.abs(offset); value.beforeCenterDistanceCm = 20;
    const markup = render(value);
    expect(markup).toContain(`data-testid="recommend-box-center" data-value-cm="${offset}"`);
    expect(markup).toContain(`data-testid="recommend-center-offset" data-value-cm="${offset}"`);
    expect(markup).toContain(`data-testid="recommend-center-offset" data-value-cm="${offset}">${offset > 0 ? '+' : '−'}8 cm`);
    expect(markup).not.toContain('两盒中心位于横梁中点');
  });

  it('allows a true near-field reduction under overall-only protection and displays it in red', () => {
    const value = result();
    reduceNearCoverage(value);
    value.beforeCenterDistanceCm = 34.9; value.commonReferenceXCm = 34.9; value.commonDeltaCm = -34.9;
    const markup = render(value);
    expect(markup).toContain('data-policy="overall"');
    expect(markup).toContain('近场 −10 格');
    expect(markup).not.toContain('近场少覆盖 10 格');
    expect(markup).toContain('class="is-decrease" data-testid="recommend-near-after">45.00%');
    expect(markup).not.toContain('近场及分区允许下降');
    expect(markup).not.toContain('recommend-near-decrease');
    expect(markup).not.toContain('整体覆盖与五个近场分区覆盖均保持或增加');
    expect(markup).not.toContain('未通过覆盖保护');
    expect(markup.match(/<button[^>]*data-testid="recommend-apply"[^>]*>/)?.[0]).not.toContain('disabled');
    expect(markup.match(/<input[^>]*data-testid="recommend-preview"[^>]*>/)?.[0]).not.toContain('disabled');
  });

  it.each(['all-regions', undefined] as const)('preserves the all-region guard for legacy policy %s', policy => {
    const value = result();
    value.coverageProtection = policy;
    reduceNearCoverage(value);
    const markup = render(value);
    expect(markup).toContain('data-policy="all-regions"');
    expect(markup).not.toContain('整体、近场总覆盖与五个分区覆盖均不下降');
    expect(markup).toContain('未通过覆盖保护');
    expect(markup).not.toContain('近场及分区允许下降');
    expect(markup.match(/<button[^>]*data-testid="recommend-apply"[^>]*>/)?.[0]).toContain('disabled');
  });

  it('shows a signed negative delta accurately but blocks a regressing result', () => {
    const value = result();
    value.after.overallCovered = 680; value.after.overallPercent = 68; value.after.overallBlindArea = 12.8;
    const markup = render(value);
    expect(markup).toContain('整体 −20 格');
    expect(markup).toContain('128,000');
    expect(markup).not.toContain('+-');
    expect(markup).not.toContain('+−');
    expect(markup).not.toContain('整体覆盖率可能下降');
    expect(markup).toContain('未通过覆盖保护');
    expect(markup).toContain('data-protected="false"');
    expect(markup.match(/<button[^>]*data-testid="recommend-apply"[^>]*>/)?.[0]).toContain('disabled');
    expect(markup.match(/<input[^>]*data-testid="recommend-preview"[^>]*>/)?.[0]).toContain('disabled');
  });

  it('exposes all protected near-field regions and explains an unchanged result', () => {
    const value = result();
    value.coverageProtection = 'all-regions';
    value.status = 'no-improvement'; value.movedIds = [];
    value.after = structuredClone(value.before);
    value.reason = '没有找到在五个近场分区覆盖不下降的同时进一步改善的方案。';
    const markup = render(value);
    for (const key of ['left', 'right', 'leftRear', 'rightRear', 'rear']) {
      expect(markup).toContain(`data-testid="recommend-near-${key}-before"`);
      expect(markup).toContain(`data-testid="recommend-near-${key}-after"`);
    }
    expect(markup).toContain('data-protected="true"');
    expect(markup).toContain(value.reason);
    expect(markup.match(/<button[^>]*data-testid="recommend-apply"[^>]*>/)?.[0]).toContain('disabled');
  });

  it('does not present an existing overhanging layout as a new recommendation when no protected correction exists', () => {
    const value = result();
    const current = structuredClone(value.config);
    current.chassis.frameLength = .878;
    for (const id of ['camera3', 'camera4'] as const) {
      const box = current.supports?.[id];
      if (!box) throw new Error('Missing side-box fixture.');
      box.t = .5 + .38 / current.chassis.frameLength;
    }
    value.config = sanitizeConfig(current);
    expect(supportGeometry(value.config, 'camera3').max.x).toBeGreaterThan(current.chassis.frameLength / 2);
    value.status = 'no-improvement'; value.movedIds = []; value.commonDeltaCm = 0;
    value.after = structuredClone(value.before);
    value.reason = '当前安装盒悬出梁端；未找到满足整体及五个近场分区覆盖保护的合法推荐，保留当前布局。';
    const markup = render(value);
    expect(markup).toContain('未找到满足约束的推荐');
    expect(markup).toContain(value.reason);
    expect(markup).not.toContain('data-testid="recommend-row-camera3"');
    expect(markup).not.toContain('data-testid="recommend-row-camera4"');
    expect(markup).not.toContain('aria-label="左右相机推荐坐标及角度');
    expect(markup).not.toContain('data-testid="recommend-common-delta"');
    expect(markup).not.toContain('data-testid="recommend-box-center"');
    expect(markup).not.toContain('data-testid="recommend-center-offset"');
    expect(markup.match(/<button[^>]*data-testid="recommend-apply"[^>]*>/)?.[0]).toContain('disabled');
    expect(markup.match(/<input[^>]*data-testid="recommend-preview"[^>]*>/)?.[0]).toContain('disabled');
  });
});
