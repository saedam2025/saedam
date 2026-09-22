import * as THREE from './vendor/three/three.module.js';
import {
  photographicMaterials, photographicSky, photographicStreetSky, graffitiSheet, glazedSkylight,
} from './exhibition_photoreal.js';

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
  // 천장 보 · 창틀 · 난간 기둥처럼 똑같은 상자를 수십 개 놓는 자리가 많다.
  // 크기가 같으면 기하 데이터를 함께 쓰게 해서 GPU 버퍼와 만드는 시간을 아낀다.
  const shapes = new Map();
  const shape = (width, height, depth) => {
    const key = `${width.toFixed(4)}|${height.toFixed(4)}|${depth.toFixed(4)}`;
    if (!shapes.has(key)) shapes.set(key, new THREE.BoxGeometry(width, height, depth));
    return shapes.get(key);
  };
  return (width, height, depth, material, x, y, z, parent = scene) => {
    const mesh = new THREE.Mesh(shape(width, height, depth), material);
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
  if (kind === 'white') {
    // Moulded white plastic: no metal, and a thin clear coat for the sheen.
    const plastic = new THREE.MeshPhysicalMaterial({
      color: 0xf3f4f2, roughness: 0.38, metalness: 0,
      clearcoat: 0.65, clearcoatRoughness: 0.28,
    });
    // The dark gallery lip would read as a smudge on a white frame.
    plastic.userData.lip = new THREE.MeshPhysicalMaterial({
      color: 0xe6e7e4, roughness: 0.45, metalness: 0, clearcoat: 0.5,
    });
    return plastic;
  }
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
  if (layout.style === 'lobby') return buildLobby(scene, renderer, hall, layout, lowPower);
  if (layout.style === 'theater') return buildTheater(scene, renderer, hall, layout, lowPower);
  if (layout.style === 'busstop') return buildBusStop(scene, renderer, hall, layout, lowPower);
  if (layout.style === 'alley') return buildAlley(scene, renderer, hall, layout, lowPower);
  if (layout.style === 'rooftop') return buildRooftop(scene, renderer, hall, layout, lowPower);
  return buildIndoor(scene, renderer, hall, layout, lowPower);
}

// Lounge / gallery: a roofed hall built from the wall and partition list.
function buildIndoor(scene, renderer, hall, layout, lowPower) {
  const w = layout.size.width, d = layout.size.depth, h = layout.size.height;
  const lounge = layout.style === 'lounge';
  const photos = lounge ? photographicMaterials(renderer, materialLibrary(renderer)) : null;
  const make = photos ? photos.make : materialLibrary(renderer);
  const wallKind = resolveKind(hall.wallTexture, 'plaster');
  const floorKind = resolveKind(hall.floorTexture, lounge ? 'oak' : 'concrete');
  const frameKind = resolveKind(hall.frameStyle, lounge ? 'white' : 'black');
  const blockers = [];
  const metal = new THREE.MeshStandardMaterial({ color: 0x282a28, roughness: 0.36, metalness: 0.75 });
  const trim = make('plain', 0xd6cfc2);
  const timber = make('oak', 0xb38b5c, 3, 1);
  const box = boxFactory(scene);
  const power = hall.lightIntensity || 1;
  scene.background = new THREE.Color(0xe3e8eb);
  scene.fog = lounge ? null : new THREE.Fog(0xe3e8eb, 55, 110);
  const skyReady = lounge ? photographicSky(scene, renderer) : Promise.resolve();
  scene.add(new THREE.HemisphereLight(0xeaf2ff, lounge ? 0xc6b9a5 : 0xc6c3b9, (lounge ? 0.55 : 1.25) * power));
  scene.add(new THREE.AmbientLight(0xffffff, (lounge ? 0.08 : 0.3) * power));
  const sun = new THREE.DirectionalLight(0xfff2da, (lounge ? 3.2 : 2.7) * power);
  sun.position.set(-w * 0.25, h + 8, -d * 0.22);
  sun.castShadow = true;
  sun.shadow.mapSize.set(lowPower ? 1024 : 2048, lowPower ? 1024 : 2048);
  Object.assign(sun.shadow.camera, { left: -w, right: w, top: d, bottom: -d, near: 0.5, far: 65 });
  sun.shadow.bias = -0.0003;
  sun.shadow.normalBias = 0.035;
  if (lounge) {
    // Fit the shadow frustum to the building for crisp mullions without huge maps.
    Object.assign(sun.shadow.camera, { left: -w * 0.65, right: w * 0.65, top: d * 0.8, bottom: -d * 0.8 });
    sun.shadow.normalBias = 0.025;
  }
  scene.add(sun);
  const fill = new THREE.DirectionalLight(0xe4eeff, (lounge ? 0.18 : 0.65) * power);
  fill.position.set(w, h, d);
  scene.add(fill);
  const floorColor = new THREE.Color(hall.floorColor);
  if (floorKind === 'walnut') floorColor.multiply(new THREE.Color(0x99765a));
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(w, d), make(floorKind, floorColor, w, d));
  floor.name = lounge ? 'lounge-photo-floor' : 'gallery-floor';
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
    box((w - sw) / 2, 0.28, d, lounge ? make('plaster', 0xf1ece2, (w - sw) / 2, d) : ceilingMat, sign * (w + sw) / 4, h, 0);
    box(sw, 0.28, (d - sd) / 2, lounge ? make('plaster', 0xf1ece2, sw, (d - sd) / 2) : ceilingMat, 0, h, sign * (d + sd) / 4);
    box(0.18, 0.5, sd, trim, sign * sw / 2, h + 0.12, 0);
  });
  if (lounge) {
    glazedSkylight(scene, box, metal, trim, sw, sd, h, lowPower);
  } else {
    const sky = new THREE.Mesh(new THREE.PlaneGeometry(sw, sd), new THREE.MeshBasicMaterial({ color: 0xe5f2ff, side: THREE.DoubleSide, toneMapped: false }));
    sky.rotation.x = Math.PI / 2;
    sky.position.y = h + 0.45;
    scene.add(sky);
    for (let z = -sd / 2; z <= sd / 2; z += 2.7) box(sw, 0.22, 0.10, metal, 0, h + 0.2, z);
  }
  for (let z = -d / 2 + 2; z < d / 2; z += 4) {
    if (lounge && Math.abs(z) < sd / 2) {
      // Structural ribs stop at the opening instead of hiding the glass.
      for (const sign of [-1, 1]) box((w - sw) / 2, 0.28, 0.24, ceilingMat, sign * (w + sw) / 4, h - 0.28, z);
    } else box(w, lounge ? 0.28 : 0.48, 0.24, ceilingMat, 0, h - 0.28, z);
  }
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
  const trackY = lounge && layout.balcony ? 3.5 : h - 0.65;
  [-1, 1].forEach(sign => box(0.06, 0.07, d - 2, metal, sign * (w / 2 - (lounge ? 1.3 : 2)), trackY, 0));
  if (lounge) box(w - 2, 0.07, 0.06, metal, 0, trackY, -d / 2 + 1.3);
  const wallSlots = (layout.slots || []).filter(s => s.code.startsWith('A') || s.code.startsWith('B') || s.code.startsWith('C'));
  const fixtureShape = new THREE.CylinderGeometry(0.09, 0.11, 0.25, 12);
  const lensShape = lounge ? new THREE.CircleGeometry(0.075, 16) : null;
  const lensMaterial = lounge ? new THREE.MeshStandardMaterial({
    color: 0xfff0d7, emissive: 0xffd49c, emissiveIntensity: 2.2 * power, roughness: 0.3,
  }) : null;
  const aims = [];                                  // 조명이 비출 자리(작품) 목록
  const DOWN = new THREE.Vector3(0, -1, 0);
  wallSlots.forEach((s, i) => {
    const direction = new THREE.Vector3(Math.sin(s.rotationY), 0, Math.cos(s.rotationY));
    const pos = new THREE.Vector3(s.x, Math.min(h - 0.85, layout.balcony ? 3.35 : h - 0.85), s.z).addScaledVector(direction, 1.3);
    const target = new THREE.Vector3(s.x, s.y, s.z);
    const aim = target.clone().sub(pos).normalize();
    const fixture = new THREE.Mesh(fixtureShape, metal);
    fixture.position.copy(pos);
    fixture.quaternion.setFromUnitVectors(DOWN, aim);
    scene.add(fixture);
    if (lounge) {
      // 갓(렌즈)은 자리마다 그대로 켜 둔다. 스스로 빛나 보이기만 해서 계산이 거의 없다.
      const lens = new THREE.Mesh(lensShape, lensMaterial);
      lens.position.copy(pos).addScaledVector(aim, 0.13);
      lens.lookAt(target);
      scene.add(lens);
      aims.push({ position: pos, target });
      return;
    }
    if (i % 2 === 0 && i < (lowPower ? 6 : 10)) {
      const light = new THREE.SpotLight(0xffedce, 48 * power, 11, Math.PI / 5, 0.75, 2);
      light.position.copy(pos);
      light.target.position.copy(target);
      scene.add(light, light.target);
    }
  });
  const spotlights = lounge ? trackSpotlights(scene, aims, layout, power, lowPower) : null;
  if (lounge && layout.balcony) {
    // Recessed warm strips with actual bounced fill under the balcony.
    const diffuser = new THREE.MeshStandardMaterial({ color: 0xffefda, emissive: 0xffd6a1, emissiveIntensity: 2.4 * power });
    for (const sign of [-1, 1]) {
      const strip = box(0.045, 0.025, d - 0.6, diffuser, sign * (w / 2 - layout.balcony.depth + 0.08), layout.balcony.height - 0.14, 0);
      strip.castShadow = false;
      const bounce = new THREE.PointLight(0xffe3ba, 14 * power, 12, 2);
      bounce.position.set(sign * (w / 2 - 1.5), 3.2, 0);
      scene.add(bounce);
    }
  }
  // Benches and greenery stay along the entry edge, away from artwork and spawn.
  const potShape = new THREE.CylinderGeometry(0.38, 0.27, 0.65, 20);
  const potMaterial = make('concrete', 0xc9bca5);
  const foliage = new THREE.MeshStandardMaterial({ color: 0x476441, roughness: 0.82 });
  const leaves = [];
  [-1, 1].forEach(sign => {
    const x = sign * w * 0.30, z = d / 2 - 2.0;
    box(2.6, 0.16, 0.7, timber, x, 0.48, z);
    [-0.95, 0.95].forEach(dx => box(0.1, 0.4, 0.56, metal, x + dx, 0.2, z));
    blockers.push({ minX: x - 1.65, maxX: x + 1.65, minZ: z - 0.7, maxZ: z + 0.7 });
    const px = sign * (w / 2 - 1.1), pz = d / 2 - 1.1;
    const pot = new THREE.Mesh(potShape, potMaterial);
    pot.position.set(px, 0.325, pz);
    pot.castShadow = pot.receiveShadow = true;
    scene.add(pot);
    for (let j = 0; j < 14; j++) {
      const angle = j * 2.4;
      leaves.push({
        x: px + Math.sin(angle) * 0.25, y: 0.9 + (j % 4) * 0.19, z: pz + Math.cos(angle) * 0.25,
        rotation: new THREE.Euler(Math.sin(angle) * 0.65, angle, Math.cos(angle) * 0.7),
      });
    }
    blockers.push({ minX: px - 0.7, maxX: px + 0.7, minZ: pz - 0.7, maxZ: pz + 0.7 });
  });
  // 잎이 28장이라 낱개로 두면 매 프레임 28번을 따로 그린다. 한 덩어리로 묶어 한 번에 그린다.
  if (leaves.length) {
    const clump = new THREE.InstancedMesh(new THREE.SphereGeometry(1, 8, 6), foliage, leaves.length);
    const matrix = new THREE.Matrix4();
    const quaternion = new THREE.Quaternion();
    const position = new THREE.Vector3();
    const leafScale = new THREE.Vector3(0.13, 0.44, 0.065);
    leaves.forEach((leaf, index) => {
      matrix.compose(position.set(leaf.x, leaf.y, leaf.z), quaternion.setFromEuler(leaf.rotation), leafScale);
      clump.setMatrixAt(index, matrix);
    });
    clump.instanceMatrix.needsUpdate = true;
    clump.castShadow = true;
    scene.add(clump);
  }
  const frameMaterial = frameMaterialFor(frameKind, make);
  if (lounge) {
    scene.traverse(object => {
      for (const material of (Array.isArray(object.material) ? object.material : [object.material])) {
        if (material?.isMeshStandardMaterial && !material.isMeshPhysicalMaterial) material.envMapIntensity = 0.5 * power;
      }
    });
  }
  return {
    blockers, frameMaterial,
    ready: lounge ? Promise.all([photos.ready(), skyReady]) : Promise.resolve(),
    update: spotlights ? spotlights.follow : null,
  };
}

// 라운지 트랙 조명. 조명이 하나 늘면 화면의 모든 픽셀에서 계산이 한 번씩 늘어나므로
// 자리 수만큼 켜면 넓은 바닥·벽을 볼 때 걸음이 끊긴다. 정해진 개수만 만들어 두고
// 관람객이 다가간 작품으로 옮겨 쓴다. 갓은 자리마다 그대로 켜져 있어 옮겨 다니는 것이 보이지 않는다.
function trackSpotlights(scene, aims, layout, power, lowPower) {
  const count = Math.min(aims.length, lowPower ? 3 : 6);
  if (!count) return null;
  const lights = [];
  for (let i = 0; i < count; i++) {
    // 조명 개수가 바뀌면 three.js가 셰이더를 다시 엮어 화면이 한 번 멈춘다. 개수는 처음부터 고정한다.
    const light = new THREE.SpotLight(0xffedce, 32 * power, 11, Math.PI / 3.2, 0.85, 2);
    scene.add(light, light.target);
    lights.push(light);
  }
  const held = new Array(count).fill(-1);          // 조명마다 지금 비추고 있는 자리
  const ranked = aims.map((aim, index) => index);
  function follow(viewer) {
    // 이미 켜져 있는 자리에 가산점을 줘서 경계에 서 있어도 조명이 깜빡이지 않게 한다.
    const score = index => aims[index].target.distanceToSquared(viewer)
      * (held.indexOf(index) >= 0 ? 0.64 : 1);
    ranked.sort((a, b) => score(a) - score(b));
    const wanted = ranked.slice(0, count);
    const spare = [];
    for (let i = 0; i < count; i++) if (wanted.indexOf(held[i]) < 0) spare.push(i);
    for (const index of wanted) {
      if (held.indexOf(index) >= 0) continue;
      const i = spare.pop();
      held[i] = index;
      lights[i].position.copy(aims[index].position);
      lights[i].target.position.copy(aims[index].target);
    }
  }
  follow(new THREE.Vector3(layout.spawn.x, 1.6, layout.spawn.z));
  return { follow };
}

// Seen only through the classroom windows: sky, schoolyard, trees and the block opposite.
function buildSchoolyard(scene, make, box, instanced, halfW, lowPower) {
  // The room sits a little above the yard, the way a ground-floor classroom does.
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(220, 220), make('concrete', 0x86a866, 220, 220));
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -0.55;
  scene.add(ground);
  const yard = new THREE.Mesh(new THREE.CircleGeometry(15, 40), make('limestone', 0xdcc9a6, 12, 12));
  yard.rotation.x = -Math.PI / 2;
  yard.position.set(-halfW - 19, -0.51, 0);   // keep a clear gap over the lawn
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
    box(0.33, 0.12, 42, wall, -halfW - 39.20, 0.85 + level * 3.0, -2);
  }
  const mullions = [];
  for (let level = 0; level < 4; level++) for (let z = -22; z <= 18; z += 1.6) {
    mullions.push({ x: -halfW - 39.10, y: 1.7 + level * 3, z });
  }
  instanced(new THREE.BoxGeometry(0.08, 1.6, 0.07), make('plain', 0xd9dcd9), mullions);

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
  // Several overlapping, rounded crowns give each tree an irregular silhouette.
  for (let crown = 0; crown < 3; crown++) {
    instanced(new THREE.IcosahedronGeometry(1.25, lowPower ? 1 : 2),
      new THREE.MeshStandardMaterial({ color: [0x526d3c, 0x668348, 0x718d51][crown], roughness: 0.96 }),
      trees.map(tree => ({ ...tree, x: tree.x + Math.cos(tree.rotY + crown * 2.1) * 0.7,
        z: tree.z + Math.sin(tree.rotY + crown * 2.1) * 0.7,
        y: -0.55 + (3.6 + crown * 0.35) * tree.scale })));
  }

  // Flag pole beside the yard.
  const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.07, 0.09, 8, 10),
    new THREE.MeshStandardMaterial({ color: 0xd8dade, roughness: 0.4, metalness: 0.4 }));
  pole.position.set(-halfW - 9, 3.45, -9);
  scene.add(pole);
}

