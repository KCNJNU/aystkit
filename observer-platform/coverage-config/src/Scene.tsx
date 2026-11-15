import { useEffect, useRef, useState } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { TransformControls } from 'three/addons/controls/TransformControls.js';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { constrainBoxPosition, constrainDragPosition } from './drag';
import { frameRails } from './frame';
import type { FrameRail } from './frame';
import type { Config, Sensor, SupportId, SupportPoint } from './model';
import { supportBoxes, supportForSensor, supportGeometry, supportIdForSensor, supportOpeningDirection, supportBracketParts, supportClampParts } from './mounting';
import { sensorBasis } from './geometry';
import type { computeCoverage } from './geometry';
import { formatLength } from './units';
import { contactShadow, coverageLayer, stageMaterial, volumeGlass } from './coverage-render';
import { sideRecommendationPreview } from './recommendation-preview';

type Coverage = ReturnType<typeof computeCoverage>;
interface MoveSnapshot { inSupport?: SupportPoint; t?: number }
interface Props { config: Config; recommendation?: Config | null; coverage: Coverage; selected: string; view: { mode: 'perspective' | 'top' | 'mounting'; nonce: number }; showVolumes: boolean; volumeScope: 'selected' | 'all'; showLabels: boolean; showOverlap: boolean; onSelect: (id: string) => void; dragEnabled: boolean; onMove: (id: string, position: { x: number; y: number; z: number }) => void; onSupportMove: (key: SupportId, center: { x: number; y: number; z: number }) => void; onRestoreMove: (id: string, snapshot: MoveSnapshot) => void; interactionRevision: number; onDragState?: (dragging: boolean) => void; }
interface Runtime { scene: THREE.Scene; renderer: THREE.WebGLRenderer; camera: THREE.PerspectiveCamera; controls: OrbitControls; content: THREE.Group; markers: THREE.Object3D[]; plates: THREE.Object3D[]; render: () => void; syncDrag: () => void; cancelDrag: () => void; }
function disposeGroup(group: THREE.Object3D) {
  group.traverse(object => {
    const drawable = object as THREE.Mesh; drawable.geometry?.dispose();
    const materials = Array.isArray(drawable.material) ? drawable.material : drawable.material ? [drawable.material] : [];
    materials.forEach(material => { if ('map' in material) (material.map as THREE.Texture | null)?.dispose(); material.dispose(); });
  }); group.clear();
}
function lines(points: THREE.Vector3[], color: THREE.ColorRepresentation, opacity = 1, segments = false) {
  const geometry = new THREE.BufferGeometry().setFromPoints(points);
  const material = new THREE.LineBasicMaterial({ color, transparent: opacity < 1, opacity, depthWrite: false });
  return segments ? new THREE.LineSegments(geometry, material) : new THREE.Line(geometry, material);
}
function label(text: string, color: string, height = .13, size = 34) {
  const canvas = document.createElement('canvas'), context = canvas.getContext('2d')!;
  context.font = `600 ${size}px "Noto Sans CJK SC", "Microsoft YaHei", sans-serif`;
  const width = Math.ceil(Math.min(1400, context.measureText(text).width + 38)); canvas.width = width; canvas.height = 76;
  context.font = `600 ${size}px "Noto Sans CJK SC", "Microsoft YaHei", sans-serif`; context.textBaseline = 'middle';
  context.fillStyle = 'rgba(6,20,33,.94)'; context.beginPath(); context.roundRect(1, 1, width - 2, 74, 15); context.fill();
  context.strokeStyle = 'rgba(128,181,205,.28)'; context.lineWidth = 2; context.stroke(); context.fillStyle = color; context.fillText(text, 19, 39);
  const texture = new THREE.CanvasTexture(canvas); texture.colorSpace = THREE.SRGBColorSpace;
  const material = new THREE.SpriteMaterial({ map: texture, transparent: true, depthTest: false, depthWrite: false, sizeAttenuation: true });
  const sprite = new THREE.Sprite(material); sprite.scale.set(height * width / 76, height, 1); sprite.center.set(0, .5); sprite.renderOrder = 30; return sprite;
}
const SUPPORT_PREFIX = 'support:';
const PLATE_ID = 'support:front';
function supportKey(id: string): SupportId | null { return id.startsWith(SUPPORT_PREFIX) ? id.slice(SUPPORT_PREFIX.length) as SupportId : null; }
function currentRail(config: Config, sensor: Sensor): FrameRail { return supportForSensor(config, sensor.id).rail; }
/** Keep authoritative local values: inverse world transforms lose exact precision. */
function moveSnapshot(config: Config, id: string): MoveSnapshot {
  const key = supportKey(id);
  if (key) return { t: supportGeometry(config, key).layout.t };
  const point = config.sensors.find(sensor => sensor.id === id)?.inSupport;
  return point ? { inSupport: { ...point } } : {};
}
function frameSignature(config: Config, id: string) {
  const moving = supportKey(id);
  const supports = supportBoxes(config).map(item => ({ id: item.id, ...item.layout, t: item.id === moving ? null : item.layout.t }));
  return JSON.stringify({ chassis: config.chassis, supports });
}
function selectedTarget(config: Config, id: string) {
  const key = supportKey(id);
  if (key && supportBoxes(config).some(item => item.id === key)) {
    const support = supportGeometry(config, key);
    return { id, enabled: true, isSupport: true, supportId: key, position: support.center, rail: support.rail, min: support.min, max: support.max };
  }
  const sensor = config.sensors.find(item => item.id === id);
  if (!sensor) return null;
  const support = supportForSensor(config, id);
  return { id, enabled: true, isSupport: false, supportId: support.id,
    position: { x: sensor.x, y: sensor.y, z: sensor.z }, rail: support.rail, min: support.min, max: support.max };
}

