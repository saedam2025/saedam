import * as THREE from './vendor/three/three.module.js';

// Deterministic, locally generated materials: no remote assets or network requests.
export function materialLibrary(renderer) {
  const cache = new Map();
  function texture(kind) {
    if (cache.has(kind)) return cache.get(kind);
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 512;
    const ctx = canvas.getContext('2d');
    let seed = 72491;
    const random = () => ((seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0) / 4294967296);
    const grids = [8, 16, 32].map(size => ({ size, values: Array.from({ length: size * size }, random) }));
    const noise = (x, y) => grids.reduce((sum, grid, octave) => {
      const gx = x / 512 * grid.size, gy = y / 512 * grid.size;
      const ix = Math.floor(gx), iy = Math.floor(gy);
      const fx = gx - ix, fy = gy - iy;
      const sx = fx * fx * (3 - 2 * fx), sy = fy * fy * (3 - 2 * fy);
      const at = (dx, dy) => grid.values[((iy + dy) % grid.size) * grid.size + ((ix + dx) % grid.size)];
      const top = at(0, 0) * (1 - sx) + at(1, 0) * sx;
      const bottom = at(0, 1) * (1 - sx) + at(1, 1) * sx;
      return sum + ((top * (1 - sy) + bottom * sy) - 0.5) * 24 / (octave + 1);
    }, 0);
    const wood = kind === 'oak' || kind === 'walnut';
    const pixels = ctx.createImageData(512, 512);
    for (let y = 0; y < 512; y++) {
      for (let x = 0; x < 512; x++) {
        const wave = wood || kind === 'plaster' ? 0 : noise(x, y);
        const grain = wood ? Math.sin(y * 2.6 + Math.sin(x * 0.018) * 2) * 7 : (kind === 'plaster' ? 0 : wave);
        const n = 229 + grain + (random() - 0.5) * (kind === 'plaster' ? 12 : 19);
        const i = (y * 512 + x) * 4;
        pixels.data[i] = n;
        pixels.data[i + 1] = n - (wood ? 7 : 0);
        pixels.data[i + 2] = n - (wood ? 18 : 0);
        pixels.data[i + 3] = 255;
      }
    }
    ctx.putImageData(pixels, 0, 0);
    if (wood) {
      for (let row = 0; row < 8; row++) {
        ctx.fillStyle = `rgba(75,51,29,${0.02 + random() * 0.1})`;
        ctx.fillRect(0, row * 64, 512, 64);
        ctx.fillStyle = 'rgba(55,42,30,.35)';
        ctx.fillRect(0, row * 64, 512, 1);
        ctx.fillRect((row % 3) * 170, row * 64, 1, 64);
      }
    } else if (kind === 'limestone') {
      ctx.fillStyle = 'rgba(90,85,75,.28)';
      ctx.fillRect(0, 0, 512, 2);
      ctx.fillRect(0, 0, 2, 512);
    }
    const map = new THREE.CanvasTexture(canvas);
    map.wrapS = map.wrapT = THREE.RepeatWrapping;
    map.colorSpace = THREE.SRGBColorSpace;
    map.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy());
    // Bump data must remain linear, unlike the surface color.
    const bump = map.clone();
    bump.colorSpace = THREE.NoColorSpace;
    cache.set(kind, { map, bump });
    return { map, bump };
  }
  return (kind, color, width = 1, height = 1) => {
    const material = new THREE.MeshStandardMaterial({ color, roughness: kind === 'oak' || kind === 'walnut' ? 0.48 : 0.84 });
    if (kind !== 'plain') {
      const source = texture(kind);
      material.map = source.map.clone();
      material.bumpMap = source.bump.clone();
      const tile = kind === 'oak' || kind === 'walnut' ? 3.2 : 2.4;
      material.map.repeat.set(width / tile, height / tile);
      material.bumpMap.repeat.copy(material.map.repeat);
      material.bumpScale = kind === 'plaster' ? 0.003 : 0.012;
    }
    return material;
  };
}

const UP = new THREE.Vector3(0, 1, 0);

// Every room builder places plain boxes; sharing one helper keeps them comparable.
function boxFactory(scene) {
  return (width, height, depth, material, x, y, z, parent = scene) => {
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(width, height, depth), material);
    mesh.position.set(x, y, z);
    mesh.castShadow = mesh.receiveShadow = true;
    parent.add(mesh);
    return mesh;
  };
}

// Desks, trees and flowers repeat a lot: one draw call each keeps stereo VR affordable.
function instancedFactory(scene) {
  const matrix = new THREE.Matrix4();
  const position = new THREE.Vector3();
  const quaternion = new THREE.Quaternion();
  const scale = new THREE.Vector3();
  return (geometry, material, items) => {
    if (!items.length) return null;
    const mesh = new THREE.InstancedMesh(geometry, material, items.length);
    items.forEach((item, index) => {
      position.set(item.x, item.y || 0, item.z);
      quaternion.setFromAxisAngle(UP, item.rotY || 0);
      const size = item.scale || 1;
      scale.set(size, item.scaleY || size, size);
      matrix.compose(position, quaternion, scale);
      mesh.setMatrixAt(index, matrix);
    });
    mesh.instanceMatrix.needsUpdate = true;
    mesh.castShadow = mesh.receiveShadow = true;
    scene.add(mesh);
    return mesh;
  };
}

function resolveKind(value, fallback) {
  return !value || value === 'auto' ? fallback : value;
}

function frameMaterialFor(kind, make) {
  return kind === 'black' || kind === 'brass'
    ? new THREE.MeshStandardMaterial({ color: kind === 'brass' ? 0xb99a59 : 0x292a28, roughness: 0.32, metalness: 0.75 })
    : make(kind, kind === 'walnut' ? 0x67472f : 0xb69263, 2, 0.2);
}