// Classroom: window wall on the left, blackboard at the front, desks in rows.
function buildClassroom(scene, renderer, hall, layout, lowPower) {
  const photos = photographicMaterials(renderer, materialLibrary(renderer));
  const make = photos.make;
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
  const ceilingMat = classroomCeiling(renderer, w, d);
  const trim = make('plain', 0xe9e3d6);
  const timber = make('oak', 0xc8a173, 2, 1);
  const deskTop = make('oak', 0xe2c69e, 1, 1);
  const metal = new THREE.MeshStandardMaterial({ color: 0x9aa0a8, roughness: 0.42, metalness: 0.55 });

  scene.background = new THREE.Color(0xdfe9f5);
  const skyReady = photographicSky(scene, renderer);
  // 창밖 풍경만 멀리서 흐려지게 한다(교실 안은 12m도 안 되므로 영향이 없다).
  scene.fog = new THREE.Fog(0xdbe9f7, 60, 200);

  // Daylight comes through the window wall (-X), so the sun sits far out on that side.
  scene.add(new THREE.HemisphereLight(0xf4f8ff, 0xc9c2b2, 0.55 * power));
  scene.add(new THREE.AmbientLight(0xffffff, 0.12 * power));
  const sun = new THREE.DirectionalLight(0xfff1d2, 2.2 * power);
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
  floor.name = 'classroom-photo-floor';
  floor.rotation.x = -Math.PI / 2;
  floor.receiveShadow = true;
  scene.add(floor);
  box(w + 0.4, 0.22, d + 0.4, ceilingMat, 0, h + 0.11, 0);

  buildSchoolyard(scene, make, box, instanced, halfW, lowPower);
  // The yard sits lower than the floor, so close the gap under the room.
  // Its top must stay below y=0: level with the floor plane the two surfaces
  // fight over the same depth values and the floor flickers.
  box(w + 0.6, 0.6, d + 0.6, make('concrete', 0xb9b2a4, 6, 1), 0, -0.36, 0);

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

  // Painted skirting and a suspended acoustic ceiling with recessed grid seams.
  const skirting = make('plain', 0xbcb7aa);
  [-1, 1].forEach(sign => {
    box(w, 0.12, 0.035, skirting, 0, 0.06, sign * (halfD - 0.018));
    box(0.035, 0.12, d, skirting, sign * (halfW - 0.018), 0.06, 0);
  });
  const ceilingGrid = make('plain', 0xd0cec7);
  const gridX = [], gridZ = [];
  for (let x = -halfW + 0.6; x < halfW; x += 0.6) gridX.push({ x, y: h - 0.007, z: 0 });
  for (let z = -halfD + 0.6; z < halfD; z += 0.6) gridZ.push({ x: 0, y: h - 0.007, z });
  instanced(new THREE.BoxGeometry(0.016, 0.014, d), ceilingGrid, gridX);
  instanced(new THREE.BoxGeometry(w, 0.014, 0.016), ceilingGrid, gridZ);

  // Window: glass, mullions, sill board and a curtain at each end.
  // 창밖이 그대로 보여야 하므로 유리는 반사만 살짝 남기고 거의 투명하게 둔다.
  const glass = new THREE.MeshPhysicalMaterial({
    color: 0xeaf6ff, transparent: true, opacity: 0.07, roughness: 0.04, depthWrite: false,
  });
  const pane = box(0.05, head - sill, bandHalf * 2, glass, -halfW + 0.02, (sill + head) / 2, 0);
  pane.name = 'classroom-window-glass';
  pane.castShadow = false;
  box(0.12, 0.1, bandHalf * 2 + 0.2, trim, -halfW + 0.07, sill, 0);
  box(0.12, 0.1, bandHalf * 2 + 0.2, trim, -halfW + 0.07, head, 0);
  for (let z = -bandHalf; z <= bandHalf + 0.01; z += 1.35) {
    box(0.1, head - sill, 0.08, trim, -halfW + 0.07, (sill + head) / 2, z);
  }
  box(0.34, 0.07, bandHalf * 2, timber, -halfW + 0.20, sill + 0.035, 0);
  box(0.1, 0.04, bandHalf * 2, metal, -halfW + 0.09, 1.8, 0);
  const curtain = make('plaster', 0xe7e3d5, 0.65, 1.9);
  curtain.side = THREE.DoubleSide;
  [-1, 1].forEach(sign => {
    // Real folds, rather than an opaque rectangular block over the glass.
    const folds = new THREE.PlaneGeometry(0.56, 1.9, 32, 1);
    const vertices = folds.attributes.position;
    for (let i = 0; i < vertices.count; i++) vertices.setZ(i, Math.sin(vertices.getX(i) * 65) * 0.045);
    folds.computeVertexNormals();
    const drape = new THREE.Mesh(folds, curtain);
    drape.rotation.y = Math.PI / 2;
    drape.position.set(-halfW + 0.28, 1.86, sign * (bandHalf - 0.25));
    drape.castShadow = drape.receiveShadow = true;
    scene.add(drape);
  });
  box(0.05, 0.05, bandHalf * 2 + 0.2, metal, -halfW + 0.28, 2.85, 0);

  // The directional sun and window mullions produce the actual light patches.

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

  // Artwork-sized panel edges are built by the viewer, including when slides resize.

  // Lockers fill the back corners; the teacher's desk stands beside the blackboard.
  [-1, 1].forEach(sign => {
    const x = sign * 4.1;
    box(2.1, 1.0, 0.46, make('plain', 0xc5cfc9), x, 0.5, halfD - 0.25);
    box(2.16, 0.04, 0.5, timber, x, 1.02, halfD - 0.26);
    for (let i = 0; i < 3; i++) {
      const cx = x - 0.7 + i * 0.7;
      box(0.675, 0.89, 0.018, make('plain', 0xd9dfd7), cx, 0.51, halfD - 0.49);
      box(0.08, 0.018, 0.025, metal, cx + 0.21, 0.62, halfD - 0.515);
    }
    blockers.push({ minX: x - 1.33, maxX: x + 1.33, minZ: halfD - 0.78, maxZ: halfD });
  });
  const podiumX = -2.8, podiumZ = -halfD + 1.5;
  box(1.25, 0.08, 0.62, timber, podiumX, 1.03, podiumZ);
  box(1.05, 0.96, 0.5, make('plain', 0xdacfba), podiumX, 0.5, podiumZ);
  blockers.push({ minX: podiumX - 1.1, maxX: podiumX + 1.1, minZ: podiumZ - 0.8, maxZ: podiumZ + 0.8 });

  // Door and timetable on the corridor side.
  const doorZ = halfD - 1.3;
  box(0.05, 2.3, 1.2, trim, halfW - 0.02, 1.15, doorZ);
  box(0.07, 2.1, 1.0, make('plain', 0xdbd2c2), halfW - 0.07, 1.05, doorZ);
  // A recessed safety-glass insert and metal kick plate on the classroom door.
  box(0.012, 0.66, 0.67, metal, halfW - 0.113, 1.64, doorZ);
  box(0.012, 0.58, 0.59, new THREE.MeshStandardMaterial({
    color: 0x9aadb0, roughness: 0.28, metalness: 0.15, envMapIntensity: 0.6,
  }), halfW - 0.122, 1.64, doorZ);
  box(0.012, 0.22, 0.88, metal, halfW - 0.114, 0.17, doorZ);
  const knob = new THREE.Mesh(new THREE.SphereGeometry(0.05, 10, 8), metal);
  knob.position.set(halfW - 0.12, 1.05, doorZ - 0.38);
  scene.add(knob);
  const timetable = new THREE.Mesh(
    new THREE.PlaneGeometry(0.86, 0.62),
    new THREE.MeshBasicMaterial({ map: labelTexture('시간표', '우리 반 하루') }),
  );
  timetable.rotation.y = -Math.PI / 2;
  // Above the door, clear of both the rear locker and the last photo panel.
  timetable.position.set(halfW - 0.065, 2.8, doorZ);
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
  [-3.6, -1.4, 1.4, 3.6].forEach(x => [-1.7, 0.2, 2.1].forEach(z => seats.push({ x, z })));
  const seatPart = (sx, sy, sz, material, ox, oy, oz) => instanced(
    material === deskTop && sy <= 0.04 ? classroomSlab(sx, sy, sz) : new THREE.BoxGeometry(sx, sy, sz), material,
    seats.map(seat => ({ x: seat.x + ox, y: oy, z: seat.z + oz })),
  );
  // Thin tubular legs instead of solid panels, so the rows stay light.
  const legMat = new THREE.MeshStandardMaterial({ color: 0xb9bfc7, roughness: 0.36, metalness: 0.55 });
  const legs = (offsets, sx, sy, sz, height) => instanced(
    new THREE.CylinderGeometry(sx / 2, sx / 2, sy, lowPower ? 8 : 12), legMat,
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
  const feet = new THREE.MeshStandardMaterial({ color: 0x414644, roughness: 0.92 });
  instanced(new THREE.CylinderGeometry(0.024, 0.024, 0.026, 8), feet,
    seats.flatMap(seat => [[-0.52, -0.22], [0.52, -0.22], [-0.52, 0.22], [0.52, 0.22],
      [-0.17, 0.54], [0.17, 0.54], [-0.17, 0.87], [0.17, 0.87]]
      .map(([dx, dz]) => ({ x: seat.x + dx, y: 0.013, z: seat.z + dz }))));
  seats.forEach(seat => blockers.push({
    minX: seat.x - 0.825, maxX: seat.x + 0.825, minZ: seat.z - 0.53, maxZ: seat.z + 1.14,
  }));

  classroomSupplies(scene, box, instanced, seats, metal, timber, podiumX, podiumZ, halfD);
  const frameMaterial = frameMaterialFor(frameKind, make);
  const materialsReady = photos.ready().then(() => {
    // Crop inside one photographed plank. Furniture must not inherit floor joints.
    for (const material of [deskTop, timber]) {
      for (const map of [material.map, material.normalMap, material.roughnessMap]) {
        if (!map) continue;
        map.repeat.set(0.083, 0.48);
        map.offset.set(0.012, 0.54);
        map.center.set(0, 0);
        map.rotation = Math.PI / 2;
      }
      material.normalScale.setScalar(0.1);
      material.roughness = 0.85;
    }
  });
  return { blockers, frameMaterial, ready: Promise.all([materialsReady, skyReady]) };
}

function classroomSlab(width, height, depth) {
  const x = -width / 2, y = -depth / 2, r = 0.025;
  const shape = new THREE.Shape();
  shape.moveTo(x + r, y);
  shape.lineTo(x + width - r, y);
  shape.quadraticCurveTo(x + width, y, x + width, y + r);
  shape.lineTo(x + width, y + depth - r);
  shape.quadraticCurveTo(x + width, y + depth, x + width - r, y + depth);
  shape.lineTo(x + r, y + depth);
  shape.quadraticCurveTo(x, y + depth, x, y + depth - r);
  shape.lineTo(x, y + r);
  shape.quadraticCurveTo(x, y, x + r, y);
  const geometry = new THREE.ExtrudeGeometry(shape, {
    depth: height - 0.004, bevelEnabled: true, bevelThickness: 0.002,
    bevelSize: 0.002, bevelSegments: 1, steps: 1, curveSegments: 3,
  });
  const uv = geometry.attributes.uv;
  for (let i = 0; i < uv.count; i++) {
    uv.setXY(i, THREE.MathUtils.clamp((uv.getX(i) + width / 2) / width, 0, 1),
      THREE.MathUtils.clamp((uv.getY(i) + depth / 2) / depth, 0, 1));
  }
  geometry.translate(0, 0, -(height - 0.004) / 2);
  geometry.rotateX(-Math.PI / 2);
  return geometry;
}

function classroomCeiling(renderer, width, depth) {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 128;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#faf9f5';
  ctx.fillRect(0, 0, 128, 128);
  // Fine, deterministic acoustic perforations, one texture tile per 60 cm panel.
  let seed = 73;
  for (let i = 0; i < 950; i++) {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    const x = (seed >>> 16) % 128;
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    ctx.fillStyle = i % 4 ? '#eeede8' : '#d7d6d0';
    ctx.fillRect(x, (seed >>> 16) % 128, 1, 1);
  }
  const map = new THREE.CanvasTexture(canvas);
  map.wrapS = map.wrapT = THREE.RepeatWrapping;
  map.repeat.set(width / 0.6, depth / 0.6);
  map.colorSpace = THREE.SRGBColorSpace;
  map.anisotropy = Math.min(4, renderer.capabilities.getMaxAnisotropy());
  return new THREE.MeshStandardMaterial({ map, color: 0xffffff, roughness: 0.98 });
}


// Small classroom objects use shared geometry and materials to keep VR affordable.
function classroomSupplies(scene, box, instanced, seats, metal, timber, podiumX, podiumZ, halfD) {
  const paper = new THREE.MeshStandardMaterial({ color: 0xf5f1e5, roughness: 0.95 });
  const graphite = new THREE.MeshStandardMaterial({ color: 0x303538, roughness: 0.7 });
  const covers = [0x536d78, 0x9e6552, 0x84916b].map(color => new THREE.MeshStandardMaterial({ color, roughness: 0.8 }));
  seats.forEach((seat, index) => {
    if (index % 3 === 1) return; // Some desks remain empty.
    const x = seat.x - 0.2, z = seat.z - 0.01;
    box(0.23, 0.008, 0.3, covers[index % 3], x, 0.759, z);
    box(0.215, 0.016, 0.285, paper, x + 0.003, 0.771, z);
    box(0.23, 0.004, 0.3, covers[index % 3], x, 0.781, z);
    const pencil = box(0.008, 0.008, 0.17, timber, seat.x + 0.16, 0.759, z);
    pencil.rotation.y = 0.22;
    box(0.009, 0.009, 0.016, graphite, seat.x + 0.14, 0.76, z - 0.088);
  });
  // Closed books, an open attendance book and a ceramic pen cup on the lectern.
  for (let i = 0; i < 3; i++) {
    box(0.28, 0.025, 0.34, paper, podiumX - 0.35, 1.083 + i * 0.032, podiumZ);
    box(0.29, 0.007, 0.35, covers[i], podiumX - 0.35, 1.099 + i * 0.032, podiumZ);
  }
  box(0.42, 0.007, 0.29, paper, podiumX + 0.12, 1.074, podiumZ + 0.05);
  box(0.004, 0.008, 0.29, graphite, podiumX + 0.12, 1.077, podiumZ + 0.05);
  const cup = new THREE.Mesh(new THREE.CylinderGeometry(0.055, 0.046, 0.12, 20, 1, true),
    new THREE.MeshStandardMaterial({ color: 0xe5ded0, roughness: 0.32, side: THREE.DoubleSide }));
  cup.position.set(podiumX + 0.44, 1.13, podiumZ - 0.14);
  cup.castShadow = true;
  scene.add(cup);
  for (let i = 0; i < 4; i++) {
    box(0.008, 0.17, 0.008, covers[i % 3], podiumX + 0.415 + i * 0.015, 1.17, podiumZ - 0.14);
  }
  // Chalk and eraser rest on the chalk tray, below the projection area.
  box(0.16, 0.055, 0.08, timber, 2.8, 0.622, -halfD + 0.16);
  box(0.16, 0.015, 0.08, graphite, 2.8, 0.657, -halfD + 0.16);
  instanced(new THREE.CylinderGeometry(0.009, 0.009, 0.07, 8), paper,
    [-0.12, 0, 0.12].map(x => ({ x: 2.25 + x, y: 0.63, z: -halfD + 0.16 })));
  // Face markers turn the clock into a readable object at viewing distance.
  for (let i = 0; i < 12; i++) {
    const angle = i * Math.PI / 6;
    const tick = box(0.012, i % 3 ? 0.025 : 0.04, 0.008, metal,
      5 + Math.sin(angle) * 0.21, 2.45 + Math.cos(angle) * 0.21, -halfD + 0.097);
    tick.rotation.z = -angle;
  }
}

// A flush photograph on a 4 cm composite panel: the backing never extends past it.
export function createPhotoPanel(width, height) {
  const panel = new THREE.Mesh(new THREE.BoxGeometry(width, height, 0.04),
    new THREE.MeshStandardMaterial({ color: 0xd9d7d1, roughness: 0.72 }));
  panel.name = 'classroom-photo-panel';
  panel.position.z = -0.016;
  panel.castShadow = panel.receiveShadow = true;
  return panel;
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

// 벽 아래에 낀 때. 위로 갈수록 옅어지는 한 장을 alphaMap 으로 쓰면
// 벽과 바닥이 맞닿은 선이 훨씬 자연스러워진다.
function grimeTexture() {
  const canvas = document.createElement('canvas');
  canvas.width = 128;
  canvas.height = 128;
  const ctx = canvas.getContext('2d');
  const gradient = ctx.createLinearGradient(0, 128, 0, 0);
  gradient.addColorStop(0, '#ffffff');
  gradient.addColorStop(0.3, '#8a8a8a');
  gradient.addColorStop(0.72, '#1d1d1d');
  gradient.addColorStop(1, '#000000');
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 128, 128);
  // 얼룩이 없으면 띠 하나를 두른 것처럼 보인다.
  let seed = 918273;
  const random = () => ((seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0) / 4294967296);
  ctx.globalCompositeOperation = 'lighter';
  for (let blot = 0; blot < 26; blot += 1) {
    const x = random() * 128, y = 96 + random() * 40, r = 6 + random() * 22;
    const spot = ctx.createRadialGradient(x, y, 1, x, y, r);
    spot.addColorStop(0, `rgba(255,255,255,${0.18 + random() * 0.3})`);
    spot.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = spot;
    ctx.fillRect(x - r, y - r, r * 2, r * 2);
  }
  const texture = new THREE.CanvasTexture(canvas);
  texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
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
  // Stacked paths need a clear gap between them; a couple of millimetres is
  // inside the depth buffer's precision at this distance and flickers.
  pathSlab(5.0, d - 2.4, 0, 0, 0.02);
  pathSlab(w - 4.0, 4.2, 0, 1.6, 0.035);
  const ring = new THREE.Mesh(new THREE.RingGeometry(3.1, 5.3, 48), pathMat);
  ring.rotation.x = -Math.PI / 2;
  ring.position.y = 0.05;
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
  box(w - 1.4, hedgeH, 0.7, hedge, 0, hedgeH / 2, -halfD + 0.35);
  [-1, 1].forEach(sign => {
    const len = halfW - 3.2;
    box(len, hedgeH, 0.7, hedge, sign * (halfW - 0.7 - len / 2), hedgeH / 2, halfD - 0.35);
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


// A lit case behind a poster / picture, as cinemas and screening rooms use.
function posterCase(box, group, slot, rim, shell) {
  // The rim sits a little behind the inner panel: level with it the two faces
  // share the same depth values and an empty case flickers.
  box(slot.width + 0.46, slot.height + 0.46, 0.05, rim, 0, slot.y, -0.035, group);
  box(slot.width + 0.3, slot.height + 0.3, 0.09, shell, 0, slot.y, -0.04, group);
}

// Cinema lobby: auditorium doors and a trailer screen at the front, concession
// counter on one side, poster light-boxes all round and seating in the middle.
function buildLobby(scene, renderer, hall, layout, lowPower) {
  const make = materialLibrary(renderer);
  const box = boxFactory(scene);
  const instanced = instancedFactory(scene);
  const w = layout.size.width, d = layout.size.depth, h = layout.size.height;
  const halfW = w / 2, halfD = d / 2;
  const power = hall.lightIntensity || 1;
  const blockers = [];
  const wallKind = resolveKind(hall.wallTexture, 'plaster');
  const floorKind = resolveKind(hall.floorTexture, 'concrete');
  const frameKind = resolveKind(hall.frameStyle, 'brass');
  const accent = new THREE.Color(hall.accentColor || '#e11d48');

  const wallMat = make(wallKind, hall.wallColor, w, h);
  const carpet = make(floorKind, hall.floorColor, w, d);
  const ceilingMat = make('plain', 0x2b2430);
  const brass = new THREE.MeshStandardMaterial({ color: 0xc09349, roughness: 0.3, metalness: 0.8 });
  const steel = new THREE.MeshStandardMaterial({ color: 0x4a4f57, roughness: 0.4, metalness: 0.6 });
  const velvet = new THREE.MeshStandardMaterial({ color: 0x6d1f2e, roughness: 0.96 });
  const seatFabric = new THREE.MeshStandardMaterial({ color: 0x6a5462, roughness: 0.94 });
  const glassDark = new THREE.MeshPhysicalMaterial({
    color: 0x101720, transparent: true, opacity: 0.55, roughness: 0.1, depthWrite: false,
  });
  const lit = (color, strength = 1) => new THREE.MeshBasicMaterial({
    color: new THREE.Color(color).multiplyScalar(strength), toneMapped: false,
  });
  const rim = lit(0xfff1cf);
  const shell = new THREE.MeshStandardMaterial({ color: 0x2a2531, roughness: 0.55, metalness: 0.4 });

  scene.background = new THREE.Color(0x140f18);
  scene.fog = null;

  scene.add(new THREE.HemisphereLight(0x9a93b8, 0x3a2f3c, 1.15 * power));
  scene.add(new THREE.AmbientLight(0xffe8cf, 0.95 * power));
  const key = new THREE.DirectionalLight(0xffe2bb, 1.8 * power);
  key.position.set(w * 0.3, h + 7, d * 0.45);
  key.castShadow = true;
  key.shadow.mapSize.set(lowPower ? 1024 : 2048, lowPower ? 1024 : 2048);
  Object.assign(key.shadow.camera, { left: -w, right: w, top: d, bottom: -d, near: 0.5, far: 60 });
  key.shadow.bias = -0.0004;
  key.shadow.normalBias = 0.04;
  scene.add(key, key.target);
  // Warm pools over the concession and the seating keep the lobby from going flat.
  if (!lowPower) {
    const glowA = new THREE.PointLight(0xffd7a0, 34 * power, 18, 2);
    glowA.position.set(halfW - 3.5, 3.2, 3.0);
    const glowB = new THREE.PointLight(0xffd7a0, 26 * power, 17, 2);
    glowB.position.set(-3.0, 3.4, 1.0);
    const glowC = new THREE.PointLight(0xffe0b4, 22 * power, 16, 2);
    glowC.position.set(-halfW + 3.0, 3.4, 0);
    scene.add(glowA, glowB, glowC);
  }

  const floor = new THREE.Mesh(new THREE.PlaneGeometry(w, d), carpet);
  floor.rotation.x = -Math.PI / 2;
  floor.receiveShadow = true;
  scene.add(floor);
  // The shell must not cast shadows, or it would block the key light from outside.
  [
    box(w + 0.4, 0.24, d + 0.4, ceilingMat, 0, h + 0.12, 0),
    box(w, h, 0.22, wallMat, 0, h / 2, -halfD - 0.11),
    box(w, h, 0.22, wallMat, 0, h / 2, halfD + 0.11),
    box(0.22, h, d, wallMat, -halfW - 0.11, h / 2, 0),
    box(0.22, h, d, wallMat, halfW + 0.11, h / 2, 0),
  ].forEach(mesh => { mesh.castShadow = false; });
  [[-halfD, 0.12], [halfD, -0.12]].forEach(([z, offset]) => box(w, 0.16, 0.05, brass, 0, 0.08, z + offset));
  [-1, 1].forEach(sign => box(0.05, 0.16, d, brass, sign * (halfW - 0.02), 0.08, 0));

  // Cove lighting and downlights.
  [-1, 1].forEach(sign => {
    box(0.5, 0.35, d - 2, ceilingMat, sign * (halfW - 1.6), h - 0.3, 0);
    box(0.24, 0.07, d - 2.4, lit(0xffca7a), sign * (halfW - 1.35), h - 0.46, 0);
  });
  const lamps = [];
  for (let x = -halfW + 4; x <= halfW - 4; x += 4.4) {
    for (let z = -halfD + 4; z <= halfD - 3; z += 4.6) lamps.push({ x, y: h - 0.14, z });
  }
  instanced(new THREE.CylinderGeometry(0.26, 0.26, 0.07, 16), lit(0xfff0d2), lamps);

  // Two auditorium entrances with velvet drapes and lit signs.
  [-4.6, 4.6].forEach((x, index) => {
    box(3.5, 2.95, 0.3, brass, x, 1.45, -halfD + 0.16);
    box(3.0, 2.6, 0.12, lit(0x120c14), x, 1.3, -halfD + 0.3);
    [-1, 1].forEach(sign => box(0.55, 2.6, 0.26, velvet, x + sign * 1.3, 1.3, -halfD + 0.42));
    box(3.1, 0.3, 0.2, velvet, x, 2.72, -halfD + 0.42);
    const sign = new THREE.Mesh(
      new THREE.PlaneGeometry(1.5, 0.47),
      new THREE.MeshBasicMaterial({ map: labelTexture(`${index + 1}관`, 'NOW SHOWING', '#1b1420', '#ffd9a0') }),
    );
    sign.position.set(x, 3.25, -halfD + 0.33);
    scene.add(sign);
  });

  // Trailer screen bezel behind the big slot.
  box(7.9, 3.9, 0.24, shell, 0, 3.8, -halfD + 0.16);
  box(8.3, 4.3, 0.06, rim, 0, 3.8, -halfD + 0.06);

  // Entrance doors on the back wall.
  for (let i = -2; i <= 2; i += 1) {
    const x = i * 1.5;
    const pane = box(1.4, 2.8, 0.06, glassDark, x, 1.45, halfD - 0.14);
    pane.castShadow = false;
    box(0.09, 2.9, 0.12, brass, x - 0.72, 1.45, halfD - 0.17);
    box(0.06, 1.0, 0.06, brass, x + 0.45, 1.15, halfD - 0.24);
  }
  box(w * 0.55, 0.5, 0.14, brass, 0, 3.4, halfD - 0.16);

  // Concession counter along the right wall.
  const counterZ = 2.6, counterLen = 7.4;
  box(1.2, 1.12, counterLen, shell, halfW - 1.5, 0.56, counterZ);
  box(1.44, 0.1, counterLen + 0.3, brass, halfW - 1.45, 1.17, counterZ);
  box(0.12, 0.14, counterLen + 0.2, lit(0xff9a5c, 0.8), halfW - 2.1, 0.78, counterZ);
  box(0.5, 2.6, counterLen, shell, halfW - 0.45, 1.3, counterZ);
  ['팝콘', '음료', '콤보'].forEach((text, index) => {
    const menu = new THREE.Mesh(
      new THREE.PlaneGeometry(1.5, 0.47),
      new THREE.MeshBasicMaterial({ map: labelTexture(text, 'SNACK BAR', '#1b1420', '#ffd9a0') }),
    );
    menu.rotation.y = -Math.PI / 2;
    menu.position.set(halfW - 0.78, 2.35, counterZ - 2.3 + index * 2.3);
    scene.add(menu);
  });
  // Popcorn cabinet and cup stacks on the counter.
  const popX = halfW - 1.5, popZ = counterZ - 2.6;
  box(0.84, 0.1, 1.14, brass, popX, 1.25, popZ);
  box(0.84, 0.12, 1.14, brass, popX, 2.06, popZ);
  [-1, 1].forEach(sx => [-1, 1].forEach(sz => {
    box(0.06, 0.75, 0.06, brass, popX + sx * 0.39, 1.68, popZ + sz * 0.54);
  }));
  const kernelBox = new THREE.Mesh(
    new THREE.BoxGeometry(0.7, 0.52, 1.0),
    new THREE.MeshBasicMaterial({ color: 0xf6d88a, toneMapped: false }),
  );
  kernelBox.position.set(popX, 1.58, popZ);
  scene.add(kernelBox);
  const cups = [];
  [0, 1, 2].forEach(i => cups.push({ x: halfW - 1.5, y: 1.35, z: counterZ + 1.4 + i * 0.35 }));
  instanced(new THREE.CylinderGeometry(0.12, 0.09, 0.3, 12), make('plain', 0xd9483f), cups);
  blockers.push({
    minX: halfW - 2.6, maxX: halfW, minZ: counterZ - counterLen / 2 - 0.6, maxZ: counterZ + counterLen / 2 + 0.6,
  });

  // Ticket kiosks near the entrance.
  [-9.0, -6.6].forEach(x => {
    box(0.9, 1.5, 0.7, shell, x, 0.75, halfD - 3.2);
    const screen = new THREE.Mesh(new THREE.PlaneGeometry(0.66, 0.5), lit(0x2b6fd6, 0.9));
    screen.position.set(x, 1.22, halfD - 3.52);
    screen.rotation.x = -0.45;
    scene.add(screen);
    blockers.push({ minX: x - 0.9, maxX: x + 0.9, minZ: halfD - 4.0, maxZ: halfD - 2.4 });
  });

  // Waiting seats: two sofa rows facing a low table, plus a pair of benches.
  function sofa(x, z, rotY) {
    const group = new THREE.Group();
    group.position.set(x, 0, z);
    group.rotation.y = rotY;
    scene.add(group);
    box(2.6, 0.42, 0.9, seatFabric, 0, 0.26, 0, group);
    box(2.6, 0.62, 0.22, seatFabric, 0, 0.72, -0.34, group);
    [-1.2, 1.2].forEach(dx => box(0.2, 0.34, 0.9, seatFabric, dx, 0.6, 0, group));
    [-1.15, 1.15].forEach(dx => [-0.34, 0.34].forEach(dz => box(0.09, 0.2, 0.09, brass, dx, 0.1, dz, group)));
    blockers.push({ minX: x - 1.6, maxX: x + 1.6, minZ: z - 1.0, maxZ: z + 1.0 });
  }
  sofa(-3.2, -1.2, 0);
  sofa(-3.2, 2.6, Math.PI);
  box(2.0, 0.09, 0.9, brass, -3.2, 0.42, 0.7);
  [-0.85, 0.85].forEach(dx => box(0.1, 0.4, 0.1, steel, -3.2 + dx, 0.2, 0.7));
  blockers.push({ minX: -4.5, maxX: -1.9, minZ: 0.1, maxZ: 1.3 });
  sofa(3.4, 6.4, Math.PI);
  sofa(-8.6, 4.0, -Math.PI / 2);

  // Rope stanchions guiding the queue.
  const ropeLine = [-2.2, -0.4, 1.4, 3.2];
  ropeLine.forEach((z, index) => {
    const x = 8.2;
    box(0.26, 0.08, 0.26, brass, x, 0.04, z);
    const post = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.06, 0.95, 10), brass);
    post.position.set(x, 0.48, z);
    post.castShadow = true;
    scene.add(post);
    if (index < ropeLine.length - 1) {
      box(0.05, 0.05, ropeLine[index + 1] - z, velvet, x, 0.82, (z + ropeLine[index + 1]) / 2);
    }
  });

  // Every poster hangs in a lit case (the trailer screen has its own bezel).
  (layout.slots || []).forEach(slot => {
    if (slot.code === 'A1') return;
    const dir = new THREE.Vector3(Math.sin(slot.rotationY), 0, Math.cos(slot.rotationY));
    const group = new THREE.Group();
    group.position.set(slot.x, 0, slot.z).addScaledVector(dir, -0.045);
    group.rotation.y = slot.rotationY;
    scene.add(group);
    posterCase(box, group, slot, rim, shell);
  });

  // A carpet inlay in the accent colour ties the seating area together.
  const inlay = new THREE.Mesh(
    new THREE.PlaneGeometry(w - 8, d - 9),
    make('plain', accent.clone().multiplyScalar(0.45)),
  );
  inlay.rotation.x = -Math.PI / 2;
  inlay.position.y = 0.03;      // clear of the carpet below it
  inlay.receiveShadow = true;
  scene.add(inlay);

  return { blockers, frameMaterial: frameMaterialFor(frameKind, make) };
}

// Home theatre: one recliner facing a big screen, fabric panels, cove light and
// picture lights over the framed works on the side walls.
function buildTheater(scene, renderer, hall, layout, lowPower) {
  const make = materialLibrary(renderer);
  const box = boxFactory(scene);
  const instanced = instancedFactory(scene);
  const w = layout.size.width, d = layout.size.depth, h = layout.size.height;
  const halfW = w / 2, halfD = d / 2;
  const power = hall.lightIntensity || 1;
  const blockers = [];
  const wallKind = resolveKind(hall.wallTexture, 'plaster');
  const floorKind = resolveKind(hall.floorTexture, 'concrete');
  const frameKind = resolveKind(hall.frameStyle, 'brass');

  const panel = make(wallKind, hall.wallColor, 2, h);
  const carpet = make(floorKind, hall.floorColor, w, d);
  const walnut = make('walnut', 0x5a3a26, 2, 1);
  const gold = new THREE.MeshStandardMaterial({ color: 0xc9a227, roughness: 0.28, metalness: 0.85 });
  const leather = new THREE.MeshStandardMaterial({ color: 0x4a2f28, roughness: 0.55 });
  const blackout = new THREE.MeshStandardMaterial({ color: 0x0d0f14, roughness: 0.95 });
  const drape = new THREE.MeshStandardMaterial({ color: 0x5b1622, roughness: 0.96 });
  const lit = (color, strength = 1) => new THREE.MeshBasicMaterial({
    color: new THREE.Color(color).multiplyScalar(strength), toneMapped: false,
  });

  scene.background = new THREE.Color(0x0a0c11);
  scene.fog = null;

  scene.add(new THREE.HemisphereLight(0x8d97ad, 0x2a231e, 1.0 * power));
  scene.add(new THREE.AmbientLight(0xffe4c4, 0.95 * power));
  const key = new THREE.DirectionalLight(0xffdcae, 1.5 * power);
  key.position.set(w, h + 5, d * 0.6);
  key.castShadow = true;
  key.shadow.mapSize.set(lowPower ? 1024 : 2048, lowPower ? 1024 : 2048);
  Object.assign(key.shadow.camera, { left: -w, right: w, top: d, bottom: -d, near: 0.5, far: 40 });
  key.shadow.bias = -0.0004;
  key.shadow.normalBias = 0.03;
  scene.add(key, key.target);
  if (!lowPower) {
    const wash = new THREE.PointLight(0xffc98a, 20 * power, 11, 2);
    wash.position.set(0, 2.6, 3.0);
    const glow = new THREE.PointLight(0xffd9a8, 12 * power, 9, 2);
    glow.position.set(0, 2.3, -1.6);
    scene.add(wash, glow);
  }

  const floor = new THREE.Mesh(new THREE.PlaneGeometry(w, d), carpet);
  floor.rotation.x = -Math.PI / 2;
  floor.receiveShadow = true;
  scene.add(floor);
  // The shell must not cast shadows, or it would block the key light from outside.
  [
    box(w, h, 0.22, blackout, 0, h / 2, -halfD - 0.11),
    box(w, h, 0.22, panel, 0, h / 2, halfD + 0.11),
    box(0.22, h, d, panel, -halfW - 0.11, h / 2, 0),
    box(0.22, h, d, panel, halfW + 0.11, h / 2, 0),
    // Coffered ceiling with a warm cove and a starlit centre.
    box(w + 0.4, 0.24, d + 0.4, make('plain', 0x14171f), 0, h + 0.12, 0),
  ].forEach(mesh => { mesh.castShadow = false; });
  box(w - 1.6, 0.16, d - 1.6, make('plain', 0x1b202b), 0, h - 0.08, 0);
  [-1, 1].forEach(sign => {
    box(0.7, 0.3, d - 0.4, walnut, sign * (halfW - 0.35), h - 0.15, 0);
    box(0.1, 0.05, d - 1.2, lit(0xffbe74), sign * (halfW - 0.72), h - 0.27, 0);
    box(w - 1.4, 0.3, 0.7, walnut, 0, h - 0.15, sign * (halfD - 0.35));
  });
  if (!lowPower) {
    const stars = [];
    let seed = 991;
    const random = () => ((seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0) / 4294967296);
    for (let i = 0; i < 48; i += 1) {
      stars.push({ x: (random() - 0.5) * (w - 2.4), y: h - 0.17, z: (random() - 0.5) * (d - 2.4) });
    }
    instanced(new THREE.SphereGeometry(0.022, 6, 4), lit(0xdff0ff), stars);
  }

  // Fabric acoustic panels with walnut battens down both side walls.
  for (let z = -halfD + 1.0; z <= halfD - 1.0; z += 1.35) {
    [-1, 1].forEach(sign => {
      box(0.07, 2.1, 1.05, panel, sign * (halfW - 0.05), 1.25, z);
      box(0.1, 2.3, 0.09, walnut, sign * (halfW - 0.06), 1.25, z + 0.66);
    });
  }
  [-1, 1].forEach(sign => box(0.14, 0.18, d, walnut, sign * (halfW - 0.06), 0.09, 0));

  // Screen wall: bezel, masking drapes and a pelmet.
  box(7.0, 3.0, 0.16, blackout, 0, 1.78, -halfD + 0.12);
  box(6.4, 2.72, 0.06, make('plain', 0x101318), 0, 1.78, -halfD + 0.2);
  [-1, 1].forEach(sign => box(0.85, 2.95, 0.3, drape, sign * 3.6, 1.6, -halfD + 0.3));
  box(w - 0.6, 0.34, 0.34, drape, 0, 3.06, -halfD + 0.3);   // clear of the cove below
  // Tower speakers either side of the screen.
  [-1, 1].forEach(sign => {
    const x = sign * 3.8;
    box(0.42, 1.25, 0.36, blackout, x, 0.63, -halfD + 0.75);
    [0.45, 0.85, 1.12].forEach(y => {
      const driver = new THREE.Mesh(new THREE.CylinderGeometry(0.11, 0.11, 0.03, 14), make('plain', 0x2b2f38));
      driver.rotation.x = Math.PI / 2;
      driver.position.set(x, y, -halfD + 0.57);
      scene.add(driver);
    });
    blockers.push({ minX: x - 0.6, maxX: x + 0.6, minZ: -halfD, maxZ: -halfD + 1.4 });
  });
  // Surround speakers up on the side walls.
  [-1, 1].forEach(sign => box(0.3, 0.42, 0.3, blackout, sign * (halfW - 0.24), 2.25, 1.6));

  // The single recliner on its riser, with a side table.
  // The backrest stays below eye level so the screen is never hidden behind it.
  const seatZ = 1.6;
  box(3.2, 0.12, 2.8, make('plain', 0x2f2622), 0, 0.06, seatZ + 0.35);
  const chair = new THREE.Group();
  chair.position.set(0, 0.12, seatZ);
  scene.add(chair);
  // The screen is at -Z, so the backrest goes behind the sitter (+Z) and the
  // footrest in front of them (-Z).
  box(1.02, 0.28, 0.92, leather, 0, 0.4, 0, chair);
  const backRest = box(1.02, 0.72, 0.22, leather, 0, 0.78, 0.46, chair);
  backRest.rotation.x = -0.18;
  box(0.9, 0.2, 0.26, leather, 0, 0.58, -0.56, chair);
  [-1, 1].forEach(sign => {
    box(0.19, 0.3, 0.95, leather, sign * 0.61, 0.6, 0.02, chair);
    const holder = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.06, 0.05, 12), make('plain', 0x1d1a1a));
    holder.position.set(sign * 0.61, 0.76, -0.16);
    chair.add(holder);
  });
  box(0.94, 0.26, 0.8, blackout, 0, 0.13, -0.02, chair);
  blockers.push({ minX: -1.8, maxX: 1.8, minZ: seatZ - 1.4, maxZ: seatZ + 1.8 });
  const table = new THREE.Mesh(new THREE.CylinderGeometry(0.34, 0.3, 0.06, 20), walnut);
  table.position.set(1.42, 0.72, seatZ - 0.1);
  table.castShadow = true;
  scene.add(table);
  box(0.09, 0.5, 0.09, gold, 1.42, 0.47, seatZ - 0.1);
  const glass = new THREE.Mesh(
    new THREE.CylinderGeometry(0.06, 0.05, 0.14, 12),
    new THREE.MeshPhysicalMaterial({ color: 0xc7a55f, transparent: true, opacity: 0.72, roughness: 0.1 }),
  );
  glass.position.set(1.42, 0.82, seatZ - 0.1);
  scene.add(glass);

  // Projector above and behind the seat.
  box(0.5, 0.2, 0.7, make('plain', 0x24282f), 0, h - 0.4, halfD - 2.0);
  const lens = new THREE.Mesh(new THREE.CylinderGeometry(0.07, 0.07, 0.06, 14), lit(0xbcd8ff, 0.8));
  lens.rotation.x = Math.PI / 2;
  lens.position.set(0, h - 0.42, halfD - 2.36);
  scene.add(lens);

  // Door at the back, with a sconce beside it.
  // Back wall, so the door is wide across X and thin through Z.
  box(1.45, 2.4, 0.16, walnut, 0, 1.2, halfD - 0.06);
  box(1.18, 2.18, 0.1, make('plain', 0x4a3527), 0, 1.09, halfD - 0.16);
  const handle = new THREE.Mesh(new THREE.SphereGeometry(0.055, 12, 8), gold);
  handle.position.set(-0.46, 1.05, halfD - 0.24);
  scene.add(handle);
  [-1, 1].forEach(sign => {
    box(0.18, 0.5, 0.18, gold, sign * 1.5, 1.85, halfD - 0.16);
    const shade = new THREE.Mesh(new THREE.CylinderGeometry(0.15, 0.11, 0.26, 14), lit(0xffd39a, 0.85));
    shade.position.set(sign * 1.5, 2.16, halfD - 0.16);
    scene.add(shade);
  });

  // Picture light above every framed work.
  (layout.slots || []).forEach(slot => {
    if (slot.frameless) return;
    const group = new THREE.Group();
    group.position.set(slot.x, 0, slot.z);
    group.rotation.y = slot.rotationY;
    scene.add(group);
    // A mounting panel deep enough to reach the wall, so the fabric panels and
    // battens behind it can never poke through the picture.
    // 액자 테두리를 절반으로 가늘게 했으므로 받침판도 같이 줄여 액자 밖으로 삐져나오지 않게 한다.
    box(slot.width + 0.2, slot.height + 0.2, 0.17, make('plain', 0x2a3140), 0, slot.y, -0.155, group);
    box(0.5, 0.09, 0.16, gold, 0, slot.y + slot.height / 2 + 0.34, 0.1, group);
    box(0.44, 0.03, 0.1, lit(0xffe2b0, 0.9), 0, slot.y + slot.height / 2 + 0.3, 0.12, group);
  });

  return { blockers, frameMaterial: frameMaterialFor(frameKind, make) };
}