function metal(color: THREE.ColorRepresentation, roughness = .38, metalness = .60) { return new THREE.MeshStandardMaterial({ color, roughness, metalness }); }
function box(root: THREE.Object3D, size: [number, number, number], position: [number, number, number], material: THREE.Material) {
  const mesh = new THREE.Mesh(new RoundedBoxGeometry(...size, 1, Math.min(.003, Math.min(...size) * .18)), material);
  mesh.userData.bevelledPart = true; mesh.position.set(...position); root.add(mesh); return mesh;
}
function cylinder(root: THREE.Object3D, radius: number, length: number, position: [number, number, number], material: THREE.Material, axis: 'x' | 'y' | 'z' = 'z') {
  const mesh = new THREE.Mesh(new THREE.CylinderGeometry(radius, radius, length, 24), material);
  if (axis === 'z') mesh.rotation.x = Math.PI / 2; else if (axis === 'x') mesh.rotation.z = -Math.PI / 2;
  mesh.position.set(...position); root.add(mesh); return mesh;
}
/** Decorative hardware has no bearing on installation bounds or coverage. */
function fastener(root: THREE.Object3D, position: [number, number, number], material: THREE.Material, axis: 'x' | 'y' | 'z' = 'z', radius = .0055, facing: 1 | -1 = 1) {
  const mount = new THREE.Group(); mount.position.set(...position);
  if (axis === 'x') mount.rotation.y = Math.PI / 2;
  else if (axis === 'y') mount.rotation.x = -Math.PI / 2;
  if (facing < 0) mount.rotateX(Math.PI);
  const washer = cylinder(mount, radius * 1.36, .0014, [0, 0, 0], material);
  const head = new THREE.Mesh(new THREE.CylinderGeometry(radius, radius, .0032, 6), material);
  head.rotation.x = Math.PI / 2; head.position.z = .0018; mount.add(head);
  const socket = new THREE.Mesh(new THREE.CylinderGeometry(radius * .39, radius * .39, .0005, 6), new THREE.MeshBasicMaterial({ color: '#253541' }));
  socket.rotation.x = Math.PI / 2; socket.position.z = .0036; mount.add(socket);
  washer.userData.fastenerPart = true; root.add(mount); return mount;
}
function roundedDeck(length: number, width: number, height: number, radius: number) {
  const x = -length / 2, y = -width / 2, r = Math.min(radius, width / 4, length / 4), shape = new THREE.Shape();
  shape.moveTo(x + r, y); shape.lineTo(x + length - r, y); shape.quadraticCurveTo(x + length, y, x + length, y + r);
  shape.lineTo(x + length, y + width - r); shape.quadraticCurveTo(x + length, y + width, x + length - r, y + width);
  shape.lineTo(x + r, y + width); shape.quadraticCurveTo(x, y + width, x, y + width - r);
  shape.lineTo(x, y + r); shape.quadraticCurveTo(x, y, x + r, y);
  return new THREE.ExtrudeGeometry(shape, { depth: height, bevelEnabled: false, curveSegments: 5, steps: 1 });
}
/** Mounting ranges are geometry; the solid plastic pieces are installation guides. */
function addMountSupports(root: THREE.Group, config: Config, selected: string) {
  const selectedSensor = config.sensors.find(sensor => sensor.id === selected);
  const activeId = supportKey(selected) ?? (selectedSensor ? supportIdForSensor(selectedSensor.id) : null);
  const pickTargets: THREE.Object3D[] = [];
  const bracketModels: { id: SupportId; openingDirection: { x: number; y: number; z: number }; openingCenter: number[]; roof: { center: number[]; size: number[] }; backplate: { center: number[]; size: number[] }; arms: { start: number[]; end: number[] }[] }[] = [];
  const mountModels: { id: SupportId; belowBeam: boolean; beamBottomZ: number; beamGap: number | null; topEnvelopeZ: number;
    clampFoot: { center: number[]; size: number[]; min: number[]; max: number[]; fastenerDirection: number };
    bodyMin: number[]; bodyMax: number[]; bodyMaxZ: number; connectorMaxZ: number | null }[] = [];
  for (const data of supportBoxes(config)) {
    const active = data.id === activeId, { layout, anchor, center, min, max } = data;
    const group = new THREE.Group(); group.name = 'plastic-support-' + data.id; root.add(group);
    const tint = active ? '#b6eff0' : '#8ba9b8';
    const geometry = new THREE.BoxGeometry(layout.sizeX, layout.sizeY, layout.sizeZ);
    const range = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({ color: tint, transparent: true,
      opacity: active ? .058 : .012, side: THREE.DoubleSide, depthWrite: false }));
    range.position.set(center.x, center.y, center.z); range.userData.selectionId = SUPPORT_PREFIX + data.id;
    range.userData.supportId = data.id; range.renderOrder = 7; group.add(range); pickTargets.push(range);
    const outline = new THREE.LineSegments(new THREE.EdgesGeometry(geometry), new THREE.LineBasicMaterial({
      color: tint, transparent: true, opacity: active ? .88 : .25, depthWrite: false }));
    outline.position.copy(range.position); outline.renderOrder = 8; group.add(outline);
    // Corner accents give the bounded volume a readable depth without opaque walls.
    if (active) {
      const corners: THREE.Vector3[] = [];
      for (const x of [min.x, max.x]) for (const y of [min.y, max.y]) for (const z of [min.z, max.z]) {
        const origin = new THREE.Vector3(x, y, z), inward = new THREE.Vector3(x === min.x ? 1 : -1, y === min.y ? 1 : -1, z === min.z ? 1 : -1);
        for (const axis of ['x', 'y', 'z'] as const) {
          const end = origin.clone(); end[axis] += inward[axis] * Math.min(.035, (max[axis] - min[axis]) * .24); corners.push(origin.clone(), end);
        }
      }
      const accents = lines(corners, '#c5f8fb', .94, true); accents.renderOrder = 9; group.add(accents);
    }
    const plastic = new THREE.MeshStandardMaterial({ color: active ? '#e9ece9' : '#c6cfce', roughness: .58, metalness: .025 });
    const bolt = metal('#a6b2bc', .30, .68), thickness = layout.thickness;
    const dimensions = (value: { x: number; y: number; z: number }): [number, number, number] => [value.x, value.y, value.z];
    // Hanging fixtures clamp to the underside; the sensor body is measured separately.
    const clamp = supportClampParts(data);
    const clampFoot = box(group, dimensions(clamp.foot.size), dimensions(clamp.foot.center), plastic);
    for (const position of clamp.fasteners) fastener(group, dimensions(position), bolt, 'z', .0055, clamp.direction);
    const bodyGroup = new THREE.Group(), connectorGroup = new THREE.Group(); group.add(bodyGroup, connectorGroup);
    if (layout.style === 'pedestal') {
      // A tapered printed riser models the photographed MID360 plastic mould.
      const topZ = data.belowBeam ? max.z : center.z - layout.sizeZ * .12;
      const bottomZ = data.belowBeam ? clamp.foot.center.z - clamp.foot.size.z / 2 : anchor.z + .018 + thickness;
      const halfX = layout.sizeX * .43, halfY = layout.sizeY * .43;
      const vertices = [
        anchor.x - halfX, anchor.y - halfY, bottomZ, anchor.x + halfX, anchor.y - halfY, bottomZ,
        anchor.x + halfX, anchor.y + halfY, bottomZ, anchor.x - halfX, anchor.y + halfY, bottomZ,
        center.x - halfX * .72, center.y - halfY * .72, topZ, center.x + halfX * .72, center.y - halfY * .72, topZ,
        center.x + halfX * .72, center.y + halfY * .72, topZ, center.x - halfX * .72, center.y + halfY * .72, topZ,
      ];
      const riserGeometry = new THREE.BufferGeometry();
      riserGeometry.setAttribute('position', new THREE.Float32BufferAttribute(vertices, 3));
      const riserFaces = [0,2,1,0,3,2,4,5,6,4,6,7,0,1,5,0,5,4,1,2,6,1,6,5,2,3,7,2,7,6,3,0,4,3,4,7];
      if (topZ < bottomZ) for (let i = 0; i < riserFaces.length; i += 3) [riserFaces[i + 1], riserFaces[i + 2]] = [riserFaces[i + 2], riserFaces[i + 1]];
      riserGeometry.setIndex(riserFaces);
      // Flat face normals keep the photographed mould's creases legible.
      const facetedRiser = riserGeometry.toNonIndexed(); riserGeometry.dispose(); facetedRiser.computeVertexNormals();
      const riser = new THREE.Mesh(facetedRiser, plastic); connectorGroup.add(riser);
      const riserEdges = new THREE.LineSegments(new THREE.EdgesGeometry(facetedRiser, 28), new THREE.LineBasicMaterial({ color: '#85949c', transparent: true, opacity: .42 })); connectorGroup.add(riserEdges);
      box(bodyGroup, [layout.sizeX * .82, layout.sizeY * .82, thickness], [center.x, center.y, topZ + thickness / 2], plastic);
      if (data.belowBeam) for (const sign of [-1, 1]) fastener(bodyGroup,
        [center.x + sign * layout.sizeX * .28, center.y, topZ + thickness + .001], bolt, 'z', .0045);
      const seam = lines([new THREE.Vector3(center.x - layout.sizeX * .3, center.y - layout.sizeY * .31, topZ + thickness + .001),
        new THREE.Vector3(center.x + layout.sizeX * .3, center.y - layout.sizeY * .31, topZ + thickness + .001)], '#8b9ca5', .7); bodyGroup.add(seam);
    } else if (data.id === 'front') {
      // The front pair shares one real sheet while retaining independent XYZ ranges.
      box(bodyGroup, [thickness, layout.sizeY, layout.sizeZ], [center.x, center.y, center.z], plastic);
      for (const sign of [-1, 1]) fastener(bodyGroup,
        [center.x + thickness / 2 + .001, center.y + sign * layout.sizeY * .39, min.z + layout.sizeZ * .12], bolt, 'x');
      const sheetEdge = lines([new THREE.Vector3(center.x + thickness / 2 + .0006, min.y + layout.sizeY * .08, max.z - layout.sizeZ * .06),
        new THREE.Vector3(center.x + thickness / 2 + .0006, max.y - layout.sizeY * .08, max.z - layout.sizeZ * .06)], '#8f9da4', .50); bodyGroup.add(sheetEdge);
    } else {
      const parts = supportBracketParts(data), opening = supportOpeningDirection(data);
      const backplate = box(bodyGroup, dimensions(parts.backplate.size), dimensions(parts.backplate.center), plastic);
      backplate.userData.supportBackplate = data.id;
      backplate.userData.openingDirection = opening;
      const roof = box(bodyGroup, dimensions(parts.roof.size), dimensions(parts.roof.center), plastic);
      roof.userData.supportRoof = data.id;
      const openingCenter = new THREE.Vector3(...dimensions(parts.openingCenter));
      const actualOpening = openingCenter.clone().sub(backplate.position).normalize();
      const renderedArms: { start: number[]; end: number[] }[] = [];
      for (const position of parts.fasteners) fastener(bodyGroup, dimensions(position), bolt, 'z', .0045);
      for (const brace of parts.arms) {
        const foot = new THREE.Vector3(...dimensions(brace.start)), top = new THREE.Vector3(...dimensions(brace.end));
        const direction = top.clone().sub(foot), length = direction.length();
        if (length > 1e-6) {
          const armWidth = data.belowBeam ? thickness : Math.max(.009, thickness);
          const arm = new THREE.Mesh(new RoundedBoxGeometry(armWidth, length, armWidth, 1, Math.min(.002, thickness * .2)), plastic);
          arm.userData.bevelledPart = true;
          arm.position.copy(foot).add(top).multiplyScalar(.5); arm.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction.normalize()); connectorGroup.add(arm);
          arm.updateMatrixWorld();
          renderedArms.push({ start: arm.localToWorld(new THREE.Vector3(0, -length / 2, 0)).toArray(),
            end: arm.localToWorld(new THREE.Vector3(0, length / 2, 0)).toArray() });
        }
      }
      bracketModels.push({ id: data.id, openingDirection: { x: actualOpening.x, y: actualOpening.y, z: actualOpening.z },
        openingCenter: openingCenter.toArray(),
        roof: { center: roof.position.toArray(), size: dimensions(parts.roof.size) },
        backplate: { center: backplate.position.toArray(), size: dimensions(parts.backplate.size) }, arms: renderedArms });
    }

    group.updateMatrixWorld(true);
    const footBounds = new THREE.Box3().setFromObject(clampFoot), bodyBounds = new THREE.Box3().setFromObject(bodyGroup);
    const connectorBounds = new THREE.Box3().setFromObject(connectorGroup);
    mountModels.push({ id: data.id, belowBeam: data.belowBeam, beamBottomZ: data.beamBottomZ, beamGap: data.beamGap,
      topEnvelopeZ: data.topEnvelopeZ,
      clampFoot: { center: clampFoot.position.toArray(), size: dimensions(clamp.foot.size),
        min: footBounds.min.toArray(), max: footBounds.max.toArray(), fastenerDirection: clamp.direction },
      bodyMin: bodyBounds.min.toArray(), bodyMax: bodyBounds.max.toArray(), bodyMaxZ: bodyBounds.max.z,
      connectorMaxZ: connectorBounds.isEmpty() ? null : connectorBounds.max.z });

    // The short support link makes an intentional beam offset visually explicit.
    const link = lines([new THREE.Vector3(anchor.x, anchor.y, anchor.z), new THREE.Vector3(center.x, center.y, center.z)], active ? '#a6e3ed' : '#698e9d', active ? .60 : .28);
    group.add(link);
    if (selected === SUPPORT_PREFIX + data.id) {
      const start = new THREE.Vector3(data.rail.start.x, data.rail.start.y, data.rail.start.z), end = new THREE.Vector3(data.rail.end.x, data.rail.end.y, data.rail.end.z);
      const guide = lines([start, end], '#a4edf5', .7); guide.renderOrder = 9; root.add(guide);
      for (const point of [start, end]) { const stop = new THREE.Mesh(new THREE.SphereGeometry(.016, 10, 8), new THREE.MeshBasicMaterial({ color: '#c8f6fa' })); stop.position.copy(point); root.add(stop); }
    }
  }
  root.userData.supportBracketModels = bracketModels;
  root.userData.supportMountModels = mountModels;
  return pickTargets;
}

