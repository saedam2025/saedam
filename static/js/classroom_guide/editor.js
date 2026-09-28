// 교실안내 배치도 편집 화면
//
// 왼쪽: 격자 캔버스 (① 학교 부지·건물 배치  ② 층별 교실 배치)
// 오른쪽: 선택한 항목 입력 양식 + 실시간 3D 미리보기
import { GuideScene } from './scene.js';
import {
    BUILDING_KINDS, GATE_KINDS, LANDMARK_TYPES, ROOM_TYPES, SIDES, STAIR_SHAPES,
    buildingColor, canHaveDoors, doorCells, doorSegment, emptyLayout, fitDoor, fitGate, gateSpan, hasFloor, landmarkLabel,
    makeDoor, makeGate, newId, roomTitle, snapDoor, snapGate,
    stairDir, stairPlan, stairShape, topFloor,
} from './model.js';
import { unreachableRooms } from './route.js';

function esc(text) {
    return String(text ?? '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
}

const clone = obj => JSON.parse(JSON.stringify(obj));
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

const DEFAULT_NAMES = {
    afterschool: '방과후교실', special: '특별교실', care: '돌봄교실', office: '교무실',
    restroom: '화장실', entrance: '현관', stairs: '계단', elevator: '엘리베이터', corridor: '복도', etc: '',
};

export function mountEditor(root, { layout, floorplan, schoolId, saveUrl, analyzeUrl, floorplanUrl }) {
    const $ = sel => root.querySelector(sel);
    const state = {
        layout: normalizeClient(layout || emptyLayout()),
        tab: 'site',
        buildingId: null,
        floor: 1,
        drawType: 'afterschool',
        sel: null,           // { kind: 'building'|'landmark'|'gate'|'door'|'room', id }
        dirty: false,
        history: [],
        future: [],
        // 평면도 밑그림: 서버에 보관한 도면과 부지·층별 위치(routes/classroom_guide.py _floorplan_info)
        underlay: { data: floorplan || null, on: true, alpha: 0.45 },
        manualStart: false,  // 배치도가 없을 때 [평면도 없이 직접 그리기]를 골랐는지

    };
    state.buildingId = firstRoomBuilding()?.id || null;

    const canvas = $('[data-ed-canvas]');
    const ctx = canvas.getContext('2d');
    const form = $('[data-ed-form]');
    let view = { ox: 0, oy: 0, px: 12, cols: 1, rows: 1 };

    const preview = new GuideScene($('[data-ed-preview]'), {
        onPickRoom: (id) => {
            const found = findRoomById(id);
            if (!found) return;
            state.tab = 'floor';
            state.buildingId = found.building.id;
            state.floor = found.room.floor;
            select({ kind: 'room', id });
            renderAll();
        },
        onPickBuilding: (id, floor) => {
            if (state.tab === 'floor') {
                // 층 편집 중에는 다른 건물을 누르면 그 건물의 층 배치로 넘어간다.
                const b = state.layout.buildings.find(x => x.id === id);
                if (!b || b.kind === 'connector') return;
                state.buildingId = id;
                if (floor) state.floor = floor;
                state.sel = null;
            } else {
                state.sel = { kind: 'building', id };
            }
            renderAll();
        },
    });
    preview.setLayout(state.layout);

    function renderAll() {
        renderForm();
        renderTabs();
    }

    // ------------------------------------------------------------ 데이터 도우미
    function normalizeClient(raw) {
        const l = clone(raw);
        l.site = l.site || { cols: 64, rows: 44 };
        // 예전 출입문(x, z 한 점)은 가장 가까운 담장 위 구간으로 바꾼다.
        l.gates = (l.gates || []).map((g) => {
            const sp = gateSpan(g, l.site);
            return { id: g.id, kind: sp.kind, label: g.label || GATE_KINDS[sp.kind].label, side: sp.side, at: sp.at, w: sp.w, x: sp.x, z: sp.z };
        });
        l.landmarks = (l.landmarks || []).map(lm => ({ ...lm, label: lm.label ? landmarkLabel(lm) : '' }));
        l.buildings = (l.buildings || []).map((b) => {
            const out = {
                baseFloor: 1, floorInfo: {}, rooms: [], note: '', color: '', ...b,
                rooms: (b.rooms || []).map(r => ({ courses: [], note: '', no: '', name: '', ...r })),
            };
            out.doors = (b.doors || []).map(d => fitDoor(out, { label: '', ...d }));
            return out;
        });
        l.cell = l.cell || 3;
        delete l.planShift;     // 예전 밑그림 이동량(밑그림은 이제 건물에 붙여 그린다)
        return l;
    }

    function building() {
        return state.layout.buildings.find(b => b.id === state.buildingId) || null;
    }

    function firstRoomBuilding() {
        return state.layout.buildings.find(b => b.kind !== 'connector') || state.layout.buildings[0] || null;
    }

    function findDoorById(id) {
        for (const b of state.layout.buildings) {
            const door = (b.doors || []).find(d => d.id === id);
            if (door) return { building: b, door };
        }
        return null;
    }

    function fitDoors(b) {
        b.doors = canHaveDoors(b) ? (b.doors || []).map(d => fitDoor(b, d)) : [];
    }

    function findRoomById(id) {
        for (const b of state.layout.buildings) {
            const room = b.rooms.find(r => r.id === id);
            if (room) return { building: b, room };
        }
        return null;
    }

    function selected() {
        const s = state.sel;
        if (!s) return null;
        const l = state.layout;
        if (s.kind === 'building') return l.buildings.find(b => b.id === s.id) || null;
        if (s.kind === 'landmark') return l.landmarks.find(x => x.id === s.id) || null;
        if (s.kind === 'gate') return l.gates.find(x => x.id === s.id) || null;
        if (s.kind === 'door') return findDoorById(s.id)?.door || null;
        if (s.kind === 'room') return findRoomById(s.id)?.room || null;
        return null;
    }

    function select(sel) {
        state.sel = sel;
        renderForm();
    }

    function snapshot() {
        state.history.push(JSON.stringify(state.layout));
        if (state.history.length > 60) state.history.shift();
        state.future = [];
        updateUndo();
    }

    function changed({ form: rerenderForm = false } = {}) {
        state.dirty = true;
        $('[data-ed-dirty]')?.classList.add('is-on');
        drawCanvas();
        if (rerenderForm) renderForm();
        schedulePreview();
    }

    function undo() {
        if (!state.history.length) return;
        state.future.push(JSON.stringify(state.layout));
        state.layout = JSON.parse(state.history.pop());
        if (!selected()) state.sel = null;
        if (!building()) state.buildingId = firstRoomBuilding()?.id || null;
        changed({ form: true });
        renderTabs();
        updateUndo();
    }

    function redo() {
        if (!state.future.length) return;
        state.history.push(JSON.stringify(state.layout));
        state.layout = JSON.parse(state.future.pop());
        if (!selected()) state.sel = null;
        changed({ form: true });
        renderTabs();
        updateUndo();
    }

    function updateUndo() {
        const u = $('[data-ed-undo]');
        const r = $('[data-ed-redo]');
        if (u) u.disabled = !state.history.length;
        if (r) r.disabled = !state.future.length;
    }

    let previewTimer = null;
    function schedulePreview() {
        clearTimeout(previewTimer);
        previewTimer = setTimeout(() => {
            preview.setLayout(state.layout, { keepCamera: true });
            syncPreviewFloor();
            if (state.sel?.kind === 'room') preview.selectRoom(state.sel.id);
        }, 220);
        scheduleCheck();
    }

    function syncPreviewFloor() {
        const b = building();
        if (state.tab === 'floor' && b && b.kind !== 'connector') preview.setFloor(state.floor);
        else preview.setFloor('all');
        preview.selectRoom(state.sel?.kind === 'room' ? state.sel.id : null);
    }

    // ------------------------------------------------------------ 탭 · 툴바
    function renderTabs() {
        root.querySelectorAll('[data-ed-tab]').forEach(el => el.classList.toggle('is-active', el.dataset.edTab === state.tab));
        root.querySelectorAll('[data-ed-pane]').forEach(el => { el.hidden = el.dataset.edPane !== state.tab; });

        const bSelect = $('[data-ed-building]');
        if (bSelect) {
            bSelect.innerHTML = state.layout.buildings.map(b =>
                `<option value="${esc(b.id)}"${b.id === state.buildingId ? ' selected' : ''}>${esc(b.name)} (${(BUILDING_KINDS[b.kind] || {}).label || ''} · ${b.floors}층)</option>`,
            ).join('') || '<option value="">건물을 먼저 추가하세요</option>';
        }
        const b = building();
        if (b && !hasFloor(b, state.floor)) state.floor = b.baseFloor;
        const floors = $('[data-ed-floors]');
        if (floors) {
            floors.innerHTML = b ? Array.from({ length: b.floors }, (_, i) => b.baseFloor + i).map(f =>
                `<button type="button" class="cg-floor-tab${f === state.floor ? ' is-active' : ''}" data-ed-floor="${f}">${f}층</button>`,
            ).join('') : '';
        }
        const copyTo = $('[data-ed-copy-to]');
        if (copyTo) {
            copyTo.innerHTML = b ? Array.from({ length: b.floors }, (_, i) => b.baseFloor + i)
                .filter(f => f !== state.floor)
                .map(f => `<option value="${f}">${f}층</option>`).join('') : '';
            copyTo.closest('[data-ed-copy]')?.toggleAttribute('hidden', !b || b.floors < 2 || b.kind === 'connector');
        }
        const addDoor = $('[data-ed-add-door]');
        if (addDoor) addDoor.hidden = !(b && canHaveDoors(b) && state.floor === 1);
        const pal = $('[data-ed-palette]');
        if (pal && !pal.childElementCount) {
            pal.innerHTML = Object.entries(ROOM_TYPES).map(([key, t]) =>
                `<button type="button" class="cg-palette-item" data-ed-type="${key}" style="--room:${t.color};--room-edge:${t.edge}"><i></i>${t.label}</button>`,
            ).join('');
        }
        pal?.querySelectorAll('[data-ed-type]').forEach(el => el.classList.toggle('is-active', el.dataset.edType === state.drawType));
        const siteCols = $('[data-ed-site-cols]');
        const siteRows = $('[data-ed-site-rows]');
        const cell = $('[data-ed-cell]');
        if (siteCols) siteCols.value = state.layout.site.cols;
        if (siteRows) siteRows.value = state.layout.site.rows;
        if (cell) cell.value = state.layout.cell;
        renderUnderlayBar();
        renderStart();
        syncPreviewFloor();
        resizeCanvas();
    }

    // ------------------------------------------------------------ 캔버스
    function resizeCanvas() {
        const wrap = canvas.parentElement;
        const width = Math.max(280, wrap.clientWidth);
        let cols;
        let rows;
        if (state.tab === 'site') {
            cols = state.layout.site.cols;
            rows = state.layout.site.rows;
        } else {
            const b = building();
            cols = b ? b.cols : 20;
            rows = b ? b.rows : 8;
        }
        const pad = state.tab === 'site' ? 28 : 42;
        const maxPx = state.tab === 'site' ? 22 : 44;
        const px = clamp(Math.floor((width - pad * 2) / cols), 6, maxPx);
        const cssW = width;
        const cssH = Math.max(220, rows * px + pad * 2);
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        canvas.width = Math.round(cssW * dpr);
        canvas.height = Math.round(cssH * dpr);
        canvas.style.height = `${cssH}px`;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        view = { ox: Math.round((cssW - cols * px) / 2), oy: pad, px, cols, rows, w: cssW, h: cssH };
        drawCanvas();
    }

    function rectPx(x, z, w, d) {
        return [view.ox + x * view.px, view.oy + z * view.px, w * view.px, d * view.px];
    }

    function drawGrid(cols, rows, fill) {
        const [x, y, w, h] = rectPx(0, 0, cols, rows);
        ctx.fillStyle = fill;
        ctx.fillRect(x, y, w, h);
        ctx.strokeStyle = 'rgba(100,116,139,.13)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        for (let i = 0; i <= cols; i += 1) { ctx.moveTo(x + i * view.px + 0.5, y); ctx.lineTo(x + i * view.px + 0.5, y + h); }
        for (let j = 0; j <= rows; j += 1) { ctx.moveTo(x, y + j * view.px + 0.5); ctx.lineTo(x + w, y + j * view.px + 0.5); }
        ctx.stroke();
        ctx.strokeStyle = 'rgba(71,85,105,.5)';
        ctx.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
    }

    function fitText(text, maxWidth, size, weight = 600) {
        let s = size;
        ctx.font = `${weight} ${s}px Pretendard, 'Malgun Gothic', sans-serif`;
        while (s > 8 && ctx.measureText(text).width > maxWidth) {
            s -= 1;
            ctx.font = `${weight} ${s}px Pretendard, 'Malgun Gothic', sans-serif`;
        }
        return ctx.measureText(text).width <= maxWidth;
    }

    function labelIn(x, y, w, h, lines, color = '#1e293b') {
        ctx.fillStyle = color;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        const shown = lines.filter(Boolean);
        const lineH = Math.min(16, Math.max(10, h / (shown.length + 1)));
        shown.forEach((line, i) => {
            const size = i === 0 ? Math.min(14, lineH) : Math.min(11, lineH - 2);
            if (!fitText(line, w - 6, size, i === 0 ? 700 : 500)) return;
            ctx.fillText(line, x + w / 2, y + h / 2 + (i - (shown.length - 1) / 2) * lineH);
        });
    }

    /** 둥근 배경의 작은 이름표. vertical 이면 세로로 돌려 좁은 여백에도 들어가게 한다. */
    function pill(text, cx, cy, bg, vertical = false) {
        ctx.save();
        ctx.translate(cx, cy);
        if (vertical) ctx.rotate(-Math.PI / 2);
        ctx.font = '700 11px Pretendard, sans-serif';
        const w = ctx.measureText(text).width + 12;
        const h = 17;
        ctx.fillStyle = bg;
        ctx.beginPath();
        ctx.roundRect(-w / 2, -h / 2, w, h, 8);
        ctx.fill();
        ctx.fillStyle = '#fff';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(text, 0, 0.5);
        ctx.restore();
    }

    /** 교문·후문: 담장 선 위에 걸친 막대(칸 좌표). */
    function gateRect(g) {
        const sp = gateSpan(g, state.layout.site);
        const { cols, rows } = state.layout.site;
        const t = 0.9;
        if (sp.horizontal) return { x: sp.at, z: (sp.side === 'n' ? 0 : rows) - t / 2, w: sp.w, d: t, sp };
        return { x: (sp.side === 'w' ? 0 : cols) - t / 2, z: sp.at, w: t, d: sp.w, sp };
    }

    /** 건물출입문: 외벽 선 위에 걸친 막대. local 이면 건물 기준 좌표(층별 배치 화면). */
    function doorRect(b, door, local = false) {
        const seg = doorSegment(b, door);
        const ox = local ? b.x : 0;
        const oz = local ? b.z : 0;
        const t = 0.7;
        return seg.side === 'n' || seg.side === 's'
            ? { x: seg.x1 - ox, z: seg.z1 - oz - t / 2, w: seg.x2 - seg.x1, d: t, seg }
            : { x: seg.x1 - ox - t / 2, z: seg.z1 - oz, w: t, d: seg.z2 - seg.z1, seg };
    }

    function inPadded(c, r, pad) {
        return c.fx >= r.x - pad && c.fx < r.x + r.w + pad && c.fz >= r.z - pad && c.fz < r.z + r.d + pad;
    }

    function drawGate(g) {
        const r = gateRect(g);
        const kind = GATE_KINDS[r.sp.kind];
        const [x, y, w, h] = rectPx(r.x, r.z, r.w, r.d);
        ctx.fillStyle = '#fff';
        ctx.fillRect(x - 2, y - 2, w + 4, h + 4);
        ctx.fillStyle = kind.color;
        ctx.fillRect(x, y, w, h);
        // 문 양쪽 기둥
        ctx.fillStyle = '#334155';
        const p = Math.max(3, view.px * 0.35);
        if (r.sp.horizontal) { ctx.fillRect(x - p, y - 1, p, h + 2); ctx.fillRect(x + w, y - 1, p, h + 2); }
        else { ctx.fillRect(x - 1, y - p, w + 2, p); ctx.fillRect(x - 1, y + h, w + 2, p); }
        // 이름표는 부지 안쪽에
        const { dx, dz } = SIDES[r.sp.side];
        const off = view.px * 0.45 + 12;
        pill(g.label || kind.label, x + w / 2 - dx * off, y + h / 2 - dz * off, kind.color, !r.sp.horizontal);
    }

    function drawDoor(b, door, local) {
        const r = doorRect(b, door, local);
        const color = GATE_KINDS.door.color;
        const [x, y, w, h] = rectPx(r.x, r.z, r.w, r.d);
        ctx.fillStyle = '#fff';
        ctx.fillRect(x - 1.5, y - 1.5, w + 3, h + 3);
        ctx.fillStyle = color;
        ctx.fillRect(x, y, w, h);
        // 들어가는 방향(바깥 → 안) 삼각형
        const { dx, dz } = SIDES[r.seg.side];
        const cx = x + w / 2;
        const cy = y + h / 2;
        const s = Math.max(5, Math.min(12, view.px * 0.6));
        const bx = cx - dx * (h / 2 > w / 2 ? w / 2 : h / 2);
        const by = cy - dz * (h / 2 > w / 2 ? w / 2 : h / 2);
        ctx.beginPath();
        ctx.moveTo(bx - dx * s, by - dz * s);
        ctx.lineTo(bx - dz * s * 0.7, by + dx * s * 0.7);
        ctx.lineTo(bx + dz * s * 0.7, by - dx * s * 0.7);
        ctx.closePath();
        ctx.fill();
        const off = Math.min(view.px * 0.35, 8) + 12;
        pill(door.label || '출입구', cx + dx * off, cy + dz * off, color, dx !== 0);
    }

    /** 계단: 디딤판 선 · U자 가운데 난간 · 오르는 방향 화살표(시작 점 → 화살촉). */
    function drawStairs(room, rx, ry, b) {
        const plan = stairPlan(room, b);
        const P = p => [rx + p.x * view.px, ry + p.z * view.px];
        ctx.lineWidth = 1;
        ctx.strokeStyle = 'rgba(154,52,18,.45)';
        for (const flight of plan.flights) {
            for (const st of flight.steps) {
                const [x, y] = P(st);
                ctx.strokeRect(x + 0.5, y + 0.5, st.w * view.px - 1, st.d * view.px - 1);
            }
        }
        if (plan.landing) {
            const [x, y] = P(plan.landing);
            ctx.fillStyle = 'rgba(234,88,12,.12)';
            ctx.fillRect(x + 1, y + 1, plan.landing.w * view.px - 2, plan.landing.d * view.px - 2);
        }
        if (plan.divider) {
            const [ax, ay] = P(plan.divider.a);
            const [bx, by] = P(plan.divider.b);
            ctx.strokeStyle = '#475569';
            ctx.lineWidth = 2.5;
            ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
        }
        const pts = plan.path.map(P);
        ctx.strokeStyle = '#c2410c';
        ctx.fillStyle = '#c2410c';
        ctx.lineWidth = 2;
        ctx.beginPath();
        pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(pts[0][0], pts[0][1], 3, 0, Math.PI * 2);
        ctx.fill();
        const [tx, ty] = pts[pts.length - 1];
        const [px, py] = pts[pts.length - 2];
        const ang = Math.atan2(ty - py, tx - px);
        const s = Math.max(6, Math.min(10, view.px * 0.5));
        ctx.beginPath();
        ctx.moveTo(tx, ty);
        ctx.lineTo(tx - s * Math.cos(ang - 0.45), ty - s * Math.sin(ang - 0.45));
        ctx.lineTo(tx - s * Math.cos(ang + 0.45), ty - s * Math.sin(ang + 0.45));
        ctx.closePath();
        ctx.fill();
    }

    /** 선 그림 위에 얹는 이름: 흰 바탕을 깔아 읽히게 한다. */
    function boxedLabel(text, x, y, w, h) {
        if (!text || !fitText(text, w - 4, Math.min(12, h - 2), 700)) return;
        const tw = ctx.measureText(text).width + 6;
        const th = Math.min(16, h - 2);
        ctx.fillStyle = 'rgba(255,255,255,.88)';
        ctx.fillRect(x + w / 2 - tw / 2, y + h / 2 - th / 2, tw, th);
        ctx.fillStyle = '#1e293b';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(text, x + w / 2, y + h / 2 + 0.5);
    }

    // ------------------------------------------------------------ 평면도 밑그림
    const planImages = new Map();

    function planImage(id) {
        let img = planImages.get(id);
        if (!img) {
            const info = (state.underlay.data?.images || []).find(i => i.id === id);
            if (!info) return null;
            img = new Image();
            img.onload = () => drawCanvas();
            img.src = info.url;
            planImages.set(id, img);
        }
        return img.complete && img.naturalWidth ? img : null;
    }

    /** 지금 화면(부지 또는 선택한 건물·층)에 깔 도면 자리. 칸 좌표 { image, x, z, w, d }. */
    function underlayPlace() {
        const data = state.underlay.data;
        if (!data) return null;
        if (state.tab === 'site') {
            // 부지 도면은 그 도면에 그려진 건물(가장 큰 것)에 붙여 깐다. 건물을 옮기거나 부지를 넓혀도
            // 도면이 건물을 따라 움직인다. 도면으로 만든 건물이 모두 지워졌으면 깔지 않는다.
            if (!data.site) return null;
            let anchor = null;
            for (const f of data.floors) {
                if (f.image !== data.site.image) continue;
                const b = state.layout.buildings.find(x => x.id === f.building);
                if (b && (!anchor || b.cols * b.rows > anchor.b.cols * anchor.b.rows)) anchor = { b, f };
            }
            if (!anchor) return null;
            const place = planPlace(anchor.b, anchor.f);
            return { ...place, x: place.x + anchor.b.x, z: place.z + anchor.b.z };
        }
        const b = building();
        const f = b && data.floors.find(x => x.building === b.id && x.floor === state.floor);
        return f ? planPlace(b, f) : null;
    }

    /** 건물 기준 도면 자리(건물 가로·세로를 1로 본 비율 fx·fz·fw·fd)를 건물 안 칸 좌표로. */
    function planPlace(b, f) {
        return { image: f.image, x: f.fx * b.cols, z: f.fz * b.rows, w: f.fw * b.cols, d: f.fd * b.rows };
    }

    /** 격자 안쪽에만 도면을 반투명하게 그린다. 그렸으면 true. */
    function drawUnderlay(cols, rows) {
        if (!state.underlay.on) return false;
        const place = underlayPlace();
        const img = place && planImage(place.image);
        if (!img) return false;
        const [gx, gy, gw, gh] = rectPx(0, 0, cols, rows);
        const [x, y, w, h] = rectPx(place.x, place.z, place.w, place.d);
        ctx.save();
        ctx.beginPath();
        ctx.rect(gx, gy, gw, gh);
        ctx.clip();
        ctx.globalAlpha = state.underlay.alpha;
        ctx.drawImage(img, x, y, w, h);
        ctx.restore();
        return true;
    }

    function renderUnderlayBar() {
        const bar = $('[data-ed-underlay]');
        if (!bar) return;
        const data = state.underlay.data;
        bar.hidden = !data;
        if (!data) return;
        bar.querySelector('[data-ed-underlay-on]').checked = state.underlay.on;
        bar.querySelector('[data-ed-underlay-alpha]').value = Math.round(state.underlay.alpha * 100);
        const note = bar.querySelector('[data-ed-underlay-note]');
        const here = underlayPlace();
        note.textContent = here
            ? `도면 ${data.images.length}장 · ${data.created_at} 분석`
            : (state.tab === 'site' ? '지금 배치와 이어진 부지 도면이 없습니다' : '이 층에 맞춘 도면이 없습니다');
    }

    function drawHandle(x, y, w, h) {
        ctx.fillStyle = '#2563eb';
        ctx.fillRect(x + w - 7, y + h - 7, 10, 10);
        ctx.strokeStyle = '#fff';
        ctx.lineWidth = 1.5;
        ctx.strokeRect(x + w - 7, y + h - 7, 10, 10);
    }

    function drawCanvas() {
        ctx.clearRect(0, 0, view.w || canvas.width, view.h || canvas.height);
        if (state.tab === 'site') drawSite();
        else drawFloor();
        if (drag && drag.kind === 'draw') {
            const r = normRect(drag.start, drag.cur);
            const [x, y, w, h] = rectPx(r.x, r.z, r.w, r.d);
            const t = ROOM_TYPES[state.drawType];
            ctx.fillStyle = `${t.color}cc`;
            ctx.fillRect(x, y, w, h);
            ctx.setLineDash([5, 4]);
            ctx.strokeStyle = t.edge;
            ctx.lineWidth = 2;
            ctx.strokeRect(x + 1, y + 1, w - 2, h - 2);
            ctx.setLineDash([]);
            labelIn(x, y, w, h, [`${r.w} × ${r.d}칸`]);
        }
    }

    function drawSite() {
        const l = state.layout;
        drawGrid(l.site.cols, l.site.rows, '#eef2e6');
        // 밑그림이 깔리면 위에 얹는 건물·시설을 옅게 칠해 도면과 비교할 수 있게 한다.
        const ghost = drawUnderlay(l.site.cols, l.site.rows);
        for (const lm of l.landmarks) {
            const [x, y, w, h] = rectPx(lm.x, lm.z, lm.w, lm.d);
            ctx.fillStyle = (LANDMARK_TYPES[lm.type] || LANDMARK_TYPES.etc).color;
            ctx.globalAlpha = ghost ? 0.4 : 0.75;
            ctx.fillRect(x, y, w, h);
            ctx.globalAlpha = 1;
            labelIn(x, y, w, h, [landmarkLabel(lm)], '#3f3f46');
        }
        for (const b of l.buildings) {
            const [x, y, w, h] = rectPx(b.x, b.z, b.cols, b.rows);
            ctx.fillStyle = buildingColor(b);
            ctx.globalAlpha = ghost ? 0.55 : 1;
            ctx.fillRect(x, y, w, h);
            ctx.globalAlpha = 1;
            ctx.strokeStyle = '#64748b';
            ctx.lineWidth = 1.5;
            ctx.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
            const floors = b.kind === 'connector' ? `${b.baseFloor}~${topFloor(b)}층` : `${b.floors}층`;
            labelIn(x, y, w, h, [b.name, floors]);
        }
        for (const b of l.buildings) {
            if (canHaveDoors(b)) for (const door of b.doors || []) drawDoor(b, door, false);
        }
        for (const gate of l.gates) drawGate(gate);
        const s = state.sel;
        const item = selected();
        if (item && s.kind !== 'room') {
            let r;
            if (s.kind === 'building') r = [item.x, item.z, item.cols, item.rows];
            else if (s.kind === 'landmark') r = [item.x, item.z, item.w, item.d];
            else if (s.kind === 'gate') { const g = gateRect(item); r = [g.x, g.z, g.w, g.d]; }
            else { const d = doorRect(findDoorById(item.id).building, item); r = [d.x, d.z, d.w, d.d]; }
            const [x, y, w, h] = rectPx(...r);
            ctx.strokeStyle = '#f59e0b';
            ctx.lineWidth = 3;
            if (s.kind === 'building' || s.kind === 'landmark') ctx.strokeStyle = '#2563eb';
            ctx.strokeRect(x - 2.5, y - 2.5, w + 5, h + 5);
            if (s.kind === 'building' || s.kind === 'landmark') drawHandle(x, y, w, h);
        }
        // 방위
        ctx.fillStyle = '#475569';
        ctx.font = '700 12px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('▲ 북', view.ox + 18, view.oy - 10);
    }

    function drawFloor() {
        const b = building();
        if (!b) {
            ctx.fillStyle = '#64748b';
            ctx.font = '600 14px Pretendard, sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('[학교 부지·건물] 탭에서 건물을 먼저 추가해 주세요.', view.w / 2, view.h / 2);
            return;
        }
        drawGrid(b.cols, b.rows, b.kind === 'connector' ? '#e2e8f0' : '#f8fafc');
        const ghost = drawUnderlay(b.cols, b.rows);
        const doorsHere = canHaveDoors(b) && state.floor === 1 ? (b.doors || []) : [];
        const drawDoorsHere = () => {
            for (const door of doorsHere) drawDoor(b, door, true);
            const sel = state.sel?.kind === 'door' ? findDoorById(state.sel.id) : null;
            if (sel && sel.building === b && doorsHere.includes(sel.door)) {
                const r = doorRect(b, sel.door, true);
                const [x, y, w, h] = rectPx(r.x, r.z, r.w, r.d);
                ctx.strokeStyle = '#f59e0b';
                ctx.lineWidth = 3;
                ctx.strokeRect(x - 2.5, y - 2.5, w + 5, h + 5);
            }
        };
        if (b.kind === 'connector') {
            const [x, y, w, h] = rectPx(0, 0, b.cols, b.rows);
            labelIn(x, y, w, h, ['연결통로', '전체가 복도로 처리됩니다']);
            drawDoorsHere();
            return;
        }
        for (const room of b.rooms) {
            if (room.floor !== state.floor) continue;
            const t = ROOM_TYPES[room.type] || ROOM_TYPES.etc;
            const [x, y, w, h] = rectPx(room.x, room.z, room.w, room.d);
            ctx.fillStyle = t.color;
            ctx.globalAlpha = ghost ? 0.55 : 1;
            ctx.fillRect(x + 1, y + 1, w - 2, h - 2);
            ctx.globalAlpha = 1;
            ctx.strokeStyle = t.edge;
            ctx.lineWidth = 1.5;
            ctx.strokeRect(x + 1.5, y + 1.5, w - 3, h - 3);
            if (room.type === 'stairs') {
                drawStairs(room, x, y, b);
                boxedLabel(room.name || t.label, x, y, w, h);
                continue;
            }
            if (room.type === 'elevator') {
                ctx.strokeStyle = 'rgba(99,102,241,.45)';
                ctx.lineWidth = 1;
                ctx.beginPath();
                ctx.moveTo(x + 4, y + 4); ctx.lineTo(x + w - 4, y + h - 4);
                ctx.moveTo(x + w - 4, y + 4); ctx.lineTo(x + 4, y + h - 4);
                ctx.stroke();
                boxedLabel(room.name || t.label, x, y, w, h);
                continue;
            }
            const course = (room.courses || []).map(c => c.name).filter(Boolean).join(', ');
            labelIn(x, y, w, h, [room.no || room.name || t.label, room.no ? room.name : '', course]);
        }
        drawDoorsHere();
        // 빈 칸 = 복도 안내
        ctx.fillStyle = 'rgba(100,116,139,.8)';
        ctx.font = '500 11px Pretendard, sans-serif';
        ctx.textAlign = 'left';
        ctx.fillText(doorsHere.length || (canHaveDoors(b) && state.floor === 1)
            ? '빈 칸은 복도 · 파란 막대는 건물출입문(외벽을 따라 끌어 옮김) · 드래그해서 교실을 그리세요'
            : '빈 칸은 복도로 처리됩니다 · 드래그해서 교실을 그리세요', view.ox, view.oy + b.rows * view.px + 36);
        const item = state.sel?.kind === 'room' ? selected() : null;
        if (item && item.floor === state.floor && findRoomById(item.id)?.building === b) {
            const [x, y, w, h] = rectPx(item.x, item.z, item.w, item.d);
            ctx.strokeStyle = '#2563eb';
            ctx.lineWidth = 3;
            ctx.strokeRect(x - 0.5, y - 0.5, w + 1, h + 1);
            drawHandle(x, y, w, h);
        }
    }

    // ------------------------------------------------------------ 캔버스 조작
    let drag = null;

    function cellAt(e) {
        const rect = canvas.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const my = e.clientY - rect.top;
        return {
            fx: (mx - view.ox) / view.px,
            fz: (my - view.oy) / view.px,
            x: Math.floor((mx - view.ox) / view.px),
            z: Math.floor((my - view.oy) / view.px),
            mx, my,
        };
    }

    function normRect(a, b) {
        const x = Math.min(a.x, b.x);
        const z = Math.min(a.z, b.z);
        return { x, z, w: Math.abs(a.x - b.x) + 1, d: Math.abs(a.z - b.z) + 1 };
    }

    function inRect(c, x, z, w, d) {
        return c.fx >= x && c.fx < x + w && c.fz >= z && c.fz < z + d;
    }

    function onHandle(c, x, z, w, d) {
        const [px, py, pw, ph] = rectPx(x, z, w, d);
        return Math.abs(c.mx - (px + pw)) < 10 && Math.abs(c.my - (py + ph)) < 10;
    }

    function hitSite(c) {
        const l = state.layout;
        const s = state.sel;
        const item = selected();
        if (item && s.kind === 'building' && onHandle(c, item.x, item.z, item.cols, item.rows)) return { ...s, resize: true };
        if (item && s.kind === 'landmark' && onHandle(c, item.x, item.z, item.w, item.d)) return { ...s, resize: true };
        const gate = [...l.gates].reverse().find(g => inPadded(c, gateRect(g), 0.8));
        if (gate) return { kind: 'gate', id: gate.id };
        for (const b of [...l.buildings].reverse()) {
            if (!canHaveDoors(b)) continue;
            const door = (b.doors || []).find(d => inPadded(c, doorRect(b, d), 0.5));
            if (door) return { kind: 'door', id: door.id };
        }
        const b = [...l.buildings].reverse().find(x => inRect(c, x.x, x.z, x.cols, x.rows));
        if (b) return { kind: 'building', id: b.id };
        const lm = [...l.landmarks].reverse().find(x => inRect(c, x.x, x.z, x.w, x.d));
        if (lm) return { kind: 'landmark', id: lm.id };
        return null;
    }

    function floorDoorAt(b, c) {
        if (!canHaveDoors(b) || state.floor !== 1) return null;
        return (b.doors || []).find(d => inPadded(c, doorRect(b, d, true), 0.5)) || null;
    }

    /** 부지 좌표 한 점에서 외벽이 가장 가까운 (출입문을 둘 수 있는) 건물. */
    function nearestDoorBuilding(fx, fz) {
        let best = null;
        for (const b of state.layout.buildings) {
            if (!canHaveDoors(b)) continue;
            const dist = snapDoor(b, fx - b.x, fz - b.z, { w: 1 });
            if (!best || dist < best.dist) best = { b, dist };
        }
        return best?.b || null;
    }

    function moveDoorTo(door, target) {
        const found = findDoorById(door.id);
        if (!found || found.building === target) return;
        found.building.doors = found.building.doors.filter(d => d !== door);
        target.doors = target.doors || [];
        target.doors.push(door);
    }

    function roomsOnFloor(b, exceptId) {
        return b.rooms.filter(r => r.floor === state.floor && r.id !== exceptId);
    }

    function overlaps(b, rect, exceptId) {
        return roomsOnFloor(b, exceptId).some(r =>
            rect.x < r.x + r.w && r.x < rect.x + rect.w && rect.z < r.z + r.d && r.z < rect.z + rect.d);
    }

    canvas.addEventListener('pointerdown', (e) => {
        const c = cellAt(e);
        canvas.setPointerCapture(e.pointerId);
        if (state.tab === 'site') {
            const hit = hitSite(c);
            if (!hit) { select(null); drawCanvas(); return; }
            select({ kind: hit.kind, id: hit.id });
            const item = selected();
            drag = { kind: hit.resize ? 'resize' : 'move', start: c, orig: clone(item), before: JSON.stringify(state.layout) };
            drawCanvas();
            return;
        }
        const b = building();
        if (!b) return;
        const doorHit = floorDoorAt(b, c);
        if (doorHit) {
            select({ kind: 'door', id: doorHit.id });
            drag = { kind: 'move', start: c, orig: clone(doorHit), before: JSON.stringify(state.layout) };
            drawCanvas();
            return;
        }
        if (b.kind === 'connector') return;
        const cur = state.sel?.kind === 'room' ? selected() : null;
        if (cur && cur.floor === state.floor && b.rooms.includes(cur) && onHandle(c, cur.x, cur.z, cur.w, cur.d)) {
            drag = { kind: 'resize', start: c, orig: clone(cur), before: JSON.stringify(state.layout) };
            return;
        }
        const room = roomsOnFloor(b).find(r => inRect(c, r.x, r.z, r.w, r.d));
        if (room) {
            select({ kind: 'room', id: room.id });
            preview.selectRoom(room.id);
            drag = { kind: 'move', start: c, orig: clone(room), before: JSON.stringify(state.layout) };
            drawCanvas();
            return;
        }
        if (c.x < 0 || c.z < 0 || c.x >= b.cols || c.z >= b.rows) { select(null); drawCanvas(); return; }
        drag = { kind: 'draw', start: { x: c.x, z: c.z }, cur: { x: c.x, z: c.z } };
        drawCanvas();
    });

    canvas.addEventListener('pointermove', (e) => {
        const c = cellAt(e);
        if (!drag) {
            let cursor = 'crosshair';
            if (state.tab === 'site') {
                const hit = hitSite(c);
                cursor = hit ? (hit.resize ? 'nwse-resize' : 'move') : 'default';
            } else {
                const b = building();
                const cur = state.sel?.kind === 'room' ? selected() : null;
                if (b && floorDoorAt(b, c)) cursor = 'move';
                else if (cur && b && b.rooms.includes(cur) && cur.floor === state.floor && onHandle(c, cur.x, cur.z, cur.w, cur.d)) cursor = 'nwse-resize';
                else if (b && roomsOnFloor(b).some(r => inRect(c, r.x, r.z, r.w, r.d))) cursor = 'move';
            }
            canvas.style.cursor = cursor;
            return;
        }
        const dx = Math.round(c.fx - drag.start.fx);
        const dz = Math.round(c.fz - drag.start.fz);
        const l = state.layout;
        if (drag.kind === 'draw') {
            const b = building();
            drag.cur = { x: clamp(c.x, 0, b.cols - 1), z: clamp(c.z, 0, b.rows - 1) };
            drawCanvas();
            return;
        }
        const item = selected();
        if (!item) return;
        const o = drag.orig;
        if (state.tab === 'site') {
            if (state.sel.kind === 'building') {
                if (drag.kind === 'move') {
                    item.x = clamp(o.x + dx, 0, l.site.cols - item.cols);
                    item.z = clamp(o.z + dz, 0, l.site.rows - item.rows);
                } else {
                    const minW = Math.max(1, ...item.rooms.map(r => r.x + r.w));
                    const minD = Math.max(1, ...item.rooms.map(r => r.z + r.d));
                    item.cols = clamp(o.cols + dx, minW, Math.min(80, l.site.cols - item.x));
                    item.rows = clamp(o.rows + dz, minD, Math.min(80, l.site.rows - item.z));
                    fitDoors(item);
                }
            } else if (state.sel.kind === 'landmark') {
                if (drag.kind === 'move') {
                    item.x = clamp(o.x + dx, 0, l.site.cols - item.w);
                    item.z = clamp(o.z + dz, 0, l.site.rows - item.d);
                } else {
                    item.w = clamp(o.w + dx, 1, l.site.cols - item.x);
                    item.d = clamp(o.d + dz, 1, l.site.rows - item.z);
                }
            } else if (state.sel.kind === 'gate') {
                // 가장 가까운 담장에 붙어 담장을 따라 움직인다.
                snapGate(l.site, c.fx, c.fz, item);
            } else if (state.sel.kind === 'door') {
                // 가장 가까운 건물 외벽에 붙어 벽을 따라 움직인다(다른 건물로 옮길 수도 있다).
                const target = nearestDoorBuilding(c.fx, c.fz);
                if (target) {
                    moveDoorTo(item, target);
                    snapDoor(target, c.fx - target.x, c.fz - target.z, item);
                }
            }
        } else if (state.sel.kind === 'door') {
            const b = building();
            snapDoor(b, c.fx, c.fz, item);
        } else {
            const b = building();
            const next = drag.kind === 'move'
                ? { x: clamp(o.x + dx, 0, b.cols - o.w), z: clamp(o.z + dz, 0, b.rows - o.d), w: o.w, d: o.d }
                : { x: o.x, z: o.z, w: clamp(o.w + dx, 1, b.cols - o.x), d: clamp(o.d + dz, 1, b.rows - o.z) };
            if (!overlaps(b, next, item.id)) Object.assign(item, next);
        }
        drag.moved = true;
        drawCanvas();
    });

    canvas.addEventListener('pointerup', () => {
        if (!drag) return;
        const d = drag;
        drag = null;
        if (d.kind === 'draw') {
            const b = building();
            const r = normRect(d.start, d.cur);
            if (overlaps(b, r)) {
                flash('다른 교실과 겹치게 그릴 수 없습니다.');
                drawCanvas();
                return;
            }
            snapshot();
            const room = {
                id: newId('r'), floor: state.floor, type: state.drawType, ...r,
                no: autoNumber(b, state.drawType), name: DEFAULT_NAMES[state.drawType] ?? '', courses: [], note: '',
            };
            b.rooms.push(room);
            select({ kind: 'room', id: room.id });
            changed();
            form.querySelector('[data-f="name"]')?.focus();
            return;
        }
        if (d.moved) {
            state.history.push(d.before);
            state.future = [];
            updateUndo();
            changed({ form: true });
        }
    });

    function autoNumber(b, type) {
        if (!ROOM_TYPES[type]?.course && type !== 'office') return '';
        const nums = b.rooms.filter(r => r.floor === state.floor).map(r => parseInt(r.no, 10)).filter(n => !Number.isNaN(n));
        const base = state.floor * 100;
        const next = nums.length ? Math.max(...nums) + 1 : base + 1;
        return String(next);
    }

    // ------------------------------------------------------------ 입력 양식
    function numberField(label, key, value, min, max) {
        return `<label class="cg-field cg-field-sm"><span>${label}</span><input type="number" data-f="${key}" value="${value}" min="${min}" max="${max}"></label>`;
    }

    function renderForm() {
        const s = state.sel;
        const item = selected();
        if (!item) {
            form.innerHTML = `
                <h3><i class="fa-solid fa-pen-ruler"></i> 만드는 순서</h3>
                <ol class="cg-help">
                    <li><b>평면도로 자동 그리기</b>: 층별 평면도를 올리면 건물·교실·계단·출입구가 초안으로 그려지고, 올린 평면도가 밑그림으로 깔립니다.
                        후관·별관은 나중에 <b>새 건물로 추가</b>하거나, [건물 추가]로 놓은 뒤 건물 정보의 <b>평면도로 교실 채우기</b>로 그 건물만 그릴 수 있습니다.</li>
                    <li><b>점검 목록 따라 고치기</b>: 오른쪽 <b>완성도 점검</b>의 항목을 누르면 문제가 있는 건물·층·교실로 바로 이동합니다. 밑그림과 비교해 크기·이름·계단을 고치세요.</li>
                    <li><b>빠진 것 채우기</b>: 층별 교실 배치 탭에서 교실 종류를 고르고 격자를 드래그해 그립니다. 교실을 누르면 호수·이름과 방과후 <b>강좌명·강사·수업시간</b>을 입력합니다.</li>
                    <li><b>저장하기</b>: 점검에 <b>오류</b>가 없으면 교문에서 모든 교실까지 길안내가 됩니다.</li>
                </ol>
                <h3 style="margin-top:14px"><i class="fa-solid fa-keyboard"></i> 편집 요령</h3>
                <ul class="cg-help">
                    <li>빈 칸은 복도입니다. 맞닿은 건물·연결통로의 복도는 서로 이어집니다.</li>
                    <li>교문·후문은 담장에, 건물출입문은 1층 외벽에 저절로 붙습니다. 길찾기는 <b>교문 → 건물출입문 → 복도 → 계단</b> 순서로 이어집니다.</li>
                    <li>계단은 위·아래층이 <b>같은 자리</b>에 있어야 층이 이어집니다. 계단을 고르고 <kbd>R</kbd>로 오르는 방향을 돌립니다. <b>이 층 배치를 복사</b>로 같은 구조의 층을 빠르게 만들 수 있습니다.</li>
                    <li><kbd>Delete</kbd> 삭제 · <kbd>Ctrl</kbd>+<kbd>Z</kbd> 되돌리기 · <kbd>Ctrl</kbd>+<kbd>S</kbd> 저장</li>
                </ul>`;
            return;
        }
        let html = '';
        if (s.kind === 'building') {
            const kinds = Object.entries(BUILDING_KINDS).map(([k, v]) => `<option value="${k}"${item.kind === k ? ' selected' : ''}>${v.label}</option>`).join('');
            const floorInfo = item.kind === 'connector' ? '' : Array.from({ length: item.floors }, (_, i) => item.baseFloor + i).map(f =>
                `<label class="cg-field"><span>${f}층 정보</span><input type="text" data-floorinfo="${f}" maxlength="200" value="${esc(item.floorInfo?.[f] || '')}" placeholder="예) 교무실 · 행정실 · 돌봄교실"></label>`).join('');
            html = `
                <h3><i class="fa-solid fa-building"></i> 건물 정보</h3>
                <div class="cg-form-grid">
                    <label class="cg-field"><span>건물 이름</span><input type="text" data-f="name" maxlength="20" value="${esc(item.name)}"></label>
                    <label class="cg-field"><span>구분</span><select data-f="kind">${kinds}</select></label>
                    ${numberField('층수', 'floors', item.floors, 1, 12)}
                    ${item.kind === 'connector' ? numberField('시작 층', 'baseFloor', item.baseFloor, 1, 12) : ''}
                    ${numberField('가로(칸)', 'cols', item.cols, 1, 80)}
                    ${numberField('세로(칸)', 'rows', item.rows, 1, 80)}
                    ${numberField('X 위치', 'x', item.x, 0, state.layout.site.cols - 1)}
                    ${numberField('Y 위치', 'z', item.z, 0, state.layout.site.rows - 1)}
                    <label class="cg-field cg-field-sm"><span>외벽 색</span><input type="color" data-f="color" value="${item.color || buildingColor(item)}"></label>
                </div>
                ${floorInfo ? `<div class="cg-subhead">층별 정보</div>${floorInfo}` : ''}
                <label class="cg-field"><span>건물 설명</span><textarea data-f="note" maxlength="300" rows="2">${esc(item.note)}</textarea></label>
                <div class="cg-form-actions">
                    ${item.kind !== 'connector' ? `<button type="button" class="cg-btn cg-btn-primary" data-ed-open-floor><i class="fa-solid fa-table-cells"></i> 층별 교실 배치</button>
                        <button type="button" class="cg-btn cg-btn-ai" data-ed-import-building title="이 건물의 층별 평면도를 올려 교실을 자동으로 그립니다"><i class="fa-solid fa-wand-magic-sparkles"></i> 평면도로 교실 채우기</button>` : ''}
                    <button type="button" class="cg-btn cg-btn-danger" data-ed-delete><i class="fa-solid fa-trash"></i> 건물 삭제</button>
                </div>`;
        } else if (s.kind === 'landmark') {
            const types = Object.entries(LANDMARK_TYPES).map(([k, v]) => `<option value="${k}"${item.type === k ? ' selected' : ''}>${v.label}</option>`).join('');
            html = `
                <h3><i class="fa-solid fa-tree"></i> 바깥 시설</h3>
                <div class="cg-form-grid">
                    <label class="cg-field"><span>종류</span><select data-f="type">${types}</select></label>
                    <label class="cg-field"><span>이름</span><input type="text" data-f="label" maxlength="20" value="${esc(item.label)}"></label>
                    ${numberField('X 위치', 'x', item.x, 0, state.layout.site.cols - 1)}
                    ${numberField('Y 위치', 'z', item.z, 0, state.layout.site.rows - 1)}
                    ${numberField('가로(칸)', 'w', item.w, 1, state.layout.site.cols)}
                    ${numberField('세로(칸)', 'd', item.d, 1, state.layout.site.rows)}
                </div>
                <div class="cg-form-actions"><button type="button" class="cg-btn cg-btn-danger" data-ed-delete><i class="fa-solid fa-trash"></i> 삭제</button></div>`;
        } else if (s.kind === 'gate' || s.kind === 'door') {
            const kindKey = s.kind === 'door' ? 'door' : (item.kind || 'main');
            const kindTabs = Object.entries(GATE_KINDS).map(([k, v]) =>
                `<button type="button" class="cg-seg-btn${k === kindKey ? ' is-active' : ''}" data-gate-kind="${k}" style="--seg:${v.color}"><i class="fa-solid ${v.icon}"></i> ${v.label}</button>`).join('');
            const sideOpts = suffix => Object.entries(SIDES).map(([k, v]) =>
                `<option value="${k}"${item.side === k ? ' selected' : ''}>${v.wall} ${suffix}</option>`).join('');
            if (s.kind === 'gate') {
                const len = item.side === 'n' || item.side === 's' ? state.layout.site.cols : state.layout.site.rows;
                html = `
                    <h3><i class="fa-solid ${GATE_KINDS[kindKey].icon}"></i> 출입문 <small>길찾기 출발 지점</small></h3>
                    <div class="cg-seg">${kindTabs}</div>
                    <div class="cg-form-grid">
                        <label class="cg-field cg-span2"><span>이름</span><input type="text" data-f="label" maxlength="20" value="${esc(item.label)}"></label>
                        <label class="cg-field"><span>놓인 담장</span><select data-f="side">${sideOpts('담장')}</select></label>
                        ${numberField('폭(칸)', 'w', item.w, 1, 12)}
                        ${numberField('담장 위 위치(칸)', 'at', item.at, 0, len - item.w)}
                    </div>
                    <p class="cg-muted">교문·후문은 학교 담장 위에 붙습니다. 캔버스에서 끌면 가장 가까운 담장을 따라 움직입니다.</p>
                    <div class="cg-form-actions"><button type="button" class="cg-btn cg-btn-danger" data-ed-delete><i class="fa-solid fa-trash"></i> 삭제</button></div>`;
            } else {
                const b = findDoorById(item.id).building;
                const len = item.side === 'n' || item.side === 's' ? b.cols : b.rows;
                const bOpts = state.layout.buildings.filter(canHaveDoors).map(x =>
                    `<option value="${esc(x.id)}"${x === b ? ' selected' : ''}>${esc(x.name)}</option>`).join('');
                const blocked = doorBlocked(b, item);
                html = `
                    <h3><i class="fa-solid fa-door-closed"></i> 건물출입문 <small>${esc(b.name)} 1층</small></h3>
                    <div class="cg-seg">${kindTabs}</div>
                    <div class="cg-form-grid">
                        <label class="cg-field cg-span2"><span>이름</span><input type="text" data-f="label" maxlength="20" value="${esc(item.label)}" placeholder="예) 중앙현관, 후관 출입구"></label>
                        <label class="cg-field"><span>건물</span><select data-f="building">${bOpts}</select></label>
                        <label class="cg-field"><span>외벽</span><select data-f="side">${sideOpts('외벽')}</select></label>
                        ${numberField('폭(칸)', 'w', item.w, 1, 8)}
                        ${numberField('벽 위 위치(칸)', 'at', item.at, 0, len - item.w)}
                    </div>
                    ${blocked ? '<p class="cg-error"><i class="fa-solid fa-triangle-exclamation"></i> 문 안쪽이 교실로 막혀 있어 드나들 수 없습니다. 문을 복도·현관·계단 쪽으로 옮기거나 안쪽을 비워 주세요.</p>' : ''}
                    <p class="cg-muted">건물 1층 외벽 위에 붙는 문입니다. 길안내는 이 문으로만 건물에 드나듭니다. 캔버스에서 끌면 외벽을 따라 움직입니다.</p>
                    <div class="cg-form-actions"><button type="button" class="cg-btn cg-btn-danger" data-ed-delete><i class="fa-solid fa-trash"></i> 삭제</button></div>`;
            }
        } else if (s.kind === 'room') {
            const t = ROOM_TYPES[item.type] || ROOM_TYPES.etc;
            const types = Object.entries(ROOM_TYPES).map(([k, v]) => `<option value="${k}"${item.type === k ? ' selected' : ''}>${v.label}</option>`).join('');
            const courses = (item.courses || []).map((c, i) => `
                <div class="cg-course-edit" data-course="${i}">
                    <div class="cg-course-edit-head"><b>강좌 ${i + 1}</b><button type="button" class="cg-icon-btn" data-course-remove="${i}" aria-label="강좌 삭제"><i class="fa-solid fa-xmark"></i></button></div>
                    <div class="cg-form-grid">
                        <label class="cg-field"><span>강좌명</span><input type="text" data-c="name" maxlength="40" value="${esc(c.name)}" placeholder="예) 바이올린"></label>
                        <label class="cg-field"><span>강사명</span><input type="text" data-c="teacher" maxlength="30" value="${esc(c.teacher)}"></label>
                        <label class="cg-field"><span>수업시간</span><input type="text" data-c="time" maxlength="60" value="${esc(c.time)}" placeholder="예) 화요일 14:00 ~ 15:40"></label>
                        <label class="cg-field"><span>대상학년</span><input type="text" data-c="grade" maxlength="30" value="${esc(c.grade)}" placeholder="예) 1 ~ 6학년"></label>
                    </div>
                </div>`).join('');
            const b = findRoomById(item.id)?.building;
            html = `
                <h3 style="--room:${t.color};--room-edge:${t.edge}"><span class="cg-swatch"></span> 교실 정보 <small>${esc(b?.name || '')} ${item.floor}층</small></h3>
                <div class="cg-form-grid">
                    <label class="cg-field"><span>교실 종류</span><select data-f="type">${types}</select></label>
                    <label class="cg-field"><span>교실번호(호수)</span><input type="text" data-f="no" maxlength="12" value="${esc(item.no)}" placeholder="예) 201"></label>
                    <label class="cg-field cg-span2"><span>교실명</span><input type="text" data-f="name" maxlength="30" value="${esc(item.name)}" placeholder="예) 음악실"></label>
                    ${numberField('X', 'x', item.x, 0, 79)}
                    ${numberField('Y', 'z', item.z, 0, 79)}
                    ${numberField('가로', 'w', item.w, 1, 80)}
                    ${numberField('세로', 'd', item.d, 1, 80)}
                </div>
                ${item.type === 'stairs' ? `
                    <div class="cg-subhead">계단 방향</div>
                    <div class="cg-form-grid">
                        <label class="cg-field"><span>올라가는 방향</span><select data-f="dir">${Object.entries(SIDES).map(([k, v]) =>
                            `<option value="${k}"${stairDir(item, b) === k ? ' selected' : ''}>${v.arrow} ${v.label}</option>`).join('')}</select></label>
                        <label class="cg-field"><span>계단 모양</span><select data-f="shape">${Object.entries(STAIR_SHAPES).map(([k, v]) =>
                            `<option value="${k}"${stairShape(item, b) === k ? ' selected' : ''}>${v.label}</option>`).join('')}</select></label>
                    </div>
                    <div class="cg-form-actions" style="margin:-4px 0 10px"><button type="button" class="cg-btn cg-btn-sm" data-ed-rotate><i class="fa-solid fa-rotate-right"></i> 방향 돌리기 <kbd>R</kbd></button></div>
                    <p class="cg-muted" style="margin-top:0">화살표의 점(●)에서 출발해 화살촉 쪽으로 올라갑니다. 복도와 맞닿은 쪽에서 출발하도록 맞춰 주세요.</p>` : ''}
                ${t.course || (item.courses || []).length ? `
                    <div class="cg-subhead">방과후 강좌 <button type="button" class="cg-btn cg-btn-sm" data-course-add><i class="fa-solid fa-plus"></i> 강좌 추가</button></div>
                    ${courses || '<p class="cg-muted">이 교실에서 열리는 방과후 강좌를 추가하세요.</p>'}` : ''}
                <label class="cg-field"><span>안내 메모</span><textarea data-f="note" maxlength="300" rows="2" placeholder="예) 신발을 갈아 신고 입실">${esc(item.note)}</textarea></label>
                <div class="cg-form-actions"><button type="button" class="cg-btn cg-btn-danger" data-ed-delete><i class="fa-solid fa-trash"></i> 교실 삭제</button></div>`;
        }
        form.innerHTML = html;
    }

    const INT_FIELDS = new Set(['floors', 'baseFloor', 'cols', 'rows', 'x', 'z', 'w', 'd', 'at']);
    // 입력칸 하나를 고치는 동안의 변경은 되돌리기 한 번으로 묶는다.
    let pendingSnapshot = null;
    form.addEventListener('focusin', (e) => {
        if (e.target.matches('input, select, textarea')) pendingSnapshot = JSON.stringify(state.layout);
    });
    form.addEventListener('focusout', () => { pendingSnapshot = null; });

    form.addEventListener('input', (e) => {
        const el = e.target;
        const item = selected();
        if (!item) return;
        if (pendingSnapshot) {
            state.history.push(pendingSnapshot);
            state.future = [];
            pendingSnapshot = null;
            updateUndo();
        }
        if (el.dataset.floorinfo) {
            item.floorInfo = item.floorInfo || {};
            item.floorInfo[el.dataset.floorinfo] = el.value;
            changed();
            return;
        }
        if (el.dataset.c) {
            const idx = Number(el.closest('[data-course]').dataset.course);
            item.courses[idx][el.dataset.c] = el.value;
            changed();
            return;
        }
        const key = el.dataset.f;
        if (!key) return;
        const s = state.sel;
        if (INT_FIELDS.has(key)) {
            const v = parseInt(el.value, 10);
            if (Number.isNaN(v)) return;
            applyNumber(item, key, v);
        } else if (s.kind === 'landmark' && key === 'type') {
            // 이름을 따로 고치지 않았다면 종류에 맞춰 이름도 바꾼다(주차장인데 '운동장'으로 보이던 문제).
            const defaults = Object.values(LANDMARK_TYPES).map(t => t.label);
            if (!item.label || defaults.includes(item.label)) item.label = LANDMARK_TYPES[el.value]?.label || item.label;
            item.type = el.value;
        } else if (s.kind === 'gate' && key === 'side') {
            item.side = el.value;
            fitGate(state.layout.site, item);
        } else if (s.kind === 'door' && key === 'side') {
            item.side = el.value;
            fitDoor(findDoorById(item.id).building, item);
        } else if (s.kind === 'door' && key === 'building') {
            const target = state.layout.buildings.find(b => b.id === el.value);
            if (target) { moveDoorTo(item, target); fitDoor(target, item); }
        } else {
            item[key] = el.value;
        }
        // 종류를 바꾸면 양식 항목(강좌 입력 등)이 달라지므로 양식을 다시 그린다.
        changed({ form: el.tagName === 'SELECT' });
        if (key === 'floors' || key === 'baseFloor' || key === 'kind' || key === 'name') renderTabs();
    });
    form.addEventListener('change', (e) => {
        // 숫자 칸은 입력을 마치면 실제 적용된 값으로 다시 맞춘다.
        if (e.target.dataset.f && INT_FIELDS.has(e.target.dataset.f)) renderForm();
    });

    function applyNumber(item, key, v) {
        const l = state.layout;
        const s = state.sel;
        if (s.kind === 'building') {
            if (key === 'floors') {
                item.floors = clamp(v, 1, 12 - item.baseFloor + 1);
                item.rooms = item.rooms.filter(r => r.floor <= topFloor(item));
            } else if (key === 'baseFloor') {
                item.baseFloor = clamp(v, 1, 12);
                item.floors = clamp(item.floors, 1, 12 - item.baseFloor + 1);
            } else if (key === 'cols') item.cols = clamp(v, Math.max(1, ...item.rooms.map(r => r.x + r.w)), Math.min(80, l.site.cols - item.x));
            else if (key === 'rows') item.rows = clamp(v, Math.max(1, ...item.rooms.map(r => r.z + r.d)), Math.min(80, l.site.rows - item.z));
            else if (key === 'x') item.x = clamp(v, 0, l.site.cols - item.cols);
            else if (key === 'z') item.z = clamp(v, 0, l.site.rows - item.rows);
            if (key === 'cols' || key === 'rows') fitDoors(item);
        } else if (s.kind === 'landmark') {
            if (key === 'w') item.w = clamp(v, 1, l.site.cols - item.x);
            else if (key === 'd') item.d = clamp(v, 1, l.site.rows - item.z);
            else if (key === 'x') item.x = clamp(v, 0, l.site.cols - item.w);
            else if (key === 'z') item.z = clamp(v, 0, l.site.rows - item.d);
        } else if (s.kind === 'gate') {
            item[key] = v;
            fitGate(l.site, item);
        } else if (s.kind === 'door') {
            item[key] = v;
            fitDoor(findDoorById(item.id).building, item);
        } else if (s.kind === 'room') {
            const b = findRoomById(item.id).building;
            const next = { x: item.x, z: item.z, w: item.w, d: item.d };
            if (key === 'x') next.x = clamp(v, 0, b.cols - item.w);
            else if (key === 'z') next.z = clamp(v, 0, b.rows - item.d);
            else if (key === 'w') next.w = clamp(v, 1, b.cols - item.x);
            else if (key === 'd') next.d = clamp(v, 1, b.rows - item.z);
            const prevFloor = state.floor;
            state.floor = item.floor;
            if (!overlaps(b, next, item.id)) Object.assign(item, next);
            else flash('다른 교실과 겹칩니다.');
            state.floor = prevFloor;
        }
    }

    form.addEventListener('click', (e) => {
        const btn = e.target.closest('button');
        if (!btn) return;
        const item = selected();
        if ('edDelete' in btn.dataset && item) {
            const s = state.sel;
            const name = s.kind === 'room' ? roomTitle(item) : (item.name || item.label || '');
            const extra = s.kind === 'building' && item.rooms.length ? `\n건물 안의 교실 ${item.rooms.length}개도 함께 삭제됩니다.` : '';
            if (!window.confirm(`'${name}'을(를) 삭제할까요?${extra}`)) return;
            snapshot();
            const l = state.layout;
            if (s.kind === 'building') {
                l.buildings = l.buildings.filter(b => b.id !== item.id);
                if (state.buildingId === item.id) state.buildingId = firstRoomBuilding()?.id || null;
            } else if (s.kind === 'landmark') l.landmarks = l.landmarks.filter(x => x.id !== item.id);
            else if (s.kind === 'gate') l.gates = l.gates.filter(x => x.id !== item.id);
            else if (s.kind === 'door') {
                const b = findDoorById(item.id).building;
                b.doors = b.doors.filter(x => x.id !== item.id);
            } else if (s.kind === 'room') {
                const b = findRoomById(item.id).building;
                b.rooms = b.rooms.filter(r => r.id !== item.id);
            }
            state.sel = null;
            changed({ form: true });
            renderTabs();
        } else if ('edOpenFloor' in btn.dataset && item) {
            state.tab = 'floor';
            state.buildingId = item.id;
            state.floor = item.baseFloor;
            state.sel = null;
            renderForm();
            renderTabs();
        } else if ('edImportBuilding' in btn.dataset && item) {
            openImport(item.id);
        } else if (btn.dataset.gateKind && item) {
            changeGateKind(item, btn.dataset.gateKind);
        } else if ('edRotate' in btn.dataset && item) {
            rotateStairs(item);
        } else if ('courseAdd' in btn.dataset && item) {
            snapshot();
            item.courses.push({ name: '', teacher: '', time: '', grade: '' });
            changed({ form: true });
            const inputs = form.querySelectorAll('[data-c="name"]');
            inputs[inputs.length - 1]?.focus();
        } else if (btn.dataset.courseRemove !== undefined && item) {
            snapshot();
            item.courses.splice(Number(btn.dataset.courseRemove), 1);
            changed({ form: true });
        }
    });

    function doorBlocked(b, door) {
        return doorCells(b, door).every((c) => {
            const lx = c.x - b.x;
            const lz = c.z - b.z;
            const room = b.rooms.find(r => r.floor === 1 && lx >= r.x && lx < r.x + r.w && lz >= r.z && lz < r.z + r.d);
            return room && !ROOM_TYPES[room.type]?.walk;
        });
    }

    const isDefaultGateLabel = label => !label || label === '정문' || Object.values(GATE_KINDS).some(k => k.label === label) || label === '출입구';

    /** 교문 ↔ 후문 ↔ 건물출입문 전환. 담장 위 문과 외벽 위 문은 서로 옮겨 붙인다. */
    function changeGateKind(item, kind) {
        const l = state.layout;
        const from = state.sel.kind === 'door' ? 'door' : item.kind;
        if (from === kind) return;
        if (from !== 'door' && kind !== 'door') {
            snapshot();
            if (isDefaultGateLabel(item.label)) item.label = GATE_KINDS[kind].label;
            item.kind = kind;
            changed({ form: true });
            return;
        }
        if (kind === 'door') {
            const sp = gateSpan(item, l.site);
            const target = nearestDoorBuilding(sp.anchor.x, sp.anchor.z);
            if (!target) { flash('1층부터 시작하는 건물이 있어야 건물출입문을 둘 수 있습니다.'); return; }
            snapshot();
            const door = { id: item.id, label: isDefaultGateLabel(item.label) ? '출입구' : item.label, side: 's', at: 0, w: 2 };
            snapDoor(target, sp.anchor.x - target.x, sp.anchor.z - target.z, door);
            l.gates = l.gates.filter(g => g !== item);
            target.doors = target.doors || [];
            target.doors.push(door);
            state.sel = { kind: 'door', id: door.id };
        } else {
            const found = findDoorById(item.id);
            snapshot();
            const seg = doorSegment(found.building, item);
            const gate = { id: item.id, kind, label: isDefaultGateLabel(item.label) ? GATE_KINDS[kind].label : item.label, w: 3 };
            snapGate(l.site, (seg.x1 + seg.x2) / 2, (seg.z1 + seg.z2) / 2, gate);
            found.building.doors = found.building.doors.filter(d => d !== item);
            l.gates.push(gate);
            state.sel = { kind: 'gate', id: gate.id };
            if (state.tab === 'floor') state.tab = 'site';
        }
        changed({ form: true });
        renderTabs();
    }

    function rotateStairs(item) {
        if (!item || item.type !== 'stairs') return;
        snapshot();
        const order = ['n', 'e', 's', 'w'];
        item.dir = order[(order.indexOf(stairDir(item, findRoomById(item.id)?.building)) + 1) % 4];
        changed({ form: true });
    }

    function addGate(kind) {
        const l = state.layout;
        if (kind === 'door') {
            const b = (state.sel?.kind === 'building' && selected()?.baseFloor === 1 ? selected() : null)
                || (state.tab === 'floor' ? building() : null)
                || l.buildings.find(x => canHaveDoors(x) && x.kind !== 'connector') || l.buildings.find(canHaveDoors);
            if (!b || !canHaveDoors(b)) { flash('1층부터 시작하는 건물을 먼저 추가해 주세요.'); return; }
            snapshot();
            b.doors = b.doors || [];
            const door = fitDoor(b, makeDoor(b.doors.length ? '출입구' : '중앙현관', 's', Math.floor(b.cols / 2) - 1, 2));
            b.doors.push(door);
            select({ kind: 'door', id: door.id });
        } else {
            snapshot();
            const used = new Set(l.gates.map(g => `${g.side}`));
            const side = kind === 'main' ? 's' : (used.has('n') ? 'e' : 'n');
            const len = side === 'n' || side === 's' ? l.site.cols : l.site.rows;
            const gate = makeGate(l.site, kind, GATE_KINDS[kind].label, side, Math.floor(len / 2) - 1, kind === 'main' ? 4 : 3);
            l.gates.push(gate);
            select({ kind: 'gate', id: gate.id });
        }
        changed();
    }

    // ------------------------------------------------------------ 상단 · 툴바 동작
    function freeSpot(w, d) {
        const l = state.layout;
        for (let z = 1; z < l.site.rows - d; z += 1) {
            for (let x = 1; x < l.site.cols - w; x += 1) {
                const hit = l.buildings.some(b => x < b.x + b.cols && b.x < x + w && z < b.z + b.rows && b.z < z + d)
                    || l.landmarks.some(b => x < b.x + b.w && b.x < x + w && z < b.z + b.d && b.z < z + d);
                if (!hit) return { x, z };
            }
        }
        return null;
    }

    // ------------------------------------------------------------ 부지 넓히기 · 줄이기
    const MAX_SITE = 160;

    /** 부지를 한 방향(side)으로 n칸 넓힌다. 북·서쪽이면 건물·시설·교문을 그만큼 옮겨 제자리를 지킨다. 실제로 넓힌 칸 수를 돌려준다. */
    function growSite(side, n) {
        const l = state.layout;
        const horizontal = side === 'e' || side === 'w';
        const room = MAX_SITE - (horizontal ? l.site.cols : l.site.rows);
        const add = Math.max(0, Math.min(n, room));
        if (!add) return 0;
        if (horizontal) l.site.cols += add;
        else l.site.rows += add;
        if (side === 'w' || side === 'n') shiftSite(side === 'w' ? add : 0, side === 'n' ? add : 0);
        for (const g of l.gates) fitGate(l.site, g);
        return add;
    }

    /** 부지 위 모든 것을 (dx, dz)칸 옮긴다. 담장 위 교문은 담장을 따라 옮긴다. */
    function shiftSite(dx, dz) {
        const l = state.layout;
        for (const b of l.buildings) { b.x += dx; b.z += dz; }
        for (const lm of l.landmarks) { lm.x += dx; lm.z += dz; }
        for (const g of l.gates) g.at += g.side === 'n' || g.side === 's' ? dx : dz;
        // 평면도 밑그림은 건물에 붙여 그리므로(underlayPlace) 건물과 함께 옮겨진다.
    }

    /** 건물·시설이 차지한 범위 바깥의 빈 여백을 margin칸만 남기고 줄인다. */
    function trimSite(margin = 4) {
        const l = state.layout;
        const items = [
            ...l.buildings.map(b => [b.x, b.z, b.x + b.cols, b.z + b.rows]),
            ...l.landmarks.map(m => [m.x, m.z, m.x + m.w, m.z + m.d]),
        ];
        if (!items.length) return false;
        const x0 = Math.min(...items.map(r => r[0]));
        const z0 = Math.min(...items.map(r => r[1]));
        const x1 = Math.max(...items.map(r => r[2]));
        const z1 = Math.max(...items.map(r => r[3]));
        const dx = -Math.max(0, x0 - margin);
        const dz = -Math.max(0, z0 - margin);
        const cols = clamp(x1 + dx + margin, 10, MAX_SITE);
        const rows = clamp(z1 + dz + margin, 10, MAX_SITE);
        if (!dx && !dz && cols >= l.site.cols && rows >= l.site.rows) return false;
        // 교문이 있던 담장 위 위치(부지 기준)를 먼저 기억해 두고 옮긴 뒤 다시 붙인다.
        const gates = l.gates.map(g => gateSpan(g, l.site).anchor);
        shiftSite(dx, dz);
        l.site.cols = Math.min(l.site.cols, Math.max(cols, 10));
        l.site.rows = Math.min(l.site.rows, Math.max(rows, 10));
        l.gates.forEach((g, i) => {
            const a = gates[i];
            const along = g.side === 'n' || g.side === 's' ? a.x + dx : a.z + dz;
            g.at = Math.round(along - g.w / 2);
            fitGate(l.site, g);
        });
        return true;
    }

    /** w×d 크기가 들어갈 빈자리. 없으면 부지를 남쪽(안 되면 동쪽)으로 넓혀 자리를 만든다. */
    function placeNew(w, d, what) {
        let spot = freeSpot(w, d);
        if (spot) return spot;
        const l = state.layout;
        const need = d + 2;
        if (l.site.rows + need <= MAX_SITE) {
            const top = Math.max(0, ...l.buildings.map(b => b.z + b.rows), ...l.landmarks.map(m => m.z + m.d));
            growSite('s', Math.max(need, top + need - l.site.rows));
            spot = freeSpot(w, d);
        }
        if (!spot && l.site.cols + w + 2 <= MAX_SITE) {
            growSite('e', w + 2);
            spot = freeSpot(w, d);
        }
        if (spot) {
            flash(`빈자리가 없어 부지를 넓히고 새 ${what} 자리를 만들었습니다. (지금 ${l.site.cols} × ${l.site.rows}칸)`);
            return spot;
        }
        flash(`부지가 최대 크기(${MAX_SITE} × ${MAX_SITE}칸)라 새 ${what} 자리가 없습니다. 건물 크기를 줄이거나 [빈 여백 줄이기]를 눌러 보세요.`, true);
        return null;
    }

    root.addEventListener('click', (e) => {
        const btn = e.target.closest('button');
        if (!btn || !root.contains(btn) || form.contains(btn) || importDlg?.contains(btn)) return;
        const d = btn.dataset;
        const l = state.layout;
        if ('edImportOpen' in d) openImport();
        else if ('edIssueBadge' in d) $('[data-ed-check-card]')?.scrollIntoView({ block: 'start', behavior: 'smooth' });
        else if ('edStartManual' in d) {
            state.manualStart = true;
            state.tab = 'site';
            renderTabs();
            flash('[건물 추가]로 본관부터 놓고, [출입문 추가]로 교문을 놓으세요.');
        }
        else if ('edUnderlayDelete' in d) deleteFloorplan();
        else if (d.edGrow) {
            const step = Number($('[data-ed-grow-step]')?.value) || 10;
            snapshot();
            const added = growSite(d.edGrow, step);
            if (!added) {
                state.history.pop();
                updateUndo();
                flash(`부지는 가로·세로 최대 ${MAX_SITE}칸까지 넓힐 수 있습니다.`, true);
                return;
            }
            changed({ form: true });
            renderTabs();
            flash(`${SIDES[d.edGrow].wall}으로 ${added}칸 넓혔습니다. (지금 ${l.site.cols} × ${l.site.rows}칸)`);
        } else if ('edTrim' in d) {
            snapshot();
            if (!trimSite()) {
                state.history.pop();
                updateUndo();
                flash('줄일 빈 여백이 없습니다.');
                return;
            }
            changed({ form: true });
            renderTabs();
            flash(`빈 여백을 줄였습니다. (지금 ${l.site.cols} × ${l.site.rows}칸)`);
        } else if (d.edTab) {
            state.tab = d.edTab;
            if (state.tab === 'floor' && !building()) state.buildingId = firstRoomBuilding()?.id || null;
            if (state.tab === 'floor' && state.sel && state.sel.kind !== 'room') state.sel = null;
            if (state.tab === 'site' && state.sel?.kind === 'room') state.sel = null;
            renderForm();
            renderTabs();
        } else if (d.edFloor) {
            state.floor = Number(d.edFloor);
            if (state.sel?.kind === 'room') state.sel = null;
            renderForm();
            renderTabs();
        } else if (d.edType) {
            state.drawType = d.edType;
            const item = state.sel?.kind === 'room' ? selected() : null;
            renderTabs();
            if (item && item.type !== d.edType && window.confirm(`선택한 교실을 '${ROOM_TYPES[d.edType].label}'(으)로 바꿀까요?`)) {
                snapshot();
                item.type = d.edType;
                changed({ form: true });
            }
        } else if ('edAddBuilding' in d) {
            snapshot();
            const spot = placeNew(20, 8, '건물');
            if (!spot) { state.history.pop(); updateUndo(); return; }
            const count = l.buildings.filter(b => b.kind !== 'connector').length;
            const kind = count === 0 ? 'main' : count === 1 ? 'annex' : 'wing';
            const b = {
                id: newId('b'), name: BUILDING_KINDS[kind].label, kind, x: spot.x, z: spot.z, cols: 20, rows: 8,
                floors: 3, baseFloor: 1, color: '', note: '', floorInfo: {}, rooms: [],
            };
            l.buildings.push(b);
            state.buildingId = b.id;
            select({ kind: 'building', id: b.id });
            changed();
            renderTabs();
        } else if ('edAddConnector' in d) {
            snapshot();
            const spot = placeNew(3, 8, '연결통로');
            if (!spot) { state.history.pop(); updateUndo(); return; }
            const b = {
                id: newId('b'), name: '연결통로', kind: 'connector', x: spot.x, z: spot.z, cols: 3, rows: 8,
                floors: 1, baseFloor: 1, color: '', note: '', floorInfo: {}, rooms: [],
            };
            l.buildings.push(b);
            select({ kind: 'building', id: b.id });
            changed();
            renderTabs();
        } else if ('edAddDoor' in d) {
            addGate('door');
        } else if ('edUndo' in d) undo();
        else if ('edRedo' in d) redo();
        else if ('edSave' in d) save();
        else if ('edCopyFloor' in d) copyFloor();
    });

    root.addEventListener('change', (e) => {
        const el = e.target;
        const l = state.layout;
        if (form.contains(el) || importDlg?.contains(el)) return;
        if (el.matches('[data-ed-underlay-on]')) {
            state.underlay.on = el.checked;
            drawCanvas();
        } else if (el.matches('[data-ed-building]')) {
            state.buildingId = el.value;
            state.sel = null;
            renderForm();
            renderTabs();
        } else if (el.matches('[data-ed-add-landmark]')) {
            const type = el.value;
            el.value = '';
            if (!LANDMARK_TYPES[type]) return;
            snapshot();
            const size = { field: [16, 10], parking: [12, 6], garden: [8, 4], playground: [8, 6], etc: [8, 6] }[type];
            const spot = placeNew(size[0], size[1], LANDMARK_TYPES[type].label);
            if (!spot) { state.history.pop(); updateUndo(); return; }
            const lm = { id: newId('l'), type, label: LANDMARK_TYPES[type].label, x: spot.x, z: spot.z, w: size[0], d: size[1] };
            l.landmarks.push(lm);
            select({ kind: 'landmark', id: lm.id });
            changed();
        } else if (el.matches('[data-ed-add-gate]')) {
            const kind = el.value;
            el.value = '';
            if (GATE_KINDS[kind]) addGate(kind);
        } else if (el.matches('[data-ed-site-cols], [data-ed-site-rows], [data-ed-cell]')) {
            snapshot();
            const l = state.layout;
            const need = {
                cols: Math.max(10, ...l.buildings.map(b => b.x + b.cols), ...l.landmarks.map(x => x.x + x.w)),
                rows: Math.max(10, ...l.buildings.map(b => b.z + b.rows), ...l.landmarks.map(x => x.z + x.d)),
            };
            if (el.matches('[data-ed-site-cols]')) l.site.cols = clamp(parseInt(el.value, 10) || l.site.cols, need.cols, 160);
            if (el.matches('[data-ed-site-rows]')) l.site.rows = clamp(parseInt(el.value, 10) || l.site.rows, need.rows, 160);
            if (el.matches('[data-ed-cell]')) l.cell = clamp(parseInt(el.value, 10) || 3, 1, 10);
            for (const g of l.gates) fitGate(l.site, g);
            changed();
            renderTabs();
        } else if (el.matches('[data-ed-school]')) {
            if (state.dirty && !window.confirm('저장하지 않은 변경 내용이 있습니다. 다른 학교로 이동할까요?')) {
                el.value = String(schoolId);
                return;
            }
            state.dirty = false;
            window.location.href = `?school=${encodeURIComponent(el.value)}`;
        }
    });

    function copyFloor() {
        const b = building();
        const target = Number($('[data-ed-copy-to]')?.value);
        if (!b || !target || target === state.floor) return;
        const src = b.rooms.filter(r => r.floor === state.floor);
        if (!src.length) { flash('복사할 교실이 없습니다.'); return; }
        const existing = b.rooms.filter(r => r.floor === target).length;
        if (!window.confirm(`${state.floor}층 배치(교실 ${src.length}개)를 ${target}층에 복사할까요?${existing ? `\n${target}층의 교실 ${existing}개는 지워집니다.` : ''}\n강좌 정보는 복사하지 않습니다.`)) return;
        snapshot();
        b.rooms = b.rooms.filter(r => r.floor !== target);
        for (const r of src) {
            let no = r.no;
            const m = /^(\d)(\d{2})$/.exec(no || '');
            if (m && Number(m[1]) === state.floor) no = `${target}${m[2]}`;
            b.rooms.push({ ...clone(r), id: newId('r'), floor: target, no, courses: [] });
        }
        state.floor = target;
        changed();
        renderTabs();
        flash(`${target}층에 복사했습니다.`);
    }

    $('[data-ed-underlay-alpha]')?.addEventListener('input', (e) => {
        state.underlay.alpha = clamp(Number(e.target.value) / 100, 0.1, 0.9);
        drawCanvas();
    });

    // ------------------------------------------------------------ 평면도로 자동 그리기
    const importDlg = $('[data-ed-import]');
    // target: 평면도를 쓸 곳 — REPLACE(전체 새로 그리기) · ADD(새 건물로 추가) · 건물 id(그 건물 교실 채우기)
    // ran: 분석을 돌릴 때의 target(결과를 적용할 때 씀)
    const importState = { files: [], busy: false, result: null, timer: null, target: null, ran: null };
    const MAX_IMPORT_FILES = 12;
    const MAX_BUILDINGS = 20;           // routes/classroom_guide.py MAX_BUILDINGS
    const REPLACE = '__replace';
    const ADD = '__add';

    function importStep(name) {
        importDlg.querySelectorAll('[data-ed-import-step]').forEach((el) => { el.hidden = el.dataset.edImportStep !== name; });
    }

    /** targetId: 건물 정보의 [평면도로 교실 채우기]에서 열면 그 건물. */
    function openImport(targetId = null) {
        if (!importDlg) return;
        if (!importState.busy && !importState.result) {
            const rooms = state.layout.buildings.filter(b => b.kind !== 'connector');
            const valid = id => rooms.some(b => b.id === id);
            // 기본값: 부른 건물 → 아직 교실이 없는 건물(새로 놓은 후관 등) → 새 건물로 추가
            importState.target = !state.layout.buildings.length ? REPLACE
                : valid(targetId) ? targetId
                    : rooms.find(b => !b.rooms.length)?.id || ADD;
            renderImportTarget();
        }
        if (!importState.busy) {
            importStep(importState.result ? 'done' : 'pick');
            renderImportList();
        }
        if (!importDlg.open) importDlg.showModal();
    }

    function renderImportTarget() {
        const wrap = importDlg.querySelector('[data-ed-import-target-wrap]');
        const l = state.layout;
        wrap.hidden = !l.buildings.length;
        if (!l.buildings.length) return;
        const select = wrap.querySelector('[data-ed-import-target]');
        const opt = (value, label) => `<option value="${esc(value)}"${importState.target === value ? ' selected' : ''}>${esc(label)}</option>`;
        select.innerHTML = [
            opt(ADD, '새 건물로 추가 (지금 배치는 그대로)'),
            ...l.buildings.filter(b => b.kind !== 'connector').map(b => opt(b.id, `${b.name}의 교실 채우기${b.rooms.length ? ` (교실 ${b.rooms.length}개 있음)` : ' (비어 있음)'}`)),
            opt(REPLACE, '전체 배치도를 새로 그리기 (지금 배치가 바뀜)'),
        ].join('');
        const t = importState.target;
        const b = l.buildings.find(x => x.id === t);
        const note = wrap.querySelector('[data-ed-import-target-note]');
        note.classList.toggle('is-danger', t === REPLACE);
        note.textContent = t === REPLACE
            ? '지금 편집 중인 배치 전체가 새 초안으로 바뀌고, 보관한 평면도도 새 도면으로 바뀝니다. 저장하기 전에는 되돌리기로 복구할 수 있습니다.'
            : b ? `${b.name}의 부지 위치는 그대로 두고, 올린 층의 교실·계단·출입구를 도면대로 다시 그립니다. 올리지 않은 층은 그대로 둡니다. 도면에 다른 건물이 함께 있어도 ${b.name}만 넣습니다.`
                : '지금 배치는 그대로 두고, 도면에서 찾은 건물을 부지의 빈자리에 더합니다. 이미 있는 이름의 건물(도면에 함께 그려진 본관 등)은 넣지 않습니다.';
    }

    /** 파일 이름의 '3층', '3F', '3floor' 등에서 층을 짐작한다(없으면 0 = 자동). */
    function guessFloor(name) {
        const m = /(?:^|[^0-9지하B])(\d{1,2})\s*(?:층|f\b|floor)/i.exec(` ${name.replace(/\.[^.]+$/, '')} `);
        const floor = m ? Number(m[1]) : 0;
        return floor >= 1 && floor <= 12 ? floor : 0;
    }

    // 올릴 때 고르는 건물. 고르면 AI가 한 층을 여러 동으로 나눠 읽어도 그 건물 하나로 합친다.
    const PLAN_BUILDINGS = ['본관', '후관', '별관', '특별관'];

    /** 파일 이름에 건물 이름이 있으면 그 건물로 짐작한다(없으면 '' = 자동). */
    function guessBuilding(name) {
        const names = [...new Set([...PLAN_BUILDINGS, ...state.layout.buildings.map(b => b.name)])];
        return names.filter(n => n && name.includes(n)).sort((a, b) => b.length - a.length)[0] || '';
    }

    function addImportFiles(list) {
        const accepted = Array.from(list || []).filter(f => /^image\//.test(f.type) || /\.pdf$/i.test(f.name) || f.type === 'application/pdf');
        if (accepted.length < (list?.length || 0)) flash('이미지(JPG·PNG) 또는 PDF 파일만 올릴 수 있습니다.', true);
        for (const file of accepted) {
            if (importState.files.length >= MAX_IMPORT_FILES) { flash(`평면도는 한 번에 ${MAX_IMPORT_FILES}개까지 올릴 수 있습니다.`, true); break; }
            if (file.size > 25 * 1024 * 1024) { flash(`'${file.name}'은(는) 25MB를 넘어 올릴 수 없습니다.`, true); continue; }
            importState.files.push({ file, floor: guessFloor(file.name), building: guessBuilding(file.name) });
        }
        importState.files.sort((a, b) => (a.floor || 99) - (b.floor || 99));
        renderImportList();
    }

    function renderImportList() {
        const list = importDlg.querySelector('[data-ed-import-list]');
        const floorOptions = f => ['<option value="0">층 자동 인식</option>',
            ...Array.from({ length: 12 }, (_, i) => `<option value="${i + 1}"${f === i + 1 ? ' selected' : ''}>${i + 1}층</option>`)].join('');
        // 건물 채우기(특정 건물)로 올릴 때는 모두 그 건물이므로 건물을 고르지 않는다.
        const fixed = state.layout.buildings.find(b => b.id === importState.target);
        const names = [...new Set([...PLAN_BUILDINGS, ...state.layout.buildings.filter(b => b.kind !== 'connector').map(b => b.name)])];
        const buildingOptions = v => ['<option value="">건물 자동 인식</option>',
            ...names.map(n => `<option value="${esc(n)}"${v === n ? ' selected' : ''}>${esc(n)}</option>`)].join('');
        list.innerHTML = importState.files.map(({ file, floor, building: bName }, i) => `
            <li>
                <i class="fa-regular ${/\.pdf$/i.test(file.name) ? 'fa-file-pdf' : 'fa-file-image'}"></i>
                <span class="cg-import-name" title="${esc(file.name)}">${esc(file.name)}<small>${(file.size / 1048576).toFixed(1)}MB</small></span>
                ${fixed ? '' : `<select class="cg-select" data-ed-import-building="${i}" aria-label="${esc(file.name)} 건물">${buildingOptions(bName)}</select>`}
                <select class="cg-select" data-ed-import-floor="${i}" aria-label="${esc(file.name)} 층">${floorOptions(floor)}</select>
                <button type="button" class="cg-icon-btn" data-ed-import-remove="${i}" aria-label="빼기"><i class="fa-solid fa-xmark"></i></button>
            </li>`).join('');
        importDlg.querySelector('[data-ed-import-run]').disabled = !importState.files.length;
    }

    async function runImport() {
        if (importState.busy || !importState.files.length) return;
        const body = new FormData();
        for (const { file, floor, building: bName } of importState.files) {
            body.append('files', file, file.name);
            body.append('floors', String(floor || 0));
            body.append('buildings', bName || '');
        }
        const l = state.layout;
        const target = l.buildings.length ? importState.target : REPLACE;
        const targetBuilding = l.buildings.find(b => b.id === target);
        if (target !== REPLACE) {
            if (!targetBuilding && l.buildings.length >= MAX_BUILDINGS) {
                flash(`건물은 최대 ${MAX_BUILDINGS}개까지 둘 수 있습니다. 채울 건물을 고르세요.`, true);
                return;
            }
            body.append('mode', 'append');
            if (targetBuilding) {
                body.append('target', targetBuilding.id);
                body.append('target_name', targetBuilding.name);
            }
            body.append('existing', JSON.stringify(l.buildings.map(b => b.name)));
        }
        importState.ran = target;
        importState.busy = true;
        importStep('busy');
        const started = Date.now();
        const elapsed = importDlg.querySelector('[data-ed-import-elapsed]');
        const tick = () => {
            const s = Math.floor((Date.now() - started) / 1000);
            elapsed.textContent = `(${Math.floor(s / 60)}분 ${String(s % 60).padStart(2, '0')}초)`;
        };
        tick();
        importState.timer = setInterval(tick, 1000);
        try {
            const res = await fetch(analyzeUrl, { method: 'POST', headers: { 'X-Requested-With': 'XMLHttpRequest' }, body });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data.status !== 'success') throw new Error(data.message || `평면도를 분석하지 못했습니다. (${res.status})`);
            importState.result = data;
            state.underlay.data = data.floorplan || null;
            planImages.clear();
            renderImportResult(data);
            importStep('done');
        } catch (err) {
            importStep('pick');
            flash(err.message && !/fetch|network/i.test(err.message) ? err.message : '네트워크 오류로 분석하지 못했습니다.', true);
        } finally {
            clearInterval(importState.timer);
            importState.busy = false;
        }
    }

    function renderImportResult(data) {
        const s = data.stats || {};
        importDlg.querySelector('[data-ed-import-stats]').innerHTML = `
            <div><b>${s.buildings || 0}</b><span>건물</span></div>
            <div><b>${s.floors || 0}</b><span>층 도면</span></div>
            <div><b>${s.rooms || 0}</b><span>교실·실</span></div>`;
        const warnings = data.warnings || [];
        importDlg.querySelector('[data-ed-import-warnings]').innerHTML = warnings.length
            ? `<ul>${warnings.map(w => `<li><i class="fa-solid fa-circle-exclamation"></i> ${esc(w)}</li>`).join('')}</ul>`
            : '<p class="cg-ok"><i class="fa-solid fa-circle-check"></i> 특별히 확인할 점이 없습니다.</p>';
    }

    function applyImport() {
        const data = importState.result;
        if (!data?.layout) return;
        snapshot();
        let done;
        if (importState.ran === REPLACE) {
            state.layout = normalizeClient(data.layout);
            done = { building: firstRoomBuilding(), msg: '초안을 불러왔습니다.' };
        } else {
            done = mergeImport(data, importState.ran);
            if (!done) {
                // 자리가 없어 넣지 못했으면(부지 최대 크기 등) 넣기 전으로 돌린다.
                state.layout = JSON.parse(state.history.pop());
                updateUndo();
                return;
            }
        }
        state.underlay.on = true;
        state.tab = 'floor';
        state.buildingId = done.building?.id || firstRoomBuilding()?.id || null;
        state.floor = done.floor || building()?.baseFloor || 1;
        state.sel = null;
        importState.result = null;
        importState.files = [];
        importDlg.close();
        changed({ form: true });
        renderTabs();
        if (importState.ran === REPLACE) preview.resetView(true);
        renderIssues();
        const counts = issueCounts();
        flash(counts.error || counts.warn
            ? `${done.msg} 오른쪽 점검 목록의 오류 ${counts.error}개 · 확인 ${counts.warn}개를 눌러 차례로 고치세요.`
            : `${done.msg} 점검을 모두 통과했습니다. 밑그림과 비교해 본 뒤 저장하세요.`);
    }

    /**
     * 지금 배치도에 가져온 건물을 더한다(importState.ran 이 건물 id 면 그 건물의 교실을 채운다).
     * 돌려주는 값: { building(열어 볼 건물), floor, msg(안내 문구) } · 넣지 못하면 null
     */
    function mergeImport(data, target) {
        const l = state.layout;
        const incoming = normalizeClient(data.layout).buildings;
        const b = l.buildings.find(x => x.id === target);
        if (b) {
            const src = incoming.find(x => x.id === b.id) || incoming[0];
            if (!src) {
                flash('도면에서 채울 건물을 찾지 못했습니다.', true);
                return null;
            }
            // 올린 층만 새 교실로 바꾸고, 올리지 않은 층의 교실은 그대로 둔다.
            const floors = new Set([
                ...(data.floorplan?.floors || []).filter(f => f.building === b.id).map(f => f.floor),
                ...src.rooms.map(r => r.floor),
            ]);
            const kept = b.rooms.filter(r => !floors.has(r.floor));
            b.rooms = [...kept, ...src.rooms];
            b.cols = Math.max(src.cols, ...kept.map(r => r.x + r.w));
            b.rows = Math.max(src.rows, ...kept.map(r => r.z + r.d));
            b.floors = Math.max(topFloor(b), topFloor(src), ...b.rooms.map(r => r.floor)) - b.baseFloor + 1;
            if (floors.has(1) && src.doors.length) b.doors = src.doors;
            fitIntoSite(b);
            fitDoors(b);
            const first = [...floors].sort((x, y) => x - y)[0];
            return { building: b, floor: first, msg: `${b.name}에 평면도 초안을 넣었습니다.` };
        }
        const list = incoming.slice(0, Math.max(0, MAX_BUILDINGS - l.buildings.length));
        if (!list.length) {
            flash(`건물은 최대 ${MAX_BUILDINGS}개까지 둘 수 있습니다.`, true);
            return null;
        }
        // 가져온 건물끼리의 위치(구름다리 등)는 그대로 두고, 묶음째 부지의 빈자리에 놓는다.
        const x0 = Math.min(...list.map(x => x.x));
        const z0 = Math.min(...list.map(x => x.z));
        const w = Math.max(...list.map(x => x.x + x.cols)) - x0;
        const d = Math.max(...list.map(x => x.z + x.rows)) - z0;
        const spot = placeNew(w, d, '건물');
        if (!spot) return null;
        for (const x of list) {
            x.x += spot.x - x0;
            x.z += spot.z - z0;
            l.buildings.push(x);
        }
        const main = list.find(x => x.kind !== 'connector') || list[0];
        return { building: main, floor: main.baseFloor, msg: `새 건물 ${list.length}개(${list.map(x => x.name).join(', ')})를 더했습니다.` };
    }

    /** 커진 건물이 부지 밖으로 나가면 부지를 동·남쪽으로 넓혀 담는다. */
    function fitIntoSite(b) {
        const l = state.layout;
        if (b.x + b.cols > l.site.cols) growSite('e', b.x + b.cols - l.site.cols + 2);
        if (b.z + b.rows > l.site.rows) growSite('s', b.z + b.rows - l.site.rows + 2);
        b.cols = Math.min(b.cols, l.site.cols, 80);
        b.rows = Math.min(b.rows, l.site.rows, 80);
        b.x = clamp(b.x, 0, l.site.cols - b.cols);
        b.z = clamp(b.z, 0, l.site.rows - b.rows);
    }

    function closeImport() {
        if (importState.busy) return;
        if (importState.result) {
            // 적용하지 않아도 서버에는 새 도면이 보관되었으므로 밑그림 정보는 새것으로 둔다.
            importState.result = null;
            importState.files = [];
            renderTabs();
        }
        importDlg.close();
    }

    async function deleteFloorplan() {
        if (!window.confirm('보관한 평면도 파일을 지울까요?\n배치도는 그대로 두고 밑그림만 사라집니다.')) return;
        try {
            const res = await fetch(floorplanUrl, { method: 'DELETE', headers: { 'X-Requested-With': 'XMLHttpRequest' } });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data.status !== 'success') throw new Error(data.message || '평면도를 지우지 못했습니다.');
            state.underlay.data = null;
            planImages.clear();
            renderUnderlayBar();
            drawCanvas();
            flash('평면도를 지웠습니다.');
        } catch (err) {
            flash(err.message || '평면도를 지우지 못했습니다.', true);
        }
    }

    if (importDlg) {
        const drop = importDlg.querySelector('[data-ed-import-drop]');
        importDlg.querySelector('[data-ed-import-files]').addEventListener('change', (e) => {
            addImportFiles(e.target.files);
            e.target.value = '';
        });
        drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('is-over'); });
        drop.addEventListener('dragleave', () => drop.classList.remove('is-over'));
        drop.addEventListener('drop', (e) => {
            e.preventDefault();
            drop.classList.remove('is-over');
            addImportFiles(e.dataTransfer?.files);
        });
        importDlg.addEventListener('change', (e) => {
            if (e.target.matches('[data-ed-import-target]')) {
                importState.target = e.target.value;
                renderImportTarget();
                renderImportList();
                return;
            }
            const bi = e.target.dataset.edImportBuilding;
            if (bi !== undefined && importState.files[bi]) {
                importState.files[bi].building = e.target.value;
                return;
            }
            const i = e.target.dataset.edImportFloor;
            if (i !== undefined && importState.files[i]) importState.files[i].floor = Number(e.target.value) || 0;
        });
        importDlg.addEventListener('click', (e) => {
            const btn = e.target.closest('button');
            if (!btn) return;
            const d = btn.dataset;
            if (d.edImportRemove !== undefined) { importState.files.splice(Number(d.edImportRemove), 1); renderImportList(); }
            else if ('edImportRun' in d) runImport();
            else if ('edImportApply' in d) applyImport();
            else if ('edImportClose' in d) closeImport();
        });
        importDlg.addEventListener('cancel', (e) => {
            e.preventDefault();
            closeImport();
        });
    }

    document.addEventListener('keydown', (e) => {
        if (importDlg?.open) return;
        const typing = e.target.matches('input, textarea, select');
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z' && !typing) { e.preventDefault(); e.shiftKey ? redo() : undo(); }
        else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); save(); }
        else if (e.key.toLowerCase() === 'r' && !typing && !e.ctrlKey && !e.metaKey && state.sel?.kind === 'room') {
            rotateStairs(selected());
        } else if ((e.key === 'Delete' || e.key === 'Backspace') && !typing && state.sel) {
            e.preventDefault();
            form.querySelector('[data-ed-delete]')?.click();
        }
    });

    window.addEventListener('beforeunload', (e) => {
        if (!state.dirty) return;
        e.preventDefault();
        e.returnValue = '';
    });

    // ------------------------------------------------------------ 완성도 점검
    // 오류(error): 길안내가 끊기는 문제 · 확인(warn): AI 초안에서 자주 틀리는 곳 · 참고(info): 채우면 좋은 정보
    // 항목마다 go(이동할 곳)를 두어 누르면 해당 건물·층·교실을 바로 연다.
    const ISSUE_LEVELS = {
        error: { label: '오류', icon: 'fa-circle-xmark' },
        warn: { label: '확인', icon: 'fa-triangle-exclamation' },
        info: { label: '참고', icon: 'fa-circle-info' },
    };
    const NAMED_TYPES = new Set(['general', 'afterschool', 'special', 'care', 'office', 'etc']);

    const rectsOverlap = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.z < b.z + b.d && b.z < a.z + a.d;
    const roomGo = (b, r) => ({ tab: 'floor', buildingId: b.id, floor: r.floor, sel: { kind: 'room', id: r.id } });
    const roomLabel = (b, r) => `${b.name} ${r.floor}층 ${r.no || r.name || ROOM_TYPES[r.type]?.label || '교실'}`;

    function collectIssues() {
        const l = state.layout;
        const out = [];
        // blocks: 이 문제 때문에 통째로 못 가는 층('건물id:층') — 그 층 교실마다 '길이 끊김'을 또 적지 않는다.
        // fix: { label, run } — 항목 옆 [고치기] 단추로 바로 고칠 수 있을 때.
        const add = (level, text, go = null, blocks = null, fix = null) => out.push({ level, text, go, blocks, fix });
        if (!l.buildings.length) {
            add('error', '건물이 없습니다. [평면도로 자동 그리기]로 시작하거나 [건물 추가]로 놓아 주세요.', { tab: 'site' });
            return out;
        }
        if (!l.gates.length) add('error', '교문이 없어 학교 밖에서 출발하는 길안내를 할 수 없습니다. [출입문 추가]로 교문을 놓아 주세요.', { tab: 'site' });

        l.buildings.forEach((b, i) => {
            const clash = l.buildings.slice(i + 1).find(o =>
                rectsOverlap({ x: b.x, z: b.z, w: b.cols, d: b.rows }, { x: o.x, z: o.z, w: o.cols, d: o.rows })
                && !(b.kind === 'connector' || o.kind === 'connector'));
            if (clash) add('error', `${b.name}과(와) ${clash.name}이(가) 부지에서 겹쳐 있습니다.`, { tab: 'site', sel: { kind: 'building', id: b.id } });
        });

        for (const b of l.buildings) {
            for (const door of canHaveDoors(b) ? b.doors || [] : []) {
                if (doorBlocked(b, door)) {
                    add('error', `${b.name} '${door.label || '출입구'}': 문 안쪽이 교실로 막혀 드나들 수 없습니다.`,
                        { tab: 'floor', buildingId: b.id, floor: 1, sel: { kind: 'door', id: door.id } });
                }
            }
            if (b.kind === 'connector') continue;
            const vertical = r => r.type === 'stairs' || r.type === 'elevator';
            if (b.floors > 1 && !b.rooms.some(vertical)) {
                add('error', `${b.name}: 계단·엘리베이터가 없어 위층으로 안내할 수 없습니다.`, { tab: 'floor', buildingId: b.id, floor: b.baseFloor },
                    Array.from({ length: b.floors - 1 }, (_, i) => `${b.id}:${b.baseFloor + 1 + i}`));
            }
            for (let f = b.baseFloor + 1; f <= topFloor(b); f += 1) {
                const up = b.rooms.filter(r => r.floor === f && vertical(r));
                const down = b.rooms.filter(r => r.floor === f - 1 && vertical(r));
                if (up.length && down.length && !up.some(u => down.some(dn => dn.type === u.type && rectsOverlap(u, dn)))) {
                    add('error', `${b.name}: ${f - 1}층과 ${f}층의 계단 자리가 겹치지 않아 층이 이어지지 않습니다.`,
                        { tab: 'floor', buildingId: b.id, floor: f, sel: { kind: 'room', id: up[0].id } }, `${b.id}:${f}`);
                } else if (!up.length && b.rooms.some(vertical) && b.rooms.some(r => r.floor === f)) {
                    add('error', `${b.name} ${f}층: 계단·엘리베이터가 없어 이 층으로 올라올 수 없습니다.`, { tab: 'floor', buildingId: b.id, floor: f }, `${b.id}:${f}`);
                }
            }
            // 엘리베이터는 위·아래층의 같은 자리 엘리베이터와만 이어진다. 한 층이라도 빠지면
            // 계단으로는 갈 수 있어도 [엘리베이터 이용] 길안내가 그 층에서 끊긴다.
            const elevators = b.rooms.filter(r => r.type === 'elevator');
            const brokenAt = new Set();
            for (const r of elevators) {
                for (const g of [r.floor - 1, r.floor + 1]) {
                    if (!hasFloor(b, g) || brokenAt.has(Math.min(g, r.floor))) continue;
                    if (elevators.some(o => o.floor === g && rectsOverlap(o, r))) continue;
                    if (!b.rooms.some(x => x.floor === g)) continue;      // 빈 층은 '교실이 없습니다'로 알린다
                    brokenAt.add(Math.min(g, r.floor));
                    add('warn', `${b.name} ${g}층에 ${r.floor}층 엘리베이터와 같은 자리의 엘리베이터가 없어, 엘리베이터 길안내가 ${r.floor}층에서 끊깁니다.`,
                        { tab: 'floor', buildingId: b.id, floor: g }, null,
                        { label: `${g}층에 넣기`, run: () => extendElevator(b.id, r.id, g) });
                }
            }
            if (b.baseFloor === 1 && !(b.doors || []).length && !b.rooms.some(r => r.type === 'entrance')) {
                add('warn', `${b.name}: 건물출입문이 없습니다. 지금은 1층 가장자리 어디로든 들어가는 것으로 안내합니다.`,
                    { tab: 'floor', buildingId: b.id, floor: 1 });
            }
            for (let f = b.baseFloor; f <= topFloor(b); f += 1) {
                if (!b.rooms.some(r => r.floor === f)) add('warn', `${b.name} ${f}층에 교실이 하나도 없습니다.`, { tab: 'floor', buildingId: b.id, floor: f });
            }
            const numbers = new Map();
            for (const r of b.rooms) {
                if (NAMED_TYPES.has(r.type) && !r.no && !r.name) {
                    add('warn', `${b.name} ${r.floor}층: 이름·호수가 없는 ${ROOM_TYPES[r.type].label}이 있습니다.`, roomGo(b, r));
                }
                const m = /^(\d{1,2})(\d{2})$/.exec(r.no || '');
                if (m && Number(m[1]) !== r.floor) {
                    add('warn', `${roomLabel(b, r)}: 호수(${r.no})가 ${m[1]}층을 가리키는데 ${r.floor}층에 있습니다.`, roomGo(b, r));
                }
                if (r.no) {
                    const same = numbers.get(r.no);
                    if (same) add('warn', `${b.name}: 호수 ${r.no}이(가) 두 곳(${same.floor}층·${r.floor}층)에 있습니다.`, roomGo(b, r));
                    else numbers.set(r.no, r);
                }
                if (r.type === 'afterschool' && !(r.courses || []).length) {
                    add('info', `${roomLabel(b, r)}: 방과후 강좌 정보가 없습니다.`, roomGo(b, r));
                }
            }
        }

        if (l.gates.length) {
            // 계단이 없어 못 올라가는 층은 위에서 이미 알렸으니 그 층 교실은 다시 적지 않는다.
            const noAccess = new Set(out.flatMap(i => i.blocks || []));
            const cutByFloor = new Map();
            for (const { building: b, room: r, reason } of unreachableRooms(l)) {
                if (reason === 'nodoor') {
                    add('error', `${roomLabel(b, r)}: 복도에 닿지 않아 들어갈 문이 없습니다. 교실 옆에 빈 칸(복도)을 두세요.`, roomGo(b, r));
                } else if (!noAccess.has(`${b.id}:${r.floor}`)) {
                    const key = `${b.id}:${r.floor}`;
                    if (!cutByFloor.has(key)) cutByFloor.set(key, { b, rooms: [] });
                    cutByFloor.get(key).rooms.push(r);
                }
            }
            for (const { b, rooms } of cutByFloor.values()) {
                const names = rooms.slice(0, 4).map(r => r.no || r.name || ROOM_TYPES[r.type]?.label).join(', ');
                add('error', rooms.length === 1
                    ? `${roomLabel(b, rooms[0])}: 교문에서 가는 길이 끊겨 있습니다. 복도·계단·출입문이 이어지는지 확인하세요.`
                    : `${b.name} ${rooms[0].floor}층: 교문에서 갈 수 없는 교실 ${rooms.length}개 (${names}${rooms.length > 4 ? ' 외' : ''}). 복도·계단·출입문이 이어지는지 확인하세요.`,
                roomGo(b, rooms[0]));
            }
        }
        const order = { error: 0, warn: 1, info: 2 };
        return out.sort((a, b) => order[a.level] - order[b.level]);
    }

    let issues = [];
    let checkTimer = null;
    function scheduleCheck(delay = 450) {
        clearTimeout(checkTimer);
        checkTimer = setTimeout(renderIssues, delay);
    }

    function issueCounts() {
        return {
            error: issues.filter(i => i.level === 'error').length,
            warn: issues.filter(i => i.level === 'warn').length,
            info: issues.filter(i => i.level === 'info').length,
        };
    }

    function renderIssues() {
        issues = collectIssues();
        const counts = issueCounts();
        const badge = $('[data-ed-issue-badge]');
        if (badge) {
            badge.className = `cg-issue-badge ${counts.error ? 'is-error' : counts.warn ? 'is-warn' : 'is-ok'}`;
            badge.innerHTML = counts.error || counts.warn
                ? `<i class="fa-solid ${counts.error ? 'fa-circle-xmark' : 'fa-triangle-exclamation'}"></i> 오류 ${counts.error} · 확인 ${counts.warn}`
                : '<i class="fa-solid fa-circle-check"></i> 점검 통과';
        }
        const box = $('[data-ed-issues]');
        if (!box) return;
        const filter = box.dataset.filter || 'all';
        const shown = issues.filter(i => filter === 'all' || i.level === filter);
        const chips = Object.entries(ISSUE_LEVELS).map(([key, lv]) =>
            `<button type="button" class="cg-issue-chip is-${key}${filter === key ? ' is-active' : ''}" data-issue-filter="${key}">${lv.label} ${counts[key]}</button>`).join('');
        const LIMIT = 60;
        box.innerHTML = `
            <div class="cg-issue-chips">
                <button type="button" class="cg-issue-chip${filter === 'all' ? ' is-active' : ''}" data-issue-filter="all">전체 ${issues.length}</button>${chips}
            </div>
            ${shown.length ? `<ul class="cg-issue-list">${shown.slice(0, LIMIT).map((it) => `
                <li${it.fix ? ' class="has-fix"' : ''}><button type="button" class="cg-issue is-${it.level}" data-issue="${issues.indexOf(it)}"${it.go ? '' : ' disabled'}>
                    <i class="fa-solid ${ISSUE_LEVELS[it.level].icon}"></i><span>${esc(it.text)}</span>${it.go ? '<i class="fa-solid fa-chevron-right cg-issue-go"></i>' : ''}
                </button>${it.fix ? `<button type="button" class="cg-issue-fix" data-issue-fix="${issues.indexOf(it)}"><i class="fa-solid fa-wrench"></i> ${esc(it.fix.label)}</button>` : ''}</li>`).join('')}</ul>${shown.length > LIMIT ? `<p class="cg-muted">외 ${shown.length - LIMIT}개 — 위 항목을 고치면 다음 항목이 보입니다.</p>` : ''}`
            : `<p class="cg-ok"><i class="fa-solid fa-circle-check"></i> ${filter === 'all' ? '모든 점검을 통과했습니다. 교문에서 모든 교실까지 길안내가 됩니다.' : '해당 항목이 없습니다.'}</p>`}`;
    }

    /** 점검 항목이 가리키는 건물·층·교실로 이동한다. */
    function goTo(go) {
        if (!go) return;
        state.tab = go.tab || state.tab;
        if (go.buildingId) state.buildingId = go.buildingId;
        if (go.floor) state.floor = go.floor;
        state.sel = go.sel || null;
        renderForm();
        renderTabs();
        canvas.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }

    $('[data-ed-issues]')?.addEventListener('click', (e) => {
        const btn = e.target.closest('button');
        if (!btn) return;
        if (btn.dataset.issueFilter) {
            e.currentTarget.dataset.filter = btn.dataset.issueFilter;
            renderIssues();
        } else if (btn.dataset.issueFix !== undefined) {
            issues[Number(btn.dataset.issueFix)]?.fix?.run();
        } else if (btn.dataset.issue !== undefined) {
            goTo(issues[Number(btn.dataset.issue)]?.go);
        }
    });

    /**
     * floor층의 src(엘리베이터)와 같은 자리에 엘리베이터를 넣는다(점검 목록의 [고치기]).
     * 그 자리를 계단이 넓게 차지하고 있으면(도면에서 계단실과 승강기를 한 칸으로 읽은 경우)
     * src 층 계단 크기로 줄여 자리를 만든다. 교실 등 다른 실이 있으면 그 층을 열어 직접 고치게 한다.
     */
    function extendElevator(buildingId, srcId, floor) {
        const b = state.layout.buildings.find(x => x.id === buildingId);
        const src = b?.rooms.find(r => r.id === srcId);
        if (!src || !hasFloor(b, floor)) return;
        const within = (a, o) => a.x >= o.x && a.z >= o.z && a.x + a.w <= o.x + o.w && a.z + a.d <= o.z + o.d;
        const shrink = [];
        for (const r of b.rooms.filter(x => x.floor === floor && rectsOverlap(x, src))) {
            const fit = r.type === 'stairs' && b.rooms.find(u =>
                u.floor === src.floor && u.type === 'stairs' && within(u, r) && !rectsOverlap(u, src));
            if (!fit) {
                goTo({ tab: 'floor', buildingId: b.id, floor, sel: { kind: 'room', id: r.id } });
                flash(`${floor}층 그 자리에 '${roomTitle(r)}'이(가) 있어 엘리베이터를 넣지 못했습니다. 크기를 줄이거나 옮긴 뒤 엘리베이터를 그려 주세요.`, true);
                return;
            }
            shrink.push([r, fit]);
        }
        snapshot();
        for (const [r, fit] of shrink) Object.assign(r, { x: fit.x, z: fit.z, w: fit.w, d: fit.d, dir: fit.dir, shape: fit.shape });
        const room = { ...clone(src), id: newId('r'), floor, courses: [], note: '' };
        b.rooms.push(room);
        changed();
        goTo({ tab: 'floor', buildingId: b.id, floor, sel: { kind: 'room', id: room.id } });
        flash(shrink.length
            ? `${floor}층에 엘리베이터를 넣고, 겹치던 계단을 ${src.floor}층 계단 크기로 줄였습니다.`
            : `${floor}층에 엘리베이터를 넣었습니다.`);
    }

    function renderStart() {
        const start = !state.layout.buildings.length && !state.manualStart;
        root.querySelector('[data-ed-editor-card]')?.classList.toggle('is-starting', start);
        const start$ = $('[data-ed-start]');
        if (start$) start$.hidden = !start;
    }

    // ------------------------------------------------------------ 저장
    let saving = false;
    async function save() {
        if (saving) return;
        if (!state.layout.buildings.length) { flash('건물을 1개 이상 추가해 주세요.'); return; }
        saving = true;
        const btn = $('[data-ed-save]');
        btn?.setAttribute('disabled', '');
        try {
            const res = await fetch(saveUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                body: JSON.stringify({ layout: state.layout }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data.status !== 'success') {
                flash(data.message || '저장하지 못했습니다.', true);
                return;
            }
            const selId = state.sel?.id;
            state.layout = normalizeClient(data.layout);
            if (selId && !selected()) state.sel = null;
            state.dirty = false;
            $('[data-ed-dirty]')?.classList.remove('is-on');
            const at = $('[data-ed-saved-at]');
            if (at) at.textContent = `${data.updated_at} 저장됨`;
            changed({ form: true });
            state.dirty = false;
            $('[data-ed-dirty]')?.classList.remove('is-on');
            renderTabs();
            renderIssues();
            const counts = issueCounts();
            flash(counts.error ? `저장했습니다. 다만 점검 오류가 ${counts.error}개 남아 있어 일부 교실은 길안내가 되지 않습니다.` : '저장했습니다.');
        } catch (err) {
            flash('네트워크 오류로 저장하지 못했습니다.', true);
        } finally {
            saving = false;
            btn?.removeAttribute('disabled');
        }
    }

    function flash(message, error = false) {
        let box = root.querySelector('.cg-toast');
        if (!box) {
            box = document.createElement('div');
            box.className = 'cg-toast';
            box.setAttribute('role', 'status');
            root.appendChild(box);
        }
        box.textContent = message;
        box.classList.toggle('is-error', error);
        box.classList.add('is-on');
        clearTimeout(box._t);
        box._t = setTimeout(() => box.classList.remove('is-on'), 2800);
    }

    new ResizeObserver(() => resizeCanvas()).observe(canvas.parentElement);
    renderTabs();
    renderForm();
    renderIssues();
    updateUndo();
    // 배치도가 없는 학교는 평면도 올리기로 시작한다(안내도 화면의 [평면도로 자동 그리기]로 와도 같다).
    if (!state.layout.buildings.length || new URLSearchParams(window.location.search).get('start') === 'floorplan') openImport();
}