// 저녁 하늘: 해가 막 넘어간 도시 위 하늘과 별.
function duskSkyTexture() {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 512;
  const ctx = canvas.getContext('2d');
  const gradient = ctx.createLinearGradient(0, 0, 0, 512);
  gradient.addColorStop(0, '#101a36');
  gradient.addColorStop(0.34, '#2d3566');
  gradient.addColorStop(0.62, '#7c5686');
  gradient.addColorStop(0.82, '#d9805e');
  gradient.addColorStop(1, '#f3b57a');
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 512, 512);
  let seed = 51237;
  const random = () => ((seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0) / 4294967296);
  // 별은 윗쪽에만 뿌린다(아래는 노을과 도시 불빛에 묻힌다).
  for (let i = 0; i < 170; i += 1) {
    const y = random() * 250;
    ctx.fillStyle = `rgba(255,255,245,${(0.25 + random() * 0.6) * (1 - y / 320)})`;
    const size = random() > 0.9 ? 2.4 : 1.4;
    ctx.fillRect(random() * 512, y, size, size);
  }
  // 노을에 물든 얇은 구름띠.
  for (let i = 0; i < 9; i += 1) {
    ctx.fillStyle = `rgba(255,${170 + Math.floor(random() * 50)},${140 + Math.floor(random() * 60)},${0.1 + random() * 0.2})`;
    ctx.beginPath();
    ctx.ellipse(random() * 512, 300 + random() * 150, 60 + random() * 150, 6 + random() * 12, 0, 0, Math.PI * 2);
    ctx.fill();
  }
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