// Canvas-drawn label (classroom notices); no fonts or images are fetched.
function labelTexture(title, subtitle, background = '#fffdf5', color = '#1f2937') {
  const canvas = document.createElement('canvas');
  canvas.width = 512;
  canvas.height = 160;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = background;
  ctx.fillRect(0, 0, 512, 160);
  ctx.fillStyle = color;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.font = 'bold 62px "Malgun Gothic", "Noto Sans KR", sans-serif';
  ctx.fillText(title, 256, subtitle ? 58 : 80);
  if (subtitle) {
    ctx.font = '40px "Malgun Gothic", "Noto Sans KR", sans-serif';
    ctx.fillStyle = '#6b7280';
    ctx.fillText(subtitle, 256, 118);
  }
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

export function buildEnvironment(scene, renderer, hall, layout, lowPower) {
  if (layout.style === 'classroom') return buildClassroom(scene, renderer, hall, layout, lowPower);
  if (layout.style === 'park') return buildPark(scene, renderer, hall, layout, lowPower);
  return buildIndoor(scene, renderer, hall, layout, lowPower);
}

// Lounge / gallery: a roofed hall built from the wall and partition list.
function buildIndoor(scene, renderer, hall, layout, lowPower) {
  const make = materialLibrary(renderer);
  const w = layout.size.width, d = layout.size.depth, h = layout.size.height;
  const lounge = layout.style === 'lounge';
  const wallKind = resolveKind(hall.wallTexture, 'plaster');
  const floorKind = resolveKind(hall.floorTexture, lounge ? 'oak' : 'concrete');
  const frameKind = resolveKind(hall.frameStyle, lounge ? 'oak' : 'black');
  const blockers = [];
  const metal = new THREE.MeshStandardMaterial({ color: 0x282a28, roughness: 0.36, metalness: 0.75 });
  const trim = make('plain', 0xd6cfc2);
  const timber = make('oak', 0xb38b5c, 3, 1);
  const box = boxFactory(scene);
  const power = hall.lightIntensity || 1;
  scene.background = new THREE.Color(0xe3e8eb);
  scene.fog = new THREE.Fog(0xe3e8eb, 55, 110);
  scene.add(new THREE.HemisphereLight(0xeaf2ff, 0xc6c3b9, 1.25 * power));
  scene.add(new THREE.AmbientLight(0xffffff, 0.3 * power));
  const sun = new THREE.DirectionalLight(0xfff2da, 2.7 * power);
  sun.position.set(-w * 0.25, h + 8, -d * 0.22);
  sun.castShadow = true;
  sun.shadow.mapSize.set(lowPower ? 1024 : 2048, lowPower ? 1024 : 2048);
  Object.assign(sun.shadow.camera, { left: -w, right: w, top: d, bottom: -d, near: 0.5, far: 65 });
  sun.shadow.bias = -0.0003;
  sun.shadow.normalBias = 0.035;
  scene.add(sun);
  const fill = new THREE.DirectionalLight(0xe4eeff, 0.65 * power);
  fill.position.set(w, h, d);
  scene.add(fill);
  const floorColor = new THREE.Color(hall.floorColor);
  if (floorKind === 'walnut') floorColor.multiply(new THREE.Color(0x99765a));
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(w, d), make(floorKind, floorColor, w, d));
  floor.rotation.x = -Math.PI / 2;
  floor.receiveShadow = true;
  scene.add(floor);
  layout.walls.forEach(wall => {
    const group = new THREE.Group();
    group.position.set(wall.x, 0, wall.z);
    group.rotation.y = wall.rotationY;
    scene.add(group);
    // Outer wall thickness extends outwards; artwork coordinates remain unchanged.
    box(wall.width, wall.height, 0.22, make(wallKind, hall.wallColor, wall.width, wall.height), 0, wall.height / 2, -0.11, group);
    box(wall.width, 0.13, 0.035, trim, 0, 0.065, 0.018, group);
  });
  (layout.partitions || []).forEach(p => {
    const sx = p.axis === 'x' ? p.length : p.depth;
    const sz = p.axis === 'x' ? p.depth : p.length;
    box(sx, p.height, sz, make(wallKind, hall.wallColor, p.length, p.height), p.x, p.height / 2, p.z);
    blockers.push({ minX: p.x - sx / 2 - 0.45, maxX: p.x + sx / 2 + 0.45, minZ: p.z - sz / 2 - 0.45, maxZ: p.z + sz / 2 + 0.45 });
  });
  // A real opening, recessed reveals and crossbars let the sun cast architectural shadows.
  const sw = layout.skylight?.width || w * 0.46;
  const sd = layout.skylight?.depth || d * 0.65;
  const ceilingMat = make(lounge ? 'plaster' : 'concrete', lounge ? 0xeee6d8 : 0xd1d1cc, 8, 6);
  [-1, 1].forEach(sign => {
    box((w - sw) / 2, 0.28, d, ceilingMat, sign * (w + sw) / 4, h, 0);
    box(sw, 0.28, (d - sd) / 2, ceilingMat, 0, h, sign * (d + sd) / 4);
    box(0.18, 0.5, sd, trim, sign * sw / 2, h + 0.12, 0);
  });
  const sky = new THREE.Mesh(new THREE.PlaneGeometry(sw, sd), new THREE.MeshBasicMaterial({ color: 0xe5f2ff, side: THREE.DoubleSide, toneMapped: false }));
  sky.rotation.x = Math.PI / 2;
  sky.position.y = h + 0.45;
  scene.add(sky);
  for (let z = -sd / 2; z <= sd / 2; z += 2.7) box(sw, 0.22, 0.10, metal, 0, h + 0.2, z);
  for (let z = -d / 2 + 2; z < d / 2; z += 4) box(w, lounge ? 0.28 : 0.48, 0.24, ceilingMat, 0, h - 0.28, z);
  if (layout.balcony) {
    const b = layout.balcony;
    const glass = new THREE.MeshPhysicalMaterial({ color: 0xbfcfcd, transparent: true, opacity: 0.22, roughness: 0.15, depthWrite: false });
    [-1, 1].forEach(sign => {
      box(b.depth, 0.22, d, ceilingMat, sign * (w / 2 - b.depth / 2), b.height, 0);
      const x = sign * (w / 2 - b.depth);
      const rail = box(0.035, 0.86, d, glass, x, b.height + 0.57, 0);
      rail.castShadow = false;
      box(0.075, 0.055, d, metal, x, b.height + 1.02, 0);
      for (let z = -d / 2; z <= d / 2; z += 3) box(0.05, 0.94, 0.05, metal, x, b.height + 0.52, z);
    });
  }
  // Shared track lighting (bounded light count keeps mobile / stereo rendering affordable).
  [-1, 1].forEach(sign => box(0.06, 0.07, d - 2, metal, sign * (w / 2 - 2), h - 0.65, 0));
  const wallSlots = (layout.slots || []).filter(s => s.code.startsWith('A') || s.code.startsWith('B') || s.code.startsWith('C'));
  wallSlots.forEach((s, i) => {
    const direction = new THREE.Vector3(Math.sin(s.rotationY), 0, Math.cos(s.rotationY));
    const pos = new THREE.Vector3(s.x, Math.min(h - 0.85, layout.balcony ? 3.35 : h - 0.85), s.z).addScaledVector(direction, 1.3);
    const fixture = new THREE.Mesh(new THREE.CylinderGeometry(0.09, 0.11, 0.25, 12), metal);
    fixture.position.copy(pos);
    const target = new THREE.Vector3(s.x, s.y, s.z);
    fixture.quaternion.setFromUnitVectors(new THREE.Vector3(0, -1, 0), target.clone().sub(pos).normalize());
    scene.add(fixture);
    if (i % 2 === 0 && i < (lowPower ? 6 : 10)) {
      const light = new THREE.SpotLight(0xffedce, 48 * power, 11, Math.PI / 5, 0.75, 2);
      light.position.copy(pos);
      light.target.position.copy(target);
      scene.add(light, light.target);
    }
  });
  // Benches and greenery stay along the entry edge, away from artwork and spawn.
  [-1, 1].forEach(sign => {
    const x = sign * w * 0.30, z = d / 2 - 2.0;
    box(2.6, 0.16, 0.7, timber, x, 0.48, z);
    [-0.95, 0.95].forEach(dx => box(0.1, 0.4, 0.56, metal, x + dx, 0.2, z));
    blockers.push({ minX: x - 1.65, maxX: x + 1.65, minZ: z - 0.7, maxZ: z + 0.7 });
    const px = sign * (w / 2 - 1.1), pz = d / 2 - 1.1;
    const pot = new THREE.Mesh(new THREE.CylinderGeometry(0.38, 0.27, 0.65, 20), make('concrete', 0xc9bca5));
    pot.position.set(px, 0.325, pz);
    pot.castShadow = pot.receiveShadow = true;
    scene.add(pot);
    const foliage = new THREE.MeshStandardMaterial({ color: 0x476441, roughness: 0.82 });
    for (let j = 0; j < 14; j++) {
      const angle = j * 2.4;
      const leaf = new THREE.Mesh(new THREE.SphereGeometry(1, 8, 6), foliage);
      leaf.scale.set(0.13, 0.44, 0.065);
      leaf.position.set(px + Math.sin(angle) * 0.25, 0.9 + (j % 4) * 0.19, pz + Math.cos(angle) * 0.25);
      leaf.rotation.set(Math.sin(angle) * 0.65, angle, Math.cos(angle) * 0.7);
      leaf.castShadow = true;
      scene.add(leaf);
    }
    blockers.push({ minX: px - 0.7, maxX: px + 0.7, minZ: pz - 0.7, maxZ: pz + 0.7 });
  });
  return { blockers, frameMaterial: frameMaterialFor(frameKind, make) };
}

