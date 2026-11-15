import * as THREE from 'three';
import { buildCoverageSurface } from './surface';
import type { CoverageResult } from './geometry';

const worldVertex = /* glsl */`
  varying vec3 vWorld;
  void main() {
    vec4 world = modelMatrix * vec4(position, 1.0);
    vWorld = world.xyz;
    gl_Position = projectionMatrix * viewMatrix * world;
  }
`;

const palette = /* glsl */`
  vec3 classColor(float category) {
    if (category < 0.5) return vec3(1.0, 0.045, 0.04);
    if (category < 1.5) return vec3(0.015, 0.65, 0.46);
    return vec3(0.38, 0.16, 1.0);
  }
`;

/** Decoration never changes a cell's class, extent or height. */
export function coverageLayer(result: CoverageResult, z: number, overlap: boolean) {
  const surface = buildCoverageSurface(result, z, overlap);
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(surface.positions, 3));
  geometry.setAttribute('coverageClass', new THREE.BufferAttribute(surface.categories, 1));
  const material = new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, side: THREE.DoubleSide, toneMapped: false,
    vertexShader: /* glsl */`
      attribute float coverageClass;
      varying float vClass;
      varying vec3 vWorld;
      void main() {
        vClass = coverageClass;
        vec4 world = modelMatrix * vec4(position, 1.0);
        vWorld = world.xyz;
        gl_Position = projectionMatrix * viewMatrix * world;
      }
    `,
    fragmentShader: /* glsl */`
      varying float vClass;
      varying vec3 vWorld;
      ${palette}
      float grid(vec2 point, float spacing) {
        vec2 coordinate = point / spacing;
        vec2 distanceToLine = abs(fract(coordinate - 0.5) - 0.5) / max(fwidth(coordinate), vec2(0.0001));
        return 1.0 - min(min(distanceToLine.x, distanceToLine.y), 1.0);
      }
      void main() {
        vec3 color = classColor(vClass);
        float blind = 1.0 - step(0.5, vClass);
        float multi = step(1.5, vClass);
        float fineGrid = grid(vWorld.xy, 0.5);
        float coordinate = (vWorld.x + vWorld.y) / 0.15;
        float stripeDistance = abs(fract(coordinate - 0.5) - 0.5) / max(fwidth(coordinate), 0.0001);
        float stripe = 1.0 - smoothstep(0.25, 1.05, stripeDistance);
        vec3 sight = normalize(cameraPosition - vWorld);
        float grazing = pow(1.0 - abs(sight.z), 2.0);
        float sheen = 0.025 * exp(-pow((vWorld.x - vWorld.y * 0.5) * 0.48, 2.0));
        color = mix(color, vec3(0.43, 0.006, 0.008), blind * stripe * 0.45);
        color += vec3(fineGrid * 0.035 + sheen * 0.5);
        float alpha = mix(0.38, 0.60, blind) + multi * 0.08 + blind * stripe * 0.045 + grazing * 0.07;
        gl_FragColor = vec4(color, alpha);
        #include <colorspace_fragment>
      }
    `,
  });
  const group = new THREE.Group();
  const mesh = new THREE.Mesh(geometry, material); mesh.renderOrder = 2; mesh.userData.coverageSurface = true; group.add(mesh);

  // A narrow ribbon keeps a crisp center plus a restrained halo on the exact
  // classification boundary. No interpolation crosses into a neighboring cell.
  const positions: number[] = [], categories: number[] = [], sides: number[] = [];
  const source = surface.boundaryPositions;
  for (let index = 0; index < source.length; index += 6) {
    const ax = source[index], ay = source[index + 1], bx = source[index + 3], by = source[index + 4];
    const length = Math.hypot(bx - ax, by - ay); if (length < 1e-12) continue;
    const nx = -(by - ay) / length * .018, ny = (bx - ax) / length * .018;
    const corners = [[ax + nx, ay + ny], [ax - nx, ay - ny], [bx - nx, by - ny], [bx + nx, by + ny]];
    for (const vertex of [0, 1, 2, 0, 2, 3]) {
      positions.push(corners[vertex][0], corners[vertex][1], z + .001);
      categories.push(surface.boundaryCategories[index / 3]); sides.push(vertex === 0 || vertex === 3 ? 1 : -1);
    }
  }
  const edgeGeometry = new THREE.BufferGeometry();
  edgeGeometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  edgeGeometry.setAttribute('coverageClass', new THREE.Float32BufferAttribute(categories, 1));
  edgeGeometry.setAttribute('edgeSide', new THREE.Float32BufferAttribute(sides, 1));
  const edges = new THREE.Mesh(edgeGeometry, new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, side: THREE.DoubleSide, toneMapped: false,
    vertexShader: /* glsl */`
      attribute float coverageClass;
      attribute float edgeSide;
      varying float vClass;
      varying float vEdge;
      void main() {
        vClass = coverageClass; vEdge = edgeSide;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: /* glsl */`
      varying float vClass;
      varying float vEdge;
      ${palette}
      void main() {
        float d = abs(vEdge);
        float core = 1.0 - smoothstep(0.08, 0.32, d);
        float halo = 1.0 - smoothstep(0.12, 1.0, d);
        float blind = 1.0 - step(0.5, vClass);
        vec3 edgeColor = mix(classColor(vClass), vec3(0.82, 0.92, 0.97), 0.18);
        edgeColor = mix(edgeColor, vec3(1.0, 0.08, 0.055), blind);
        gl_FragColor = vec4(edgeColor, core * 0.76 + halo * 0.16);
        #include <colorspace_fragment>
      }
    `,
  }));
  edges.renderOrder = 3; group.add(edges);
  geometry.computeBoundingBox();
  const sourceBounds = new THREE.Box3().setFromBufferAttribute(new THREE.BufferAttribute(surface.boundaryPositions, 3));
  group.userData.surfaceStats = { classCounts: surface.classCounts, vertexCount: geometry.getAttribute('position').count,
    boundarySegments: surface.boundaryPositions.length / 6, height: z, cellCount: surface.positions.length / 18,
    heightRange: { min: geometry.boundingBox?.isEmpty() ? null : geometry.boundingBox!.min.z, max: geometry.boundingBox?.isEmpty() ? null : geometry.boundingBox!.max.z },
    boundaryHeightRange: { min: sourceBounds.isEmpty() ? null : sourceBounds.min.z, max: sourceBounds.isEmpty() ? null : sourceBounds.max.z } };
  return group;
}

/** A static, world-space engineering grid; no moving scan or invented data. */
export function stageMaterial() {
  return new THREE.ShaderMaterial({
    toneMapped: false, vertexShader: worldVertex,
    fragmentShader: /* glsl */`
      varying vec3 vWorld;
      float grid(float spacing) {
        vec2 p = vWorld.xy / spacing;
        vec2 line = abs(fract(p - 0.5) - 0.5) / max(fwidth(p), vec2(0.0001));
        return 1.0 - min(min(line.x, line.y), 1.0);
      }
      void main() {
        float exposure = exp(-dot(vWorld.xy, vWorld.xy) * 0.055);
        vec3 color = mix(vec3(0.021, 0.034, 0.05), vec3(0.033, 0.055, 0.076), exposure);
        color += vec3(0.035, 0.057, 0.071) * (grid(1.0) * 0.38 + grid(0.5) * 0.14);
        gl_FragColor = vec4(color, 1.0);
        #include <colorspace_fragment>
      }
    `,
  });
}

export function contactShadow() {
  return new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, toneMapped: false,
    vertexShader: /* glsl */`varying vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
    fragmentShader: /* glsl */`
      varying vec2 vUv;
      void main() {
        vec2 p = (vUv - 0.5) * 2.0;
        float shadow = exp(-dot(p * vec2(1.05, 1.2), p * vec2(1.05, 1.2)) * 3.4);
        gl_FragColor = vec4(vec3(0.004, 0.009, 0.016), shadow * 0.65);
        #include <colorspace_fragment>
      }
    `,
  });
}