// 창문에 불이 들어온 건물 외벽. 건너편 건물과 멀리 보이는 도시에 함께 쓴다.
function cityWindowTexture() {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 256;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#222a38';
  ctx.fillRect(0, 0, 256, 256);
  let seed = 8831;
  const random = () => ((seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0) / 4294967296);
  for (let row = 0; row < 16; row += 1) {
    for (let col = 0; col < 12; col += 1) {
      ctx.fillStyle = random() > 0.52
        ? `rgba(255,${196 + Math.floor(random() * 46)},${132 + Math.floor(random() * 80)},${0.55 + random() * 0.45})`
        : 'rgba(44,54,70,.95)';
      ctx.fillRect(col * 21 + 5, row * 16 + 4, 13, 9);
    }
  }
  const texture = new THREE.CanvasTexture(canvas);
  texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

// 담벼락 그라피티. 글자 대신 스프레이 자국과 굵은 획으로 그려 글꼴에 기대지 않는다.
function graffitiTexture(seedValue) {
  const canvas = document.createElement('canvas');
  canvas.width = 512;
  canvas.height = 256;
  const ctx = canvas.getContext('2d');
  let seed = (seedValue >>> 0) || 1;
  const random = () => ((seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0) / 4294967296);
  const palette = ['#f43f5e', '#fb923c', '#facc15', '#22d3ee', '#a855f7', '#4ade80'];
  const pick = () => palette[Math.floor(random() * palette.length)];
  // 번져 나간 스프레이 자국
  for (let i = 0; i < 7; i += 1) {
    ctx.globalAlpha = 0.16 + random() * 0.2;
    ctx.fillStyle = pick();
    ctx.beginPath();
    ctx.ellipse(random() * 512, random() * 256, 40 + random() * 110, 26 + random() * 64,
      random() * Math.PI, 0, Math.PI * 2);
    ctx.fill();
  }
  // 굵은 획
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  for (let stroke = 0; stroke < 5; stroke += 1) {
    ctx.globalAlpha = 0.85 + random() * 0.15;
    ctx.strokeStyle = pick();
    ctx.lineWidth = 13 + random() * 20;
    ctx.beginPath();
    let x = 26 + random() * 90;
    let y = 50 + random() * 150;
    ctx.moveTo(x, y);
    for (let segment = 0; segment < 4; segment += 1) {
      const nx = x + 60 + random() * 80;
      const ny = 42 + random() * 170;
      ctx.quadraticCurveTo(x + 24, y + (random() - 0.5) * 150, nx, ny);
      x = nx;
      y = ny;
    }
    ctx.stroke();
  }
  // 흘러내린 자국
  ctx.globalAlpha = 0.75;
  ctx.strokeStyle = '#fdf6ec';
  ctx.lineWidth = 3.5;
  for (let i = 0; i < 6; i += 1) {
    const x = 40 + random() * 430;
    const y = 60 + random() * 120;
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x + (random() - 0.5) * 60, y + 30 + random() * 70);
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

// 벽에 덧붙은 전단. 글자는 캔버스로 직접 그려 글꼴 파일을 받지 않는다.
function streetPoster(scene, title, subtitle, background, color, width, height, x, y, z, rotY) {
  const poster = new THREE.Mesh(
    new THREE.PlaneGeometry(width, height),
    new THREE.MeshStandardMaterial({ map: labelTexture(title, subtitle, background, color), roughness: 0.94 }),
  );
  poster.position.set(x, y, z);
  poster.rotation.y = rotY;
  scene.add(poster);
  return poster;
}

// 전시 자리에 맞춘 빈 그룹. 원점이 작품 한가운데이고 -Z 쪽이 벽(또는 바닥) 안쪽이다.
// 바닥에 눕힌 자리(tilt)도 같은 방법으로 다룰 수 있다.
function slotGroup(scene, slot) {
  const group = new THREE.Group();
  group.position.set(slot.x, slot.y, slot.z);
  group.rotation.set(slot.tilt || 0, slot.rotationY, 0, 'YXZ');
  scene.add(group);
  return group;
}

// 자리 방향에 맞춘 대략적인 가로·세로 점유 범위(이동 막이를 만들 때 쓴다).
function slotBlocker(slot, halfWidth, halfDepth) {
  const spanX = Math.abs(Math.cos(slot.rotationY)) * halfWidth + Math.abs(Math.sin(slot.rotationY)) * halfDepth;
  const spanZ = Math.abs(Math.sin(slot.rotationY)) * halfWidth + Math.abs(Math.cos(slot.rotationY)) * halfDepth;
  return { minX: slot.x - spanX, maxX: slot.x + spanX, minZ: slot.z - spanZ, maxZ: slot.z + spanZ };
}


// ── 전시관7 · 버스정류장형 ───────────────────────────────────────────────
// 사진 재질과 HDR 하늘로 꾸민 길가. 유리 부스 대기실, 인도에 늘어선 전시봉,
// 셔터를 내린 가게와 그 위의 그라피티, 차도 건너편 건물까지 한 장면이다.
function buildBusStop(scene, renderer, hall, layout, lowPower) {
  const photos = photographicMaterials(renderer, materialLibrary(renderer));
  const make = photos.make;
  const box = boxFactory(scene);
  const instanced = instancedFactory(scene);
  const w = layout.size.width, d = layout.size.depth;
  const halfW = w / 2, halfD = d / 2;
  const power = hall.lightIntensity || 1;
  const blockers = [];
  // 재질을 'auto'로 둔 전시관은 사진에 찍힌 색 그대로 보여 주고,
  // 관리자가 직접 고른 경우에만 저장된 색을 덧입힌다.
  const picked = value => value && value !== 'auto';
  const floorKind = resolveKind(hall.floorTexture, 'paving');
  const floorTint = picked(hall.floorTexture) ? hall.floorColor : 0xffffff;
  const wallKind = resolveKind(hall.wallTexture, 'streetwall');
  const wallTint = picked(hall.wallTexture) ? hall.wallColor : 0xd8d2c2;
  const frameKind = resolveKind(hall.frameStyle, 'black');

  const kerbZ = -4.4;            // 인도와 차도의 경계
  const shopZ = 6.4;             // 가게 앞면
  const backZ = 1.8;             // 유리 부스 뒷유리
  const sideX = 5.5;             // 유리 부스 양옆 유리
  const roofY = 2.8;
  const kerbH = 0.16;            // 인도가 차도보다 높은 만큼
  const roadZ = -9.5;            // 차도 한가운데(중앙선)
  const farWalkZ = -14.6;        // 건너편 인도

  const paving = make(floorKind, floorTint, 130, shopZ - kerbZ);
  const asphalt = make('asphalt', 0xb6b6b6, 280, 280);
  const granite = make('limestone', 0xd3cec2, 10, 1);
  const shopWall = make(wallKind, wallTint, 130, 11);
  const farWall = make('streetwall', 0xc3bdae, 130, 11);
  const shutter = make('shutter', 0xb2b6bc, 6.4, 2.6);
  const timber = make('oak', 0xb98b5a, 2, 1);
  const steel = new THREE.MeshStandardMaterial({
    color: 0x9aa1a9, roughness: 0.33, metalness: 0.78, envMapIntensity: 0.9,
  });
  const dark = new THREE.MeshStandardMaterial({
    color: 0x353c45, roughness: 0.48, metalness: 0.45, envMapIntensity: 0.7,
  });
  const rubber = new THREE.MeshStandardMaterial({ color: 0x14171b, roughness: 0.93 });
  const paint = new THREE.MeshStandardMaterial({ color: 0xe9e7df, roughness: 0.72 });
  const yellow = new THREE.MeshStandardMaterial({ color: 0xd9ae2a, roughness: 0.78 });
  // 부스 유리. 뒤가 비쳐야 하므로 거의 투명하게 두고, 하늘이 비치는 반사만 남긴다.
  const glass = new THREE.MeshPhysicalMaterial({
    color: 0xe4f1f6, transparent: true, opacity: 0.13, roughness: 0.03,
    metalness: 0, depthWrite: false, envMapIntensity: 1.4,
  });
  const tinted = new THREE.MeshStandardMaterial({
    color: 0x2a4256, roughness: 0.16, metalness: 0.55, envMapIntensity: 1.1,
  });
  const shell = new THREE.MeshStandardMaterial({ color: 0x2b303a, roughness: 0.55, metalness: 0.4 });
  const leaf = new THREE.MeshStandardMaterial({ color: 0x53783f, roughness: 0.95 });
  const lit = (color, strength = 1) => new THREE.MeshBasicMaterial({
    color: new THREE.Color(color).multiplyScalar(strength), toneMapped: false,
  });
  const rim = lit(0xfff1cf);

  // 하늘은 사진 한 장을 돔에 두르고, 같은 하늘의 HDR로 빛과 반사를 만든다.
  scene.background = new THREE.Color(0x9fb4cf);
  scene.fog = new THREE.Fog(0xa9b2c0, 55, 150);
  const sky = photographicStreetSky(scene, renderer, lowPower);

  // 맑은 날 한낮. 하늘 전체가 내는 빛은 HDR이 맡으므로 반구광은 HDR을 읽지
  // 못했을 때를 대비한 몫만 남긴다. 태양은 하늘 사진에 찍힌 해와 같은 자리
  // (방위 -117°, 고도 48°)에 두어 그림자가 눈에 보이는 해와 어긋나지 않게 한다.
  scene.add(new THREE.HemisphereLight(0xcfe1f7, 0x6f6a5e, 0.42 * power));
  scene.add(new THREE.AmbientLight(0xffffff, 0.1 * power));
  const sun = new THREE.DirectionalLight(0xfff1d6, 3.2 * power);
  sun.position.set(-10.5, 26.0, -20.8).setLength(62);
  sun.target.position.set(0, 1.2, -1.5);
  sun.castShadow = true;
  sun.shadow.mapSize.set(lowPower ? 1024 : 2048, lowPower ? 1024 : 2048);
  Object.assign(sun.shadow.camera, {
    left: -24, right: 24, top: 24, bottom: -24, near: 20, far: 115,
  });
  sun.shadow.bias = -0.0004;
  sun.shadow.normalBias = 0.04;
  scene.add(sun, sun.target);

  // ── 차도 · 인도 · 연석 ─────────────────────────────────────────────────
  // 차도는 안개에 잠길 만큼 넓게 깔아 하늘 돔과 맞닿는 자리를 감춘다.
  const road = new THREE.Mesh(new THREE.PlaneGeometry(280, 280), asphalt);
  road.name = 'street-road';
  road.rotation.x = -Math.PI / 2;
  road.position.y = -kerbH;
  road.receiveShadow = true;
  scene.add(road);
  const walk = new THREE.Mesh(new THREE.PlaneGeometry(130, shopZ - kerbZ), paving);
  walk.name = 'street-walk';
  walk.rotation.x = -Math.PI / 2;
  walk.position.set(0, 0, (shopZ + kerbZ) / 2);
  walk.receiveShadow = true;
  scene.add(walk);
  box(130, kerbH + 0.02, 0.42, granite, 0, -kerbH / 2 + 0.01, kerbZ - 0.21);
  // 점자블록 한 줄
  const tactile = [];
  for (let x = -26; x <= 26; x += 0.62) tactile.push({ x, y: 0.015, z: kerbZ + 0.95 });
  instanced(new THREE.BoxGeometry(0.56, 0.03, 0.56), yellow, tactile);

  // 맨홀과 빗물받이. 아스팔트 한 장만 깔아 두면 바닥이 너무 말끔해 보인다.
  instanced(new THREE.CylinderGeometry(0.34, 0.34, 0.03, 20), dark, [
    { x: -14.2, y: -kerbH + 0.012, z: -7.0 },
    { x: 4.6, y: -kerbH + 0.012, z: -11.8 },
    { x: 21.5, y: -kerbH + 0.012, z: -6.4 },
    { x: -8.0, y: 0.012, z: shopZ - 1.1 },
  ]);
  const drains = [];
  for (let x = -24; x <= 24; x += 12) drains.push({ x, y: -kerbH + 0.012, z: kerbZ - 0.55 });
  instanced(new THREE.BoxGeometry(0.9, 0.03, 0.42), dark, drains);

  // ── 차선 ───────────────────────────────────────────────────────────────
  // 노면 표시는 바닥에 얇게 붙는 조각이라 그림자를 만들지 않는다.
  const markings = (geometry, material, items) => {
    const mesh = instanced(geometry, material, items);
    if (mesh) mesh.castShadow = false;
  };
  const centre = [];
  for (const dz of [-0.16, 0.16]) {
    for (let x = -60; x <= 60; x += 20) centre.push({ x, y: -kerbH + 0.014, z: roadZ + dz });
  }
  markings(new THREE.BoxGeometry(19.4, 0.02, 0.14), yellow, centre);
  const edge = [];
  for (let x = -60; x <= 60; x += 20) edge.push({ x, y: -kerbH + 0.014, z: kerbZ - 0.95 });
  markings(new THREE.BoxGeometry(19.4, 0.02, 0.15), paint, edge);
  const lane = [];
  for (let x = -60; x <= 60; x += 6) lane.push({ x, y: -kerbH + 0.014, z: -6.9 });
  markings(new THREE.BoxGeometry(3.0, 0.02, 0.15), paint, lane);
  const crossing = [];
  for (let z = -13.9; z <= -4.9; z += 1.1) crossing.push({ x: -20.5, y: -kerbH + 0.014, z });
  markings(new THREE.BoxGeometry(3.4, 0.02, 0.62), paint, crossing);
  markings(new THREE.BoxGeometry(0.4, 0.02, 4.7), paint,
    [{ x: -18.3, y: -kerbH + 0.014, z: kerbZ - 2.5 }]);

  // ── 유리 부스 대기실 ───────────────────────────────────────────────────
  const backGlass = box(sideX * 2, 2.3, 0.05, glass, 0, 1.3, backZ);
  backGlass.castShadow = false;
  [-1, 1].forEach(sign => {
    const pane = box(0.05, 2.3, 3.0, glass, sign * sideX, 1.3, backZ - 1.5);
    pane.castShadow = false;
    box(0.11, roofY, 0.11, steel, sign * sideX, roofY / 2, backZ);
    box(0.11, roofY, 0.11, steel, sign * sideX, roofY / 2, backZ - 3.0);
    box(0.18, 0.1, 0.18, dark, sign * sideX, 0.05, backZ);
    box(0.18, 0.1, 0.18, dark, sign * sideX, 0.05, backZ - 3.0);
    blockers.push({
      minX: sign * sideX - 0.2, maxX: sign * sideX + 0.2, minZ: backZ - 3.1, maxZ: backZ + 0.15,
    });
  });
  box(sideX * 2 + 0.22, 0.13, 0.13, steel, 0, 2.5, backZ);
  box(sideX * 2 + 0.7, 0.16, 4.0, make('plain', 0xdfe4e8), 0, roofY, backZ - 1.6);
  box(sideX * 2 + 0.74, 0.06, 4.04, steel, 0, roofY + 0.1, backZ - 1.6);
  box(sideX * 2 + 0.8, 0.36, 0.16, dark, 0, roofY + 0.22, backZ - 3.62);
  blockers.push({ minX: -sideX - 0.2, maxX: sideX + 0.2, minZ: backZ - 0.14, maxZ: backZ + 0.2 });
  const stopSign = new THREE.Mesh(
    new THREE.PlaneGeometry(3.6, 0.52),
    new THREE.MeshBasicMaterial({ map: labelTexture('새담 정류장', 'SAEDAM BUS STOP', '#12243c', '#ffd9a0') }),
  );
  stopSign.rotation.y = Math.PI;
  stopSign.position.set(0, roofY + 0.22, backZ - 3.71);
  scene.add(stopSign);

  // 부스 안 대기 의자
  const benchZ = backZ - 0.58;
  [-2.7, 0, 2.7].forEach(x => box(0.1, 0.44, 0.48, steel, x, 0.22, benchZ));
  box(5.8, 0.07, 0.46, timber, 0, 0.47, benchZ);
  box(5.8, 0.07, 0.2, timber, 0, 0.74, benchZ + 0.22);
  blockers.push({ minX: -3.0, maxX: 3.0, minZ: benchZ - 0.4, maxZ: backZ });

  // 노선도와 쓰레기통
  const routeBoard = box(1.5, 0.95, 0.07, shell, -3.4, 2.02, backZ - 0.06);
  routeBoard.castShadow = false;
  const route = new THREE.Mesh(
    new THREE.PlaneGeometry(1.36, 0.82),
    new THREE.MeshBasicMaterial({ map: labelTexture('노선 안내', '새담선 · 05-142', '#f7f5ef', '#1f2937') }),
  );
  route.rotation.y = Math.PI;
  route.position.set(-3.4, 2.02, backZ - 0.11);
  scene.add(route);
  const bin = new THREE.Mesh(new THREE.CylinderGeometry(0.31, 0.26, 0.82, 16), dark);
  bin.position.set(6.6, 0.41, 2.2);
  bin.castShadow = true;
  scene.add(bin);
  box(0.72, 0.05, 0.72, steel, 6.6, 0.85, 2.2);
  blockers.push({ minX: 6.2, maxX: 7.0, minZ: 1.8, maxZ: 2.6 });

  // 정류장 표지봉
  const signPole = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.075, 3.3, 12), steel);
  signPole.position.set(-6.9, 1.65, -3.7);
  signPole.castShadow = true;
  scene.add(signPole);
  box(1.24, 1.0, 0.08, dark, -6.9, 3.15, -3.7);
  [-1, 1].forEach(sign => {
    const plate = new THREE.Mesh(
      new THREE.PlaneGeometry(1.12, 0.86),
      new THREE.MeshBasicMaterial({ map: labelTexture('버스', '05-142', '#1d4ed8', '#ffffff') }),
    );
    plate.rotation.y = sign > 0 ? 0 : Math.PI;
    plate.position.set(-6.9, 3.15, -3.7 + sign * 0.05);
    scene.add(plate);
  });
  blockers.push({ minX: -7.2, maxX: -6.6, minZ: -4.0, maxZ: -3.4 });

  // 연석 위 볼라드. 승차 자리와 횡단보도 앞은 비워 둔다.
  const bollards = [];
  for (let x = -25; x <= 25; x += 2.4) {
    if (Math.abs(x) < 7.2 || Math.abs(x + 20.5) < 3.2) continue;
    bollards.push({ x, y: 0.26, z: kerbZ + 0.22 });
  }
  instanced(new THREE.CylinderGeometry(0.075, 0.09, 0.52, 10), steel, bollards);
  instanced(new THREE.CylinderGeometry(0.079, 0.079, 0.07, 10), lit(0xf0c93f, 0.55),
    bollards.map(item => ({ ...item, y: 0.4 })));

  // ── 가게 앞줄 ──────────────────────────────────────────────────────────
  const shops = [['분식', 'SNACK', false], ['카페', 'COFFEE', false], ['꽃집', 'FLOWER', true],
    ['문구', 'STATIONERY', false], ['약국', 'PHARMACY', true]];
  box(130, 11.0, 2.6, shopWall, 0, 5.5, shopZ + 1.3);
  box(130, 0.4, 3.0, make('plain', 0xb7b1a4), 0, 11.1, shopZ + 1.5);
  box(130, 0.26, 0.34, make('plain', 0xcdc7ba), 0, 3.95, shopZ - 0.12);   // 1층과 2층을 가르는 띠
  shops.forEach(([name, sub, closed], index) => {
    const x = -18 + index * 9;
    if (closed) {
      // 셔터를 내린 가게. 그라피티가 얹히는 자리이기도 하다.
      box(6.2, 2.5, 0.12, shutter, x, 1.35, shopZ - 0.06);
      box(6.3, 0.14, 0.2, steel, x, 2.66, shopZ - 0.06);
      box(6.3, 0.1, 0.18, dark, x, 0.12, shopZ - 0.06);
    } else {
      const front = box(6.2, 2.5, 0.12, tinted, x, 1.35, shopZ - 0.06);
      front.castShadow = false;
      [-2.1, 0, 2.1].forEach(dx => box(0.09, 2.5, 0.16, make('plain', 0xd8d4cb), x + dx, 1.35, shopZ - 0.1));
      box(6.3, 0.12, 0.22, make('plain', 0xd8d4cb), x, 2.62, shopZ - 0.1);
    }
    box(6.6, 0.5, 0.26, make('plain', 0xf2efe6), x, 3.05, shopZ - 0.1);
    box(6.4, 0.16, 1.5, make('plain', index % 2 ? 0xa8544d : 0x44715f), x, 3.62, shopZ - 0.78);
    [-1, 1].forEach(sign => box(0.08, 0.72, 0.08, steel, x + sign * 3.0, 3.3, shopZ - 1.45));
    const sign = new THREE.Mesh(
      new THREE.PlaneGeometry(2.9, 0.44),
      new THREE.MeshBasicMaterial({ map: labelTexture(name, sub, '#1f2937', '#ffe6a8') }),
    );
    sign.rotation.y = Math.PI;
    sign.position.set(x, 3.05, shopZ - 0.24);
    scene.add(sign);
    // 가게마다 하나씩 튀어나온 세로 간판
    const blade = box(0.14, 1.9, 0.62, shell, x + 3.4, 5.3, shopZ - 0.45);
    blade.castShadow = false;
    box(0.02, 1.7, 0.5, lit(0xf6efdf, 0.5), x + 3.31, 5.3, shopZ - 0.45);
  });

  // 2 · 3층 창과 실외기, 그리고 벽을 타고 내려오는 빗물받이
  const upperGlass = [], upperFrame = [], condenser = [], grille = [], pipes = [];
  for (let level = 0; level < 2; level += 1) {
    const y = 5.5 + level * 3.0;
    for (let x = -40.5; x <= 40.5; x += 4.5) {
      // 유리가 창틀보다 앞에 와야 창틀이 유리를 덮어 백지장이 되지 않는다.
      upperGlass.push({ x, y, z: shopZ - 0.09 });
      upperFrame.push({ x, y, z: shopZ - 0.03 });
      if ((Math.round(x / 4.5) + level) % 3 === 0) {
        condenser.push({ x: x + 1.55, y: y - 0.75, z: shopZ - 0.28 });
        grille.push({ x: x + 1.55, y: y - 0.75, z: shopZ - 0.46 });
      }
    }
  }
  for (let x = -38.25; x <= 38.25; x += 9) pipes.push({ x, y: 5.4, z: shopZ - 0.16 });
  instanced(new THREE.BoxGeometry(2.3, 1.7, 0.06), tinted, upperGlass);
  instanced(new THREE.BoxGeometry(2.5, 1.9, 0.08), make('plain', 0xb6b0a3), upperFrame);
  instanced(new THREE.BoxGeometry(0.82, 0.62, 0.34), make('plain', 0xd5d2cb), condenser);
  const fan = new THREE.CylinderGeometry(0.22, 0.22, 0.04, 14);
  fan.rotateX(Math.PI / 2);
  instanced(fan, dark, grille);
  instanced(new THREE.CylinderGeometry(0.07, 0.07, 10.4, 8), make('plain', 0xb9b4a9), pipes);

  // 가게 앞과 건너편 건물 아래에 때를 입힌다. 바닥과 벽이 맞닿은 선이 누그러진다.
  const grime = new THREE.MeshStandardMaterial({
    color: 0x33302a, roughness: 1, transparent: true, depthWrite: false,
    alphaMap: grimeTexture(), envMapIntensity: 0.3,
  });
  grime.alphaMap.repeat.set(26, 1);
  const shopGrime = new THREE.Mesh(new THREE.PlaneGeometry(130, 1.5), grime);
  shopGrime.rotation.y = Math.PI;
  shopGrime.position.set(0, 0.75, shopZ - 0.13);
  scene.add(shopGrime);

  // 꽃집 앞 화분과 쌓아 둔 상자. 사람이 쓰는 길로 보이게 하는 자잘한 것들.
  const pots = [{ x: 3.6, z: shopZ - 0.55 }, { x: 4.3, z: shopZ - 0.52 }, { x: -13.6, z: shopZ - 0.5 }];
  instanced(new THREE.CylinderGeometry(0.22, 0.17, 0.34, 12), make('plain', 0xa4573c),
    pots.map(pot => ({ ...pot, y: 0.17 })));
  instanced(new THREE.IcosahedronGeometry(0.26, 1), leaf,
    pots.map(pot => ({ ...pot, y: 0.45, scaleY: 0.72 })));
  const crates = [
    { x: -21.4, y: 0.16, z: shopZ - 0.62, rotY: 0.12 },
    { x: -21.4, y: 0.48, z: shopZ - 0.6, rotY: -0.06 },
    { x: -20.75, y: 0.16, z: shopZ - 0.58, rotY: 0.3 },
  ];
  instanced(new THREE.BoxGeometry(0.58, 0.32, 0.42), make('plain', 0x2f6f52), crates);

  // 가게 안쪽과 차도는 들어가지 못하게 막는다.
  blockers.push({ minX: -halfW, maxX: halfW, minZ: shopZ - 0.1, maxZ: halfD });
  blockers.push({ minX: -halfW, maxX: halfW, minZ: -halfD, maxZ: kerbZ + 0.05 });

  // ── 길 건너편 ──────────────────────────────────────────────────────────
  box(130, kerbH + 0.02, 3.4, granite, 0, -kerbH / 2 + 0.01, farWalkZ);
  box(130, 11.0, 4.0, farWall, 0, 5.5, -18.5);
  box(130, 0.5, 4.6, make('plain', 0xb0aa9d), 0, 11.25, -18.5);
  const farWindows = [], farFrames = [], farShops = [];
  for (let level = 0; level < 4; level += 1) {
    for (let x = -46; x <= 46; x += 4.4) {
      farWindows.push({ x, y: 4.3 + level * 2.5, z: -16.44 });
      farFrames.push({ x, y: 4.3 + level * 2.5, z: -16.46 });
    }
  }
  for (let x = -46; x <= 46; x += 4.4) farShops.push({ x, y: 1.5, z: -16.42 });
  instanced(new THREE.BoxGeometry(2.4, 1.5, 0.14), tinted, farWindows);
  instanced(new THREE.BoxGeometry(2.56, 1.66, 0.06), make('plain', 0xb0aa9d), farFrames);
  instanced(new THREE.BoxGeometry(3.9, 2.6, 0.12), shutter, farShops);

  const farGrime = new THREE.Mesh(new THREE.PlaneGeometry(130, 1.5), grime);
  farGrime.position.set(0, 0.75, -16.41);
  scene.add(farGrime);

  // ── 전봇대와 전선 ──────────────────────────────────────────────────────
  // 한국 길가의 첫인상은 사실 전선이다. 선은 가늘게 보여야 해서 Line 으로 긋는다.
  const cable = new THREE.LineBasicMaterial({ color: 0x23262b });
  const poleX = [-22.5, -3.5, 15.5, 34.5];
  const poleTop = 8.6, poleZ = shopZ - 1.9;
  poleX.forEach(x => {
    const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.15, 0.21, poleTop, 10), make('plain', 0xa8a49b));
    pole.position.set(x, poleTop / 2, poleZ);
    pole.castShadow = true;
    scene.add(pole);
    [0, 0.85].forEach((drop, row) => {
      box(2.3, 0.1, 0.1, dark, x, poleTop - 0.55 - drop, poleZ);
      for (let i = -1; i <= 1; i += 1) {
        box(0.08, 0.22, 0.08, make('plain', 0x6b7076), x + i * 1.0, poleTop - 0.38 - drop, poleZ);
      }
      if (row) box(0.52, 0.72, 0.46, make('plain', 0x8d9298), x - 0.72, poleTop - 2.1, poleZ + 0.15);
    });
    blockers.push({ minX: x - 0.3, maxX: x + 0.3, minZ: poleZ - 0.3, maxZ: poleZ + 0.3 });
  });
  // 전선 열여덟 가닥을 선 하나로 묶는다. 따로 그리면 그리기 요청만 열여덟 번이다.
  const strands = [];
  for (let i = 0; i + 1 < poleX.length; i += 1) {
    for (let k = -1; k <= 1; k += 1) {
      for (const drop of [0, 0.85]) {
        let previous = null;
        for (let t = 0; t <= 8; t += 1) {
          const f = t / 8;
          const point = new THREE.Vector3(
            poleX[i] + (poleX[i + 1] - poleX[i]) * f,
            poleTop - 0.3 - drop - Math.sin(Math.PI * f) * 0.55,
            poleZ + k * 1.0,
          );
          if (previous) strands.push(previous, point);
          previous = point;
        }
      }
    }
  }
  scene.add(new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(strands), cable));

  // ── 가로등 ─────────────────────────────────────────────────────────────
  [-13.5, 2.0, 15.0].forEach(x => {
    const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.09, 0.12, 5.4, 12), steel);
    pole.position.set(x, 2.7, kerbZ + 0.55);
    pole.castShadow = true;
    scene.add(pole);
    box(1.5, 0.12, 0.16, steel, x + 0.75, 5.36, kerbZ + 0.55);
    const lamp = box(0.7, 0.16, 0.34, lit(0xfff3d6, 0.7), x + 1.45, 5.22, kerbZ + 0.55);
    lamp.castShadow = false;
    blockers.push({ minX: x - 0.35, maxX: x + 0.35, minZ: kerbZ + 0.2, maxZ: kerbZ + 0.9 });
  });

  // ── 가로수 ─────────────────────────────────────────────────────────────
  const trees = [{ x: -16.5, s: 1.0 }, { x: -9.5, s: 0.86 }, { x: 9.5, s: 0.92 }, { x: 17.5, s: 1.05 }];
  const treeZ = kerbZ + 1.75;
  instanced(new THREE.BoxGeometry(1.3, 0.05, 1.3), dark, trees.map(t => ({ x: t.x, y: 0.03, z: treeZ })));
  instanced(new THREE.CylinderGeometry(0.16, 0.26, 3.4, 8), make('walnut', 0x6f5540, 1, 2),
    trees.map(t => ({ x: t.x, y: 1.7 * t.s, z: treeZ, scale: t.s, scaleY: t.s })));
  const foliage = [leaf, new THREE.MeshStandardMaterial({ color: 0x648c4a, roughness: 0.95 })];
  for (let crown = 0; crown < 6; crown += 1) {
    const spin = crown * 1.9;
    instanced(new THREE.IcosahedronGeometry(0.98, lowPower ? 1 : 2), foliage[crown % 2],
      trees.map((t, i) => ({
        x: t.x + Math.cos(i * 2.1 + spin) * (0.55 + (crown % 3) * 0.34),
        z: treeZ + Math.sin(i * 2.1 + spin) * (0.5 + (crown % 3) * 0.3),
        y: (3.75 + (crown % 4) * 0.42) * t.s,
        scale: t.s * (0.78 + (crown % 3) * 0.16),
      })));
  }
  trees.forEach(t => blockers.push({
    minX: t.x - 0.7, maxX: t.x + 0.7, minZ: treeZ - 0.7, maxZ: treeZ + 0.7,
  }));

  // ── 횡단보도 신호등 ────────────────────────────────────────────────────
  const signalX = -18.9;
  const signalPole = new THREE.Mesh(new THREE.CylinderGeometry(0.08, 0.1, 3.6, 10), dark);
  signalPole.position.set(signalX, 1.8, kerbZ + 0.4);
  signalPole.castShadow = true;
  scene.add(signalPole);
  box(0.42, 0.86, 0.3, dark, signalX, 3.35, kerbZ + 0.25);
  box(0.2, 0.2, 0.03, lit(0xff4436, 0.9), signalX, 3.56, kerbZ + 0.09);
  box(0.2, 0.2, 0.03, lit(0x1f3a2a, 0.5), signalX, 3.16, kerbZ + 0.09);
  blockers.push({ minX: signalX - 0.3, maxX: signalX + 0.3, minZ: kerbZ + 0.1, maxZ: kerbZ + 0.7 });

  // ── 자판기 ─────────────────────────────────────────────────────────────
  const vendX = -11.4;
  box(1.15, 1.9, 0.72, make('plain', 0xb23a3a), vendX, 0.95, shopZ - 0.62);
  box(0.78, 1.05, 0.04, lit(0xf4f7ff, 0.7), vendX - 0.13, 1.28, shopZ - 0.99);
  box(0.92, 0.22, 0.06, dark, vendX, 0.42, shopZ - 1.0);
  blockers.push({ minX: vendX - 0.7, maxX: vendX + 0.7, minZ: shopZ - 1.1, maxZ: shopZ });

  // ── 정차한 버스 ────────────────────────────────────────────────────────
  // 앞머리는 +X 쪽이다(전조등이 그쪽에 있다). 문은 인도 쪽인 +Z 면에 낸다.
  const busX = 10.5, busZ = -7.2, busL = 11.0, busW = 2.52;
  const body = make('plain', 0x2f6fd0);
  box(busL, 1.24, busW, body, busX, 1.05, busZ);                          // 허리 아래
  box(busL, 0.1, busW + 0.04, make('plain', 0xf2f5f8), busX, 1.72, busZ); // 허리 띠
  box(busL, 0.92, busW, tinted, busX, 2.24, busZ);                        // 창
  box(busL - 0.5, 0.36, busW - 0.08, make('plain', 0xeaeef3), busX, 2.86, busZ);
  box(2.2, 0.26, 1.5, make('plain', 0xdfe3e8), busX - 1.6, 3.06, busZ);   // 지붕 냉방기
  box(busL + 0.02, 0.32, 0.34, dark, busX, 0.3, busZ - 1.1);              // 옆면 아래 가림막
  box(busL + 0.02, 0.32, 0.34, dark, busX, 0.3, busZ + 1.1);
  [-2.9, 1.9].forEach(dx => {                                             // 인도 쪽 승강문
    box(1.3, 2.0, 0.07, make('plain', 0x24425e), busX + dx, 1.58, busZ + busW / 2);
  });
  [-1, 1].forEach(sign => {
    box(0.3, 0.26, 0.16, lit(0xfff6e2, sign > 0 ? 0.95 : 0.3), busX + sign * (busL / 2 + 0.02), 0.86, busZ - 0.82);
    box(0.3, 0.26, 0.16, lit(sign > 0 ? 0xffd9a0 : 0xff5a49, 0.65), busX + sign * (busL / 2 + 0.02), 0.86, busZ + 0.82);
  });
  box(0.1, 0.34, 0.46, dark, busX + busL / 2 + 0.12, 2.5, busZ - 1.1);    // 사이드미러
  box(0.1, 0.34, 0.46, dark, busX + busL / 2 + 0.12, 2.5, busZ + 1.1);
  const destination = new THREE.Mesh(
    new THREE.PlaneGeometry(1.7, 0.36),
    new THREE.MeshBasicMaterial({ map: labelTexture('05-142', '새담문화센터', '#101418', '#ffc95e') }),
  );
  destination.position.set(busX + 3.9, 2.52, busZ + busW / 2 + 0.01);
  scene.add(destination);
  const wheel = new THREE.CylinderGeometry(0.52, 0.52, 0.34, 18);
  wheel.rotateZ(Math.PI / 2);
  const hub = new THREE.CylinderGeometry(0.24, 0.24, 0.36, 14);
  hub.rotateZ(Math.PI / 2);
  const axles = [-3.7, 3.4].flatMap(dx => [-1, 1].map(sz => ({ x: busX + dx, y: 0.52, z: busZ + sz * 1.1 })));
  instanced(wheel, rubber, axles);
  instanced(hub, steel, axles);
  blockers.push({ minX: busX - 5.9, maxX: busX + 5.9, minZ: busZ - 1.5, maxZ: busZ + 1.5 });

  // ── 전시물 자리 ────────────────────────────────────────────────────────
  (layout.slots || []).forEach(slot => {
    if (slot.code[0] === 'A') {
      // 유리에 그대로 붙인 포스터
      const group = slotGroup(scene, slot);
      const backing = box(slot.width + 0.12, slot.height + 0.12, 0.015,
        make('plain', 0xf8f6f0), 0, 0, -0.02, group);
      backing.castShadow = false;
    } else if (slot.code[0] === 'B') {
      // 부스 양옆의 조명 광고판. 길에서 뒷면만 보이는 자리라 뒤쪽도 불이 들어오는
      // 양면 광고판으로 만든다.
      const group = slotGroup(scene, slot);
      box(slot.width + 0.46, slot.height + 0.46, 0.05, rim, 0, 0, -0.08, group);
      box(slot.width + 0.3, slot.height + 0.3, 0.1, shell, 0, 0, -0.09, group);
      const backLit = box(slot.width, slot.height, 0.02, lit(0xfdf6e6, 0.85), 0, 0, -0.146, group);
      backLit.castShadow = false;
    }
  });

  // 인도에 선 전시봉(C · D) — 봉 두 개 사이에 사진을 건다.
  (layout.slots || []).forEach(slot => {
    if (slot.code[0] !== 'C' && slot.code[0] !== 'D') return;
    const stand = new THREE.Group();
    stand.position.set(slot.x, 0, slot.z);
    stand.rotation.y = slot.rotationY;
    scene.add(stand);
    const panelW = slot.width + 0.3, panelH = slot.height + 0.3;
    const top = slot.y + panelH / 2;
    const poleH = top + 0.42;
    const poleShape = new THREE.CylinderGeometry(0.055, 0.075, poleH, 12);
    [-1, 1].forEach(sign => {
      const pole = new THREE.Mesh(poleShape, steel);
      pole.position.set(sign * (panelW / 2 + 0.13), poleH / 2, -0.05);
      pole.castShadow = true;
      stand.add(pole);
      box(0.26, 0.05, 0.26, dark, sign * (panelW / 2 + 0.13), 0.025, -0.05, stand);
    });
    box(panelW, panelH, 0.06, dark, 0, slot.y, -0.055, stand);
    box(panelW + 0.34, 0.1, 0.18, steel, 0, top + 0.34, -0.03, stand);
    const lamp = box(panelW * 0.6, 0.05, 0.1, lit(0xfff0cf), 0, top + 0.26, 0.06, stand);
    lamp.castShadow = false;
    blockers.push(slotBlocker(slot, panelW / 2 + 0.3, 0.4));
  });

  // ── 그라피티 ───────────────────────────────────────────────────────────
  // 사진 한 장에 열여섯 점이 4×4로 담겨 있다. 칸을 골라 잘라 쓰고,
  // 벽에서 1 cm 앞에 띄워 두 면이 같은 깊이를 다투지 않게 한다.
  const graffiti = graffitiSheet().then(sheet => {
    const pieces = [
      // [칸, 한 변(m), x, y, z, 바라보는 방향]
      [1, 2.4, -22.6, 1.7, shopZ - 0.14, Math.PI],
      [6, 2.0, -25.8, 1.4, shopZ - 0.14, Math.PI],
      [12, 1.7, -20.3, 2.5, shopZ - 0.14, Math.PI],
      [4, 2.6, -0.4, 1.6, shopZ - 0.14, Math.PI],       // 꽃집 셔터
      [9, 1.6, 2.0, 2.2, shopZ - 0.14, Math.PI],
      [14, 2.5, 17.6, 1.6, shopZ - 0.14, Math.PI],      // 약국 셔터
      [3, 1.8, 19.9, 2.3, shopZ - 0.14, Math.PI],
      [7, 2.2, 25.6, 1.8, shopZ - 0.14, Math.PI],
      [10, 2.8, -30.0, 2.2, -16.34, 0],                 // 건너편 건물
      [2, 2.2, -24.5, 1.8, -16.34, 0],
      [13, 2.4, 22.0, 2.0, -16.34, 0],
      [5, 1.9, 29.5, 1.6, -16.34, 0],
    ];
    for (const [cell, size, x, y, z, rotY] of pieces) {
      const map = sheet.clone();
      map.repeat.set(0.25, 0.25);
      map.offset.set((cell % 4) * 0.25, 0.75 - Math.floor(cell / 4) * 0.25);
      map.needsUpdate = true;
      const piece = new THREE.Mesh(new THREE.PlaneGeometry(size, size), new THREE.MeshStandardMaterial({
        map, transparent: true, roughness: 0.9, depthWrite: false, envMapIntensity: 0.5,
      }));
      piece.position.set(x, y, z);
      piece.rotation.y = rotY;
      scene.add(piece);
    }
  });

  return {
    blockers,
    frameMaterial: frameMaterialFor(frameKind, make),
    ready: Promise.all([photos.ready(), sky.ready, graffiti]),
  };
}