// Seen only through the classroom windows: sky, schoolyard, trees and the block opposite.
function buildSchoolyard(scene, make, box, instanced, halfW, lowPower) {
  const sky = new THREE.Mesh(
    new THREE.SphereGeometry(150, 32, 20),
    new THREE.MeshBasicMaterial({ map: skyTexture(), side: THREE.BackSide, fog: false, toneMapped: false }),
  );
  scene.add(sky);

  // The room sits a little above the yard, the way a ground-floor classroom does.
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(220, 220), make('concrete', 0x86a866, 220, 220));
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -0.55;
  scene.add(ground);
  const yard = new THREE.Mesh(new THREE.CircleGeometry(15, 40), make('limestone', 0xdcc9a6, 12, 12));
  yard.rotation.x = -Math.PI / 2;
  yard.position.set(-halfW - 19, -0.54, 0);
  scene.add(yard);

  const hedge = new THREE.MeshStandardMaterial({ color: 0x4e7a42, roughness: 0.96 });
  box(0.6, 1.0, 46, hedge, -halfW - 38, -0.05, 0);

  // The building across the yard: four storeys of window bands under a flat roof.
  const wall = make('plaster', 0xe4e0d4, 6, 3);
  box(1.4, 13.0, 46, wall, -halfW - 40, 5.95, -2);
  box(2.0, 0.5, 47, make('plain', 0xb9b4a8), -halfW - 40, 12.6, -2);
  const windowBand = new THREE.MeshStandardMaterial({ color: 0x8fb6cf, roughness: 0.25, metalness: 0.3 });
  for (let level = 0; level < 4; level += 1) {
    box(0.25, 1.6, 42, windowBand, -halfW - 39.25, 1.7 + level * 3.0, -2);
  }

  const trees = [];
  const count = lowPower ? 7 : 12;
  for (let i = 0; i < count; i += 1) {
    trees.push({
      x: -halfW - 12 - (i % 4) * 7,
      z: (i % 2 ? 1 : -1) * (7 + (i % 5) * 4.5),
      scale: 0.95 + (i % 3) * 0.2,
      rotY: i * 1.7,
    });
  }
  instanced(new THREE.CylinderGeometry(0.22, 0.34, 3.2, 8), make('walnut', 0x6b4f35, 1, 2),
    trees.map(tree => ({ ...tree, y: -0.55 + 1.6 * tree.scale })));
  instanced(new THREE.IcosahedronGeometry(1.8, 0), new THREE.MeshStandardMaterial({ color: 0x4f7f43, roughness: 0.92 }),
    trees.map(tree => ({ ...tree, y: -0.55 + 3.9 * tree.scale })));

  // Flag pole beside the yard.
  const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.07, 0.09, 8, 10),
    new THREE.MeshStandardMaterial({ color: 0xd8dade, roughness: 0.4, metalness: 0.4 }));
  pole.position.set(-halfW - 9, 3.45, -9);
  scene.add(pole);
}

