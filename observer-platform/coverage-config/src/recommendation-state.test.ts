import { describe, expect, it } from 'vitest';
import { DEFAULT_CONFIG, sanitizeConfig } from './model';
import { applySideRecommendation, recommendationIsProtected, recommendationIsApplicable, recommendationIsSymmetric } from './recommendation-state';
import { computeCoverage } from './geometry';
import { computeNearFieldMetrics } from './near-field';
import { supportGeometry } from './mounting';
import { sidePositionReference } from './side-symmetry';

describe('applying side-camera recommendations', () => {
  it('requires both whole-area and each near-field region to be preserved', () => {
    const config = sanitizeConfig(DEFAULT_CONFIG), coverage = computeCoverage(config);
    const before = { overallCovered: coverage.covered, nearField: computeNearFieldMetrics(config, coverage) };
    const result = { before, after: structuredClone(before) };
    expect(recommendationIsProtected(result)).toBe(true);
    result.after.overallCovered--;
    expect(recommendationIsProtected(result)).toBe(false);
    result.after = structuredClone(before);
    result.after.nearField.covered--;
    expect(recommendationIsProtected(result)).toBe(false);
    result.after = structuredClone(before);
    result.after.nearField.rightRear.covered--;
    expect(recommendationIsProtected(result)).toBe(false);
    expect(recommendationIsProtected({ before, after: { overallCovered: before.overallCovered } })).toBe(false);
  });

  it('allows an explicitly selected overall protection policy to trade near coverage for central placement', () => {
    const config = sanitizeConfig(DEFAULT_CONFIG), coverage = computeCoverage(config);
    const before = {overallCovered:coverage.covered,nearField:computeNearFieldMetrics(config,coverage)};
    const after = structuredClone(before);
    after.nearField.covered--;
    after.nearField.left.covered--;
    expect(recommendationIsProtected({before,after})).toBe(false);
    expect(recommendationIsProtected({before,after,coverageProtection:'overall'})).toBe(true);
    after.overallCovered--;
    expect(recommendationIsProtected({before,after,coverageProtection:'overall'})).toBe(false);
  });

  it('changes side poses and boxes while preserving every fixed parameter', () => {
    const current = sanitizeConfig(DEFAULT_CONFIG);
    const original = structuredClone(current);
    const proposed = structuredClone(current);
    proposed.supports!.camera3!.t = proposed.supports!.camera3!.t! + .1;
    proposed.supports!.camera4!.t = proposed.supports!.camera4!.t! + .1;
    proposed.sensors.find(sensor => sensor.id === 'camera3')!.pitch = 50;
    proposed.sensors.find(sensor => sensor.id === 'camera3')!.yaw = 105;
    proposed.sensors.find(sensor => sensor.id === 'camera4')!.yaw = -105;
    proposed.sensors.find(sensor => sensor.id === 'camera4')!.pitch = 50;
    proposed.chassis.length = 3;
    proposed.supports!.front!.offsetX = .3;
    proposed.sensors.find(sensor => sensor.id === 'p100r')!.pitch = 10;
    const result = applySideRecommendation(current, proposed);
    expect(current).toEqual(original);
    expect(result.chassis).toEqual(current.chassis);
    expect(result.analysis).toEqual(current.analysis);
    expect(result.supports!.front).toEqual(current.supports!.front);
    for (const sensor of current.sensors) {
      const updated = result.sensors.find(item => item.id === sensor.id)!;
      if (!['camera3', 'camera4'].includes(sensor.id)) expect(updated).toEqual(sensor);
      else {
        expect(updated.x - sensor.x).toBeCloseTo(.1, 12);
        expect(updated.inSupport).toEqual(sensor.inSupport);
        expect(updated.y).toBe(sensor.y);
        expect(updated.z).toBe(sensor.z);
      }
    }
  });

  it('applies a pure angle improvement without requiring position changes', () => {
    const current = sanitizeConfig(DEFAULT_CONFIG);
    const proposed = structuredClone(current);
    proposed.sensors.find(sensor => sensor.id === 'camera3')!.pitch = 50;
    proposed.sensors.find(sensor => sensor.id === 'camera4')!.pitch = 50;
    const result = applySideRecommendation(current, proposed);
    const left = result.sensors.find(sensor => sensor.id === 'camera3')!;
    expect(left.pitch).toBe(50);
    expect(left.x).toBe(current.sensors.find(sensor => sensor.id === 'camera3')!.x);
    expect(result.supports).toEqual(current.supports);
  });

  it('rejects independent or out-of-range rail movements', () => {
    const current = sanitizeConfig(DEFAULT_CONFIG);
    const proposed = structuredClone(current);
    proposed.supports!.camera3!.t = proposed.supports!.camera3!.t! + .1;
    expect(() => applySideRecommendation(current, proposed)).toThrow('same X');
    proposed.supports!.camera4!.t = 1.1;
    expect(() => applySideRecommendation(current, proposed)).toThrow('Invalid side-box');
  });

  it('rejects a recommendation whose box center fits but whose body overhangs', () => {
    const current = sanitizeConfig(DEFAULT_CONFIG);
    const proposed = structuredClone(current);
    proposed.supports!.camera3!.t = 1;
    proposed.supports!.camera4!.t = 1;
    expect(() => applySideRecommendation(current, proposed)).toThrow('extends beyond');
  });

  it('checks the physical beam ends when a box has an X offset', () => {
    const current = sanitizeConfig(DEFAULT_CONFIG);
    current.supports!.camera3!.offsetX = .3;
    const proposed = structuredClone(current);
    proposed.supports!.camera3!.t = .8;
    proposed.supports!.camera4!.t = .8;
    expect(() => applySideRecommendation(current, proposed)).toThrow('extends beyond');
  });

  it('aligns unequal starting positions before translating the pair without changing points inside boxes', () => {
    const current = sanitizeConfig(DEFAULT_CONFIG);
    current.supports!.camera3!.t = .6;
    current.supports!.camera4!.t = .4;
    const base = sanitizeConfig(current), reference = sidePositionReference(base);
    expect(reference.commonReferenceXCm).toBeCloseTo(0, 10);
    expect(reference.alignmentOffsetsCm.camera3).toBeCloseTo(-10, 10);
    expect(reference.alignmentOffsetsCm.camera4).toBeCloseTo(10, 10);
    const proposed = structuredClone(base);
    proposed.supports!.camera3!.t = .65;
    proposed.supports!.camera4!.t = .65;
    const next = applySideRecommendation(base, proposed);
    expect(recommendationIsSymmetric(next)).toBe(true);
    for (const id of ['camera3', 'camera4'] as const) {
      const delta = (supportGeometry(next, id).center.x - supportGeometry(base, id).center.x) * 100;
      expect(delta - reference.alignmentOffsetsCm[id]).toBeCloseTo(15, 10);
      expect(next.sensors.find(sensor => sensor.id === id)!.inSupport).toEqual(base.sensors.find(sensor => sensor.id === id)!.inSupport);
    }
  });

  it('rejects equal translations of an unaligned pair and independently chosen angles', () => {
    const base = sanitizeConfig(DEFAULT_CONFIG);
    base.supports!.camera4!.t = .45;
    const proposed = structuredClone(base);
    proposed.supports!.camera3!.t! += .1;
    proposed.supports!.camera4!.t! += .1;
    expect(() => applySideRecommendation(base, proposed)).toThrow('same X');
    proposed.supports!.camera4!.t = proposed.supports!.camera3!.t;
    proposed.sensors.find(sensor => sensor.id === 'camera3')!.yaw = 105;
    expect(() => applySideRecommendation(base, proposed)).toThrow('mirrored camera angles');
    proposed.sensors.find(sensor => sensor.id === 'camera4')!.yaw = -105;
    proposed.sensors.find(sensor => sensor.id === 'camera3')!.pitch = 50;
    expect(() => applySideRecommendation(base, proposed)).toThrow('mirrored camera angles');
  });

  it('blocks asymmetric and overhanging preview results even when coverage metrics are protected', () => {
    const config = sanitizeConfig(DEFAULT_CONFIG), coverage = computeCoverage(config);
    const before = {overallCovered:coverage.covered,nearField:computeNearFieldMetrics(config,coverage)};
    const result = {status:'improved',config,before,after:structuredClone(before)};
    expect(recommendationIsApplicable(result)).toBe(true);
    config.sensors.find(sensor => sensor.id === 'camera4')!.pitch += 1;
    expect(recommendationIsApplicable(result)).toBe(false);
    config.sensors.find(sensor => sensor.id === 'camera4')!.pitch -= 1;
    config.supports!.camera3!.t = config.supports!.camera4!.t = 1;
    expect(recommendationIsApplicable(result)).toBe(false);
  });

  it('does not sanitize malformed worker values into an apparently applicable symmetric result', () => {
    const config = sanitizeConfig(DEFAULT_CONFIG), coverage = computeCoverage(config);
    const before = {overallCovered:coverage.covered,nearField:computeNearFieldMetrics(config,coverage)};
    const result = {status:'improved',config,before,after:structuredClone(before)};
    config.supports!.camera3!.t = config.supports!.camera4!.t = NaN;
    expect(recommendationIsApplicable(result)).toBe(false);
    config.supports!.camera3!.t = config.supports!.camera4!.t = .5;
    config.sensors.find(sensor => sensor.id === 'camera3')!.yaw = 450;
    config.sensors.find(sensor => sensor.id === 'camera4')!.yaw = -450;
    expect(recommendationIsApplicable(result)).toBe(false);
  });
});