// ── 전시관8 · 골목 담벼락형 ──────────────────────────────────────────────
// 양옆 담벼락과 건물 외벽, 그라피티, 그리고 바닥에 깔아 둔 전시물.
function buildAlley(scene, renderer, hall, layout, lowPower) {
  const make = materialLibrary(renderer);
  const box = boxFactory(scene);
  const instanced = instancedFactory(scene);
  const w = layout.size.width, d = layout.size.depth, h = layout.size.height;
  const halfW = w / 2, halfD = d / 2;
  const power = hall.lightIntensity || 1;
  const blockers = [];
  const wallKind = resolveKind(hall.wallTexture, 'concrete');
  const floorKind = resolveKind(hall.floorTexture, 'concrete');
  const frameKind = resolveKind(hall.frameStyle, 'black');

  const fenceH = 3.3;            // 담벼락 높이
  const leftFace = -5.6;         // 담벼락 뒤로 물러선 왼쪽 건물 외벽
  const buildingH = h - 1;

  const ground = make(floorKind, hall.floorColor, 20, 60);
  const brick = make(wallKind, hall.wallColor, 4, 3);
  const plaster = make('plaster', 0xcfc7ba, 6, 10);
  const grey = make('concrete', 0xa9a49b, 6, 10);
  const metal = new THREE.MeshStandardMaterial({ color: 0x7d838b, roughness: 0.42, metalness: 0.6 });
  const rust = new THREE.MeshStandardMaterial({ color: 0x8a5a3b, roughness: 0.85, metalness: 0.2 });
  const leaf = new THREE.MeshStandardMaterial({ color: 0x4f7f43, roughness: 0.94 });
  const lit = (color, strength = 1) => new THREE.MeshBasicMaterial({
    color: new THREE.Color(color).multiplyScalar(strength), toneMapped: false,
  });

  const sky = new THREE.Mesh(
    new THREE.SphereGeometry(170, 32, 20),
    new THREE.MeshBasicMaterial({ map: skyTexture(), side: THREE.BackSide, fog: false, toneMapped: false }),
  );
  scene.add(sky);
  scene.background = new THREE.Color(0xc9d8e6);
  scene.fog = new THREE.Fog(0xd3dde7, 40, 150);

  // 골목은 건물 그늘에 잠기고 윗부분에만 햇살이 닿는다.
  scene.add(new THREE.HemisphereLight(0xd3e4f7, 0x6b6560, 1.0 * power));
  scene.add(new THREE.AmbientLight(0xffffff, 0.34 * power));
  const sun = new THREE.DirectionalLight(0xfff0d4, 2.4 * power);
  sun.position.set(w * 1.6, 26, d * 0.25);
  sun.castShadow = true;
  sun.shadow.mapSize.set(lowPower ? 1024 : 2048, lowPower ? 1024 : 2048);
  Object.assign(sun.shadow.camera, {
    left: -halfW - 6, right: halfW + 6, top: halfD + 4, bottom: -halfD - 4, near: 1, far: 70,
  });
  sun.shadow.bias = -0.0004;
  sun.shadow.normalBias = 0.04;
  scene.add(sun, sun.target);

  const floor = new THREE.Mesh(new THREE.PlaneGeometry(w + 24, d + 30), ground);
  floor.rotation.x = -Math.PI / 2;
  floor.receiveShadow = true;
  scene.add(floor);
  // 가운데 배수로와 철망
  box(0.46, 0.03, d + 6, make('plain', 0x7c7872), 0, 0.012, 0);
  const grates = [];
  for (let z = -halfD + 2; z <= halfD - 2; z += 5.5) grates.push({ x: 0, y: 0.028, z });
  instanced(new THREE.BoxGeometry(0.42, 0.03, 0.7), metal, grates);

  // 왼쪽 담벼락과 그 뒤 건물, 오른쪽 건물 외벽
  box(0.4, fenceH, d + 8, brick, -halfW - 0.2, fenceH / 2, 0);
  box(0.58, 0.14, d + 8, make('plain', 0x9b9288), -halfW - 0.2, fenceH + 0.07, 0);
  box(3.0, buildingH, d + 8, plaster, leftFace - 1.5, buildingH / 2, 0);
  box(3.0, buildingH, d + 8, grey, halfW + 1.5, buildingH / 2, 0);
  box(3.4, 0.5, d + 8, make('plain', 0x8e8981), halfW + 1.5, buildingH + 0.25, 0);
  box(3.4, 0.5, d + 8, make('plain', 0x8e8981), leftFace - 1.5, buildingH + 0.25, 0);
  // 막다른 끝과, 골목을 나서면 보이는 큰길 건너 건물
  box(w + 6, buildingH, 0.4, grey, 0, buildingH / 2, -halfD - 0.2);
  box(70, buildingH + 2, 3.0, plaster, 0, (buildingH + 2) / 2, halfD + 11);

  // 건물 창문(전시 자리와 겹치지 않는 높이에만 둔다)
  const windowMat = new THREE.MeshStandardMaterial({ color: 0x4a5a68, roughness: 0.2, metalness: 0.45 });
  const windows = [];
  for (let level = 0; level < 3; level += 1) {
    for (let z = -halfD + 3; z <= halfD - 3; z += 4.4) {
      windows.push({ x: halfW - 0.06, y: 9.6 + level * 1.5, z });
      windows.push({ x: leftFace + 0.06, y: 9.6 + level * 1.5, z });
    }
  }
  instanced(new THREE.BoxGeometry(0.12, 1.0, 1.5), windowMat, windows);
  instanced(new THREE.BoxGeometry(0.3, 0.1, 1.8), make('plain', 0x9d968c),
    windows.map(spot => ({ ...spot, y: spot.y - 0.58 })));

  // 담벼락 그라피티(전시 자리 사이사이에 그린다)
  function graffiti(width, height, x, y, z, rotY, seed) {
    const art = new THREE.Mesh(
      new THREE.PlaneGeometry(width, height),
      new THREE.MeshStandardMaterial({
        map: graffitiTexture(seed), transparent: true, roughness: 1, depthWrite: false,
      }),
    );
    art.position.set(x, y, z);
    art.rotation.y = rotY;
    scene.add(art);
  }
  [[-14.5, 1.6, 2.8], [-8.2, 1.9, 2.6], [-1.8, 1.5, 2.4], [4.6, 2.0, 2.8], [11.0, 1.7, 2.5]]
    .forEach(([z, y, size], index) => graffiti(size, size * 0.62, -halfW + 0.05, y, z, Math.PI / 2, 4211 + index * 977));
  [[-10.0, 1.5, 2.4], [-3.5, 2.1, 2.6], [3.2, 1.6, 2.2], [9.3, 2.2, 2.4]]
    .forEach(([z, y, size], index) => graffiti(size, size * 0.62, halfW - 0.05, y, z, -Math.PI / 2, 9137 + index * 613));
  graffiti(6.0, 3.6, 0, 2.2, -halfD + 0.02, 0, 7723);

  // 덧붙은 전단과 안내문
  streetPoster(scene, '골목 야시장', '토요일 저녁 6시', '#1f2937', '#ffd166', 0.7, 0.98,
    -halfW + 0.06, 2.55, -6.6, Math.PI / 2);
  streetPoster(scene, '독립영화', 'ALLEY CINEMA', '#7c2d12', '#fde68a', 0.7, 0.98,
    -halfW + 0.06, 2.55, 3.2, Math.PI / 2);
  streetPoster(scene, '세입자 구함', '010-0000-0000', '#f8fafc', '#334155', 0.62, 0.86,
    halfW - 0.06, 2.62, -2.0, -Math.PI / 2);
  streetPoster(scene, '주차금지', 'NO PARKING', '#fef2f2', '#b91c1c', 0.62, 0.86,
    halfW - 0.06, 2.62, 8.4, -Math.PI / 2);

  // 전봇대와 전선, 벽등
  const poleZ = [-12.0, -1.0, 10.0];
  poleZ.forEach((z, index) => {
    const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.16, 0.2, 8.4, 10), make('concrete', 0x9c968d, 1, 3));
    pole.position.set(-halfW + 0.55, 4.2, z);
    pole.castShadow = true;
    scene.add(pole);
    box(1.5, 0.1, 0.1, rust, -halfW + 0.55, 7.7, z);
    box(1.5, 0.1, 0.1, rust, -halfW + 0.55, 7.2, z);
    if (index < poleZ.length - 1) {
      const next = poleZ[index + 1];
      box(0.05, 0.05, next - z, make('plain', 0x2f3238), -halfW + 0.9, 7.55, (z + next) / 2);
      box(0.05, 0.05, next - z, make('plain', 0x2f3238), -halfW + 0.9, 7.1, (z + next) / 2);
    }
    blockers.push({ minX: -halfW + 0.2, maxX: -halfW + 0.95, minZ: z - 0.4, maxZ: z + 0.4 });
  });
  [-9.0, 0.5, 9.5].forEach(z => {
    box(0.22, 0.34, 0.5, metal, halfW - 0.16, 3.1, z);
    const lamp = box(0.2, 0.06, 0.4, lit(0xfff0cc, 0.8), halfW - 0.3, 2.92, z);
    lamp.castShadow = false;
  });

  // 실외기 · 화분 · 상자
  [[-6.0, 2.7], [1.5, 3.4], [8.0, 2.7]].forEach(([z, y]) => {
    box(0.55, 0.72, 0.9, make('plain', 0xd3d6da), halfW - 0.3, y, z);
    const grille = new THREE.Mesh(new THREE.CylinderGeometry(0.26, 0.26, 0.04, 16), make('plain', 0x8b9096));
    grille.rotation.z = Math.PI / 2;
    grille.position.set(halfW - 0.58, y, z);
    scene.add(grille);
    box(0.62, 0.1, 1.0, rust, halfW - 0.3, y - 0.42, z);
  });
  const pots = [];
  [-13.5, -7.5, 2.5, 12.5].forEach((z, index) => {
    pots.push({ x: -halfW + 0.62, z, scale: 0.9 + (index % 2) * 0.25 });
    blockers.push({ minX: -halfW, maxX: -halfW + 1.25, minZ: z - 0.6, maxZ: z + 0.6 });
  });
  instanced(new THREE.CylinderGeometry(0.28, 0.22, 0.42, 12), rust, pots.map(pot => ({ ...pot, y: 0.21 })));
  instanced(new THREE.IcosahedronGeometry(0.42, 0), leaf, pots.map(pot => ({ ...pot, y: 0.7 })));
  [0, 0.46].forEach((y, index) => box(0.66, 0.44, 0.66, make('plain', index ? 0x3f6fae : 0xc0562f), -3.9, y + 0.22, -15.2));
  blockers.push({ minX: -4.4, maxX: -3.4, minZ: -15.8, maxZ: -14.6 });

  // 막다른 끝의 셔터
  box(3.6, 2.6, 0.12, metal, 1.2, 1.3, -halfD + 0.3);
  for (let y = 0.2; y < 2.5; y += 0.26) box(3.5, 0.1, 0.06, make('plain', 0x9aa0a7), 1.2, y, -halfD + 0.24);

  // 벽에 붙이는 전시물의 뒤판과, 바닥에 까는 깔개
  const paper = make('plain', 0xf6f3ec);
  const rugMat = make('plain', 0xe9e4d8);
  (layout.slots || []).forEach(slot => {
    const group = slotGroup(scene, slot);
    const backing = slot.tilt
      ? box(slot.width + 0.5, slot.height + 0.5, 0.02, rugMat, 0, 0, -0.025, group)
      : box(slot.width + 0.14, slot.height + 0.14, 0.02, paper, 0, 0, -0.02, group);
    backing.castShadow = false;
  });

  return { blockers, frameMaterial: frameMaterialFor(frameKind, make) };
}


