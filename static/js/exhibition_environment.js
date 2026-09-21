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

export function buildEnvironment(scene, renderer, hall, layout, lowPower) {
  const make = materialLibrary(renderer);
  const w = layout.size.width, d = layout.size.depth, h = layout.size.height;
  const lounge = layout.style === 'lounge';
  const resolve = (v, fallback) => !v || v === 'auto' ? fallback : v;
  const wallKind = resolve(hall.wallTexture, 'plaster');
  const floorKind = resolve(hall.floorTexture, lounge ? 'oak' : 'concrete');
  const frameKind = resolve(hall.frameStyle, lounge ? 'oak' : 'black');
  const blockers = [];
  const metal = new THREE.MeshStandardMaterial({ color: 0x282a28, roughness: 0.36, metalness: 0.75 });
  const trim = make('plain', 0xd6cfc2);
  const timber = make('oak', 0xb38b5c, 3, 1);
  function box(width, height, depth, material, x, y, z, parent = scene) {
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(width, height, depth), material);
    mesh.position.set(x, y, z);
    mesh.castShadow = mesh.receiveShadow = true;
    parent.add(mesh);
    return mesh;
  }
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
  const frameMaterial = frameKind === 'black' || frameKind === 'brass'
    ? new THREE.MeshStandardMaterial({ color: frameKind === 'brass' ? 0xb99a59 : 0x292a28, roughness: 0.32, metalness: 0.75 })
    : make(frameKind, frameKind === 'walnut' ? 0x67472f : 0xb69263, 2, 0.2);
  return { blockers, frameMaterial };
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
