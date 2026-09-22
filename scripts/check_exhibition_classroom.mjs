// Browser regression against the real viewer, with isolated fixture data (no app/DB startup).
// Usage: node scripts/check_exhibition_classroom.mjs
// Optional: PYTHON, PLAYWRIGHT_MODULE, BROWSER_EXECUTABLE environment variables.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const layout = JSON.parse(execFileSync(process.env.PYTHON || 'python', ['-c', `
import ast, json, math
from pathlib import Path
tree = ast.parse(Path('routes/exhibition.py').read_text(encoding='utf-8-sig'))
nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ('_slot', '_hall3_layout')]
ns = dict(FACE_FRONT=0, FACE_BACK=math.pi, FACE_RIGHT=math.pi/2, FACE_LEFT=-math.pi/2)
exec(compile(ast.Module(body=nodes, type_ignores=[]), '<layout>', 'exec'), ns)
print(json.dumps(ns['_hall3_layout']()))
`], { cwd: root, encoding: 'utf8' }));
assert.equal(layout.slots.length, 8);
assert.ok(layout.slots.every(s => s.frameless));
for (const slot of layout.slots.filter(s => s.code.startsWith('B'))) {
  assert.ok(slot.z + slot.width / 2 < 2.9, 'Wall photographs must clear the door surround');
}
const sceneData = {
  hall: { wallColor: '#efe7d5', floorColor: '#c89b62', accentColor: '#15803d',
    lightIntensity: 1, placardEnabled: true, autoSlide: false, bgmEnabled: false },
  layout,
  slots: layout.slots.map((slot, i) => ({ ...slot, title: `작품 ${slot.code}`, artist: '교실 전시',
    media: [{ kind: 'image', url: `/sample.svg?${i}`, width: 1200, height: 800 }] })),
};
let html = fs.readFileSync(path.join(root, 'templates/exhibition/view.html'), 'utf8');
html = html.replace('{{ scene_json | safe }}', JSON.stringify(sceneData))
  .replace(/{{ url_for\('static', filename='([^']+)'\) }}/g, '/static/$1')
  .replace(/{{[\s\S]*?}}/g, '교실 전시 검증').replace(/{%[\s\S]*?%}/g, '');
const mime = { '.js': 'text/javascript', '.css': 'text/css', '.jpg': 'image/jpeg', '.hdr': 'application/octet-stream' };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/') { res.setHeader('Content-Type', 'text/html; charset=utf-8'); return res.end(html); }
  if (url.pathname === '/sample.svg') {
    res.setHeader('Content-Type', 'image/svg+xml');
    return res.end('<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800"><rect width="1200" height="800" fill="#c9d7d7"/><circle cx="830" cy="240" r="120" fill="#f1ddb2"/><path d="M0 800V660L330 210 750 800M450 800L930 350 1200 590V800" fill="#6b8274"/><path d="M0 800V710L470 530 800 800" fill="#344f49"/></svg>');
  }
  const file = path.resolve(root, '.' + decodeURIComponent(url.pathname));
  if (!file.startsWith(path.join(root, 'static') + path.sep) || !fs.existsSync(file)) { res.writeHead(404); return res.end(); }
  res.setHeader('Content-Type', mime[path.extname(file)] || 'application/octet-stream');
  let body = fs.readFileSync(file);
  if (file.endsWith('exhibition_viewer.js')) {
    body = body.toString() + '\nwindow.__classroom = {scene, renderer, camera, player, artworks, blockers, refit, clampPosition, THREE};';
  }
  res.end(body);
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const browser = await chromium.launch({ headless: true,
  ...(process.env.BROWSER_EXECUTABLE ? { executablePath: process.env.BROWSER_EXECUTABLE } : {}),
  args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const output = path.join(root, 'outputs/classroom-check');
fs.mkdirSync(output, { recursive: true });
try {
  for (const mobile of [false, true]) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 960 }, hasTouch: mobile });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error' && !message.text().includes('favicon')) errors.push(message.text()); });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForFunction(() => window.__classroom && !document.getElementById('exGate').hidden, null, { timeout: 60000 });
    const checks = await page.evaluate(() => {
      const { scene, artworks, blockers, refit, clampPosition, THREE } = window.__classroom;
      const ensure = (value, label) => { if (!value) throw new Error(label); };
      ensure(scene.background.isTexture && scene.environment.isTexture, 'HDR sky and reflections');
      const floor = scene.getObjectByName('classroom-photo-floor');
      ensure(floor.material.map && floor.material.normalMap && floor.material.roughnessMap, 'Floor PBR');
      ensure(!scene.getObjectByName('classroom-window-glass').castShadow, 'Sun passes through glazing');
      ensure(artworks.length === 8, 'All works loaded');
      for (const art of artworks) {
        ensure(art.panel && !art.frame, `Frameless panel ${art.slot.code}`);
        for (const aspect of [0.4, 3.5, 1.5]) {
          refit(art, aspect);
          ensure(art.panel.geometry.parameters.width === art.mesh.geometry.parameters.width, 'Panel width tracks slide');
          ensure(art.panel.geometry.parameters.height === art.mesh.geometry.parameters.height, 'Panel height tracks slide');
          ensure(art.panel.position.z + 0.02 < art.mesh.position.z, 'No coplanar photo/backing');
        }
      }
      // A visitor can walk down the whole central aisle without collision pushes.
      for (let z = -4; z <= 3.75; z += 0.1) {
        const p = clampPosition(new THREE.Vector3(0, 0, z));
        ensure(Math.abs(p.x) < 0.001 && Math.abs(p.z - z) < 0.001, 'Clear centre aisle');
      }
      // Non-overlapping padded furniture bounds avoid repeated collision ejection.
      for (let i = 0; i < blockers.length; i++) for (let j = i + 1; j < blockers.length; j++) {
        const a = blockers[i], b = blockers[j];
        ensure(!(a.minX < b.maxX && a.maxX > b.minX && a.minZ < b.maxZ && a.maxZ > b.minZ), `Overlapping furniture ${i}/${j}`);
      }
      return { artworks: artworks.length, blockers: blockers.length, sky: scene.background.image.width };
    });
    await page.locator('#exEnterBtn').click();
    for (const [name, position, target] of [
      ['entry', [0, 1.7, 3.7], [0, 1.5, -3.5]],
      ['windows', [3.8, 1.8, 1.8], [-6, 1.8, -0.6]],
      ['door', [-3.8, 1.8, -0.8], [5.6, 1.5, 2.2]],
      ['rear', [0, 1.8, -2.8], [0, 1.6, 4.8]],
    ]) {
      if (mobile && name !== 'entry') continue;
      await page.evaluate(({ position, target }) => {
        const { player, camera, scene, renderer } = window.__classroom;
        renderer.setAnimationLoop(null);
        player.position.set(0, 0, 0); player.rotation.set(0, 0, 0);
        camera.position.set(...position); camera.lookAt(...target);
        renderer.render(scene, camera);
      }, { position, target });
      await page.screenshot({ path: path.join(output, `${mobile ? 'mobile' : 'desktop'}-${name}.png`) });
    }
    assert.deepEqual(errors.filter(e => !e.includes('404')), [], 'No renderer or runtime errors');
    console.log(`PASS ${mobile ? 'mobile' : 'desktop'}:`, checks);
    await page.close();
  }
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}