// ── 전시관9 · 건물 옥상형 ────────────────────────────────────────────────
// 해질 무렵 옥상. 난간 배너 · 옥탑방 외벽 · 건너편 건물 외벽 · 대형 스크린.
function buildRooftop(scene, renderer, hall, layout, lowPower) {
  const make = materialLibrary(renderer);
  const box = boxFactory(scene);
  const instanced = instancedFactory(scene);
  const w = layout.size.width, d = layout.size.depth;
  const halfW = w / 2, halfD = d / 2;
  const power = hall.lightIntensity || 1;
  const blockers = [];
  const floorKind = resolveKind(hall.floorTexture, 'concrete');
  const frameKind = resolveKind(hall.frameStyle, 'black');

  const parapetH = 1.15, parapetT = 0.34;
  const acrossX = 18.0;                 // 건너편 건물 외벽
  const penthouseZ = halfD - 2.6;       // 옥탑방 앞면
  const penthouseH = 3.2;

  const deck = make(floorKind, hall.floorColor, w, d);
  const wallMat = make('concrete', hall.wallColor || 0x3a4152, 8, 4);
  const coping = make('plain', 0x8a8d94);
  const steel = new THREE.MeshStandardMaterial({ color: 0x848a92, roughness: 0.38, metalness: 0.7 });
  const dark = new THREE.MeshStandardMaterial({ color: 0x2b303a, roughness: 0.55, metalness: 0.4 });
  const timber = make('oak', 0xa98a63, 2, 1);
  const leaf = new THREE.MeshStandardMaterial({ color: 0x4a7a46, roughness: 0.94 });
  const lit = (color, strength = 1) => new THREE.MeshBasicMaterial({
    color: new THREE.Color(color).multiplyScalar(strength), toneMapped: false,
  });

  const sky = new THREE.Mesh(
    new THREE.SphereGeometry(190, 32, 20),
    new THREE.MeshBasicMaterial({ map: duskSkyTexture(), side: THREE.BackSide, fog: false, toneMapped: false }),
  );
  scene.add(sky);
  scene.background = new THREE.Color(0x232a49);
  scene.fog = new THREE.Fog(0x2b3352, 70, 210);

  scene.add(new THREE.HemisphereLight(0x7d8dbd, 0x30302f, 0.85 * power));
  scene.add(new THREE.AmbientLight(0xffe6c9, 0.55 * power));
  const sun = new THREE.DirectionalLight(0xffb27a, 1.5 * power);
  sun.position.set(-w * 1.4, 9, -d * 0.4);
  sun.castShadow = true;
  sun.shadow.mapSize.set(lowPower ? 1024 : 2048, lowPower ? 1024 : 2048);
  Object.assign(sun.shadow.camera, {
    left: -halfW - 6, right: halfW + 6, top: halfD + 6, bottom: -halfD - 6, near: 1, far: 70,
  });
  sun.shadow.bias = -0.0004;
  sun.shadow.normalBias = 0.04;
  scene.add(sun, sun.target);
  if (!lowPower) {
    const deckGlow = new THREE.PointLight(0xffca8a, 22 * power, 16, 2);
    deckGlow.position.set(3.0, 3.0, 3.4);
    const screenGlow = new THREE.PointLight(0x9fc4ff, 26 * power, 20, 2);
    screenGlow.position.set(0, 3.6, -halfD + 3.0);
    scene.add(deckGlow, screenGlow);
  }

  // 옥상 바닥과 그 아래 건물
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(w, d), deck);
  floor.rotation.x = -Math.PI / 2;
  floor.receiveShadow = true;
  scene.add(floor);
  const cityFace = cityWindowTexture();
  function facadeMaterial(repeatX, repeatY, strength) {
    const material = new THREE.MeshStandardMaterial({
      map: cityFace.clone(), emissiveMap: cityFace.clone(), emissive: 0xffffff,
      emissiveIntensity: strength, roughness: 0.84,
    });
    material.map.repeat.set(repeatX, repeatY);
    material.emissiveMap.repeat.copy(material.map.repeat);
    return material;
  }
  const host = box(w, 30, d, facadeMaterial(4, 6, 0.38), 0, -15.05, 0);
  host.castShadow = false;

  // 난간(사방)과 갓돌
  [-1, 1].forEach(sign => {
    box(parapetT, parapetH, d, wallMat, sign * (halfW - parapetT / 2), parapetH / 2, 0);
    box(parapetT + 0.12, 0.12, d, coping, sign * (halfW - parapetT / 2), parapetH + 0.06, 0);
    box(w - parapetT * 2, parapetH, parapetT, wallMat, 0, parapetH / 2, sign * (halfD - parapetT / 2));
    box(w - parapetT * 2, 0.12, parapetT + 0.12, coping, 0, parapetH + 0.06, sign * (halfD - parapetT / 2));
  });

  // 건너편 건물과 멀리 보이는 도시 야경
  const across = box(10, 42, 34, facadeMaterial(3, 7, 0.42), acrossX + 5, 5, 0);
  across.castShadow = false;
  box(10.6, 0.6, 34.6, coping, acrossX + 5, 26.3, 0);
  const cityMat = new THREE.MeshBasicMaterial({ map: cityFace.clone() });
  cityMat.map.repeat.set(2, 5);
  const towers = [];
  const towerCount = lowPower ? 26 : 46;
  let seed = 33179;
  const random = () => ((seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0) / 4294967296);
  for (let i = 0; i < towerCount; i += 1) {
    const angle = (i / towerCount) * Math.PI * 2 + random() * 0.12;
    const radius = 46 + random() * 66;
    const tall = 14 + random() * 46;
    towers.push({
      x: Math.sin(angle) * radius, z: Math.cos(angle) * radius,
      y: -26 + tall / 2, scale: 7 + random() * 12, scaleY: tall, rotY: random() * 1.6,
    });
  }
  instanced(new THREE.BoxGeometry(1, 1, 1), cityMat, towers);
  const cityGround = new THREE.Mesh(
    new THREE.PlaneGeometry(440, 440),
    new THREE.MeshBasicMaterial({ color: 0x151a26 }),
  );
  cityGround.rotation.x = -Math.PI / 2;
  cityGround.position.y = -26.1;
  scene.add(cityGround);

  // 옥탑방 — 문 · 창 · 사다리 · 물탱크
  box(8.0, penthouseH, 2.6, wallMat, 0, penthouseH / 2, halfD - 1.3);
  box(8.4, 0.28, 3.0, coping, 0, penthouseH + 0.14, halfD - 1.3);
  box(1.0, 2.1, 0.12, make('plain', 0x6c4b33), 3.1, 1.05, penthouseZ - 0.06);
  const knob = new THREE.Mesh(new THREE.SphereGeometry(0.05, 10, 8), steel);
  knob.position.set(2.72, 1.05, penthouseZ - 0.14);
  scene.add(knob);
  const pane = box(1.2, 0.8, 0.1, lit(0xffd79a, 0.55), -3.3, 1.95, penthouseZ - 0.05);
  pane.castShadow = false;
  blockers.push({ minX: -4.2, maxX: 4.2, minZ: penthouseZ - 0.2, maxZ: halfD });
  const tank = new THREE.Mesh(new THREE.CylinderGeometry(0.95, 0.95, 1.5, 18), make('plain', 0x9fb7c9));
  tank.position.set(-2.4, penthouseH + 0.95, halfD - 1.3);
  tank.castShadow = true;
  scene.add(tank);
  [-1, 1].forEach(sign => box(0.1, 0.9, 0.1, steel, -2.4 + sign * 0.8, penthouseH + 0.3, halfD - 1.3));
  for (let y = 0.4; y < penthouseH; y += 0.42) box(0.6, 0.06, 0.06, steel, 3.9, y, penthouseZ - 0.3);
  [-1, 1].forEach(sign => box(0.06, penthouseH, 0.06, steel, 3.9 + sign * 0.3, penthouseH / 2, penthouseZ - 0.3));

  // 대형 스크린 — 테두리와 받침대, 양옆 스피커
  const screenZ = -halfD + 0.45;
  box(9.2, 5.4, 0.3, dark, 0, 3.9, screenZ - 0.2);
  const screenRim = box(9.6, 5.8, 0.06, lit(0x6f8bd6, 0.5), 0, 3.9, screenZ - 0.36);
  screenRim.castShadow = false;
  [-1, 1].forEach(sign => {
    box(0.34, 1.35, 0.34, steel, sign * 3.7, 0.67, screenZ - 0.5);
    box(0.24, 4.0, 0.24, steel, sign * 4.4, 2.0, screenZ - 0.55);
    box(0.5, 1.1, 0.42, dark, sign * 5.1, 2.4, screenZ - 0.1);
  });
  blockers.push({ minX: -5.6, maxX: 5.6, minZ: -halfD, maxZ: screenZ + 0.15 });

  // 난간 위 배너 걸이(B) — 기둥 두 개와 윗대에 사진을 건다.
  (layout.slots || []).forEach(slot => {
    if (slot.code[0] !== 'B') return;
    const stand = new THREE.Group();
    stand.position.set(slot.x, 0, slot.z);
    stand.rotation.y = slot.rotationY;
    scene.add(stand);
    const panelW = slot.width + 0.26, panelH = slot.height + 0.26;
    const top = slot.y + panelH / 2;
    [-1, 1].forEach(sign => box(0.12, top + 0.3, 0.12, steel, sign * (panelW / 2 + 0.12), (top + 0.3) / 2, -0.05, stand));
    box(panelW + 0.5, 0.12, 0.14, steel, 0, top + 0.28, -0.05, stand);
    box(panelW, panelH, 0.06, dark, 0, slot.y, -0.055, stand);
    const lamp = box(panelW * 0.6, 0.05, 0.1, lit(0xffe6bb), 0, top + 0.2, 0.06, stand);
    lamp.castShadow = false;
  });

  // 옥탑방 외벽 전시(C)는 받침판과 그림 조명을, 건너편 건물 외벽 전시(D)는
  // 큰 현수막 테두리를 두른다.
  (layout.slots || []).forEach(slot => {
    if (slot.code[0] === 'C') {
      const group = slotGroup(scene, slot);
      box(slot.width + 0.2, slot.height + 0.2, 0.1, dark, 0, 0, -0.1, group);
      box(0.5, 0.09, 0.16, steel, 0, slot.height / 2 + 0.32, 0.1, group);
      const lamp = box(0.44, 0.03, 0.1, lit(0xffe2b0, 0.9), 0, slot.height / 2 + 0.28, 0.12, group);
      lamp.castShadow = false;
    } else if (slot.code[0] === 'D') {
      const group = slotGroup(scene, slot);
      const backing = box(slot.width + 0.34, slot.height + 0.34, 0.12, dark, 0, 0, -0.09, group);
      backing.castShadow = false;
      const glow = box(slot.width + 0.5, slot.height + 0.5, 0.04, lit(0xffd9a0, 0.35), 0, 0, -0.16, group);
      glow.castShadow = false;
    }
  });

  // 전구 줄 — 옥탑방 모서리에서 반대쪽 기둥까지 늘어뜨린다.
  const stringPole = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.08, 3.6, 10), steel);
  stringPole.position.set(-8.4, 1.8, -1.2);
  stringPole.castShadow = true;
  scene.add(stringPole);
  const bulbs = [];
  const from = { x: -4.0, y: 3.3, z: penthouseZ - 0.1 };
  const to = { x: -8.4, y: 3.5, z: -1.2 };
  for (let i = 0; i <= 14; i += 1) {
    const t = i / 14;
    bulbs.push({
      x: from.x + (to.x - from.x) * t,
      y: from.y + (to.y - from.y) * t - Math.sin(t * Math.PI) * 0.55,
      z: from.z + (to.z - from.z) * t,
    });
  }
  instanced(new THREE.SphereGeometry(0.075, 8, 6), lit(0xffd9a0), bulbs);

  // 쉼터 — 벤치 두 개와 낮은 탁자, 화분
  function bench(x, z, rotY) {
    const group = new THREE.Group();
    group.position.set(x, 0, z);
    group.rotation.y = rotY;
    scene.add(group);
    [-0.18, 0.18].forEach(dz => box(1.8, 0.08, 0.16, timber, 0, 0.42, dz, group));
    [-0.74, 0.74].forEach(dx => box(0.12, 0.42, 0.44, dark, dx, 0.21, 0, group));
    box(1.8, 0.3, 0.07, timber, 0, 0.66, -0.22, group);
    blockers.push({ minX: x - 1.1, maxX: x + 1.1, minZ: z - 0.7, maxZ: z + 0.7 });
  }
  bench(3.4, 4.0, Math.PI);
  bench(6.6, 1.4, -Math.PI / 2);
  box(1.1, 0.07, 0.7, timber, 4.6, 0.42, 2.2);
  [-0.45, 0.45].forEach(dx => box(0.08, 0.4, 0.08, dark, 4.6 + dx, 0.2, 2.2));
  const planters = [{ x: -7.2, z: 5.2 }, { x: 7.8, z: 5.6 }, { x: -9.0, z: -4.0 }];
  planters.forEach(spot => {
    box(0.9, 0.62, 0.9, make('plain', 0x7c7468), spot.x, 0.31, spot.z);
    blockers.push({ minX: spot.x - 0.7, maxX: spot.x + 0.7, minZ: spot.z - 0.7, maxZ: spot.z + 0.7 });
  });
  instanced(new THREE.IcosahedronGeometry(0.62, 0), leaf, planters.map(spot => ({ ...spot, y: 1.0 })));

  // 실외기와 환기구
  [[-9.6, 1.6], [-9.6, -0.6]].forEach(([x, z]) => {
    box(1.3, 0.95, 0.85, make('plain', 0xb9bdc2), x, 0.47, z);
    const grille = new THREE.Mesh(new THREE.CylinderGeometry(0.32, 0.32, 0.05, 16), make('plain', 0x878d94));
    grille.rotation.z = Math.PI / 2;
    grille.position.set(x + 0.68, 0.5, z);
    scene.add(grille);
    blockers.push({ minX: x - 0.9, maxX: x + 0.9, minZ: z - 0.7, maxZ: z + 0.7 });
  });
  [[9.4, -3.6], [10.2, -5.8]].forEach(([x, z]) => {
    const vent = new THREE.Mesh(new THREE.CylinderGeometry(0.34, 0.34, 1.1, 14), steel);
    vent.position.set(x, 0.55, z);
    vent.castShadow = true;
    scene.add(vent);
    const cap = new THREE.Mesh(new THREE.ConeGeometry(0.46, 0.3, 14), steel);
    cap.position.set(x, 1.24, z);
    scene.add(cap);
    blockers.push({ minX: x - 0.5, maxX: x + 0.5, minZ: z - 0.5, maxZ: z + 0.5 });
  });

  return { blockers, frameMaterial: frameMaterialFor(frameKind, make) };
}