// Classroom: window wall on the left, blackboard at the front, desks in rows.
function buildClassroom(scene, renderer, hall, layout, lowPower) {
  const make = materialLibrary(renderer);
  const box = boxFactory(scene);
  const instanced = instancedFactory(scene);
  const w = layout.size.width, d = layout.size.depth, h = layout.size.height;
  const halfW = w / 2, halfD = d / 2;
  const power = hall.lightIntensity || 1;
  const blockers = [];
  const wallKind = resolveKind(hall.wallTexture, 'plaster');
  const floorKind = resolveKind(hall.floorTexture, 'oak');
  const frameKind = resolveKind(hall.frameStyle, 'oak');

  const wallMat = make(wallKind, hall.wallColor, w, h);
  const floorMat = make(floorKind, hall.floorColor, w, d);
  const ceilingMat = make('plaster', 0xf8f6f0, 8, 6);
  const trim = make('plain', 0xe9e3d6);
  const timber = make('oak', 0xc8a173, 2, 1);
  const deskTop = make('oak', 0xe2c69e, 1, 1);
  const metal = new THREE.MeshStandardMaterial({ color: 0x9aa0a8, roughness: 0.42, metalness: 0.55 });
  const cork = make('plain', 0xd8b98c);

  scene.background = new THREE.Color(0xdfe9f5);
  // 창밖 풍경만 멀리서 흐려지게 한다(교실 안은 12m도 안 되므로 영향이 없다).
  scene.fog = new THREE.Fog(0xdbe9f7, 60, 200);

  // Daylight comes through the window wall (-X), so the sun sits far out on that side.
  scene.add(new THREE.HemisphereLight(0xf4f8ff, 0xc9c2b2, 1.0 * power));
  scene.add(new THREE.AmbientLight(0xffffff, 0.34 * power));
  const sun = new THREE.DirectionalLight(0xfff1d2, 3.0 * power);
  sun.position.set(-w * 1.25, h + 6, -d * 0.15);
  sun.target.position.set(halfW * 0.4, 0.9, d * 0.1);
  sun.castShadow = true;
  sun.shadow.mapSize.set(lowPower ? 1024 : 2048, lowPower ? 1024 : 2048);
  Object.assign(sun.shadow.camera, { left: -w, right: w, top: d, bottom: -d, near: 0.5, far: 48 });
  sun.shadow.bias = -0.0004;
  sun.shadow.normalBias = 0.03;
  scene.add(sun, sun.target);
  const fill = new THREE.DirectionalLight(0xe8f0ff, 0.45 * power);
  fill.position.set(halfW, h, halfD);
  scene.add(fill);

  const floor = new THREE.Mesh(new THREE.PlaneGeometry(w, d), floorMat);
  floor.rotation.x = -Math.PI / 2;
  floor.receiveShadow = true;
  scene.add(floor);
  box(w + 0.4, 0.22, d + 0.4, ceilingMat, 0, h + 0.11, 0);

  buildSchoolyard(scene, make, box, instancedFactory(scene), halfW, lowPower);
  // The yard sits lower than the floor, so close the gap under the room.
  box(w + 0.6, 0.62, d + 0.6, wallMat, 0, -0.31, 0);

  // Three solid walls; the fourth is opened up for windows.
  box(w, h, 0.2, wallMat, 0, h / 2, -halfD - 0.1);
  box(w, h, 0.2, wallMat, 0, h / 2, halfD + 0.1);
  box(0.2, h, d, wallMat, halfW + 0.1, h / 2, 0);
  const bandHalf = 2.7, sill = 0.95, head = 2.6;
  box(0.2, sill, bandHalf * 2, wallMat, -halfW - 0.1, sill / 2, 0);
  box(0.2, h - head, bandHalf * 2, wallMat, -halfW - 0.1, (head + h) / 2, 0);
  [-1, 1].forEach(sign => {
    const pier = halfD - bandHalf;
    box(0.2, h, pier, wallMat, -halfW - 0.1, h / 2, sign * (bandHalf + pier / 2));
  });

  // Window: glass, mullions, sill board and a curtain at each end.
  // 창밖이 그대로 보여야 하므로 유리는 반사만 살짝 남기고 거의 투명하게 둔다.
  const glass = new THREE.MeshPhysicalMaterial({
    color: 0xeaf6ff, transparent: true, opacity: 0.07, roughness: 0.04, depthWrite: false,
  });
  const pane = box(0.05, head - sill, bandHalf * 2, glass, -halfW + 0.02, (sill + head) / 2, 0);
  pane.castShadow = false;
  box(0.12, 0.1, bandHalf * 2 + 0.2, trim, -halfW + 0.07, sill, 0);
  box(0.12, 0.1, bandHalf * 2 + 0.2, trim, -halfW + 0.07, head, 0);
  for (let z = -bandHalf; z <= bandHalf + 0.01; z += 1.35) {
    box(0.1, head - sill, 0.08, trim, -halfW + 0.07, (sill + head) / 2, z);
  }
  box(0.34, 0.07, bandHalf * 2, timber, -halfW + 0.18, sill + 0.035, 0);
  const curtain = new THREE.MeshStandardMaterial({ color: 0xeae4f2, roughness: 0.92 });
  [-1, 1].forEach(sign => {
    box(0.12, head - sill + 0.25, 0.6, curtain, -halfW + 0.28, (sill + head) / 2 + 0.12, sign * (bandHalf - 0.32));
  });

  // Sunlight reads best as blown-out glass plus warm pools spilling across the floor.
  const glow = new THREE.Mesh(
    new THREE.PlaneGeometry(bandHalf * 2 - 0.1, head - sill - 0.1),
    new THREE.MeshBasicMaterial({
      color: 0xfff6e0, transparent: true, opacity: 0.12, blending: THREE.AdditiveBlending,
      depthWrite: false, side: THREE.DoubleSide, toneMapped: false,
    }),
  );
  glow.rotation.y = Math.PI / 2;
  glow.position.set(-halfW + 0.12, (sill + head) / 2, 0);
  scene.add(glow);
  const poolMat = new THREE.MeshBasicMaterial({
    color: 0xffeec4, transparent: true, opacity: 0.2, blending: THREE.AdditiveBlending,
    depthWrite: false, toneMapped: false,
  });
  for (let i = -1; i <= 1; i += 1) {
    const pool = new THREE.Mesh(new THREE.PlaneGeometry(3.4, 1.15), poolMat);
    pool.rotation.x = -Math.PI / 2;
    pool.rotation.z = 0.12;
    pool.position.set(-halfW + 2.2, 0.02, i * 1.35);
    scene.add(pool);
  }

  // Blackboard with its chalk ledge, plus the notices above it.
  const boardW = 8.0, boardH = 2.1, boardY = 1.72;
  box(boardW + 0.26, boardH + 0.26, 0.06, make('plain', 0xb6bfc7), 0, boardY, -halfD + 0.03);
  box(boardW, boardH, 0.1, new THREE.MeshStandardMaterial({ color: 0x1f3d30, roughness: 0.94 }),
    0, boardY, -halfD + 0.09);
  box(boardW + 0.3, 0.09, 0.2, timber, 0, boardY - boardH / 2 - 0.12, -halfD + 0.16);
  // The notices sit beside the board, leaving its whole face for the exhibit.
  box(1.86, 0.69, 0.05, timber, -5.0, 2.35, -halfD + 0.02);
  const motto = new THREE.Mesh(
    new THREE.PlaneGeometry(1.7, 0.53),
    new THREE.MeshBasicMaterial({ map: labelTexture('급훈', '오늘도 즐겁게') }),
  );
  motto.position.set(-5.0, 2.35, -halfD + 0.06);
  scene.add(motto);
  const clock = new THREE.Mesh(new THREE.CylinderGeometry(0.26, 0.26, 0.06, 24), make('plain', 0xfaf9f6));
  clock.rotation.x = Math.PI / 2;
  clock.position.set(5.0, 2.45, -halfD + 0.06);
  scene.add(clock);
  box(0.025, 0.17, 0.02, metal, 5.0, 2.5, -halfD + 0.11);
  box(0.12, 0.025, 0.02, metal, 5.06, 2.45, -halfD + 0.11);

  // A pinboard behind every wall slot (blackboard slots hang straight on the board).
  (layout.slots || []).forEach(slot => {
    if (slot.code.startsWith('A')) return;
    const dir = new THREE.Vector3(Math.sin(slot.rotationY), 0, Math.cos(slot.rotationY));
    const group = new THREE.Group();
    group.position.set(slot.x, 0, slot.z).addScaledVector(dir, -0.035);
    group.rotation.y = slot.rotationY;
    scene.add(group);
    box(slot.width + 0.5, slot.height + 0.5, 0.05, cork, 0, slot.y, 0, group);
    box(slot.width + 0.62, slot.height + 0.62, 0.03, timber, 0, slot.y, -0.02, group);
  });

  // Lockers fill the back corners; the teacher's desk stands beside the blackboard.
  [-1, 1].forEach(sign => {
    const x = sign * 4.5;
    box(2.5, 1.75, 0.45, make('plain', 0xd2dae0), x, 0.875, halfD - 0.24);
    for (let i = -1; i <= 1; i += 1) box(0.03, 1.66, 0.02, metal, x + i * 0.62, 0.875, halfD - 0.47);
    blockers.push({ minX: x - 1.7, maxX: x + 1.7, minZ: halfD - 1.15, maxZ: halfD });
  });
  const podiumX = -2.8, podiumZ = -halfD + 1.5;
  box(1.25, 0.08, 0.62, timber, podiumX, 1.03, podiumZ);
  box(1.05, 0.96, 0.5, make('plain', 0xdacfba), podiumX, 0.5, podiumZ);
  blockers.push({ minX: podiumX - 1.1, maxX: podiumX + 1.1, minZ: podiumZ - 0.8, maxZ: podiumZ + 0.8 });

  // Door and timetable on the corridor side.
  const doorZ = halfD - 1.4;
  box(0.05, 2.3, 1.2, trim, halfW - 0.02, 1.15, doorZ);
  box(0.07, 2.1, 1.0, make('plain', 0xdbd2c2), halfW - 0.07, 1.05, doorZ);
  const knob = new THREE.Mesh(new THREE.SphereGeometry(0.05, 10, 8), metal);
  knob.position.set(halfW - 0.12, 1.05, doorZ - 0.38);
  scene.add(knob);
  const timetable = new THREE.Mesh(
    new THREE.PlaneGeometry(0.86, 0.62),
    new THREE.MeshBasicMaterial({ map: labelTexture('시간표', '우리 반 하루') }),
  );
  timetable.rotation.y = -Math.PI / 2;
  timetable.position.set(halfW - 0.06, 1.95, doorZ + 1.1);
  scene.add(timetable);

  // Ceiling lights.
  [-2.6, 2.6].forEach(x => {
    box(0.58, 0.12, 5.0, make('plain', 0xf2f3f5), x, h - 0.12, 0);
    const tube = new THREE.Mesh(
      new THREE.BoxGeometry(0.4, 0.05, 4.8),
      new THREE.MeshBasicMaterial({ color: 0xfdfcf2, toneMapped: false }),
    );
    tube.position.set(x, h - 0.2, 0);
    scene.add(tube);
  });

  // Desks and chairs in rows (one instanced draw call per part).
  const seats = [];
  [-3.6, -1.2, 1.2, 3.6].forEach(x => [-1.3, 0.7, 2.7].forEach(z => seats.push({ x, z })));
  const seatPart = (sx, sy, sz, material, ox, oy, oz) => instanced(
    new THREE.BoxGeometry(sx, sy, sz), material,
    seats.map(seat => ({ x: seat.x + ox, y: oy, z: seat.z + oz })),
  );
  // Thin tubular legs instead of solid panels, so the rows stay light.
  const legMat = new THREE.MeshStandardMaterial({ color: 0xb9bfc7, roughness: 0.36, metalness: 0.55 });
  const legs = (offsets, sx, sy, sz, height) => instanced(
    new THREE.BoxGeometry(sx, sy, sz), legMat,
    seats.flatMap(seat => offsets.map(([dx, dz]) => ({ x: seat.x + dx, y: height, z: seat.z + dz }))),
  );
  seatPart(1.15, 0.04, 0.56, deskTop, 0, 0.735, 0);
  seatPart(1.0, 0.018, 0.42, deskTop, 0, 0.55, 0.02);
  legs([[-0.52, -0.22], [0.52, -0.22], [-0.52, 0.22], [0.52, 0.22]], 0.035, 0.715, 0.035, 0.358);
  seatPart(1.0, 0.03, 0.03, legMat, 0, 0.2, -0.22);
  seatPart(1.0, 0.03, 0.03, legMat, 0, 0.2, 0.22);
  seatPart(0.42, 0.04, 0.4, deskTop, 0, 0.44, 0.7);
  seatPart(0.42, 0.32, 0.04, deskTop, 0, 0.67, 0.87);
  legs([[-0.17, 0.54], [0.17, 0.54]], 0.03, 0.42, 0.03, 0.21);
  legs([[-0.17, 0.87], [0.17, 0.87]], 0.03, 0.85, 0.03, 0.425);
  seats.forEach(seat => blockers.push({
    minX: seat.x - 0.9, maxX: seat.x + 0.9, minZ: seat.z - 0.75, maxZ: seat.z + 1.25,
  }));

  return { blockers, frameMaterial: frameMaterialFor(frameKind, make) };
}


