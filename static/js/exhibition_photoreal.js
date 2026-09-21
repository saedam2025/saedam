import * as THREE from './vendor/three/three.module.js';
import { RGBELoader } from './vendor/three/RGBELoader.js';

// Photographed CC0 assets are bundled locally. See textures/exhibition/lounge/sources.json.
const ROOT = new URL('../textures/exhibition/lounge/', import.meta.url);
const SURFACES = {
  oak: { asset: 'wood_floor', size: 1.7, normal: 0.32, wood: true },
  walnut: { asset: 'wood_floor', size: 1.7, normal: 0.32, wood: true },
  plaster: { asset: 'plastered_wall', size: 2, normal: 0.18 },
  concrete: { asset: 'concrete_floor_02', size: 2, normal: 0.4 },
  limestone: { asset: 'stone_tiles_02', size: 2, normal: 0.28 },
};

export function photographicMaterials(renderer, fallback) {
  const loader = new THREE.TextureLoader();
  const sources = new Map();
  const pending = [];
  function source(asset) {
    if (!sources.has(asset)) {
      sources.set(asset, Promise.all(['Diffuse', 'nor_gl', 'Rough'].map(async channel => {
        const texture = await loader.loadAsync(new URL(`${asset}_${channel}.jpg`, ROOT).href);
        texture.colorSpace = channel === 'Diffuse' ? THREE.SRGBColorSpace : THREE.NoColorSpace;
        texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
        texture.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy());
        return texture;
      })));
    }
    return sources.get(asset);
  }
  const make = (kind, color, width = 1, height = 1) => {
    const spec = SURFACES[kind];
    if (!spec) return fallback(kind, color, width, height);
    const material = new THREE.MeshStandardMaterial({ color, roughness: 0.85, envMapIntensity: 0.22 });
    const ready = source(spec.asset).then(textures => {
      [material.map, material.normalMap, material.roughnessMap] = textures.map(texture => {
        const map = texture.clone();
        map.repeat.set(width / spec.size, height / spec.size);
        return map;
      });
      // The photograph already contains the wood's colour; keep the saved tint subtle.
      if (spec.wood) material.color.lerp(new THREE.Color(0xffffff), kind === 'walnut' ? 0.22 : 0.72);
      material.normalScale.setScalar(spec.normal);
      material.roughness = spec.wood ? 0.76 : 0.95;
      material.needsUpdate = true;
    }).catch(error => {
      // A missing image must never keep visitors stuck at the entrance.
      console.warn(`Exhibition material unavailable: ${spec.asset}`, error);
    });
    pending.push(ready);
    return material;
  };
  return { make, ready: () => Promise.all(pending) };
}

export async function photographicSky(scene, renderer) {
  try {
    const texture = await new RGBELoader().loadAsync(new URL('daylight_2k.hdr', ROOT).href);
    texture.mapping = THREE.EquirectangularReflectionMapping;
    scene.background = texture;
    scene.backgroundIntensity = 0.8;
    const pmrem = new THREE.PMREMGenerator(renderer);
    try {
      const environment = pmrem.fromEquirectangular(texture);
      scene.environment = environment.texture;
      // Retain the render target for explicit scene teardown if the viewer becomes an SPA.
      scene.userData.loungeEnvironment = environment;
    } finally {
      pmrem.dispose();
    }
  } catch (error) {
    console.warn('Exhibition sky unavailable; using daylight background.', error);
  }
}

export function glazedSkylight(scene, box, metal, trim, width, depth, height, lowPower) {
  const glass = new THREE.MeshPhysicalMaterial({
    color: 0xf1faf9, metalness: 0, roughness: 0.045,
    transmission: lowPower ? 0 : 0.96, thickness: 0.025, ior: 1.5,
    transparent: true, opacity: lowPower ? 0.12 : 1,
    side: THREE.DoubleSide, depthWrite: false, envMapIntensity: 0.45,
  });
  const columns = 3, rows = Math.ceil(depth / 2.7);
  const cellW = width / columns, cellD = depth / rows;
  for (let x = 0; x < columns; x++) {
    for (let z = 0; z < rows; z++) {
      const pane = new THREE.Mesh(new THREE.PlaneGeometry(cellW - 0.07, cellD - 0.07), glass);
      pane.name = 'lounge-skylight-glass';
      pane.rotation.x = -Math.PI / 2;
      pane.position.set(-width / 2 + (x + 0.5) * cellW, height + 0.32, -depth / 2 + (z + 0.5) * cellD);
      // Transparent glazing must not cast an opaque rectangle over the room.
      pane.castShadow = false;
      scene.add(pane);
    }
  }
  for (let x = 0; x <= columns; x++) box(0.07, 0.16, depth + 0.12, metal, -width / 2 + x * cellW, height + 0.26, 0);
  for (let z = 0; z <= rows; z++) box(width, 0.16, 0.07, metal, 0, height + 0.26, -depth / 2 + z * cellD);
  for (const sign of [-1, 1]) {
    box(width + 0.3, 0.35, 0.16, trim, 0, height + 0.1, sign * (depth / 2 + 0.08));
  }
}
