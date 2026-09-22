import assert from 'node:assert/strict';
import fs from 'node:fs';
import crypto from 'node:crypto';
import * as THREE from '../static/js/vendor/three/three.module.js';
import { RGBELoader } from '../static/js/vendor/three/RGBELoader.js';
import { photographicMaterials, glazedSkylight } from '../static/js/exhibition_photoreal.js';

const root = new URL('../static/textures/exhibition/lounge/', import.meta.url);
const manifest = JSON.parse(fs.readFileSync(new URL('sources.json', root), 'utf8').replace(/^\uFEFF/, ''));
for (const item of manifest) {
  const data = fs.readFileSync(new URL(item.file, root));
  assert.equal(crypto.createHash('md5').update(data).digest('hex'), item.md5, item.file);
}
const hdr = fs.readFileSync(new URL('daylight_2k.hdr', root));
const parsed = new RGBELoader().parse(hdr.buffer.slice(hdr.byteOffset, hdr.byteOffset + hdr.byteLength));
assert.equal(parsed.width, 2048);
assert.equal(parsed.height, 1024);

// Exercise async readiness, shared image loading, independent UVs and linear PBR channels.
const originalLoad = THREE.TextureLoader.prototype.loadAsync;
const requested = [];
THREE.TextureLoader.prototype.loadAsync = async function (url) {
  requested.push(url);
  assert.ok(fs.existsSync(new URL(url)));
  return new THREE.Texture({ width: 1024, height: 1024 });
};
const renderer = { capabilities: { getMaxAnisotropy: () => 16 } };
const fallback = (kind, color) => new THREE.MeshStandardMaterial({ color });
try {
  const library = photographicMaterials(renderer, fallback);
  const floor = library.make('oak', '#d8b184', 24, 18);
  const bench = library.make('oak', '#b38b5c', 3, 1);
  for (const kind of ['plaster', 'concrete', 'limestone', 'walnut']) library.make(kind, '#ffffff');
  const plain = library.make('plain', '#ffffff');
  await library.ready();
  assert.equal(requested.length, 12, 'Each photographic source is decoded once');
  assert.equal(floor.map.colorSpace, THREE.SRGBColorSpace);
  assert.equal(floor.normalMap.colorSpace, THREE.NoColorSpace);
  assert.equal(floor.roughnessMap.colorSpace, THREE.NoColorSpace);
  assert.notEqual(floor.map, bench.map);
  assert.equal(floor.map.source, bench.map.source);
  assert.equal(floor.map.repeat.x, 24 / 1.7);
  assert.equal(bench.map.repeat.x, 3 / 1.7);
  assert.equal(plain.map, null);
  THREE.TextureLoader.prototype.loadAsync = async () => { throw new Error('Simulated missing image'); };
  const degraded = photographicMaterials(renderer, fallback);
  const backup = degraded.make('oak', '#d8b184');
  const originalWarn = console.warn;
  try { console.warn = () => {}; await degraded.ready(); } finally { console.warn = originalWarn; }
  assert.equal(backup.map, null, 'A failed image leaves a usable coloured material');
} finally { THREE.TextureLoader.prototype.loadAsync = originalLoad; }

for (const lowPower of [false, true]) {
  const scene = new THREE.Scene();
  const frame = new THREE.MeshStandardMaterial();
  const box = (w, h, d, material, x, y, z) => {
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), material);
    mesh.position.set(x, y, z); mesh.castShadow = true; scene.add(mesh); return mesh;
  };
  glazedSkylight(scene, box, frame, frame, 9, 11, 7, lowPower);
  const panes = scene.children.filter(o => o.name === 'lounge-skylight-glass');
  assert.equal(panes.length, 15);
  for (const pane of panes) {
    assert.equal(pane.castShadow, false, 'Glass must not block the sun');
    assert.equal(pane.material.depthWrite, false);
    assert.equal(pane.material.transmission, lowPower ? 0 : 0.96);
    assert.ok(!lowPower || pane.material.opacity < 0.2);
  }
}
console.log('PASS: 13 source checksums, HDR decoding, PBR loading/fallback/UVs, desktop/mobile glazing');