// Painted once into a canvas: gradient sky, a few clouds and the sun's glow.
function skyTexture() {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 512;
  const ctx = canvas.getContext('2d');
  const gradient = ctx.createLinearGradient(0, 0, 0, 512);
  gradient.addColorStop(0, '#2f6cc0');
  gradient.addColorStop(0.42, '#7cb6e8');
  gradient.addColorStop(0.72, '#c5def3');
  gradient.addColorStop(1, '#e9f1f7');
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 512, 512);
  // The sun sits where the directional light comes from, so highlights line up.
  const sun = ctx.createRadialGradient(441, 151, 4, 441, 151, 96);
  sun.addColorStop(0, 'rgba(255,255,245,1)');
  sun.addColorStop(0.18, 'rgba(255,248,214,.85)');
  sun.addColorStop(1, 'rgba(255,245,210,0)');
  ctx.fillStyle = sun;
  ctx.fillRect(345, 55, 192, 192);
  let seed = 20260922;
  const random = () => ((seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0) / 4294967296);
  ctx.fillStyle = 'rgba(255,255,255,.72)';
  for (let cloud = 0; cloud < 16; cloud += 1) {
    const cx = random() * 512;
    const cy = 120 + random() * 240;
    const scale = 0.6 + random() * 0.9;
    for (let puff = 0; puff < 5; puff += 1) {
      ctx.beginPath();
      ctx.ellipse(cx + (puff - 2) * 17 * scale, cy + (puff % 2) * 6 * scale,
        26 * scale, 13 * scale, 0, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

// Park: open sky, lawn and paths, with canvas stands instead of walls.
function buildPark(scene, renderer, hall, layout, lowPower) {
  const make = materialLibrary(renderer);
  const box = boxFactory(scene);
  const instanced = instancedFactory(scene);
  const w = layout.size.width, d = layout.size.depth;
  const halfW = w / 2, halfD = d / 2;
  const power = hall.lightIntensity || 1;
  const blockers = [];
  const floorKind = resolveKind(hall.floorTexture, 'concrete');
  const frameKind = resolveKind(hall.frameStyle, 'oak');

  const stone = make('limestone', 0xd7d2c6, 3, 1);
  const timber = make('oak', 0xbe9464, 2, 1);
  const metal = new THREE.MeshStandardMaterial({ color: 0x3c4148, roughness: 0.45, metalness: 0.6 });
  const leaf = new THREE.MeshStandardMaterial({ color: 0x4f7f43, roughness: 0.92 });
  const leafLight = new THREE.MeshStandardMaterial({ color: 0x669a52, roughness: 0.92 });

  const sky = new THREE.Mesh(
    new THREE.SphereGeometry(150, 32, 20),
    new THREE.MeshBasicMaterial({ map: skyTexture(), side: THREE.BackSide, fog: false, toneMapped: false }),
  );
  scene.add(sky);
  scene.background = new THREE.Color(0xbfdcf5);
  scene.fog = new THREE.Fog(0xd6e7f6, 46, 150);

  scene.add(new THREE.HemisphereLight(0xcfe5ff, 0x6d8a51, 1.15 * power));
  scene.add(new THREE.AmbientLight(0xffffff, 0.24 * power));
  const sun = new THREE.DirectionalLight(0xfff4dc, 3.2 * power);
  sun.position.set(-w * 0.55, 28, -d * 0.7);
  sun.castShadow = true;
  sun.shadow.mapSize.set(lowPower ? 1024 : 2048, lowPower ? 1024 : 2048);
  Object.assign(sun.shadow.camera, {
    left: -halfW - 5, right: halfW + 5, top: halfD + 5, bottom: -halfD - 5, near: 1, far: 80,
  });
  sun.shadow.bias = -0.0004;
  sun.shadow.normalBias = 0.04;
  scene.add(sun, sun.target);

  // The lawn runs well past the fence so the edge of the world never shows.
  const lawn = new THREE.Mesh(
    new THREE.PlaneGeometry(w + 80, d + 80),
    make(floorKind, hall.floorColor, w + 80, d + 80),
  );
  lawn.rotation.x = -Math.PI / 2;
  lawn.receiveShadow = true;
  scene.add(lawn);

  const pathMat = make('limestone', 0xded3bd, 10, 10);
  function pathSlab(width, depth, x, z, y) {
    const slab = new THREE.Mesh(new THREE.PlaneGeometry(width, depth), pathMat);
    slab.rotation.x = -Math.PI / 2;
    slab.position.set(x, y, z);
    slab.receiveShadow = true;
    scene.add(slab);
  }
  pathSlab(5.0, d - 2.4, 0, 0, 0.014);
  pathSlab(w - 4.0, 4.2, 0, 1.6, 0.016);
  const ring = new THREE.Mesh(new THREE.RingGeometry(3.1, 5.3, 48), pathMat);
  ring.rotation.x = -Math.PI / 2;
  ring.position.y = 0.018;
  ring.receiveShadow = true;
  scene.add(ring);

  // Fountain at the centre.
  const basin = new THREE.Mesh(new THREE.CylinderGeometry(2.6, 2.75, 0.55, 32), stone);
  basin.position.y = 0.275;
  basin.castShadow = basin.receiveShadow = true;
  scene.add(basin);
  const water = new THREE.Mesh(
    new THREE.CircleGeometry(2.32, 32),
    new THREE.MeshStandardMaterial({ color: 0x79b4da, roughness: 0.12, metalness: 0.35 }),
  );
  water.rotation.x = -Math.PI / 2;
  water.position.y = 0.57;      // just proud of the basin rim, or the cylinder hides it
  scene.add(water);
  const column = new THREE.Mesh(new THREE.CylinderGeometry(0.22, 0.42, 1.3, 16), stone);
  column.position.y = 1.15;
  column.castShadow = true;
  scene.add(column);
  const bowl = new THREE.Mesh(new THREE.CylinderGeometry(0.92, 0.26, 0.28, 20), stone);
  bowl.position.y = 1.92;
  bowl.castShadow = true;
  scene.add(bowl);
  blockers.push({ minX: -3.3, maxX: 3.3, minZ: -3.3, maxZ: 3.3 });

  // A hedge marks the edge the visitor can walk to; the south side stays open.
  const hedge = new THREE.MeshStandardMaterial({ color: 0x4d7a41, roughness: 0.96 });
  const hedgeH = 1.15;
  box(0.7, hedgeH, d, hedge, -halfW + 0.35, hedgeH / 2, 0);
  box(0.7, hedgeH, d, hedge, halfW - 0.35, hedgeH / 2, 0);
  box(w, hedgeH, 0.7, hedge, 0, hedgeH / 2, -halfD + 0.35);
  [-1, 1].forEach(sign => {
    const len = halfW - 2.5;
    box(len, hedgeH, 0.7, hedge, sign * (halfW - len / 2), hedgeH / 2, halfD - 0.35);
    box(0.5, 2.2, 0.5, stone, sign * 2.5, 1.1, halfD - 0.35);
  });

  // Trees: a ring outside the hedge hides the horizon, a few stand inside the park.
  const outerCount = lowPower ? 18 : 30;
  const trees = [];
  for (let i = 0; i < outerCount; i += 1) {
    const angle = (i / outerCount) * Math.PI * 2;
    const radius = Math.max(halfW, halfD) + 5 + (i % 3) * 2.6;
    trees.push({
      x: Math.sin(angle) * radius, z: Math.cos(angle) * radius,
      scale: 0.9 + (i % 4) * 0.18, rotY: i * 1.7, inside: false,
    });
  }
  [{ x: -10.5, z: -4.5 }, { x: 9.8, z: -6.2 }, { x: -11.2, z: 6.4 }, { x: 11.0, z: 7.2 }].forEach((spot, i) => {
    trees.push({ ...spot, scale: 1.0 + (i % 2) * 0.2, rotY: i * 2.1, inside: true });
  });
  instanced(new THREE.CylinderGeometry(0.22, 0.34, 3.2, 8), make('walnut', 0x6b4f35, 1, 2),
    trees.map(tree => ({ ...tree, y: 1.6 * tree.scale })));
  instanced(new THREE.IcosahedronGeometry(1.8, 0), leaf,
    trees.map(tree => ({ ...tree, y: 3.9 * tree.scale })));
  instanced(new THREE.IcosahedronGeometry(1.25, 0), leafLight,
    trees.map(tree => ({ ...tree, y: 5.1 * tree.scale, rotY: tree.rotY + 0.8 })));
  trees.filter(tree => tree.inside).forEach(tree => blockers.push({
    minX: tree.x - 0.8, maxX: tree.x + 0.8, minZ: tree.z - 0.8, maxZ: tree.z + 0.8,
  }));

  // Flower beds.
  const beds = [{ x: -6.5, z: -8.0 }, { x: 6.5, z: -8.0 }, { x: -9.5, z: 6.5 }, { x: 9.5, z: 6.5 }];
  const blooms = [];
  beds.forEach((bed, bedIndex) => {
    box(3.2, 0.24, 2.0, make('plain', 0x6b4f3a), bed.x, 0.12, bed.z);
    blockers.push({ minX: bed.x - 1.9, maxX: bed.x + 1.9, minZ: bed.z - 1.3, maxZ: bed.z + 1.3 });
    const count = lowPower ? 9 : 18;
    for (let i = 0; i < count; i += 1) {
      blooms.push({
        x: bed.x + (((i * 37) % 29) / 29) * 2.7 - 1.35,
        z: bed.z + (((i * 53) % 23) / 23) * 1.5 - 0.75,
        scale: 0.85 + ((i % 3) * 0.22),
        tint: (bedIndex + i) % 3,
      });
    }
  });
  instanced(new THREE.CylinderGeometry(0.022, 0.022, 0.3, 5),
    new THREE.MeshStandardMaterial({ color: 0x4c7a3c, roughness: 0.9 }),
    blooms.map(bloom => ({ ...bloom, y: 0.37 })));
  const bloomMesh = instanced(new THREE.SphereGeometry(0.09, 7, 5),
    new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.72 }),
    blooms.map(bloom => ({ ...bloom, y: 0.53 })));
  if (bloomMesh) {
    const palette = [0xef7f9c, 0xf5cf5e, 0xf3efff];
    const tint = new THREE.Color();
    blooms.forEach((bloom, index) => bloomMesh.setColorAt(index, tint.setHex(palette[bloom.tint])));
    bloomMesh.instanceColor.needsUpdate = true;
  }

  // Sculptures on plinths, benches facing the fountain, lamp posts along the paths.
  // No environment map in this scene, so a highly metallic finish would read as black.
  const bronze = new THREE.MeshStandardMaterial({ color: 0xa2813f, roughness: 0.46, metalness: 0.35 });
  [{ x: -4.8, z: 6.5 }, { x: 4.8, z: 6.5 }].forEach((spot, index) => {
    box(1.1, 0.9, 1.1, stone, spot.x, 0.45, spot.z);
    const art = index === 0
      ? new THREE.Mesh(new THREE.TorusKnotGeometry(0.42, 0.13, 72, 10), bronze)
      : new THREE.Mesh(new THREE.IcosahedronGeometry(0.62, 1), bronze);
    art.position.set(spot.x, 1.55, spot.z);
    art.castShadow = true;
    scene.add(art);
    blockers.push({ minX: spot.x - 0.9, maxX: spot.x + 0.9, minZ: spot.z - 0.9, maxZ: spot.z + 0.9 });
  });
  [[-5.8, 5.8], [5.8, 5.8], [-5.8, -5.8], [5.8, -5.8]].forEach(([x, z]) => {
    const bench = new THREE.Group();
    bench.position.set(x, 0, z);
    bench.rotation.y = Math.atan2(-x, -z);   // seats face the fountain
    scene.add(bench);
    [-0.19, 0, 0.19].forEach(dz => box(1.9, 0.07, 0.15, timber, 0, 0.46, dz, bench));
    [0.62, 0.84].forEach(dy => box(1.9, 0.15, 0.06, timber, 0, dy, -0.24, bench));
    [-0.75, 0.75].forEach(dx => {
      box(0.09, 0.42, 0.5, metal, dx, 0.22, 0, bench);
      box(0.07, 0.48, 0.07, metal, dx, 0.68, -0.24, bench);
    });
    blockers.push({ minX: x - 1.1, maxX: x + 1.1, minZ: z - 1.0, maxZ: z + 1.0 });
  });
  [[-4.2, -6.5], [4.2, -6.5], [-4.2, 8.0], [4.2, 8.0]].forEach(([x, z]) => {
    box(0.3, 0.24, 0.3, stone, x, 0.12, z);
    const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.075, 0.1, 3.4, 10), metal);
    pole.position.set(x, 1.7, z);
    pole.castShadow = true;
    scene.add(pole);
    const lamp = new THREE.Mesh(
      new THREE.BoxGeometry(0.4, 0.45, 0.4),
      new THREE.MeshBasicMaterial({ color: 0xfff6dc, toneMapped: false }),
    );
    lamp.position.set(x, 3.58, z);
    scene.add(lamp);
  });

  // Every slot gets a standing canvas: panel, cap rail and two legs.
  const canvasMat = make('plain', hall.wallColor || 0xefe7d6);
  (layout.slots || []).forEach(slot => {
    const stand = new THREE.Group();
    stand.position.set(slot.x, 0, slot.z);
    stand.rotation.y = slot.rotationY;
    scene.add(stand);
    const panelW = slot.width + 0.55;
    const panelH = slot.height + 0.6;
    const legTop = slot.y - panelH / 2;
    box(panelW, panelH, 0.1, canvasMat, 0, slot.y, -0.08, stand);
    box(panelW + 0.18, 0.13, 0.17, timber, 0, slot.y + panelH / 2 + 0.06, -0.06, stand);
    [-1, 1].forEach(sign => {
      box(0.13, legTop, 0.13, timber, sign * (panelW / 2 - 0.12), legTop / 2, -0.06, stand);
    });
    const spanX = Math.abs(Math.cos(slot.rotationY)) * panelW / 2 + Math.abs(Math.sin(slot.rotationY)) * 0.5;
    const spanZ = Math.abs(Math.sin(slot.rotationY)) * panelW / 2 + Math.abs(Math.cos(slot.rotationY)) * 0.5;
    blockers.push({
      minX: slot.x - spanX, maxX: slot.x + spanX, minZ: slot.z - spanZ, maxZ: slot.z + spanZ,
    });
  });

  return { blockers, frameMaterial: frameMaterialFor(frameKind, make) };
}