export function createFrame(width, height, material) {
  const group = new THREE.Group();
  const mat = new THREE.MeshStandardMaterial({ color: 0xf5f1e7, roughness: 0.95 });
  const inner = material.userData.lip
    || new THREE.MeshStandardMaterial({ color: 0x37312a, roughness: 0.5, metalness: 0.35 });
  function bar(w, h, depth, x, y, z, surface) {
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, depth), surface);
    mesh.position.set(x, y, z);
    mesh.castShadow = mesh.receiveShadow = true;
    group.add(mesh);
  }
  // Four separate rails leave the image unobstructed. Recessed mat + inner lip + raised outer moulding.
  // 테두리 폭과 튀어나온 깊이를 예전의 절반으로 줄여 가늘고 얇은 액자로 건다.
  function ring(padding, thickness, depth, z, surface) {
    const w = width + padding * 2, h = height + padding * 2;
    [-1, 1].forEach(sign => {
      bar(w + thickness * 2, thickness, depth, 0, sign * (h + thickness) / 2, z, surface);
      bar(thickness, h, depth, sign * (w + thickness) / 2, 0, z, surface);
    });
  }
  ring(0, 0.055, 0.018, -0.012, mat);
  ring(0.055, 0.009, 0.033, -0.003, inner);
  ring(0.064, 0.0325, 0.06, 0.005, material);
  ring(0.0965, 0.0075, 0.045, -0.002, material);
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
