// 교실안내 3D 화면 (three.js r161, static/js/vendor/three)
//
// 층 보기 방식
//  - '전체' : 모든 건물을 외벽(창문)과 지붕이 있는 입체로 보여준다.
//  - N층   : N층 아래는 외벽 입체, N층은 지붕을 걷어낸 단면(교실·복도·계단),
//            N층 위는 윤곽선만 보여준다.
import * as THREE from '../vendor/three/three.module.js';
import {
    BUILDING_KINDS, FLOOR_HEIGHT, GATE_KINDS, LANDMARK_TYPES, ROOM_TYPES, SIDES,
    buildingColor, canHaveDoors, doorSegment, gateSpan, hasFloor, landmarkLabel, roomShortLabel, stairPlan, topFloor,
} from './model.js';
import { buildGrid, roomDoor } from './route.js';

const FH = FLOOR_HEIGHT;
const WALL_H = FH * 0.55;
const SLAB = 0.06;
const WINDOW_TOP = 0.8;           // 외벽 창 위 끝(한 층 높이 비율) — 초록 차양 높이
const AWNING = '#3f9a5c';         // 창 위 초록 차양
const ROOF = '#c6cbc8';           // 옥상 바닥(콘크리트)
const PARAPET = '#f3f1ea';        // 옥상 난간(흰 띠)
const UNIT_BOX = new THREE.BoxGeometry(1, 1, 1);
const UNIT_EDGES = new THREE.EdgesGeometry(UNIT_BOX);