export function createFrame(width, height, material) {
  const group = new THREE.Group();
  const mat = new THREE.MeshStandardMaterial({ color: 0xf5f1e7, roughness: 0.95 });
  const inner = new THREE.MeshStandardMaterial({ color: 0x37312a, roughness: 0.5, metalness: 0.35 });
  function bar(w, h, depth, x, y, z, surface) {
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, depth), surface);
    mesh.position.set(x, y, z);
    mesh.castShadow = mesh.receiveShadow = true;
    group.add(mesh);
  }
  // Four separate rails leave the image unobstructed. Recessed mat + inner lip + raised outer moulding.
  function ring(padding, thickness, depth, z, surface) {
    const w = width + padding * 2, h = height + padding * 2;
    [-1, 1].forEach(sign => {
      bar(w + thickness * 2, thickness, depth, 0, sign * (h + thickness) / 2, z, surface);
      bar(thickness, h, depth, sign * (w + thickness) / 2, 0, z, surface);
    });
  }
  ring(0, 0.11, 0.035, -0.02, mat);
  ring(0.11, 0.018, 0.065, -0.005, inner);
  ring(0.128, 0.065, 0.12, 0.008, material);
  ring(0.193, 0.015, 0.09, -0.003, material);
  return group;
}

export function disposeFrame(frame) {
  const materials = new Set();
  frame.traverse(child => {
    if (child.isMesh) {
      child.geometry.dispose();
      // Outer rail material is shared by every frame.
      if (child.material !== frame.userData.sharedMaterial) materials.add(child.material);
    }
  });
  materials.forEach(material => material.dispose());
}