export function volumeGlass(color: THREE.ColorRepresentation, origin: THREE.Vector3, range: number, emphasized: boolean) {
  return new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, side: THREE.DoubleSide, toneMapped: false,
    uniforms: { uTint: { value: new THREE.Color(color).lerp(new THREE.Color('#a4bdce'), .35) },
      uOrigin: { value: origin }, uRange: { value: Math.max(range, .001) }, uAlpha: { value: emphasized ? .105 : .048 } },
    vertexShader: /* glsl */`
      varying vec3 vWorld;
      varying vec3 vNormal;
      void main() {
        vec4 world = modelMatrix * vec4(position, 1.0);
        vWorld = world.xyz; vNormal = normalize(mat3(modelMatrix) * normal);
        gl_Position = projectionMatrix * viewMatrix * world;
      }
    `,
    fragmentShader: /* glsl */`
      uniform vec3 uTint;
      uniform vec3 uOrigin;
      uniform float uRange;
      uniform float uAlpha;
      varying vec3 vWorld;
      varying vec3 vNormal;
      void main() {
        vec3 sight = normalize(cameraPosition - vWorld);
        float rim = pow(1.0 - abs(dot(normalize(vNormal), sight)), 2.6);
        float distanceFraction = clamp(length(vWorld - uOrigin) / uRange, 0.0, 1.0);
        float fade = smoothstep(0.025, 0.2, distanceFraction) * mix(1.0, 0.48, distanceFraction);
        float alpha = uAlpha * (0.14 + rim * 0.86) * fade;
        gl_FragColor = vec4(mix(uTint, vec3(0.76, 0.88, 0.94), rim * 0.24), alpha);
        #include <colorspace_fragment>
      }
    `,
  });
}