function rng(seed) {
    let a = seed >>> 0;
    return () => {
        a += 0x6d2b79f5;
        let t = a;
        t = Math.imul(t ^ (t >>> 15), t | 1);
        t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

function escapeHtml(text) {
    return String(text ?? '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
}

function shade(hex, amount) {
    const c = new THREE.Color(hex);
    const hsl = {};
    c.getHSL(hsl);
    c.setHSL(hsl.h, hsl.s, Math.max(0, Math.min(1, hsl.l + amount)));
    return `#${c.getHexString()}`;
}

export class GuideScene {
    constructor(container, options = {}) {
        this.container = container;
        this.options = options;
        this.layout = null;
        this.view = 'all';
        this.route = null;
        this.selectedRoomId = null;
        this.autoRotate = false;
        this.materials = new Map();
        this.textures = new Map();
        this.labels = [];
        this.pickables = [];
        this.clock = new THREE.Clock();

        this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, preserveDrawingBuffer: false });
        this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
        this.renderer.shadowMap.enabled = true;
        this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
        this.renderer.domElement.className = 'cg-canvas';
        container.appendChild(this.renderer.domElement);

        this.labelLayer = document.createElement('div');
        this.labelLayer.className = 'cg-label-layer';
        container.appendChild(this.labelLayer);

        this.scene = new THREE.Scene();
        this.scene.background = new THREE.Color('#dfeaf5');
        this.scene.fog = new THREE.Fog('#dfeaf5', 140, 320);

        this.camera = new THREE.PerspectiveCamera(38, 1, 0.5, 900);
        this.orbit = { target: new THREE.Vector3(), radius: 70, theta: -0.45, phi: 0.92 };
        this.goal = { target: new THREE.Vector3(), radius: 70, theta: -0.45, phi: 0.92 };

        const hemi = new THREE.HemisphereLight('#f1f7ff', '#8aa27a', 1.9);
        this.scene.add(hemi);
        this.sun = new THREE.DirectionalLight('#fff6e8', 2.1);
        this.sun.castShadow = true;
        this.sun.shadow.mapSize.set(2048, 2048);
        this.sun.shadow.bias = -0.0006;
        this.sun.shadow.normalBias = 0.02;
        this.scene.add(this.sun, this.sun.target);

        this.worldGroup = new THREE.Group();
        this.buildingGroup = new THREE.Group();
        this.highlightGroup = new THREE.Group();
        this.routeGroup = new THREE.Group();
        this.scene.add(this.worldGroup, this.buildingGroup, this.highlightGroup, this.routeGroup);

        this.raycaster = new THREE.Raycaster();
        this._bindInput();
        this._resizeObserver = new ResizeObserver(() => this.resize());
        this._resizeObserver.observe(container);
        this.resize();
        this._loop = this._loop.bind(this);
        this._frame = requestAnimationFrame(this._loop);
    }

    // ------------------------------------------------------------ 공개 API
    setLayout(layout, { keepCamera = false } = {}) {
        const first = !this.layout;
        const sizeChanged = !first && (this.layout.site.cols !== layout.site.cols || this.layout.site.rows !== layout.site.rows);
        this.layout = layout;
        this.grid = buildGrid(layout);
        this.half = { x: layout.site.cols / 2, z: layout.site.rows / 2 };
        if (typeof this.view === 'number' && !(layout.buildings || []).some(b => hasFloor(b, this.view))) {
            this.view = 'all';
        }
        this._buildWorld();
        this._buildBuildings();
        this._buildHighlight();
        if (this.route) this._buildRoute();
        if (first || sizeChanged || !keepCamera) this.resetView(first);
    }

    setFloor(view) {
        this.view = view === 'all' ? 'all' : Number(view);
        this._buildBuildings();
        this._buildHighlight();
    }

    setRoute(route) {
        this.route = route && route.ok ? route : null;
        this._buildRoute();
        this._buildBuildings();
    }

    selectRoom(roomId) {
        this.selectedRoomId = roomId || null;
        this._buildHighlight();
        this._refreshLabelState();
    }

    setAutoRotate(on) { this.autoRotate = !!on; }

    resetView(immediate = false) {
        if (!this.layout) return;
        const { cols, rows } = this.layout.site;
        this.goal.target.set(0, 0, 2);
        this.goal.radius = Math.max(cols, rows * 1.4) * 1.18;
        this.goal.theta = -0.45;
        this.goal.phi = 0.92;
        if (immediate) this._snap();
    }

    zoom(factor) {
        this.goal.radius = THREE.MathUtils.clamp(this.goal.radius * factor, 6, 260);
    }

    focusRoom(roomId, { close = true } = {}) {
        const found = this._room(roomId);
        if (!found) return;
        const { building, room } = found;
        this.goal.target.copy(this._world(building.x + room.x + room.w / 2, building.z + room.z + room.d / 2, room.floor));
        if (close) this.goal.radius = Math.min(this.goal.radius, 34);
    }

    focusPath(points) {
        if (!points || !points.length) return;
        const box = new THREE.Box3();
        for (const p of points) box.expandByPoint(this._world(p.x, p.z, p.f));
        const center = box.getCenter(new THREE.Vector3());
        const size = box.getSize(new THREE.Vector3());
        this.goal.target.copy(center);
        // 세로로 긴 휴대폰 화면에서는 가로 폭이 좁으므로 그만큼 더 멀리서 본다.
        const aspect = Math.min(1, this.camera.aspect || 1);
        const span = Math.max(size.x / aspect, size.z);
        this.goal.radius = THREE.MathUtils.clamp(span * 1.5 + 14, 16, 180);
    }

    dispose() {
        cancelAnimationFrame(this._frame);
        this._resizeObserver.disconnect();
        this._unbindInput();
        this.renderer.dispose();
        this.container.innerHTML = '';
    }

    resize() {
        const w = this.container.clientWidth || 1;
        const h = this.container.clientHeight || 1;
        this.renderer.setSize(w, h, false);
        this.camera.aspect = w / h;
        this.camera.updateProjectionMatrix();
        this.size = { w, h };
    }

    // ------------------------------------------------------------ 도우미
    _world(gx, gz, floor = 1, lift = 0) {
        return new THREE.Vector3(gx - this.half.x, (floor - 1) * FH + lift, gz - this.half.z);
    }

    _room(roomId) {
        for (const building of this.layout?.buildings || []) {
            const room = building.rooms.find(r => r.id === roomId);
            if (room) return { building, room };
        }
        return null;
    }

    _mat(color, opts = {}) {
        const key = `${color}|${opts.opacity ?? 1}|${opts.basic ? 1 : 0}|${opts.map ? opts.map.uuid : ''}|${opts.emissive || ''}`;
        let mat = this.materials.get(key);
        if (!mat) {
            const params = { color, map: opts.map || null };
            if ((opts.opacity ?? 1) < 1) Object.assign(params, { transparent: true, opacity: opts.opacity, depthWrite: false });
            if (opts.emissive) params.emissive = new THREE.Color(opts.emissive);
            mat = opts.basic ? new THREE.MeshBasicMaterial(params) : new THREE.MeshLambertMaterial(params);
            this.materials.set(key, mat);
        }
        return mat;
    }

    _line(color, opacity = 1, depthTest = true) {
        const key = `line|${color}|${opacity}|${depthTest}`;
        let mat = this.materials.get(key);
        if (!mat) {
            mat = new THREE.LineBasicMaterial({ color, transparent: opacity < 1 || !depthTest, opacity, depthTest });
            this.materials.set(key, mat);
        }
        return mat;
    }

    _edges(scale, position, material) {
        const line = new THREE.LineSegments(UNIT_EDGES, material);
        line.userData.sharedGeometry = true;
        line.scale.copy(scale);
        line.position.copy(position);
        return line;
    }

    _box(group, gx, gz, w, d, y0, h, material, { cast = true, receive = true, data = null } = {}) {
        const mesh = new THREE.Mesh(UNIT_BOX, material);
        mesh.scale.set(Math.max(w, 0.001), Math.max(h, 0.001), Math.max(d, 0.001));
        mesh.position.set(gx + w / 2 - this.half.x, y0 + h / 2, gz + d / 2 - this.half.z);
        mesh.castShadow = cast;
        mesh.receiveShadow = receive;
        if (data) {
            mesh.userData = data;
            this.pickables.push(mesh);
        }
        group.add(mesh);
        return mesh;
    }

    _canvasTexture(key, width, height, draw, repeat = [1, 1]) {
        const cacheKey = `${key}|${repeat.join('x')}`;
        if (this.textures.has(cacheKey)) return this.textures.get(cacheKey);
        let base = this.textures.get(`${key}|base`);
        if (!base) {
            const canvas = document.createElement('canvas');
            canvas.width = width;
            canvas.height = height;
            draw(canvas.getContext('2d'), width, height);
            base = new THREE.CanvasTexture(canvas);
            base.colorSpace = THREE.SRGBColorSpace;
            base.anisotropy = 4;
            this.textures.set(`${key}|base`, base);
        }
        const tex = base.clone();
        tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
        tex.repeat.set(repeat[0], repeat[1]);
        tex.needsUpdate = true;
        this.textures.set(cacheKey, tex);
        return tex;
    }

    /**
     * 학교 외벽 한 칸(격자 2칸 폭 × 한 층 높이): 색 벽에 흰 창틀의 파란 유리창 두 짝,
     * 창 아래 벽띠와 층마다 흰 콘크리트 띠. 창 위 끝은 한 층 높이의 WINDOW_TOP 지점(차양 높이).
     */
    _facadeTexture(color, repeat) {
        return this._canvasTexture(`facade-${color}`, 256, 160, (ctx, w, h) => {
            // 옆면은 해를 비스듬히 받아 어두워 보이므로 벽색을 조금 밝혀 칠한다.
            ctx.fillStyle = shade(color, 0.05);
            ctx.fillRect(0, 0, w, h);
            // 층 띠(흰 콘크리트)와 그 위 그늘
            ctx.fillStyle = '#f5f2ea';
            ctx.fillRect(0, h - 14, w, 14);
            ctx.fillStyle = 'rgba(0,0,0,.12)';
            ctx.fillRect(0, h - 14, w, 2);
            // 창 아래 벽띠는 조금 진하게
            ctx.fillStyle = shade(color, -0.05);
            ctx.fillRect(0, Math.round(h * 0.74), w, h - 14 - Math.round(h * 0.74));
            const y0 = Math.round(h * (1 - WINDOW_TOP));
            const y1 = Math.round(h * 0.68);
            for (const [x0, x1] of [[16, 120], [136, 240]]) {
                ctx.fillStyle = '#fbfbf8';
                ctx.fillRect(x0 - 4, y0 - 4, x1 - x0 + 8, y1 - y0 + 8);
                const glass = ctx.createLinearGradient(0, y0, 0, y1);
                glass.addColorStop(0, '#b3d8f4');
                glass.addColorStop(0.5, '#6aa4d8');
                glass.addColorStop(1, '#3c70aa');
                ctx.fillStyle = glass;
                ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
                // 창살: 가운데 세로 + 윗창
                ctx.fillStyle = '#fbfbf8';
                ctx.fillRect((x0 + x1) / 2 - 2, y0, 4, y1 - y0);
                ctx.fillRect(x0, y0 + (y1 - y0) * 0.3 - 2, x1 - x0, 4);
                // 유리 반사
                ctx.fillStyle = 'rgba(255,255,255,.22)';
                ctx.beginPath();
                ctx.moveTo(x0 + 4, y1 - 3);
                ctx.lineTo(x0 + 30, y0 + 3);
                ctx.lineTo(x0 + 46, y0 + 3);
                ctx.lineTo(x0 + 20, y1 - 3);
                ctx.fill();
                // 창턱
                ctx.fillStyle = '#ece8df';
                ctx.fillRect(x0 - 6, y1 + 4, x1 - x0 + 12, 4);
            }
        }, repeat);
    }

    // ------------------------------------------------------------ 바깥(부지)
    _clear(group) {
        for (const child of [...group.children]) {
            group.remove(child);
            if (child.geometry && child.geometry !== UNIT_BOX && child.geometry !== UNIT_EDGES && !child.userData.sharedGeometry) {
                child.geometry.dispose();
            }
            if (child.isInstancedMesh) child.dispose();
        }
    }

    _buildWorld() {
        this._clear(this.worldGroup);
        this.labels = this.labels.filter(l => { if (l.world) l.el.remove(); return !l.world; });
        const { cols, rows } = this.layout.site;
        const g = this.worldGroup;

        const grass = new THREE.Mesh(new THREE.PlaneGeometry(cols + 160, rows + 160), this._mat('#a9d18e'));
        grass.rotation.x = -Math.PI / 2;
        grass.position.y = -0.05;
        grass.receiveShadow = true;
        g.add(grass);

        // 학교 부지(포장된 교정)
        this._box(g, 0, 0, cols, rows, -0.05, 0.06, this._mat('#e7e2d6'), { cast: false });

        // 담장: 교문·후문 폭만큼 비워 둔다. 끊긴 곳 사이는 한 덩어리로 세운다.
        const fence = this._mat('#b9b1a0');
        const spans = this.layout.gates.map(gt => gateSpan(gt, this.layout.site));
        const open = (side, i) => spans.some(sp => sp.side === side && i >= sp.at && i < sp.at + sp.w);
        const t = 0.15;
        const runFence = (side, len, place) => {
            let start = null;
            for (let i = 0; i <= len; i += 1) {
                const solid = i < len && !open(side, i);
                if (solid && start === null) start = i;
                if (!solid && start !== null) { place(start, i - start); start = null; }
            }
        };
        runFence('n', cols, (a, n) => this._box(g, a, 0, n, t, 0, 0.45, fence, { receive: false }));
        runFence('s', cols, (a, n) => this._box(g, a, rows - t, n, t, 0, 0.45, fence, { receive: false }));
        runFence('w', rows, (a, n) => this._box(g, 0, a, t, n, 0, 0.45, fence, { receive: false }));
        runFence('e', rows, (a, n) => this._box(g, cols - t, a, t, n, 0, 0.45, fence, { receive: false }));

        for (const lm of this.layout.landmarks) this._buildLandmark(lm);
        for (const gate of this.layout.gates) this._buildGate(gate);
        this._buildTrees();

        const sunTarget = new THREE.Vector3(0, 0, 0);
        const span = Math.max(cols, rows);
        this.sun.position.set(-span * 0.45, span * 0.9, span * 0.55);
        this.sun.target.position.copy(sunTarget);
        const cam = this.sun.shadow.camera;
        cam.left = -span * 0.8; cam.right = span * 0.8; cam.top = span * 0.8; cam.bottom = -span * 0.8;
        cam.near = 1; cam.far = span * 3;
        cam.updateProjectionMatrix();
    }

    _buildLandmark(lm) {
        const g = this.worldGroup;
        const type = LANDMARK_TYPES[lm.type] || LANDMARK_TYPES.etc;
        let material = this._mat(type.color);
        if (lm.type === 'field') {
            const tex = this._canvasTexture('field', 512, 320, (ctx, w, h) => {
                ctx.fillStyle = '#d6b07c';
                ctx.fillRect(0, 0, w, h);
                ctx.strokeStyle = 'rgba(255,255,255,.9)';
                ctx.lineWidth = 5;
                for (const inset of [26, 50]) {
                    const r = (h - inset * 2) / 2;
                    ctx.beginPath();
                    ctx.moveTo(inset + r, inset);
                    ctx.lineTo(w - inset - r, inset);
                    ctx.arc(w - inset - r, h / 2, r, -Math.PI / 2, Math.PI / 2);
                    ctx.lineTo(inset + r, h - inset);
                    ctx.arc(inset + r, h / 2, r, Math.PI / 2, Math.PI * 1.5);
                    ctx.stroke();
                }
                ctx.fillStyle = '#9fcf80';
                ctx.fillRect(w / 2 - 70, h / 2 - 45, 140, 90);
            });
            material = this._mat('#ffffff', { map: tex });
        } else if (lm.type === 'parking') {
            // 주차칸 한 줄(위·아래 두 열) 무늬를 크기에 맞춰 반복한다. 1칸 ≈ 주차면 폭.
            const tex = this._canvasTexture('parking', 128, 256, (ctx, w, h) => {
                ctx.fillStyle = '#6b7280';
                ctx.fillRect(0, 0, w, h);
                ctx.strokeStyle = '#f8fafc';
                ctx.lineWidth = 4;
                for (const [y0, y1] of [[6, 100], [h - 100, h - 6]]) {
                    for (const x of [2, w / 2]) { ctx.beginPath(); ctx.moveTo(x, y0); ctx.lineTo(x, y1); ctx.stroke(); }
                }
                ctx.strokeStyle = 'rgba(250,204,21,.9)';
                ctx.setLineDash([14, 12]);
                ctx.beginPath(); ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();
            }, [Math.max(1, Math.round(lm.w / 2)), Math.max(1, Math.round(lm.d / 7))]);
            material = this._mat('#ffffff', { map: tex });
        }
        // 운동장이 길게 누워 있으면 무늬를 돌려 붙인다.
        const mesh = this._box(g, lm.x, lm.z, lm.w, lm.d, 0, 0.04, material, { cast: false });
        if (lm.type === 'field' && lm.d > lm.w) {
            mesh.scale.set(lm.d, 0.04, lm.w);
            mesh.rotation.y = Math.PI / 2;
        }
        if (lm.type === 'parking') this._buildCars(lm);
        if (lm.type === 'playground') {
            const r = rng(lm.x * 131 + lm.z * 17);
            const colors = ['#f87171', '#60a5fa', '#facc15', '#34d399'];
            for (let i = 0; i < 4; i += 1) {
                const px = lm.x + 1 + r() * Math.max(1, lm.w - 3);
                const pz = lm.z + 1 + r() * Math.max(1, lm.d - 3);
                this._box(g, px, pz, 1.4, 1.4, 0.04, 0.5 + r() * 0.9, this._mat(colors[i % colors.length]));
            }
        }
        if (lm.label) {
            this._label(`<span>${escapeHtml(landmarkLabel(lm))}</span>`, 'cg-label--landmark',
                this._world(lm.x + lm.w / 2, lm.z + lm.d / 2, 1, 0.3), { world: true });
        }
    }

    _buildCars(lm) {
        const r = rng(lm.x * 97 + lm.z * 31 + lm.w);
        const colors = ['#f8fafc', '#1f2937', '#dc2626', '#2563eb', '#94a3b8', '#facc15'];
        const rowsOfCars = Math.max(1, Math.round(lm.d / 7)) * 2;
        const stall = lm.d / rowsOfCars;
        const len = Math.min(stall * 0.72, 2.3);
        for (let row = 0; row < rowsOfCars; row += 1) {
            // 위 열은 통로 쪽(아래)으로, 아래 열은 통로 쪽(위)으로 붙여 세운다.
            const cz = row % 2 === 0 ? lm.z + row * stall + 0.15 : lm.z + (row + 1) * stall - len - 0.15;
            for (let i = 0; i + 1 <= lm.w; i += 1) {
                if (r() > 0.45) continue;
                const cx = lm.x + i + 0.18;
                this._box(this.worldGroup, cx, cz, 0.64, len, 0.04, 0.34, this._mat(colors[Math.floor(r() * colors.length)]));
                this._box(this.worldGroup, cx + 0.07, cz + len * 0.28, 0.5, len * 0.44, 0.38, 0.22, this._mat('#334155'));
            }
        }
    }

    _buildGate(gate) {
        const g = this.worldGroup;
        const span = gateSpan(gate, this.layout.site);
        const kind = GATE_KINDS[span.kind];
        const main = span.kind === 'main';
        const pillar = this._mat(main ? '#8b8f98' : '#a3a9b3');
        const height = main ? 1.9 : 1.3;
        const { cols, rows } = this.layout.site;
        const p = 0.6;
        // 기둥 두 개를 담장 선 위 문 양 끝에 세운다.
        const line = { n: -0.22, s: rows - 0.38, w: -0.22, e: cols - 0.38 }[span.side];
        const ends = span.horizontal
            ? [[span.at - p, line], [span.at + span.w, line]]
            : [[line, span.at - p], [line, span.at + span.w]];
        for (const [x, z] of ends) this._box(g, x, z, p, p, 0, height, pillar);
        if (main) {
            // 교문 위 가로 간판
            const [a, b] = ends;
            const sign = this._mat(kind.color);
            if (span.horizontal) this._box(g, a[0], a[1] + 0.12, b[0] + p - a[0], 0.36, height - 0.42, 0.4, sign);
            else this._box(g, a[0] + 0.12, a[1], 0.36, b[1] + p - a[1], height - 0.42, 0.4, sign);
        }
        // 바닥에 문 폭만큼 진입로 표시
        const pave = this._mat(main ? '#cfd8c4' : '#d5dde0');
        const depth = 1.4;
        if (span.horizontal) this._box(g, span.at, span.side === 'n' ? 0 : rows - depth, span.w, depth, 0, 0.03, pave, { cast: false });
        else this._box(g, span.side === 'w' ? 0 : cols - depth, span.at, depth, span.w, 0, 0.03, pave, { cast: false });
        this._label(`<i class="fa-solid ${kind.icon}"></i> ${escapeHtml(gate.label || kind.label)}`,
            `cg-label--gate cg-gate-${span.kind}`,
            this._world(span.anchor.x, span.anchor.z, 1, height + 0.5), { world: true });
    }

    _buildTrees() {
        const { cols, rows } = this.layout.site;
        const r = rng(cols * 1000 + rows);
        const blocked = (x, z, pad = 1) => {
            if (this.layout.buildings.some(b => x > b.x - pad && x < b.x + b.cols + pad && z > b.z - pad && z < b.z + b.rows + pad)) return true;
            if (this.layout.landmarks.some(l => l.type !== 'garden' && x > l.x - 0.5 && x < l.x + l.w + 0.5 && z > l.z - 0.5 && z < l.z + l.d + 0.5)) return true;
            return this.layout.gates.some(gt => Math.abs(gt.x - x) < 5 && Math.abs(gt.z - z) < 5);
        };
        const spots = [];
        // 담장 바깥 둘레
        for (let i = 0; i < 90; i += 1) {
            const side = Math.floor(r() * 4);
            const t = r();
            const off = 2 + r() * 10;
            const x = side === 0 ? t * cols : side === 1 ? t * cols : side === 2 ? -off : cols + off;
            const z = side === 0 ? -off : side === 1 ? rows + off : t * rows;
            if (!this.layout.gates.some(gt => Math.abs(gt.x - x) < 6 && Math.abs(gt.z - z) < 12)) spots.push([x, z, 1 + r() * 0.5]);
        }
        // 담장 안쪽 가장자리
        for (let i = 0; i < 70; i += 1) {
            const side = Math.floor(r() * 4);
            const t = r();
            const off = 0.8 + r() * 1.6;
            const x = side < 2 ? t * cols : side === 2 ? off : cols - off;
            const z = side === 0 ? off : side === 1 ? rows - off : t * rows;
            if (!blocked(x, z)) spots.push([x, z, 0.8 + r() * 0.4]);
        }
        // 화단
        for (const lm of this.layout.landmarks.filter(l => l.type === 'garden')) {
            const count = Math.max(2, Math.round(lm.w * lm.d / 6));
            for (let i = 0; i < count; i += 1) {
                spots.push([lm.x + 0.6 + r() * (lm.w - 1.2), lm.z + 0.6 + r() * (lm.d - 1.2), 0.7 + r() * 0.5]);
            }
        }
        if (!spots.length) return;
        const trunkGeo = new THREE.CylinderGeometry(0.12, 0.16, 1, 6);
        const leafGeo = new THREE.IcosahedronGeometry(1, 0);
        const trunks = new THREE.InstancedMesh(trunkGeo, this._mat('#8b6b4a'), spots.length);
        const leaves = new THREE.InstancedMesh(leafGeo, this._mat('#5f9e58'), spots.length);
        const m = new THREE.Matrix4();
        const q = new THREE.Quaternion();
        const color = new THREE.Color();
        spots.forEach(([x, z, s], i) => {
            m.compose(new THREE.Vector3(x - this.half.x, 0.5 * s, z - this.half.z), q, new THREE.Vector3(s, s, s));
            trunks.setMatrixAt(i, m);
            m.compose(new THREE.Vector3(x - this.half.x, 1.5 * s, z - this.half.z),
                new THREE.Quaternion().setFromEuler(new THREE.Euler(0, r() * 3, 0)),
                new THREE.Vector3(0.9 * s, 1.1 * s, 0.9 * s));
            leaves.setMatrixAt(i, m);
            leaves.setColorAt(i, color.set(['#5f9e58', '#4f8f4c', '#6fae5f', '#7cb86a'][i % 4]));
        });
        for (const mesh of [trunks, leaves]) {
            mesh.castShadow = true;
            mesh.receiveShadow = false;
            this.worldGroup.add(mesh);
        }
    }

    // ------------------------------------------------------------ 건물
    _floorMode(building, floor) {
        if (this.view === 'all') return 'shell';
        if (floor < this.view) return 'shell';
        if (floor === this.view) return 'plan';
        return 'ghost';
    }

    _buildBuildings() {
        if (!this.layout) return;
        this._clear(this.buildingGroup);
        this.pickables = [];
        this.labels = this.labels.filter(l => { if (!l.world) l.el.remove(); return l.world; });
        for (const building of this.layout.buildings) {
            let visibleTop = 0;
            for (let f = building.baseFloor; f <= topFloor(building); f += 1) {
                const mode = this._floorMode(building, f);
                if (mode === 'shell') { this._buildShell(building, f); visibleTop = f * FH; }
                else if (mode === 'plan') { this._buildPlan(building, f); visibleTop = (f - 1) * FH + WALL_H; }
                else this._buildGhost(building, f);
            }
            if (building.baseFloor > 1) this._buildPillars(building);
            if (building.kind !== 'connector') {
                const kind = BUILDING_KINDS[building.kind] || BUILDING_KINDS.etc;
                const sub = `${kind.label !== building.name ? `${kind.label} · ` : ''}${building.floors}층`;
                this._label(
                    `<b>${escapeHtml(building.name)}</b><small>${escapeHtml(sub)}</small>`,
                    'cg-label--building',
                    this._world(building.x + building.cols / 2, building.z + building.rows / 2, 1, visibleTop + 1.1),
                );
            }
        }
        this._refreshLabelState();
    }

    _buildShell(building, floor) {
        const color = buildingColor(building);
        const y0 = (floor - 1) * FH;
        const ghostly = !!this.route && this.view !== 'all';
        const opacity = ghostly ? 0.42 : 1;
        const connector = building.kind === 'connector';
        // 창 하나 = 2칸. 벽 끝에서 창이 반쯤 잘리지 않게 창 수를 반올림한다.
        const bays = len => Math.max(1, Math.round(len / (connector ? 1 : 2)));
        const sideX = this._facadeTexture(color, [bays(building.rows), 1]);
        const sideZ = this._facadeTexture(color, [bays(building.cols), 1]);
        const top = this._mat(shade(color, -0.08), { opacity });
        const mats = [
            this._mat('#ffffff', { map: sideX, opacity }), this._mat('#ffffff', { map: sideX, opacity }),
            top, top,
            this._mat('#ffffff', { map: sideZ, opacity }), this._mat('#ffffff', { map: sideZ, opacity }),
        ];
        const mesh = this._box(this.buildingGroup, building.x, building.z, building.cols, building.rows, y0, FH, mats,
            { data: { buildingId: building.id, floor }, cast: !ghostly });
        mesh.material = mats;
        if (!connector) this._buildAwnings(building, y0, opacity, bays);
        if (floor === 1 && canHaveDoors(building)) this._buildDoorsOutside(building, { opacity, facade: true });
        if (floor === topFloor(building)) this._buildRoof(building, y0 + FH, opacity, ghostly);
    }

    /**
     * 긴 두 외벽의 창마다 초록 차양(학교 건물의 햇빛 가리개)을 단다. 창 칸마다 하나씩이라
     * 한 층에 수십 개가 되므로 층마다 InstancedMesh 두 개(차양판·앞턱)로 그린다.
     */
    _buildAwnings(building, y0, opacity, bays) {
        const alongX = building.cols >= building.rows;
        const len = alongX ? building.cols : building.rows;
        const n = bays(len);
        const bay = len / n;
        const out = 0.26;
        const y = y0 + FH * WINDOW_TOP + 0.02;
        const parts = [
            { mat: this._mat(AWNING, { opacity }), depth: out, h: 0.04, dy: 0, edge: 0 },
            { mat: this._mat(shade(AWNING, -0.12), { opacity }), depth: 0.035, h: 0.09, dy: -0.07, edge: out - 0.035 },
        ];
        const m = new THREE.Matrix4();
        const q = new THREE.Quaternion();
        for (const part of parts) {
            const mesh = new THREE.InstancedMesh(UNIT_BOX, part.mat, n * 2);
            let i = 0;
            for (let k = 0; k < n; k += 1) {
                const a = (alongX ? building.x : building.z) + bay * (k + 0.06);
                const w = bay * 0.88;
                for (const far of [false, true]) {
                    // 벽에서 바깥쪽으로 edge ~ edge+depth 만큼 떨어진 판
                    const wall = alongX ? (far ? building.z + building.rows : building.z) : (far ? building.x + building.cols : building.x);
                    const o0 = far ? wall + part.edge : wall - part.edge - part.depth;
                    const [gx, gz, sw, sd] = alongX ? [a, o0, w, part.depth] : [o0, a, part.depth, w];
                    m.compose(new THREE.Vector3(gx + sw / 2 - this.half.x, y + part.dy + part.h / 2, gz + sd / 2 - this.half.z),
                        q, new THREE.Vector3(sw, part.h, sd));
                    mesh.setMatrixAt(i, m);
                    i += 1;
                }
            }
            mesh.castShadow = true;
            this.buildingGroup.add(mesh);
        }
    }

    /** 옥상: 콘크리트 바닥 · 흰 난간 · 계단실 위 옥탑. */
    _buildRoof(building, y, opacity, ghostly) {
        const g = this.buildingGroup;
        const connector = building.kind === 'connector';
        this._box(g, building.x - 0.15, building.z - 0.15, building.cols + 0.3, building.rows + 0.3,
            y, 0.14, this._mat(connector ? shade(buildingColor(building), -0.2) : ROOF, { opacity }), { cast: !ghostly });
        if (connector || building.cols <= 4 || building.rows <= 4) return;
        const rail = this._mat(PARAPET, { opacity });
        const t = 0.18;
        const yy = y + 0.14;
        const x0 = building.x - 0.15;
        const z0 = building.z - 0.15;
        const w = building.cols + 0.3;
        const d = building.rows + 0.3;
        this._box(g, x0, z0, w, t, yy, 0.3, rail);
        this._box(g, x0, z0 + d - t, w, t, yy, 0.3, rail);
        this._box(g, x0, z0, t, d, yy, 0.3, rail);
        this._box(g, x0 + w - t, z0, t, d, yy, 0.3, rail);
        // 옥탑: 맨 위층 계단 자리(없으면 건물 한쪽 끝)에 계단실 머리를 올린다.
        const stairs = building.rooms.find(r => r.type === 'stairs' && r.floor === topFloor(building));
        const tw = stairs ? stairs.w : Math.min(3, building.cols / 4);
        const td = stairs ? stairs.d : Math.min(3, building.rows / 2);
        const tx = building.x + (stairs ? stairs.x : building.cols - tw - 0.6);
        const tz = building.z + (stairs ? stairs.z : 0.6);
        const color = buildingColor(building);
        const wall = this._mat(color, { opacity });
        this._box(g, tx, tz, tw, td, yy, FH * 0.7, wall, { cast: !ghostly });
        this._box(g, tx - 0.1, tz - 0.1, tw + 0.2, td + 0.2, yy + FH * 0.7, 0.1, rail, { cast: !ghostly });
    }

    /** 건물출입문: 바깥 바닥의 초록 매트 + (외벽 보기일 때) 유리문과 차양. */
    _buildDoorsOutside(building, { opacity = 1, facade = false } = {}) {
        const g = this.buildingGroup;
        const mat = this._mat('#22c55e', { opacity });
        const glass = this._mat('#35506b', { opacity });
        const canopy = this._mat(shade(buildingColor(building), -0.28), { opacity });
        for (const door of building.doors || []) {
            const seg = doorSegment(building, door);
            const horizontal = seg.side === 'n' || seg.side === 's';
            const len = horizontal ? seg.x2 - seg.x1 : seg.z2 - seg.z1;
            const out = seg.side === 'n' || seg.side === 'w' ? -1 : 1;
            // 바깥쪽으로 depth 만큼 나온 판의 시작 좌표
            const outer = (depth) => ({
                x: horizontal ? seg.x1 : (out < 0 ? seg.x1 - depth : seg.x1),
                z: horizontal ? (out < 0 ? seg.z1 - depth : seg.z1) : seg.z1,
            });
            const m = outer(0.9);
            this._box(g, m.x, m.z, horizontal ? len : 0.9, horizontal ? 0.9 : len, 0, 0.05, mat, { cast: false });
            if (!facade) continue;
            const t = 0.06;
            const q = outer(t);
            this._box(g, q.x + (horizontal ? 0.12 : 0), q.z + (horizontal ? 0 : 0.12),
                horizontal ? len - 0.24 : t, horizontal ? t : len - 0.24, 0, FH * 0.72, glass, { cast: false });
            const c = outer(0.7);
            this._box(g, c.x - (horizontal ? 0.15 : 0), c.z - (horizontal ? 0 : 0.15),
                horizontal ? len + 0.3 : 0.7, horizontal ? 0.7 : len + 0.3, FH * 0.76, 0.08, canopy);
        }
    }

    _buildGhost(building, floor) {
        this.buildingGroup.add(this._edges(
            new THREE.Vector3(building.cols, FH, building.rows),
            this._world(building.x + building.cols / 2, building.z + building.rows / 2, floor, FH / 2),
            this._line(shade(buildingColor(building), -0.35), 0.35),
        ));
    }

    _buildPillars(building) {
        const mat = this._mat('#9ca3af');
        const h = (building.baseFloor - 1) * FH;
        const spots = [[0, 0], [building.cols - 0.5, 0], [0, building.rows - 0.5], [building.cols - 0.5, building.rows - 0.5]];
        for (const [dx, dz] of spots) this._box(this.buildingGroup, building.x + dx, building.z + dz, 0.5, 0.5, 0, h, mat);
    }

    _buildPlan(building, floor) {
        const g = this.buildingGroup;
        const y0 = (floor - 1) * FH;
        const color = buildingColor(building);
        const floorY = y0 + SLAB;
        // 바닥판(복도)
        this._box(g, building.x, building.z, building.cols, building.rows, y0, SLAB, this._mat('#f3f4f6'),
            { cast: false, data: { buildingId: building.id, floor } });

        // 외벽: 현관 칸은 비워 둔다.
        const outer = this._mat(shade(color, -0.04));
        const t = 0.16;
        // 건물출입문이 있으면 그 벽 방향으로만, 없으면(예전 배치) 현관(로비) 칸에서 벽을 튼다.
        const isOpen = (lx, lz, side) => {
            const c = this.grid.cell(floor, building.x + lx, building.z + lz);
            if (!c || !c.entry) return false;
            if (c.entry === true) return c.kind === 'entrance';
            return c.entry.includes(side);
        };
        const runWall = (horizontal, len, open, offset) => {
            let start = null;
            for (let i = 0; i <= len; i += 1) {
                const solid = i < len && !open(i);
                if (solid && start === null) start = i;
                if (!solid && start !== null) {
                    if (horizontal) this._box(g, building.x + start, building.z + offset, i - start, t, floorY, WALL_H, outer);
                    else this._box(g, building.x + offset, building.z + start, t, i - start, floorY, WALL_H, outer);
                    start = null;
                }
            }
        };
        const touchesOther = (lx, lz, dx, dz) => {
            const c = this.grid.cell(floor, building.x + lx + dx, building.z + lz + dz);
            return c && c.walk && !c.outdoor && c.building !== building;
        };
        // 다른 건물 복도와 맞닿은 곳(연결통로 입구)도 벽 없이 튼다.
        runWall(true, building.cols, i => isOpen(i, 0, 'n') || touchesOther(i, 0, 0, -1), 0);
        runWall(true, building.cols, i => isOpen(i, building.rows - 1, 's') || touchesOther(i, building.rows - 1, 0, 1), building.rows - t);
        runWall(false, building.rows, i => isOpen(0, i, 'w') || touchesOther(0, i, -1, 0), 0);
        runWall(false, building.rows, i => isOpen(building.cols - 1, i, 'e') || touchesOther(building.cols - 1, i, 1, 0), building.cols - t);
        if (floor === 1 && canHaveDoors(building)) {
            this._buildDoorsOutside(building);
            for (const door of building.doors || []) {
                const seg = doorSegment(building, door);
                const { dx, dz } = SIDES[seg.side];
                this._label(`<i class="fa-solid fa-door-open"></i> ${escapeHtml(door.label || '출입구')}`, 'cg-label--door',
                    this._world((seg.x1 + seg.x2) / 2 + dx * 1.3, (seg.z1 + seg.z2) / 2 + dz * 1.3, 1, 0.5));
            }
        }

        if (building.kind === 'connector') return;
        for (const room of building.rooms) {
            if (room.floor === floor) this._buildRoom(building, room, floorY);
        }
    }

    _buildRoom(building, room, floorY) {
        const g = this.buildingGroup;
        const type = ROOM_TYPES[room.type] || ROOM_TYPES.etc;
        const gx = building.x + room.x;
        const gz = building.z + room.z;
        const data = { roomId: room.id, buildingId: building.id, floor: room.floor };
        const inset = 0.04;
        this._box(g, gx + inset, gz + inset, room.w - inset * 2, room.d - inset * 2, floorY, 0.03, this._mat(type.color),
            { cast: false, data });

        if (room.type === 'stairs') {
            this._buildStairs(building, room, floorY, data);
            this._roomLabel(building, room, floorY);
            return;
        }
        if (room.type === 'elevator') {
            this._box(g, gx + 0.2, gz + 0.2, room.w - 0.4, room.d - 0.4, floorY, WALL_H * 1.1, this._mat('#818cf8', { opacity: 0.85 }), { data });
            this._roomLabel(building, room, floorY);
            return;
        }
        if (type.walk) {
            this._roomLabel(building, room, floorY);
            return;
        }

        // 교실 벽: 길찾기와 같은 자리(복도 쪽)에 문을 낸다.
        const wall = this._mat('#fbfbfa');
        const t = 0.09;
        const door = roomDoor(this.grid, building, room);
        const doorWidth = 0.9;
        const makeSide = (name, horizontal, fixed, len) => {
            const segments = [];
            if (door && door.side === name) {
                const c = door.center;
                segments.push([0, Math.max(0, c - doorWidth / 2)], [Math.min(len, c + doorWidth / 2), len]);
            } else {
                segments.push([0, len]);
            }
            for (const [a, b] of segments) {
                if (b - a < 0.05) continue;
                if (horizontal) this._box(g, gx + a, fixed, b - a, t, floorY, WALL_H, wall, { data });
                else this._box(g, fixed, gz + a, t, b - a, floorY, WALL_H, wall, { data });
            }
        };
        makeSide('n', true, gz + inset, room.w);
        makeSide('s', true, gz + room.d - inset - t, room.w);
        makeSide('w', false, gx + inset, room.d);
        makeSide('e', false, gx + room.w - inset - t, room.d);

        // 벽 윗면 테두리(교실 종류 색)
        g.add(this._edges(
            new THREE.Vector3(room.w - inset * 2, 0.001, room.d - inset * 2),
            this._world(gx + room.w / 2, gz + room.d / 2, 1, floorY + WALL_H + 0.01),
            this._line(type.edge),
        ));

        // 책상 몇 개(교실 느낌)
        if (['general', 'afterschool', 'special', 'care'].includes(room.type) && room.w >= 4 && room.d >= 4) {
            const desk = this._mat('#d6b98c');
            const cols = Math.min(4, Math.floor((room.w - 1.5) / 1.1));
            const rows = Math.min(3, Math.floor((room.d - 2) / 1.2));
            for (let r = 0; r < rows; r += 1) {
                for (let c = 0; c < cols; c += 1) {
                    this._box(g, gx + 0.9 + c * ((room.w - 1.8) / Math.max(1, cols)), gz + 1.3 + r * ((room.d - 2.3) / Math.max(1, rows)),
                        0.7, 0.45, floorY, 0.28, desk, { data, cast: false });
                }
            }
            // 칠판
            this._box(g, gx + room.w * 0.25, gz + inset + t, room.w * 0.5, 0.05, floorY + 0.2, 0.35, this._mat('#2f5d50'), { data, cast: false });
        }
        this._roomLabel(building, room, floorY);
    }

    /** 계단: 오르는 방향(dir)과 모양(U자·일자)에 맞춰 디딤판·중간 참·가운데 난간·방향 화살표를 세운다. */
    _buildStairs(building, room, floorY, data) {
        const g = this.buildingGroup;
        const gx = building.x + room.x;
        const gz = building.z + room.z;
        const plan = stairPlan(room, building);
        const H = WALL_H * 1.05;
        const tread = [this._mat('#f4ede4'), this._mat('#e3d4bf')];
        const nose = this._line('#b45309', 0.5);
        const pad = 0.05;
        const box = (r, h, mat) => {
            const mesh = this._box(g, gx + r.x + pad, gz + r.z + pad, Math.max(0.02, r.w - pad * 2), Math.max(0.02, r.d - pad * 2),
                floorY, h, mat, { data });
            g.add(this._edges(mesh.scale, mesh.position, nose));
        };
        for (const flight of plan.flights) {
            flight.steps.forEach((step, i) => box(step, step.h * H, tread[i % 2]));
        }
        if (plan.landing) box(plan.landing, plan.landing.h * H, this._mat('#eadfce'));
        if (plan.divider) {
            // U자 계단 가운데 벽(난간)
            const { a, b } = plan.divider;
            const horizontal = Math.abs(a.x - b.x) > Math.abs(a.z - b.z);
            this._box(g, gx + Math.min(a.x, b.x) - (horizontal ? 0 : 0.05), gz + Math.min(a.z, b.z) - (horizontal ? 0.05 : 0),
                horizontal ? Math.abs(a.x - b.x) : 0.1, horizontal ? 0.1 : Math.abs(a.z - b.z), floorY, H + 0.18, this._mat('#64748b'), { data });
        }
        // 오르는 방향 화살표: 디딤판 위를 따라가는 선 + 끝 화살촉 + 시작점
        const lift = 0.12;
        const pts = plan.path.map(p => this._world(gx + p.x, gz + p.z, 1, floorY + p.h * H + lift));
        const curve = new THREE.CurvePath();
        for (let i = 1; i < pts.length; i += 1) curve.add(new THREE.LineCurve3(pts[i - 1], pts[i]));
        const arrowMat = this._mat('#c2410c', { basic: true });
        g.add(new THREE.Mesh(new THREE.TubeGeometry(curve, pts.length * 8, 0.05, 6, false), arrowMat));
        const tail = pts[pts.length - 1];
        const head = new THREE.Mesh(new THREE.ConeGeometry(0.2, 0.42, 12), arrowMat);
        head.position.copy(tail);
        head.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), tail.clone().sub(pts[pts.length - 2]).normalize());
        g.add(head);
        const start = new THREE.Mesh(new THREE.CylinderGeometry(0.14, 0.14, 0.04, 14), arrowMat);
        start.position.copy(pts[0]);
        g.add(start);
    }

    _roomLabel(building, room, floorY) {
        const type = ROOM_TYPES[room.type] || ROOM_TYPES.etc;
        if (room.type === 'corridor') return;
        const small = room.w * room.d < 8;
        const short = roomShortLabel(room);
        const name = room.name && room.name !== short ? room.name : '';
        let html;
        if (small || type.walk) html = `<span>${escapeHtml(room.name || short || type.label)}</span>`;
        else html = `${short ? `<b>${escapeHtml(short)}</b>` : ''}${name ? `<span>${escapeHtml(name)}</span>` : ''}`;
        if (!html) return;
        const courses = (room.courses || []).map(c => c.name).filter(Boolean);
        const courseHtml = !small && courses.length ? `<em>${escapeHtml(courses.slice(0, 2).join(' · '))}${courses.length > 2 ? ' 외' : ''}</em>` : '';
        this._label(html + courseHtml, `cg-label--room cg-room-${room.type}${type.walk ? ' cg-label--minor' : ''}`,
            this._world(building.x + room.x + room.w / 2, building.z + room.z + room.d / 2, 1, floorY + WALL_H + 0.25),
            { roomId: room.id });
    }

    // ------------------------------------------------------------ 선택 강조
    _buildHighlight() {
        this._clear(this.highlightGroup);
        const found = this.selectedRoomId && this._room(this.selectedRoomId);
        if (!found) return;
        const { building, room } = found;
        const visible = this.view === 'all' || this.view === room.floor;
        if (!visible) return;
        const y0 = (room.floor - 1) * FH + SLAB;
        const h = this.view === 'all' ? FH * 0.9 : WALL_H + 0.1;
        const mesh = this._box(this.highlightGroup, building.x + room.x - 0.05, building.z + room.z - 0.05, room.w + 0.1, room.d + 0.1,
            y0, h, this._mat('#3b82f6', { opacity: 0.22, basic: true }), { cast: false, receive: false });
        mesh.renderOrder = 5;
        this.pulse = mesh;
        const edge = this._edges(mesh.scale, mesh.position, this._line('#2563eb', 1, false));
        edge.renderOrder = 6;
        this.highlightGroup.add(edge);
    }

    // ------------------------------------------------------------ 경로
    _buildRoute() {
        this._clear(this.routeGroup);
        this.routeCurve = null;
        this.routeMarker = null;
        if (!this.route || !this.layout) return;
        const lift = SLAB + 0.2;
        const pts = [];
        const push = (v) => {
            const last = pts[pts.length - 1];
            if (!last || last.distanceToSquared(v) > 1e-6) pts.push(v);
        };
        const route = this.route;
        const from = route.from.anchor;
        push(this._world(from.x, from.z, from.f, lift));
        // 교실은 교실 가운데 → 문 → 복도 순서로 나가고, 들어올 때는 그 반대다(벽을 가로지르지 않게).
        const door = route.from.door;
        if (door) {
            push(this._world(door.inside.x, door.inside.z, door.f, lift));
            push(this._world(door.x, door.z, door.f, lift));
        }
        const cells = route.path;
        const stairAt = i => cells[i] && cells[i].kind === 'stairs';
        const climbs = (i, j) => cells[i] && cells[j] && cells[i].f !== cells[j].f && stairAt(i) && stairAt(j);
        for (let i = 0; i < cells.length; i += 1) {
            const p = cells[i];
            // 계단을 오르내리기 직전·직후의 계단 칸 가운데는 건너뛰고, 계단 디딤판 선을 따라간다.
            if (stairAt(i) && (climbs(i, i + 1) || climbs(i - 1, i))) {
                if (climbs(i, i + 1)) this._stairRoute(p, cells[i + 1], lift).forEach(push);
                else if (i === cells.length - 1) push(this._world(p.x, p.z, p.f, lift));
                continue;
            }
            push(this._world(p.x, p.z, p.f, lift));
        }
        const inDoor = route.to.door;
        if (inDoor) {
            push(this._world(inDoor.x, inDoor.z, inDoor.f, lift));
            push(this._world(inDoor.inside.x, inDoor.inside.z, inDoor.f, lift));
        }
        const to = route.to.anchor;
        push(this._world(to.x, to.z, to.f, lift));
        // 일직선 위의 중간점은 없애 곡선을 매끈하게 한다.
        const simple = pts.filter((p, i) => {
            if (i === 0 || i === pts.length - 1) return true;
            const a = new THREE.Vector3().subVectors(p, pts[i - 1]).normalize();
            const b = new THREE.Vector3().subVectors(pts[i + 1], p).normalize();
            return a.dot(b) < 0.999;
        });
        const path = new THREE.CurvePath();
        for (let i = 1; i < simple.length; i += 1) path.add(new THREE.LineCurve3(simple[i - 1], simple[i]));
        this.routeCurve = path;
        this.routeLength = path.getLength();
        const tube = new THREE.Mesh(
            new THREE.TubeGeometry(path, Math.max(32, simple.length * 12), 0.16, 8, false),
            new THREE.MeshBasicMaterial({ color: '#2563eb', transparent: true, opacity: 0.92, depthTest: false }),
        );
        tube.renderOrder = 10;
        this.routeGroup.add(tube);

        // 진행 방향 화살표
        const arrowGeo = new THREE.ConeGeometry(0.28, 0.6, 12);
        arrowGeo.rotateX(Math.PI / 2);
        const arrowMat = new THREE.MeshBasicMaterial({ color: '#ffffff', depthTest: false });
        const count = Math.max(2, Math.floor(this.routeLength / 3));
        this.routeArrows = new THREE.InstancedMesh(arrowGeo, arrowMat, count);
        this.routeArrows.renderOrder = 11;
        this.routeArrows.frustumCulled = false;
        this.routeGroup.add(this.routeArrows);

        const marker = new THREE.Mesh(new THREE.SphereGeometry(0.42, 20, 14),
            new THREE.MeshBasicMaterial({ color: '#f97316', depthTest: false }));
        marker.renderOrder = 12;
        this.routeMarker = marker;
        this.routeGroup.add(marker);

        this._pin(simple[0], '#16a34a');
        this._pin(simple[simple.length - 1], '#dc2626');
    }

    /** 계단 한 층 오르내림: 아래층 계단의 오르는 선(U자면 참에서 되돌아)을 따라 높이를 올린다. */
    _stairRoute(a, b, lift) {
        const low = a.f < b.f ? a : b;
        const cell = this.grid.cell(low.f, Math.floor(low.x), Math.floor(low.z));
        const room = cell?.room;
        const building = cell?.building;
        if (!room || room.type !== 'stairs') return [this._world(a.x, a.z, a.f, lift), this._world(b.x, b.z, b.f, lift)];
        const pts = stairPlan(room, building).path.map(p => this._world(
            building.x + room.x + p.x, building.z + room.z + p.z, low.f, lift + p.h * FH));
        return a.f < b.f ? pts : pts.reverse();
    }

    _pin(position, color) {
        const group = new THREE.Group();
        const mat = new THREE.MeshBasicMaterial({ color, depthTest: false });
        const head = new THREE.Mesh(new THREE.SphereGeometry(0.55, 20, 14), mat);
        head.position.y = 2.1;
        const stem = new THREE.Mesh(new THREE.ConeGeometry(0.4, 1.6, 16), mat);
        stem.rotation.x = Math.PI;
        stem.position.y = 1.2;
        const ring = new THREE.Mesh(new THREE.RingGeometry(0.6, 0.9, 32),
            new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.6, side: THREE.DoubleSide, depthTest: false }));
        ring.rotation.x = -Math.PI / 2;
        ring.position.y = 0.05;
        ring.userData.pulse = true;
        for (const m of [head, stem, ring]) { m.renderOrder = 13; group.add(m); }
        group.position.copy(position);
        this.routeGroup.add(group);
    }

    // ------------------------------------------------------------ 라벨
    _label(html, cls, position, extra = {}) {
        const el = document.createElement('div');
        el.className = `cg-label ${cls}`;
        el.innerHTML = html;
        this.labelLayer.appendChild(el);
        this.labels.push({ el, position, ...extra });
    }

    _refreshLabelState() {
        for (const label of this.labels) {
            label.el.classList.toggle('is-selected', !!label.roomId && label.roomId === this.selectedRoomId);
        }
    }

    _updateLabels() {
        const { w, h } = this.size;
        const v = new THREE.Vector3();
        const camPos = this.camera.position;
        for (const label of this.labels) {
            v.copy(label.position).project(this.camera);
            const hidden = v.z > 1 || v.x < -1.2 || v.x > 1.2 || v.y < -1.2 || v.y > 1.2;
            if (hidden) {
                if (!label.hidden) { label.el.style.display = 'none'; label.hidden = true; }
                continue;
            }
            if (label.hidden) { label.el.style.display = ''; label.hidden = false; }
            const x = Math.round((v.x + 1) / 2 * w);
            const y = Math.round((1 - v.y) / 2 * h);
            const key = `${x},${y}`;
            if (label.key !== key) {
                label.el.style.transform = `translate(${x}px, ${y}px) translate(-50%, -100%)`;
                label.key = key;
            }
            // 멀리 있는 교실 이름은 흐리게
            if (label.roomId) {
                const far = camPos.distanceTo(label.position) > 75;
                if (label.far !== far) { label.el.classList.toggle('is-far', far); label.far = far; }
            }
        }
    }

    // ------------------------------------------------------------ 입력
    _bindInput() {
        const el = this.renderer.domElement;
        this.pointers = new Map();
        this._handlers = {
            down: (e) => {
                el.setPointerCapture?.(e.pointerId);
                this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
                this.drag = {
                    x: e.clientX, y: e.clientY, moved: 0,
                    pan: e.button === 2 || e.shiftKey || e.ctrlKey || e.metaKey,
                };
                this.pinch = null;
                if (this.pointers.size === 2) this.pinch = this._pinchState();
                this.options.onInteract?.();
            },
            move: (e) => {
                if (!this.pointers.has(e.pointerId)) {
                    this._hover(e);
                    return;
                }
                const prev = this.pointers.get(e.pointerId);
                this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
                if (this.pointers.size === 2 && this.pinch) {
                    const now = this._pinchState();
                    this.goal.radius = THREE.MathUtils.clamp(this.goal.radius * (this.pinch.dist / now.dist), 6, 260);
                    this._pan(now.x - this.pinch.x, now.y - this.pinch.y);
                    this.pinch = now;
                    this.drag.moved += 10;
                    return;
                }
                const dx = e.clientX - prev.x;
                const dy = e.clientY - prev.y;
                this.drag.moved += Math.abs(dx) + Math.abs(dy);
                if (this.drag.pan) this._pan(dx, dy);
                else {
                    this.goal.theta -= dx * 0.006;
                    this.goal.phi = THREE.MathUtils.clamp(this.goal.phi - dy * 0.005, 0.12, 1.42);
                }
            },
            up: (e) => {
                const wasClick = this.drag && this.drag.moved < 6 && this.pointers.size === 1;
                this.pointers.delete(e.pointerId);
                if (this.pointers.size < 2) this.pinch = null;
                if (wasClick) this._pick(e);
                if (!this.pointers.size) this.drag = null;
            },
            wheel: (e) => {
                e.preventDefault();
                this.zoom(Math.exp(e.deltaY * 0.0012));
                this.options.onInteract?.();
            },
            menu: (e) => e.preventDefault(),
        };
        el.addEventListener('pointerdown', this._handlers.down);
        el.addEventListener('pointermove', this._handlers.move);
        el.addEventListener('pointerup', this._handlers.up);
        el.addEventListener('pointercancel', this._handlers.up);
        el.addEventListener('wheel', this._handlers.wheel, { passive: false });
        el.addEventListener('contextmenu', this._handlers.menu);
        el.style.touchAction = 'none';
    }

    _unbindInput() {
        const el = this.renderer.domElement;
        el.removeEventListener('pointerdown', this._handlers.down);
        el.removeEventListener('pointermove', this._handlers.move);
        el.removeEventListener('pointerup', this._handlers.up);
        el.removeEventListener('pointercancel', this._handlers.up);
        el.removeEventListener('wheel', this._handlers.wheel);
        el.removeEventListener('contextmenu', this._handlers.menu);
    }

    _pinchState() {
        const [a, b] = [...this.pointers.values()];
        return { dist: Math.max(1, Math.hypot(a.x - b.x, a.y - b.y)), x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    }

    _pan(dx, dy) {
        const scale = this.goal.radius / (this.size.h || 1) * 1.1;
        const right = new THREE.Vector3(Math.cos(this.goal.theta), 0, -Math.sin(this.goal.theta));
        const forward = new THREE.Vector3(-Math.sin(this.goal.theta), 0, -Math.cos(this.goal.theta));
        this.goal.target.addScaledVector(right, -dx * scale);
        this.goal.target.addScaledVector(forward, dy * scale);
        const lim = Math.max(this.half?.x || 40, this.half?.z || 40) + 20;
        this.goal.target.x = THREE.MathUtils.clamp(this.goal.target.x, -lim, lim);
        this.goal.target.z = THREE.MathUtils.clamp(this.goal.target.z, -lim, lim);
    }

    _ray(e) {
        const rect = this.renderer.domElement.getBoundingClientRect();
        const ndc = new THREE.Vector2(
            ((e.clientX - rect.left) / rect.width) * 2 - 1,
            -((e.clientY - rect.top) / rect.height) * 2 + 1,
        );
        this.raycaster.setFromCamera(ndc, this.camera);
        return this.raycaster.intersectObjects(this.pickables, false)[0] || null;
    }

    _hover(e) {
        if (this._hoverPending) return;
        this._hoverPending = true;
        requestAnimationFrame(() => {
            this._hoverPending = false;
            const hit = this._ray(e);
            this.renderer.domElement.style.cursor = hit ? 'pointer' : 'grab';
        });
    }

    _pick(e) {
        const hit = this._ray(e);
        if (!hit) { this.options.onPickEmpty?.(); return; }
        const data = hit.object.userData || {};
        if (data.roomId) this.options.onPickRoom?.(data.roomId);
        else if (data.buildingId) this.options.onPickBuilding?.(data.buildingId, data.floor);
    }

    // ------------------------------------------------------------ 반복
    _snap() {
        this.orbit.target.copy(this.goal.target);
        this.orbit.radius = this.goal.radius;
        this.orbit.theta = this.goal.theta;
        this.orbit.phi = this.goal.phi;
    }

    _loop() {
        this._frame = requestAnimationFrame(this._loop);
        if (document.hidden) return;
        const dt = Math.min(0.05, this.clock.getDelta());
        const t = this.clock.elapsedTime;
        if (this.autoRotate) this.goal.theta += dt * 0.18;
        const k = 1 - Math.pow(0.0015, dt);
        this.orbit.target.lerp(this.goal.target, k);
        this.orbit.radius += (this.goal.radius - this.orbit.radius) * k;
        this.orbit.theta += (this.goal.theta - this.orbit.theta) * k;
        this.orbit.phi += (this.goal.phi - this.orbit.phi) * k;
        const { target, radius, theta, phi } = this.orbit;
        this.camera.position.set(
            target.x + radius * Math.sin(phi) * Math.sin(theta),
            target.y + radius * Math.cos(phi),
            target.z + radius * Math.sin(phi) * Math.cos(theta),
        );
        this.camera.lookAt(target);
        this.options.onCamera?.(theta);

        if (this.pulse) this.pulse.material.opacity = 0.16 + Math.sin(t * 4) * 0.08;
        if (this.routeCurve && this.routeLength > 0) {
            const speed = 6;
            const u = ((t * speed) % this.routeLength) / this.routeLength;
            this.routeMarker.position.copy(this.routeCurve.getPointAt(u));
            const m = new THREE.Matrix4();
            const count = this.routeArrows.count;
            const up = new THREE.Vector3(0, 1, 0);
            for (let i = 0; i < count; i += 1) {
                const s = ((i / count) + (t * 0.04)) % 1;
                const p = this.routeCurve.getPointAt(s);
                const tangent = this.routeCurve.getTangentAt(s);
                const q = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 0, 1), tangent.normalize());
                if (Math.abs(tangent.y) > 0.9) q.setFromUnitVectors(new THREE.Vector3(0, 0, 1), up.clone().multiplyScalar(Math.sign(tangent.y)));
                m.compose(p, q, new THREE.Vector3(1, 1, 1));
                this.routeArrows.setMatrixAt(i, m);
            }
            this.routeArrows.instanceMatrix.needsUpdate = true;
            this.routeGroup.traverse(obj => {
                if (obj.userData.pulse) {
                    const s = 1 + ((t * 1.2) % 1) * 1.4;
                    obj.scale.set(s, s, s);
                    obj.material.opacity = 0.7 * (1 - ((t * 1.2) % 1));
                }
            });
        }
        this.renderer.render(this.scene, this.camera);
        this._updateLabels();
    }
}