/** A chamfered 34 mm extrusion, with four real recessed slots and a centre bore. */
function extrusionProfile(length: number) {
  const h = .017, chamfer = .0023, w = .0034, depth = .003;
  const profile = new THREE.Shape();
  const contour = [
    [-h + chamfer, -h], [-w, -h], [-w, -h + depth], [w, -h + depth], [w, -h], [h - chamfer, -h],
    [h, -h + chamfer], [h, -w], [h - depth, -w], [h - depth, w], [h, w], [h, h - chamfer],
    [h - chamfer, h], [w, h], [w, h - depth], [-w, h - depth], [-w, h], [-h + chamfer, h],
    [-h, h - chamfer], [-h, w], [-h + depth, w], [-h + depth, -w], [-h, -w], [-h, -h + chamfer],
  ];
  contour.forEach(([x, y], index) => index ? profile.lineTo(x, y) : profile.moveTo(x, y)); profile.closePath();
  const bore = new THREE.Path(); bore.absarc(0, 0, .0033, 0, Math.PI * 2, true); profile.holes.push(bore);
  const geometry = new THREE.ExtrudeGeometry(profile, { depth: length, bevelEnabled: false, steps: 1, curveSegments: 6 });
  geometry.translate(0, 0, -length / 2); geometry.rotateX(Math.PI / 2);
  return geometry;
}
function addVehicle(root: THREE.Group, config: Config, selected: string) {
  const { chassis } = config, shell = metal('#274658', .39, .52), trim = metal('#b0bec6', .34, .65);
  const body = new THREE.Mesh(roundedDeck(chassis.length, chassis.width, chassis.height, .055), shell); root.add(body);
  const deck = new THREE.Mesh(roundedDeck(chassis.length * .94, chassis.width * .9, .022, .045), metal('#34576b', .39, .72)); deck.position.z = chassis.height; root.add(deck);
  const outline = new THREE.LineSegments(new THREE.EdgesGeometry(deck.geometry, 25), new THREE.LineBasicMaterial({ color: '#77a4ba', opacity: .55, transparent: true })); outline.position.copy(deck.position); root.add(outline);
  box(root, [chassis.length * .63, chassis.width * .47, .018], [0, 0, chassis.height + .025], metal('#152d3e', .64));
  const front = chassis.length / 2 + .003;
  for (const y of [-chassis.width * .3, chassis.width * .3]) box(root, [.015, chassis.width * .16, .025], [front, y, chassis.height * .65], new THREE.MeshBasicMaterial({ color: '#75eaff' }));
  for (const y of [-chassis.width * .3, chassis.width * .3]) box(root, [.012, chassis.width * .13, .017], [-front, y, chassis.height * .65], new THREE.MeshBasicMaterial({ color: '#bb6572' }));
  const wheelRadius = Math.min(.13, chassis.height * .45);
  for (const x of [-chassis.length * .32, chassis.length * .32]) for (const y of [-chassis.width / 2, chassis.width / 2]) {
    cylinder(root, wheelRadius, .09, [x, y, wheelRadius], metal('#09131e', .94, .1), 'y');
    cylinder(root, wheelRadius * .52, .096, [x, y, wheelRadius], trim, 'y');
    cylinder(root, wheelRadius * .19, .1, [x, y, wheelRadius], metal('#152e41'), 'y');
  }
  const selectedSensor = config.sensors.find(sensor => sensor.id === selected), selectedSupport = supportKey(selected);
  const activeRail = selectedSupport ? supportGeometry(config, selectedSupport).layout.rail : selectedSensor ? currentRail(config, selectedSensor).id : '';
  const joints = new Map<string, THREE.Vector3>();
  for (const rail of frameRails(chassis)) {
    if (rail.kind === 'plate') continue;
    const start = new THREE.Vector3(rail.start.x, rail.start.y, rail.start.z), end = new THREE.Vector3(rail.end.x, rail.end.y, rail.end.z), direction = end.clone().sub(start);
    const active = rail.id === activeRail, beam = new THREE.Mesh(extrusionProfile(direction.length()), metal(active ? '#c1e5e8' : '#bec9cd', .33, .60));
    beam.userData.bevelledPart = true;
    beam.position.copy(start).add(end).multiplyScalar(.5); beam.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction.normalize()); beam.userData.railId = rail.id; root.add(beam);
    // The dark slot floors are inset into the real 34 mm extrusion profile.
    const channelLength = Math.max(.005, start.distanceTo(end) - .018);
    const channelMaterial = metal('#344954', .50, .48);
    for (const sign of [-1, 1]) {
      for (const face of ['x', 'z'] as const) {
        const slot = new THREE.Mesh(new THREE.BoxGeometry(face === 'x' ? .00008 : .0058, channelLength, face === 'z' ? .00008 : .0058), channelMaterial);
        slot.position.copy(beam.position); slot.quaternion.copy(beam.quaternion);
        slot.translateX(face === 'x' ? sign * .01406 : 0); slot.translateZ(face === 'z' ? sign * .01406 : 0);
        slot.userData.profileChannel = true; root.add(slot);
      }
    }
    // Restrained edge highlights distinguish each channel from the brushed flats.
    for (const shift of [-.007, .007]) {
      const offset = rail.axis === 'x' ? new THREE.Vector3(0, shift, .0175) : rail.axis === 'y' ? new THREE.Vector3(shift, 0, .0175) : new THREE.Vector3(.0175, shift, 0);
      const seam = lines([start.clone().add(offset), end.clone().add(offset)], active ? '#e7f4f2' : '#e2e8e5', active ? .72 : .35); seam.renderOrder = 6; root.add(seam);
    }
    if (active) {
      const glow = new THREE.Mesh(new THREE.CylinderGeometry(.045, .045, start.distanceTo(end), 12), new THREE.MeshBasicMaterial({ color: '#44ddee', transparent: true, opacity: .038, depthWrite: false }));
      glow.position.copy(beam.position); glow.quaternion.copy(beam.quaternion); root.add(glow);
      for (const point of [start, end]) {
        const stop = new THREE.Mesh(new THREE.SphereGeometry(.031, 12, 8), new THREE.MeshBasicMaterial({ color: '#b6f6ff' })); stop.position.copy(point); root.add(stop);
      }
    }
    for (const point of [start, end]) joints.set(point.toArray().join(','), point);
  }
  for (const point of joints.values()) {
    const joint = box(root, [.042, .042, .042], [point.x, point.y, point.z], trim); joint.userData.frameJoint = true;
    const sx = Math.sign(point.x), sy = Math.sign(point.y), down = point.z > chassis.height + chassis.frameHeight * .9 ? -1 : 1;
    // Interior angle plates show the three frame levels without adding fictitious rails.
    box(root, [.075, .007, .055], [point.x - sx * .034, point.y - sy * .022, point.z + down * .028], trim);
    box(root, [.007, .075, .055], [point.x - sx * .022, point.y - sy * .034, point.z + down * .028], trim);
    fastener(root, [point.x - sx * .034, point.y - sy * .027, point.z + down * .030], metal('#d0d7da', .29, .68), 'y', .0045);
  }
  const mounts = addMountSupports(root, config, selected);
  const arrowZ = chassis.height + .045;
  root.add(new THREE.ArrowHelper(new THREE.Vector3(1, 0, 0), new THREE.Vector3(-chassis.length * .14, 0, arrowZ), chassis.length * .39, '#a4f0fb', .09, .075));
  return mounts;
}
/** Sensor locations are points; no unmeasured equipment housings are implied. */
function addSensorPoint(root: THREE.Group, sensor: Sensor, active: boolean) {
  const point = new THREE.Vector3(sensor.x, sensor.y, sensor.z);
  const opacity = sensor.enabled ? 1 : .38;
  const dot = new THREE.Mesh(new THREE.SphereGeometry(active ? .029 : .022, 24, 16), new THREE.MeshBasicMaterial({ color: sensor.color, transparent: true, opacity, depthTest: false, depthWrite: false }));
  dot.position.copy(point); dot.renderOrder = 22; root.add(dot);
  // A small light cap reads as a precise point rather than an equipment housing.
  const core = new THREE.Mesh(new THREE.SphereGeometry(active ? .010 : .007, 12, 8), new THREE.MeshBasicMaterial({ color: '#ecfbff', transparent: true, opacity: sensor.enabled ? .86 : .26, depthTest: false, depthWrite: false }));
  core.position.copy(point).add(new THREE.Vector3(-.006, -.009, .012)); core.renderOrder = 23; root.add(core);
  const ring = new THREE.Mesh(new THREE.RingGeometry(active ? .039 : .031, active ? .045 : .036, 32), new THREE.MeshBasicMaterial({ color: sensor.color, transparent: true, opacity: active ? .85 : sensor.enabled ? .48 : .2, side: THREE.DoubleSide, depthTest: false, depthWrite: false }));
  ring.position.copy(point); ring.renderOrder = 21; root.add(ring);
  if (active && sensor.enabled) root.add(new THREE.ArrowHelper(sensorBasis(sensor).forward, point, .31, sensor.color, .065, .035));
}
function sensorPointName(sensor: Sensor): string {
  const names: Record<string, string> = { camera1: '后相机', camera2: '前相机', camera3: '左相机', camera4: '右相机', mid360: 'MID360', p100r: '深度相机', bluesea: '蓝海雷达', stl19p: '底部雷达' };
  return names[sensor.id] ?? sensor.name;
}
/** Hollow preview points never enter the actual marker or picking collections. */
function addRecommendation(root: THREE.Group, config: Config, recommendation: Config | null | undefined, selected: string) {
  const group = new THREE.Group(); group.name = 'camera-recommendation-preview';
  const positions = sideRecommendationPreview(config, recommendation);
  const labelIds: string[] = [];
  for (const preview of positions) {
    const proposed = recommendation!.sensors.find(sensor => sensor.id === preview.id)!;
    const actual = config.sensors.find(sensor => sensor.id === preview.id)!;
    const origin = new THREE.Vector3(actual.x, actual.y, actual.z), target = new THREE.Vector3(proposed.x, proposed.y, proposed.z);
    // Three orthogonal wire rings distinguish recommended positions from solid live points.
    const radius = selected === proposed.id ? .074 : .061;
    for (const axis of ['x', 'y', 'z'] as const) {
      const points = Array.from({ length: 49 }, (_, index) => {
        const angle = index / 48 * Math.PI * 2, a = Math.cos(angle) * radius, b = Math.sin(angle) * radius;
        return target.clone().add(axis === 'x' ? new THREE.Vector3(0, a, b) : axis === 'y' ? new THREE.Vector3(a, 0, b) : new THREE.Vector3(a, b, 0));
      });
      const ring = lines(points, proposed.color, .88);
      (ring.material as THREE.LineBasicMaterial).depthTest = false; ring.renderOrder = 24; group.add(ring);
    }
    if (preview.translated) {
      const motion = new THREE.Line(new THREE.BufferGeometry().setFromPoints([origin, target]), new THREE.LineDashedMaterial({
        color: proposed.color, transparent: true, opacity: .72, dashSize: .035, gapSize: .025, depthWrite: false, depthTest: false,
      }));
      motion.computeLineDistances(); motion.renderOrder = 23; group.add(motion);
    }
    const direction = new THREE.ArrowHelper(sensorBasis(proposed).forward, target, .43, proposed.color, .085, .05);
    direction.name = `recommended-direction-${proposed.id}`;
    direction.line.renderOrder = 25; direction.cone.renderOrder = 25;
    for (const material of [direction.line.material, direction.cone.material]) {
      for (const item of Array.isArray(material) ? material : [material]) {
        item.depthTest = false; item.depthWrite = false; item.transparent = true; item.opacity = proposed.enabled ? .96 : .4;
      }
    }
    group.add(direction);
    if (selected === proposed.id) {
      const tag = label(`推荐·${sensorPointName(proposed)}`, '#d4f5fc', .115, 30);
      tag.position.copy(target).add(new THREE.Vector3(.08, .04, .12)); group.add(tag); labelIds.push(proposed.id);
    }
  }
  root.add(group);
  root.userData.recommendationPositions = positions;
  root.userData.recommendationDirections = positions.map(point => ({ id: point.id, yaw: point.yaw, pitch: point.pitch, rotated: point.rotated }));
  root.userData.recommendationLabelIds = labelIds;
}
function addScanPlane(root: THREE.Group, sensor: Sensor, selected: boolean) {
  const origin = new THREE.Vector3(sensor.x, sensor.y, sensor.z), { forward, right } = sensorBasis(sensor);
  const half = THREE.MathUtils.degToRad(sensor.hfov / 2), near = sensor.minRange, far = sensor.maxRange;
  const count = Math.max(12, Math.ceil(sensor.hfov / 4)), vertices: number[] = [], boundary: THREE.Vector3[] = [];
  const point = (angle: number, range: number) => origin.clone().addScaledVector(forward, Math.cos(angle) * range).addScaledVector(right, Math.sin(angle) * range);
  for (let index = 0; index < count; index++) {
    const a0 = -half + 2 * half * index / count, a1 = -half + 2 * half * (index + 1) / count;
    const n0 = point(a0, near), n1 = point(a1, near), f0 = point(a0, far), f1 = point(a1, far);
    vertices.push(...n0.toArray(), ...f0.toArray(), ...f1.toArray(), ...n0.toArray(), ...f1.toArray(), ...n1.toArray());
    boundary.push(f0, f1); if (near > .01) boundary.push(n0, n1);
  }
  if (sensor.hfov < 359.99) for (const angle of [-half, half]) boundary.push(point(angle, near), point(angle, far));
  const geometry = new THREE.BufferGeometry(); geometry.setAttribute('position', new THREE.Float32BufferAttribute(vertices, 3)); geometry.computeVertexNormals();
  // A single zero-thickness surface depicts a 2-D scan, never a vertical volume.
  const plane = new THREE.Mesh(geometry, volumeGlass(sensor.color, origin, far, selected));
  plane.userData.scanPlane = sensor.id; plane.renderOrder = 4; root.add(plane);
  const edge = lines(boundary, new THREE.Color(sensor.color).lerp(new THREE.Color('#b2c5d1'), .28), selected ? .54 : .22, true); edge.renderOrder = 6; root.add(edge);
}
function addStage(root: THREE.Group, config: Config) {
  const { analysis, chassis } = config;
  const length = analysis.length + .4, width = analysis.width + .4;
  const platform = new THREE.Mesh(roundedDeck(length, width, .065, .16), metal('#111e2a', .78, .16));
  platform.position.z = -.13; root.add(platform);
  const top = new THREE.Mesh(new THREE.PlaneGeometry(length - .08, width - .08), stageMaterial());
  top.position.z = -.064; root.add(top);
  const edge = new THREE.LineSegments(new THREE.EdgesGeometry(platform.geometry, 30), new THREE.LineBasicMaterial({ color: '#4f697c', transparent: true, opacity: .24 }));
  edge.position.copy(platform.position); root.add(edge);
  const shadow = new THREE.Mesh(new THREE.PlaneGeometry(chassis.length * 2.2, chassis.width * 2.5), contactShadow());
  shadow.position.z = -.059; shadow.renderOrder = 1; root.add(shadow);
}
function addDistanceReference(root: THREE.Group, config: Config) {
  const limit = Math.min(config.analysis.length, config.analysis.width) / 2, step = limit < 1.5 ? .5 : 1;
  for (let distance = step; distance <= limit + .001; distance += step) {
    const points = Array.from({ length: 129 }, (_, index) => new THREE.Vector3(Math.cos(index / 128 * Math.PI * 2) * distance, Math.sin(index / 128 * Math.PI * 2) * distance, -.052));
    const ring = lines(points, '#6d8b9f', .17); ring.renderOrder = 1; root.add(ring);
    if (distance <= 2 && Number.isInteger(distance)) {
      const tag = label(`${formatLength(distance)} cm`, '#718b9d', .085, 28); tag.position.set(.06, -distance - .04, -.045); root.add(tag);
    }
  }
  const length = config.analysis.length / 2, width = config.analysis.width / 2, z = config.analysis.height, corner = Math.min(.22, config.analysis.length / 6, config.analysis.width / 6);
  for (const x of [-length, length]) for (const y of [-width, width]) root.add(lines([new THREE.Vector3(x, y - Math.sign(y) * corner, z), new THREE.Vector3(x, y, z), new THREE.Vector3(x - Math.sign(x) * corner, y, z)], '#89aaba', .45));
  const x = -length, y = -width;
  const ruler = lines([new THREE.Vector3(x, y, -.052), new THREE.Vector3(x, y, z)], '#7797a9', .35); root.add(ruler);
  for (const level of [-.052, z]) root.add(lines([new THREE.Vector3(x - .045, y, level), new THREE.Vector3(x + .045, y, level)], '#91adbb', .5));
}
function addVolume(root: THREE.Group, sensor: Sensor, selected: boolean) {
  if (sensor.kind === 'scan') { addScanPlane(root, sensor, selected); return; }
  const origin = new THREE.Vector3(sensor.x, sensor.y, sensor.z), { forward, right, up } = sensorBasis(sensor), color = new THREE.Color(sensor.color);
  const vertices: number[] = [], edgePoints: THREE.Vector3[] = [], rad = THREE.MathUtils.degToRad;
  function direction(horizontal: number, vertical: number) {
    if (sensor.kind === 'lidar') return forward.clone().multiplyScalar(Math.cos(vertical) * Math.cos(horizontal)).addScaledVector(right, Math.cos(vertical) * Math.sin(horizontal)).addScaledVector(up, Math.sin(vertical)).normalize();
    return forward.clone().addScaledVector(right, Math.tan(horizontal)).addScaledVector(up, Math.tan(vertical)).normalize();
  }
  const point = (h: number, v: number, range: number) => origin.clone().addScaledVector(direction(h, v), range);
  const triangle = (a: THREE.Vector3, b: THREE.Vector3, c: THREE.Vector3) => { vertices.push(...a.toArray(), ...b.toArray(), ...c.toArray()); };
  const hMin = -rad(sensor.hfov / 2), hMax = rad(sensor.hfov / 2), offset = sensor.kind === 'lidar' ? sensor.verticalOffset ?? 0 : 0;
  const vMin = rad(offset - sensor.vfov / 2), vMax = rad(offset + sensor.vfov / 2), near = Math.max(.0001, sensor.minRange), far = sensor.maxRange;
  const hSteps = sensor.kind === 'lidar' ? Math.max(12, Math.ceil(sensor.hfov / 5)) : 12, vSteps = 8;
  // Radial near/far shells match the Euclidean range used by the coverage test.
  for (let hIndex = 0; hIndex < hSteps; hIndex++) {
    const h0 = hMin + (hMax - hMin) * hIndex / hSteps, h1 = hMin + (hMax - hMin) * (hIndex + 1) / hSteps;
    for (let vIndex = 0; vIndex < vSteps; vIndex++) {
      const v0 = vMin + (vMax - vMin) * vIndex / vSteps, v1 = vMin + (vMax - vMin) * (vIndex + 1) / vSteps;
      for (const distance of [near, far]) { const a = point(h0, v0, distance), b = point(h1, v0, distance), c = point(h1, v1, distance), d = point(h0, v1, distance); triangle(a, b, c); triangle(a, c, d); }
    }
    for (const v of [vMin, vMax]) {
      const a = point(h0, v, near), b = point(h1, v, near), c = point(h1, v, far), d = point(h0, v, far); triangle(a, b, c); triangle(a, c, d);
      edgePoints.push(point(h0, v, far), point(h1, v, far)); if (sensor.minRange > .05) edgePoints.push(point(h0, v, near), point(h1, v, near));
    }
  }
  if (sensor.hfov < 359.99) for (const h of [hMin, hMax]) {
    for (let index = 0; index < vSteps; index++) {
      const v0 = vMin + (vMax - vMin) * index / vSteps, v1 = vMin + (vMax - vMin) * (index + 1) / vSteps;
      const a = point(h, v0, near), b = point(h, v1, near), c = point(h, v1, far), d = point(h, v0, far); triangle(a, b, c); triangle(a, c, d); edgePoints.push(point(h, v0, far), point(h, v1, far));
    }
    for (const v of [vMin, vMax]) edgePoints.push(point(h, v, near), point(h, v, far));
  }
  const geometry = new THREE.BufferGeometry(); geometry.setAttribute('position', new THREE.Float32BufferAttribute(vertices, 3)); geometry.computeVertexNormals();
  const volume = new THREE.Mesh(geometry, volumeGlass(color, origin, far, selected)); volume.renderOrder = 4; volume.userData.volumeId = sensor.id; root.add(volume);
  const wireframe = lines(edgePoints, color.clone().lerp(new THREE.Color('#b2c5d1'), .28), selected ? .45 : sensor.kind === 'lidar' ? .13 : .22, true); wireframe.renderOrder = 5; root.add(wireframe);
  if (selected && sensor.kind !== 'lidar') {
    const axis = new THREE.Line(new THREE.BufferGeometry().setFromPoints([origin, origin.clone().addScaledVector(forward, Math.min(far, 1.2))]), new THREE.LineDashedMaterial({ color, transparent: true, opacity: .32, dashSize: .045, gapSize: .065, depthWrite: false }));
    axis.computeLineDistances(); root.add(axis);
  }
}
function addCoverage(root: THREE.Group, config: Config, result: Coverage, overlap: boolean) {
  const layer = coverageLayer(result, config.analysis.height, overlap); root.add(layer);
  root.userData.surfaceStats = layer.userData.surfaceStats;
}
function fillScene(runtime: Runtime, props: Props) {
  const { content: root } = runtime; disposeGroup(root); runtime.markers = [];
  const { config, coverage, selected, showVolumes, volumeScope, showLabels, showOverlap } = props;
  addStage(root, config); addCoverage(root, config, coverage, showOverlap); addDistanceReference(root, config); runtime.plates = addVehicle(root, config, selected);
  for (const sensor of config.sensors) {
    const active = selected === sensor.id;
    const inScope = volumeScope === 'all' || active || (supportKey(selected) === supportIdForSensor(sensor.id));
    if (showVolumes && sensor.enabled && inScope) addVolume(root, sensor, active || volumeScope === 'selected');
    const point = new THREE.Vector3(sensor.x, sensor.y, sensor.z);
    // The larger invisible hit target is centred on the same exact installation point.
    const marker = new THREE.Mesh(new THREE.SphereGeometry(active ? .052 : .042, 16, 12), new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthTest: false, depthWrite: false }));
    marker.position.copy(point); marker.userData.sensorId = sensor.id; marker.userData.sensorKind = sensor.kind; marker.userData.enabled = sensor.enabled;
    marker.renderOrder = 20; runtime.markers.push(marker); root.add(marker);
    addSensorPoint(root, sensor, active);
    if (active) {
      const drop = lines([new THREE.Vector3(sensor.x, sensor.y, 0), point], sensor.color, .27); root.add(drop);
      const foot = new THREE.Mesh(new THREE.RingGeometry(.035, .044, 24), new THREE.MeshBasicMaterial({ color: sensor.color, transparent: true, opacity: .6, side: THREE.DoubleSide, depthWrite: false })); foot.position.set(sensor.x, sensor.y, .015); root.add(foot);
    }
    if (showLabels || active) {
      const offsets: Record<string, [number, number, number]> = { camera1: [-.12, .03, .19], camera2: [.1, -.05, .28], camera3: [-.12, .08, .14], camera4: [.1, -.08, .1], mid360: [.06, .04, .22], p100r: [.12, .04, -.13], bluesea: [.1, -.02, .11], stl19p: [-.12, .03, .12] };
      const text = sensorPointName(sensor);
      const tag = label(text, active ? '#ecf8fd' : sensor.enabled ? sensor.color : '#7e94a5', active ? .16 : .12, 34);
      tag.userData.sensorLabel = sensor.id;
      tag.position.copy(point).add(new THREE.Vector3(...(offsets[sensor.id] ?? [.08, .03, .16]))); root.add(tag);
      const lead = lines([point, tag.position], sensor.color, active ? .65 : sensor.enabled ? .35 : .17); lead.renderOrder = 10; root.add(lead);
    }
  }
  addRecommendation(root, config, props.recommendation, selected);
  root.userData.drawnVolumeIds = root.children.filter(object => typeof object.userData.volumeId === 'string').map(object => object.userData.volumeId);
  root.userData.drawnScanIds = root.children.filter(object => typeof object.userData.scanPlane === 'string').map(object => object.userData.scanPlane);
  root.userData.labelIds = root.children.filter(object => typeof object.userData.sensorLabel === 'string').map(object => object.userData.sensorLabel);
  const detailCounts = { bevelledParts: 0, profileChannels: 0, fasteners: 0, frameJoints: 0 };
  root.traverse(object => {
    if (object.userData.bevelledPart) detailCounts.bevelledParts++;
    if (object.userData.profileChannel) detailCounts.profileChannels++;
    if (object.userData.fastenerPart) detailCounts.fasteners++;
    if (object.userData.frameJoint) detailCounts.frameJoints++;
  });
  root.userData.modelDetailCounts = detailCounts;
  runtime.render();
}
export default function Scene(props: Props) {
  const host = useRef<HTMLDivElement>(null), runtime = useRef<Runtime | null>(null), latestProps = useRef(props); latestProps.current = props;
  const [error, setError] = useState(''), [ready, setReady] = useState(false);
  useEffect(() => {
    const element = host.current!; let renderer: THREE.WebGLRenderer;
    try { renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: 'low-power' }); }
    catch { setError('当前浏览器无法启动三维渲染，请启用硬件加速后重新打开。'); return; }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75)); renderer.outputColorSpace = THREE.SRGBColorSpace; renderer.setClearColor('#09131f');
    const canvas = renderer.domElement;
    canvas.setAttribute('role', 'img'); canvas.setAttribute('aria-label', '传感器覆盖三维视图，传感器可在安装空间盒内移动，拖动空白处旋转'); canvas.setAttribute('tabindex', '0'); element.appendChild(canvas);
    const scene = new THREE.Scene(); scene.fog = new THREE.Fog('#09131f', 18, 55);
    scene.add(new THREE.HemisphereLight('#e4edf2', '#2b3d4a', 2.25));
    const key = new THREE.DirectionalLight('#fff6e9', 3.0); key.position.set(3, -4, 7); scene.add(key);
    const fill = new THREE.DirectionalLight('#dbe6ec', .75); fill.position.set(-3, -2, 2); scene.add(fill);
    const rim = new THREE.DirectionalLight('#a7d2e3', 1.85); rim.position.set(-4, 3, 4); scene.add(rim);
    const camera = new THREE.PerspectiveCamera(44, 1, .03, 500); camera.up.set(0, 0, 1); camera.position.set(6, -7.5, 7);
    const controls = new OrbitControls(camera, canvas); controls.target.set(0, 0, .2); controls.enableDamping = false; controls.maxPolarAngle = Math.PI / 2 - .015; controls.minPolarAngle = .002; controls.minDistance = .65; controls.maxDistance = 65; controls.rotateSpeed = .7; controls.zoomSpeed = .8; controls.mouseButtons = { LEFT: THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: THREE.MOUSE.PAN };
    const content = new THREE.Group(); scene.add(content);
    // The anchor survives fillScene: rebuilding the coverage meshes cannot reset a drag.
    const anchor = new THREE.Object3D(); scene.add(anchor);
    const transform = new TransformControls(camera, canvas);
    // Route pointer events ourselves so a marker drag and OrbitControls never compete.
    transform.disconnect(); canvas.style.touchAction = 'none';
    transform.setMode('translate'); transform.setSpace('world'); transform.setTranslationSnap(.01); transform.setSize(.8); transform.setColors('#fb7185', '#4ade80', '#60a5fa', '#f8d568');
    // A single visible axis also hides Three.js' plane and center handles.
    transform.showX = false; transform.showY = false; transform.showZ = false;
    const helper = transform.getHelper(); scene.add(helper);
    type Drag = { kind: 'direct' | 'gizmo'; pointerId: number; id: string; start: THREE.Vector3; snapshot: MoveSnapshot; epoch: number; rail: FrameRail; isSupport: boolean; min: { x: number; y: number; z: number }; max: { x: number; y: number; z: number }; frame: string; plane?: THREE.Plane; offset?: THREE.Vector3 };
    let drag: Drag | null = null, pending: { id: string; position: { x: number; y: number; z: number }; rail: string; frame: string; epoch: number } | null = null;
    let publishFrame = 0, frames = 0, downX = 0, downY = 0;
    const ray = new THREE.Raycaster();
    // TransformControls' internal pointer API consumes NDC x/y/button, despite the upstream PointerEvent annotation.
    const pointer = (event: PointerEvent, button = event.button) => { const rect = canvas.getBoundingClientRect(); return { x: (event.clientX - rect.left) / rect.width * 2 - 1, y: -(event.clientY - rect.top) / rect.height * 2 + 1, button } as PointerEvent; };
    const cast = (event: PointerEvent) => { const point = pointer(event); ray.setFromCamera(new THREE.Vector2(point.x, point.y), camera); return ray; };
    const hitSensor = (event: PointerEvent) => {
      const hits = cast(event).intersectObjects(runtime.current?.markers ?? [], false);
      // A selected lower sensor stays reachable in top view when another point shares its XY.
      const selected = latestProps.current.selected;
      const hit = hits.find(candidate => candidate.object.userData.sensorId === selected) ?? hits[0];
      return hit?.object.userData.sensorId as string | undefined;
    };
    const hitPlate = (event: PointerEvent) => { const hit = cast(event).intersectObjects(runtime.current?.plates ?? [], false)[0]; return hit?.object.userData.selectionId as string | undefined; };
    function projection(point: THREE.Vector3) { const rect = canvas.getBoundingClientRect(), projected = point.clone().project(camera); return { x: rect.left + (projected.x + 1) * rect.width / 2, y: rect.top + (1 - projected.y) * rect.height / 2 }; }
    function updateProjectionData() {
      const owner = element.parentElement; if (!owner) return;
      const config = latestProps.current.config, mounts = supportBoxes(config), front = supportGeometry(config, 'front');
      // Public projection metadata is read-only and contains no source images or device access.
      const sensors = (runtime.current?.markers ?? []).map(marker => ({ id: String(marker.userData.sensorId), kind: marker.userData.sensorKind, enabled: marker.userData.enabled === true, ...projection(marker.position) }));
      const rails = frameRails(config.chassis), centerRail = front.rail;
      const drawnVolumes = content.userData.drawnVolumeIds as string[], drawnScans = content.userData.drawnScanIds as string[], labelIds = content.userData.labelIds as string[];
      const surface = content.userData.surfaceStats;
      const recommendationPositions = content.userData.recommendationPositions ?? [];
      owner.dataset.recommendationIds = JSON.stringify(recommendationPositions.map((point: { id: string }) => point.id));
      owner.dataset.recommendationPositions = JSON.stringify(recommendationPositions);
      owner.dataset.recommendationPointCount = String(recommendationPositions.length);
      owner.dataset.recommendationMotionCount = String(recommendationPositions.filter((point: { translated: boolean }) => point.translated).length);
      owner.dataset.recommendationRotatedIds = JSON.stringify(recommendationPositions.filter((point: { rotated: boolean }) => point.rotated).map((point: { id: string }) => point.id));
      owner.dataset.recommendationDirections = JSON.stringify(content.userData.recommendationDirections ?? []);
      owner.dataset.recommendationLabelIds = JSON.stringify(content.userData.recommendationLabelIds ?? []);
      owner.dataset.coverageStyle = 'classified-glass-v1'; owner.dataset.renderMode = 'on-demand';
      owner.dataset.modelDetailVersion = 'machined-supports-v2'; owner.dataset.modelDetailCounts = JSON.stringify(content.userData.modelDetailCounts ?? {});
      owner.dataset.volumeScope = latestProps.current.volumeScope;
      owner.dataset.drawnVolumeIds = JSON.stringify(drawnVolumes); owner.dataset.drawnVolumeCount = String(drawnVolumes.length);
      owner.dataset.drawnScanIds = JSON.stringify(drawnScans); owner.dataset.drawnScanCount = String(drawnScans.length);
      owner.dataset.labelIds = JSON.stringify(labelIds); owner.dataset.labelCount = String(labelIds.length);
      owner.dataset.surfaceClassCounts = JSON.stringify(surface.classCounts); owner.dataset.surfaceCellCount = String(surface.cellCount);
      owner.dataset.surfaceVertexCount = String(surface.vertexCount); owner.dataset.surfaceHeight = String(surface.height);
      owner.dataset.surfaceHeightRange = JSON.stringify(surface.heightRange);
      owner.dataset.surfaceBoundarySegments = String(surface.boundarySegments); owner.dataset.surfaceBoundaryVertexCount = String(surface.boundarySegments * 2);
      owner.dataset.surfaceBoundaryHeightRange = JSON.stringify(surface.boundaryHeightRange);
      owner.dataset.sensorDisplay = 'points'; owner.dataset.sensorProjections = JSON.stringify(sensors);
      owner.dataset.frameRailCount = String(rails.length); owner.dataset.aluminumMemberCount = String(rails.filter(rail => rail.kind !== 'plate').length);
      owner.dataset.plateMountCount = String(runtime.current?.plates.length ?? 0); owner.dataset.plateRailCount = String(rails.filter(rail => rail.kind === 'plate').length);
      owner.dataset.sensorPointCount = String(sensors.length); owner.dataset.cameraPosition = JSON.stringify(camera.position.toArray());
      const selectedKey = supportKey(latestProps.current.selected) ?? (config.sensors.some(sensor => sensor.id === latestProps.current.selected) ? supportIdForSensor(latestProps.current.selected) : null);
      owner.dataset.supportBoxCount = String(mounts.length);
      owner.dataset.supportBracketModels = JSON.stringify(content.userData.supportBracketModels ?? []);
      owner.dataset.supportMountModels = JSON.stringify(content.userData.supportMountModels ?? []);
      owner.dataset.supportBoxGeometry = JSON.stringify(mounts.map(item => ({ id: item.id, center: item.center, anchor: item.anchor, min: item.min, max: item.max,
        size: { x: item.layout.sizeX, y: item.layout.sizeY, z: item.layout.sizeZ }, rail: item.layout.rail, style: item.layout.style, thickness: item.layout.thickness, belowBeam: item.belowBeam,
        beamBottomZ: item.beamBottomZ, beamGap: item.beamGap, topEnvelopeZ: item.topEnvelopeZ, openingDirection: supportOpeningDirection(item) })));
      owner.dataset.supportBoxProjections = JSON.stringify(mounts.map(item => ({ id: item.id, selectionId: SUPPORT_PREFIX + item.id,
        center: projection(new THREE.Vector3(item.center.x, item.center.y, item.center.z)),
        corners: [item.min.x, item.max.x].flatMap(x => [item.min.y, item.max.y].flatMap(y => [item.min.z, item.max.z].map(z => projection(new THREE.Vector3(x, y, z))))),
        pickPoints: [-.42, .42].flatMap(x => [-.42, .42].flatMap(y => [-.42, .42].map(z => projection(new THREE.Vector3(item.center.x + item.layout.sizeX * x, item.center.y + item.layout.sizeY * y, item.center.z + item.layout.sizeZ * z))))) })));
      owner.dataset.sensorSupportIds = JSON.stringify(Object.fromEntries(config.sensors.map(sensor => [sensor.id, supportIdForSensor(sensor.id)])));
      owner.dataset.selectedSupportId = selectedKey ?? '';
      owner.dataset.selectedSupportBounds = JSON.stringify(selectedKey ? { min: supportGeometry(config, selectedKey).min, max: supportGeometry(config, selectedKey).max } : null);
      owner.dataset.sensorMovement = 'cuboid-xyz';
      // Retain front-only installation metadata for older diagnostics.
      owner.dataset.plateCenter = JSON.stringify(front.center);
      owner.dataset.plateSize = JSON.stringify({ width: front.layout.sizeY, height: front.layout.sizeZ, thickness: front.layout.thickness });
      owner.dataset.plateBounds = JSON.stringify({ minCenterY: front.rail.start.y, maxCenterY: front.rail.end.y });
      owner.dataset.plateThickness = String(front.layout.thickness); owner.dataset.plateMoveAxis = 'Y';
      owner.dataset.plateGeometryDimensions = JSON.stringify({ x: front.layout.thickness, y: front.layout.sizeY, z: front.layout.sizeZ });
      owner.dataset.plateGeometryCenter = JSON.stringify(front.center);
      owner.dataset.plateSelected = String(latestProps.current.selected === PLATE_ID); owner.dataset.plateDragActive = String(drag?.id === PLATE_ID);
      owner.dataset.plateProjection = JSON.stringify(projection(new THREE.Vector3(front.center.x, front.center.y, front.center.z)));
      owner.dataset.plateDragProjection = JSON.stringify(projection(new THREE.Vector3(front.center.x, front.center.y, front.center.z + front.layout.sizeZ * .32)));
      owner.dataset.platePickProjections = JSON.stringify([.32, -.32].map(offset => projection(new THREE.Vector3(front.center.x, front.center.y, front.center.z + front.layout.sizeZ * offset))));
      owner.dataset.plateRailProjections = JSON.stringify({ start: projection(new THREE.Vector3(centerRail.start.x, centerRail.start.y, centerRail.start.z)), end: projection(new THREE.Vector3(centerRail.end.x, centerRail.end.y, centerRail.end.z)) });
      owner.dataset.plateSensorPositions = JSON.stringify(Object.fromEntries(config.sensors.filter(sensor => sensor.id === 'camera2' || sensor.id === 'p100r').map(sensor => [sensor.id, { x: sensor.x, y: sensor.y, z: sensor.z }])));
      const selectedBox = supportKey(latestProps.current.selected), selectedData = selectedBox ? supportGeometry(config, selectedBox) : null;
      const selected = selectedData ? { id: latestProps.current.selected, ...projection(new THREE.Vector3(selectedData.center.x, selectedData.center.y, selectedData.center.z)) }
        : sensors.find(sensor => sensor.id === latestProps.current.selected);
      if (selected) { owner.dataset.selectedScreenX = String(selected.x); owner.dataset.selectedScreenY = String(selected.y); }
      else { delete owner.dataset.selectedScreenX; delete owner.dataset.selectedScreenY; }
      const handles: Record<string, { x: number; y: number }> = {};
      const handleDistances: Record<string, number> = {};
      if (helper.visible && transform.object && latestProps.current.dragEnabled) helper.traverse(object => {
        if (!['X', 'Y', 'Z'].includes(object.name) || !(object instanceof THREE.Mesh)) return;
        let visible = true; for (let parent: THREE.Object3D | null = object; parent; parent = parent.parent) if (!parent.visible) visible = false;
        const materials = Array.isArray(object.material) ? object.material : [object.material];
        if (!visible || !materials.some(material => material.visible && (!material.transparent || material.opacity > 0))) return;
        const geometry = object.geometry as THREE.BufferGeometry; geometry.computeBoundingBox(); const center = geometry.boundingBox!.getCenter(new THREE.Vector3()); object.localToWorld(center);
        const distance = center.clone().sub(anchor.position).getComponent({ X: 0, Y: 1, Z: 2 }[object.name]!);
        // Three traverses each shaft before its arrow. Report the visible positive
        // cone rather than a shaft midpoint that can overlap a plane handle.
        if (distance > .01 && distance > (handleDistances[object.name] ?? 0)) {
          handleDistances[object.name] = distance; handles[object.name] = projection(center);
        }
      });
      owner.dataset.gizmoProjections = JSON.stringify(handles);
      if (handles.Z) { owner.dataset.zHandleScreenX = String(handles.Z.x); owner.dataset.zHandleScreenY = String(handles.Z.y); }
      else { delete owner.dataset.zHandleScreenX; delete owner.dataset.zHandleScreenY; }
      owner.dataset.dragActive = drag ? 'true' : 'false';
      const target = selectedTarget(config, latestProps.current.selected);
      if (target) {
        owner.dataset.selectedRail = target.rail.id; owner.dataset.selectedRailAxis = target.rail.axis;
        owner.dataset.railProjections = JSON.stringify({ start: projection(new THREE.Vector3(target.rail.start.x, target.rail.start.y, target.rail.start.z)), end: projection(new THREE.Vector3(target.rail.end.x, target.rail.end.y, target.rail.end.z)) });
      } else { delete owner.dataset.selectedRail; delete owner.dataset.selectedRailAxis; delete owner.dataset.railProjections; }
    }
    function render() { if (!element.isConnected) return; renderer.render(scene, camera); if (element.parentElement) element.parentElement.dataset.frameCount = String(++frames); updateProjectionData(); }
    function applyTargetPosition(id: string, position: { x: number; y: number; z: number }) {
      const key = supportKey(id);
      if (key) latestProps.current.onSupportMove(key, position);
      else latestProps.current.onMove(id, position);
    }
    function publish() {
      publishFrame = 0; if (!pending) return; const next = pending; pending = null;
      const current = latestProps.current, target = selectedTarget(current.config, next.id);
      // A structural edit must never receive a position queued against the previous geometry.
      if (target && target.enabled && next.epoch === current.interactionRevision && next.frame === frameSignature(current.config, next.id) && target.rail.id === next.rail) applyTargetPosition(next.id, next.position);
    }
    function queuePosition(position: { x: number; y: number; z: number }) {
      if (!drag) return;
      if (drag.epoch !== latestProps.current.interactionRevision) { finishDrag(true, false); return; }
      anchor.position.set(position.x, position.y, position.z);
      const previous = pending?.position ?? selectedTarget(latestProps.current.config, drag.id)?.position;
      if (previous && Math.abs(previous.x - position.x) < 1e-12 && Math.abs(previous.y - position.y) < 1e-12 && Math.abs(previous.z - position.z) < 1e-12) { render(); return; }
      pending = { id: drag.id, position, rail: drag.rail.id, frame: drag.frame, epoch: drag.epoch };
      if (!publishFrame) publishFrame = requestAnimationFrame(publish); render();
    }
    function finishDrag(cancel = false, restoreStart = true) {
      if (!drag) return; const previous = drag; drag = null;
      if (publishFrame) cancelAnimationFrame(publishFrame); publishFrame = 0;
      if (cancel) {
        pending = null;
        const current = latestProps.current, target = selectedTarget(current.config, previous.id);
        if (restoreStart && target && previous.epoch === current.interactionRevision && previous.frame === frameSignature(current.config, previous.id) && target.rail.id === previous.rail.id) {
          anchor.position.copy(previous.start);
          const snapshot = moveSnapshot(current.config, previous.id);
          const saved = previous.snapshot.inSupport, actual = snapshot.inSupport;
          if (snapshot.t !== previous.snapshot.t || saved && (!actual || saved.x !== actual.x || saved.y !== actual.y || saved.z !== actual.z)) {
            current.onRestoreMove(previous.id, previous.snapshot);
          }
        } else if (target) anchor.position.set(target.position.x, target.position.y, target.position.z);
      } else publish();
      transform.pointerUp(null); controls.enabled = true;
      if (canvas.hasPointerCapture(previous.pointerId)) canvas.releasePointerCapture(previous.pointerId);
      canvas.style.cursor = latestProps.current.dragEnabled ? 'grab' : ''; latestProps.current.onDragState?.(false); render();
    }
    function configureTransform(target: NonNullable<ReturnType<typeof selectedTarget>>) {
      const { rail, isSupport, min, max } = target;
      transform.showX = !isSupport || rail.axis === 'x'; transform.showY = !isSupport || rail.axis === 'y'; transform.showZ = !isSupport || rail.axis === 'z';
      if (isSupport && transform.axis !== rail.axis.toUpperCase()) transform.axis = null;
      const bounds = isSupport ? {
        minX: Math.min(rail.start.x, rail.end.x), maxX: Math.max(rail.start.x, rail.end.x),
        minY: Math.min(rail.start.y, rail.end.y), maxY: Math.max(rail.start.y, rail.end.y),
        minZ: Math.min(rail.start.z, rail.end.z), maxZ: Math.max(rail.start.z, rail.end.z),
      } : { minX: min.x, maxX: max.x, minY: min.y, maxY: max.y, minZ: min.z, maxZ: max.z };
      // @types/three 0.180 misspells minX as minx; runtime uses camel-case bounds.
      Object.assign(transform, bounds);
    }
    function constrained(position: { x: number; y: number; z: number }) {
      if (!drag) return position;
      return drag.isSupport ? constrainDragPosition(drag.start, position, drag.rail)
        : constrainBoxPosition(drag.start, position, drag.min, drag.max);
    }
    function syncDrag() {
      const current = latestProps.current, target = selectedTarget(current.config, current.selected), rail = target?.rail;
      if (drag && (drag.epoch !== current.interactionRevision || drag.frame !== frameSignature(current.config, drag.id) || (target?.id === drag.id && rail?.id !== drag.rail.id))) finishDrag(true, false);
      if (drag && (!current.dragEnabled || !target?.enabled || target.id !== drag.id)) finishDrag(true);
      transform.enabled = current.dragEnabled && !!target?.enabled;
      if (target) configureTransform(target);
      if (!target?.enabled || !current.dragEnabled) { transform.detach(); canvas.style.cursor = ''; }
      else {
        if (!drag) anchor.position.set(target.position.x, target.position.y, target.position.z);
        if (transform.object !== anchor) transform.attach(anchor);
      }
      render();
    }
    const objectChange = () => { if (drag?.kind === 'gizmo') queuePosition(constrained(anchor.position)); };
    transform.addEventListener('objectChange', objectChange); transform.addEventListener('change', render);
    runtime.current = { scene, renderer, camera, controls, content, markers: [], plates: [], render, syncDrag, cancelDrag: () => finishDrag(true) };
    const resize = () => { const width = element.clientWidth, height = element.clientHeight; if (!width || !height) return; renderer.setSize(width, height); camera.aspect = width / height; camera.updateProjectionMatrix(); render(); };
    const observer = new ResizeObserver(resize); observer.observe(element); controls.addEventListener('change', render);
    const lost = (event: Event) => { event.preventDefault(); finishDrag(true); setReady(false); setError('三维渲染暂时中断，正在等待浏览器恢复。'); }, restored = () => { setError(''); setReady(true); render(); };
    canvas.addEventListener('webglcontextlost', lost); canvas.addEventListener('webglcontextrestored', restored);
    const consume = (event: PointerEvent) => { event.preventDefault(); event.stopImmediatePropagation(); };
    function beginDirectDrag(event: PointerEvent, id: string) {
      const config = latestProps.current.config, target = selectedTarget(config, id);
      if (!target?.enabled) return false;
      const { rail, isSupport, min, max } = target, start = new THREE.Vector3(target.position.x, target.position.y, target.position.z);
      const normal = start.clone().sub(camera.position).normalize();
      if (isSupport) {
        const axis = new THREE.Vector3(rail.axis === 'x' ? 1 : 0, rail.axis === 'y' ? 1 : 0, rail.axis === 'z' ? 1 : 0);
        normal.addScaledVector(axis, -normal.dot(axis));
      }
      if (normal.lengthSq() < 1e-8) { latestProps.current.onSelect(id); consume(event); return false; }
      const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(normal.normalize(), start), intersection = cast(event).ray.intersectPlane(plane, new THREE.Vector3());
      if (!intersection) return false;
      latestProps.current.onSelect(id); anchor.position.copy(start); configureTransform(target); if (transform.object !== anchor) transform.attach(anchor);
      drag = { kind: 'direct', pointerId: event.pointerId, id, start, snapshot: moveSnapshot(config, id), epoch: latestProps.current.interactionRevision, rail, isSupport, min, max,
        frame: frameSignature(config, id), plane, offset: start.clone().sub(intersection) };
      return true;
    }
    const down = (event: PointerEvent) => {
      downX = event.clientX; downY = event.clientY;
      if (drag) { consume(event); return; }
      if (event.button !== 0 || !latestProps.current.dragEnabled) return;
      const id = hitSensor(event), target = id ? selectedTarget(latestProps.current.config, id) : null;
      // Device points always win over the board face, including an excluded device.
      if (id && !target?.enabled) { latestProps.current.onSelect(id); consume(event); return; }
      if (target) {
        if (!beginDirectDrag(event, target.id)) return;
      } else {
        if (transform.enabled && transform.object) {
          helper.updateMatrixWorld(true); transform.pointerHover(pointer(event));
          const selected = selectedTarget(latestProps.current.config, latestProps.current.selected);
          if (selected?.enabled && transform.axis && (!selected.isSupport || transform.axis === selected.rail.axis.toUpperCase())) {
            drag = { kind: 'gizmo', pointerId: event.pointerId, id: selected.id, start: anchor.position.clone(), snapshot: moveSnapshot(latestProps.current.config, selected.id), epoch: latestProps.current.interactionRevision, rail: selected.rail,
              isSupport: selected.isSupport, min: selected.min, max: selected.max, frame: frameSignature(latestProps.current.config, selected.id) };
            transform.pointerDown(pointer(event));
            if (!transform.dragging) drag = null;
          }
        }
        const mountHit = !drag ? hitPlate(event) : undefined;
        if (mountHit) { if (!beginDirectDrag(event, mountHit)) return; }
        if (!drag) return;
      }
      controls.enabled = false; canvas.setPointerCapture(event.pointerId); canvas.focus({ preventScroll: true }); canvas.style.cursor = 'grabbing'; latestProps.current.onDragState?.(true); consume(event); render();
    };
    const move = (event: PointerEvent) => {
      if (drag) {
        if (event.pointerId !== drag.pointerId) { consume(event); return; }
        if (drag.kind === 'direct') { const point = cast(event).ray.intersectPlane(drag.plane!, new THREE.Vector3()); if (point) queuePosition(constrained(point.add(drag.offset!))); }
        else transform.pointerMove(pointer(event, -1));
        consume(event); return;
      }
      if (!latestProps.current.dragEnabled) return;
      const sensorHit = hitSensor(event);
      if (transform.enabled && !sensorHit) { helper.updateMatrixWorld(true); transform.pointerHover(pointer(event)); }
      else transform.axis = null;
      canvas.style.cursor = sensorHit || transform.axis || hitPlate(event) ? 'grab' : '';
    };
    const up = (event: PointerEvent) => {
      if (drag) { if (event.pointerId === drag.pointerId) finishDrag(); consume(event); return; }
      if (Math.hypot(event.clientX - downX, event.clientY - downY) > 5 || event.button !== 0) return;
      const id = hitSensor(event) ?? hitPlate(event); if (id) latestProps.current.onSelect(id);
    };
    const cancel = (event?: Event) => { if (drag && (!event || !('pointerId' in event) || (event as PointerEvent).pointerId === drag.pointerId)) finishDrag(true); };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape' && drag) { event.preventDefault(); event.stopPropagation(); finishDrag(true); } };
    const hidden = () => { if (document.visibilityState === 'hidden') cancel(); };
    canvas.addEventListener('pointerdown', down, true); canvas.addEventListener('pointermove', move, true); canvas.addEventListener('pointerup', up, true); canvas.addEventListener('pointercancel', cancel); canvas.addEventListener('lostpointercapture', cancel);
    window.addEventListener('blur', cancel); window.addEventListener('pagehide', cancel); window.addEventListener('keydown', escape, true); document.addEventListener('visibilitychange', hidden);
    fillScene(runtime.current, latestProps.current); syncDrag(); controls.update(); resize(); setReady(true);
    return () => {
      // Unmount clears the pending callback rather than mutating a parent that is leaving.
      if (publishFrame) cancelAnimationFrame(publishFrame); pending = null; drag = null;
      observer.disconnect(); controls.removeEventListener('change', render); controls.dispose(); transform.removeEventListener('objectChange', objectChange); transform.removeEventListener('change', render); transform.dispose();
      canvas.removeEventListener('webglcontextlost', lost); canvas.removeEventListener('webglcontextrestored', restored); canvas.removeEventListener('pointerdown', down, true); canvas.removeEventListener('pointermove', move, true); canvas.removeEventListener('pointerup', up, true); canvas.removeEventListener('pointercancel', cancel); canvas.removeEventListener('lostpointercapture', cancel);
      window.removeEventListener('blur', cancel); window.removeEventListener('pagehide', cancel); window.removeEventListener('keydown', escape, true); document.removeEventListener('visibilitychange', hidden);
      disposeGroup(content); renderer.dispose(); renderer.forceContextLoss(); element.replaceChildren(); runtime.current = null;
    };
  }, []);
  useEffect(() => { if (runtime.current) { fillScene(runtime.current, props); runtime.current.syncDrag(); } }, [props.config, props.recommendation, props.coverage, props.selected, props.showVolumes, props.volumeScope, props.showLabels, props.showOverlap, props.dragEnabled, props.interactionRevision]);
  useEffect(() => {
    const current = runtime.current; if (!current) return; current.cancelDrag();
    const config = latestProps.current.config, span = Math.max(config.analysis.width, config.analysis.length, 3);
    if (props.view.mode === 'mounting' || props.view.mode === 'perspective') {
      // Fit every actual installation volume, including legal large offsets.
      // This runs only on an explicit view reset, leaving parameter edits stable.
      const { chassis } = config, frameTop = chassis.height + chassis.frameHeight;
      const halfLength = Math.max(chassis.length, chassis.frameLength ?? chassis.length) / 2;
      const halfWidth = Math.max(chassis.frameWidth, chassis.width) / 2;
      const bounds = new THREE.Box3(new THREE.Vector3(-halfLength, -halfWidth, 0), new THREE.Vector3(halfLength, halfWidth, frameTop));
      for (const support of supportBoxes(config)) {
        bounds.expandByPoint(new THREE.Vector3(support.min.x, support.min.y, support.min.z));
        bounds.expandByPoint(new THREE.Vector3(support.max.x, support.max.y, support.max.z));
      }
      bounds.expandByScalar(.12);
      const sphere = bounds.getBoundingSphere(new THREE.Sphere()), target = sphere.center, radius = sphere.radius;
      const halfVerticalFov = THREE.MathUtils.degToRad(current.camera.fov / 2);
      const halfHorizontalFov = Math.atan(Math.tan(halfVerticalFov) * current.camera.aspect);
      const distance = radius / Math.sin(Math.min(halfVerticalFov, halfHorizontalFov)) * (props.view.mode === 'mounting' ? 1.10 : 1.44);
      current.controls.target.copy(target);
      current.camera.position.copy(target).addScaledVector(new THREE.Vector3(1.05, -1.4, .88).normalize(), distance);
    } else {
      current.controls.target.set(0, 0, .2);
      current.camera.position.set(0, -.005, span * 1.45);
    }
    current.controls.update(); current.render();
  }, [props.view]);
  return <div className="three-host" data-webgl-status={error ? 'unavailable' : ready ? 'ready' : 'loading'}><div className="three-canvas" ref={host} />{error && <div className="webgl-error" role="alert"><span>三维视图暂不可用</span><p>{error}</p></div>}</div>;
}
