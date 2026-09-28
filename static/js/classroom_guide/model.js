// 교실안내 배치도 공통 정의 (routes/classroom_guide.py 의 normalize_layout 과 짝을 이룬다)
//
// 좌표계: 학교 부지를 한 칸(cell)이 약 layout.cell 미터인 격자로 본다.
//   x → 동쪽(오른쪽), z → 남쪽(아래쪽). 건물은 부지 격자 위 (x, z)에 놓이고
//   교실은 건물 안쪽 격자 (x, z, w, d)로 놓인다.

export const ROOM_TYPES = {
    general:     { label: '일반교실',   color: '#dbeafe', edge: '#60a5fa', course: true },
    afterschool: { label: '방과후교실', color: '#fde68a', edge: '#f59e0b', course: true },
    special:     { label: '특별교실',   color: '#e9d5ff', edge: '#a855f7', course: true },
    care:        { label: '돌봄교실',   color: '#fbcfe8', edge: '#ec4899', course: true },
    office:      { label: '교무·행정실', color: '#fecaca', edge: '#ef4444', course: false },
    restroom:    { label: '화장실',     color: '#e2e8f0', edge: '#94a3b8', course: false },
    corridor:    { label: '복도',       color: '#f8fafc', edge: '#cbd5e1', course: false, walk: true },
    entrance:    { label: '현관(로비)', color: '#bbf7d0', edge: '#22c55e', course: false, walk: true },
    stairs:      { label: '계단',       color: '#fed7aa', edge: '#f97316', course: false, walk: true, vertical: true },
    elevator:    { label: '엘리베이터', color: '#c7d2fe', edge: '#6366f1', course: false, walk: true, vertical: true },
    etc:         { label: '기타',       color: '#e5e7eb', edge: '#9ca3af', course: true },
};

// 외벽 기본색: 우리나라 학교 건물처럼 따뜻한 살구·노랑과 파랑 계열(건물 정보의 '외벽 색'으로 바꿀 수 있다)
export const BUILDING_KINDS = {
    main:      { label: '본관',     color: '#efb27a' },
    annex:     { label: '후관',     color: '#f1cd82' },
    wing:      { label: '별관',     color: '#8db3e0' },
    special:   { label: '특별관',   color: '#e8a39b' },
    gym:       { label: '체육관',   color: '#9fcb96' },
    connector: { label: '연결통로', color: '#e2ddd3' },
    etc:       { label: '기타',     color: '#e2c9a6' },
};

export const LANDMARK_TYPES = {
    field:      { label: '운동장', color: '#d9b98a' },
    parking:    { label: '주차장', color: '#9ca3af' },
    garden:     { label: '화단·숲', color: '#86c58a' },
    playground: { label: '놀이터', color: '#f4c28a' },
    etc:        { label: '기타',   color: '#cbd5e1' },
};

// 출입문 구분. 교문·후문은 부지 담장 위에, 건물출입문은 건물 1층 외벽 위에 얇은 선으로 놓인다.
export const GATE_KINDS = {
    main: { label: '교문',       color: '#16a34a', icon: 'fa-school-flag' },
    back: { label: '후문',       color: '#0d9488', icon: 'fa-door-open' },
    door: { label: '건물출입문', color: '#2563eb', icon: 'fa-door-closed' },
};

// 방향(변). x → 동쪽, z → 남쪽.
export const SIDES = {
    n: { label: '위(북)',     wall: '북쪽',  dx: 0,  dz: -1, arrow: '↑' },
    e: { label: '오른쪽(동)', wall: '동쪽',  dx: 1,  dz: 0,  arrow: '→' },
    s: { label: '아래(남)',   wall: '남쪽',  dx: 0,  dz: 1,  arrow: '↓' },
    w: { label: '왼쪽(서)',   wall: '서쪽',  dx: -1, dz: 0,  arrow: '←' },
};

export const STAIR_SHAPES = {
    u:        { label: 'U자 (중간 참에서 되돌아 오름)' },
    straight: { label: '일자 (한 방향으로 오름)' },
};

export const FLOOR_HEIGHT = 1.25;   // 3D 화면에서 한 층의 높이(칸 단위)

const clampInt = (v, lo, hi) => Math.max(lo, Math.min(hi, Math.round(Number(v) || 0)));

// ------------------------------------------------------------------ 교문·후문
/** 교문·후문 위치를 담장 한 변(side)과 그 변을 따른 시작 칸(at)·폭(w)으로 맞추고 x, z(가운데 칸)를 채운다. */
export function fitGate(site, gate) {
    const horizontal = gate.side === 'n' || gate.side === 's';
    const len = horizontal ? site.cols : site.rows;
    gate.w = clampInt(gate.w || 3, 1, Math.min(12, len));
    gate.at = clampInt(gate.at, 0, len - gate.w);
    const mid = gate.at + Math.floor(gate.w / 2);
    gate.x = horizontal ? mid : (gate.side === 'w' ? 0 : site.cols - 1);
    gate.z = horizontal ? (gate.side === 'n' ? 0 : site.rows - 1) : mid;
    return gate;
}

/** 부지 안의 한 점(fx, fz)에서 가장 가까운 담장에 교문을 붙인다. */
export function snapGate(site, fx, fz, gate) {
    const dist = { n: fz, s: site.rows - fz, w: fx, e: site.cols - fx };
    gate.side = Object.keys(dist).reduce((a, b) => (dist[a] <= dist[b] ? a : b));
    const along = gate.side === 'n' || gate.side === 's' ? fx : fz;
    gate.at = Math.round(along - (gate.w || 3) / 2);
    return fitGate(site, gate);
}

/** 예전 자료(x, z 한 점만 있음)도 담장 위 구간으로 바꿔 돌려준다. 원본은 건드리지 않는다. */
export function gateSpan(gate, site) {
    const g = { ...gate };
    if (SIDES[g.side]) fitGate(site, g);
    else snapGate(site, (g.x || 0) + 0.5, (g.z || 0) + 0.5, g);
    const horizontal = g.side === 'n' || g.side === 's';
    const cells = Array.from({ length: g.w }, (_, i) => (horizontal ? { x: g.at + i, z: g.z } : { x: g.x, z: g.at + i }));
    // 담장 선 위의 가운데 점(길찾기 출발 표시 위치)
    const mid = g.at + g.w / 2;
    const anchor = horizontal
        ? { x: mid, z: g.side === 'n' ? 0.5 : site.rows - 0.5 }
        : { x: g.side === 'w' ? 0.5 : site.cols - 0.5, z: mid };
    return { ...g, kind: GATE_KINDS[g.kind] && g.kind !== 'door' ? g.kind : 'main', horizontal, cells, anchor };
}

// ------------------------------------------------------------------ 건물출입문
/** 건물출입문이 닿는 건물 안쪽 칸과 바로 바깥 칸(모두 부지 좌표). */
export function doorCells(building, door) {
    const side = SIDES[door.side] ? door.side : 's';
    const horizontal = side === 'n' || side === 's';
    const len = horizontal ? building.cols : building.rows;
    const w = clampInt(door.w || 2, 1, len);
    const at = clampInt(door.at, 0, len - w);
    const out = [];
    for (let i = 0; i < w; i += 1) {
        const lx = horizontal ? at + i : (side === 'w' ? 0 : building.cols - 1);
        const lz = horizontal ? (side === 'n' ? 0 : building.rows - 1) : at + i;
        const { dx, dz } = SIDES[side];
        out.push({ x: building.x + lx, z: building.z + lz, ox: building.x + lx + dx, oz: building.z + lz + dz, side });
    }
    return out;
}

/** 외벽 위 선분(부지 좌표): 문을 그릴 때 쓴다. */
export function doorSegment(building, door) {
    const cells = doorCells(building, door);
    const first = cells[0];
    const last = cells[cells.length - 1];
    const side = first.side;
    if (side === 'n' || side === 's') {
        const z = side === 'n' ? building.z : building.z + building.rows;
        return { side, x1: first.x, z1: z, x2: last.x + 1, z2: z };
    }
    const x = side === 'w' ? building.x : building.x + building.cols;
    return { side, x1: x, z1: first.z, x2: x, z2: last.z + 1 };
}

/** 건물 기준 좌표(lx, lz)에서 가장 가까운 외벽을 찾아 문을 붙인다. 돌려주는 값은 그 벽까지의 거리. */
export function snapDoor(building, lx, lz, door) {
    const { cols, rows } = building;
    const off = (v, len) => Math.max(0, -v, v - len);
    const dist = {
        n: Math.hypot(off(lx, cols), lz),
        s: Math.hypot(off(lx, cols), lz - rows),
        w: Math.hypot(lx, off(lz, rows)),
        e: Math.hypot(lx - cols, off(lz, rows)),
    };
    const side = Object.keys(dist).reduce((a, b) => (dist[a] <= dist[b] ? a : b));
    const horizontal = side === 'n' || side === 's';
    const len = horizontal ? cols : rows;
    door.side = side;
    door.w = clampInt(door.w || 2, 1, Math.min(8, len));
    door.at = clampInt((horizontal ? lx : lz) - door.w / 2, 0, len - door.w);
    return dist[side];
}

export function fitDoor(building, door) {
    const side = SIDES[door.side] ? door.side : 's';
    const len = side === 'n' || side === 's' ? building.cols : building.rows;
    door.side = side;
    door.w = clampInt(door.w || 2, 1, Math.min(8, len));
    door.at = clampInt(door.at, 0, len - door.w);
    return door;
}

/** 건물출입문을 둘 수 있는 건물: 1층부터 시작하는 건물(연결통로 포함). */
export function canHaveDoors(building) {
    return building.baseFloor === 1;
}

// ------------------------------------------------------------------ 계단
/**
 * 계단이 오르는 방향. 정하지 않았으면(평면도로 자동 생성한 계단 등) building 을 보고
 * 복도와 가장 넓게 맞닿은 변에서 출발해 반대쪽(보통 외벽)으로 오르게 한다.
 * building 을 모르거나 복도에 닿지 않으면 긴 변 방향.
 */
export function stairDir(room, building = null) {
    if (SIDES[room.dir]) return room.dir;
    return (building && autoStairDir(building, room)) || (room.w >= room.d ? 'e' : 's');
}

const OPPOSITE = { n: 's', s: 'n', e: 'w', w: 'e' };

function autoStairDir(building, room) {
    const others = (building.rooms || []).filter(r => r !== room && r.floor === room.floor);
    // 복도 칸: 건물 안이면서 교실이 없거나 복도·현관인 칸(계단·승강기 칸은 들어서는 곳이 아니므로 뺀다)
    const hall = (lx, lz) => {
        if (lx < 0 || lz < 0 || lx >= building.cols || lz >= building.rows) return false;
        const r = others.find(o => lx >= o.x && lx < o.x + o.w && lz >= o.z && lz < o.z + o.d);
        return !r || r.type === 'corridor' || r.type === 'entrance';
    };
    const count = (len, at) => Array.from({ length: len }, (_, i) => at(i)).filter(([x, z]) => hall(x, z)).length;
    const open = {
        n: count(room.w, i => [room.x + i, room.z - 1]),
        s: count(room.w, i => [room.x + i, room.z + room.d]),
        w: count(room.d, i => [room.x - 1, room.z + i]),
        e: count(room.d, i => [room.x + room.w, room.z + i]),
    };
    // 복도 쪽으로 열리고 반대쪽은 막힌 변일수록 들어서는 곳(열린 비율로 비교).
    // 비슷하면 긴 쪽으로 오르도록 짧은 변에서 들어선다(계단실은 보통 길이 방향으로 오른다).
    const len = side => (side === 'n' || side === 's' ? room.w : room.d);
    const frac = side => open[side] / len(side);
    const score = side => frac(side) - frac(OPPOSITE[side]) + 0.05 * len(side === 'n' || side === 's' ? 'e' : 'n');
    const entry = Object.keys(open).filter(side => open[side] > 0).sort((a, b) => score(b) - score(a))[0];
    return entry ? OPPOSITE[entry] : null;
}

export function stairShape(room, building = null) {
    if (STAIR_SHAPES[room.shape]) return room.shape;
    const dir = stairDir(room, building);
    const across = dir === 'n' || dir === 's' ? room.w : room.d;
    return across >= 2 ? 'u' : 'straight';
}

/**
 * 계단 평면 모양(교실 기준 좌표). flights: 오르는 구간들, landing: 중간 참, path: 오르는 방향 화살표 선.
 * 높이 h 는 0(이 층 바닥) ~ 1(다음 층 방향 끝)로 나타낸다. building 을 주면 방향을 정하지 않은 계단은 복도에서 출발한다.
 */
export function stairPlan(room, building = null) {
    const dir = stairDir(room, building);
    const shape = stairShape(room, building);
    const vertical = dir === 'n' || dir === 's';
    const L = vertical ? room.d : room.w;     // 오르는 방향 길이
    const W = vertical ? room.w : room.d;     // 폭
    // (u: 오르는 방향 거리, v: 진행 방향 왼쪽→오른쪽) → 교실 기준 (x, z)
    const toXZ = (u, v) => {
        if (dir === 'e') return { x: u, z: v };
        if (dir === 'w') return { x: L - u, z: W - v };
        if (dir === 's') return { x: W - v, z: u };
        return { x: v, z: L - u };
    };
    const rect = (u0, u1, v0, v1) => {
        const a = toXZ(u0, v0);
        const b = toXZ(u1, v1);
        return { x: Math.min(a.x, b.x), z: Math.min(a.z, b.z), w: Math.abs(a.x - b.x), d: Math.abs(a.z - b.z) };
    };
    const flight = (u0, u1, v0, v1, h0, h1) => {
        const len = Math.abs(u1 - u0);
        const count = Math.max(4, Math.min(14, Math.round(len * 3)));
        const steps = Array.from({ length: count }, (_, i) => {
            const a = u0 + (u1 - u0) * i / count;
            const b = u0 + (u1 - u0) * (i + 1) / count;
            return { ...rect(Math.min(a, b), Math.max(a, b), v0, v1), h: h0 + (h1 - h0) * (i + 1) / count };
        });
        return { steps, from: toXZ(u0, (v0 + v1) / 2), to: toXZ(u1, (v0 + v1) / 2), h0, h1 };
    };
    const inset = Math.min(0.3, L * 0.08);
    if (shape === 'straight' || W < 1.2) {
        const f = flight(0, L, 0, W, 0, 1);
        return {
            dir, shape: 'straight', flights: [f], landing: null, divider: null,
            path: [{ ...toXZ(inset, W / 2), h: 0 }, { ...toXZ(L - inset, W / 2), h: 1 }],
        };
    }
    const landing = Math.min(1.2, Math.max(0.6, L * 0.28));
    const mid = W / 2;
    const f1 = flight(0, L - landing, mid, W, 0, 0.5);          // 오른쪽 줄: 올라가서
    const f2 = flight(L - landing, 0, 0, mid, 0.5, 1);          // 참에서 되돌아 왼쪽 줄로
    return {
        dir, shape: 'u', flights: [f1, f2],
        landing: { ...rect(L - landing, L, 0, W), h: 0.5 },
        divider: { a: toXZ(0, mid), b: toXZ(L - landing, mid) },
        path: [
            { ...toXZ(inset, mid + W / 4), h: 0 },
            { ...toXZ(L - landing / 2, mid + W / 4), h: 0.5 },
            { ...toXZ(L - landing / 2, W / 4), h: 0.5 },
            { ...toXZ(inset, W / 4), h: 1 },
        ],
    };
}

/**
 * 바깥 시설 이름. 종류를 바꿔도 이름이 예전 종류 기본값(예: 주차장인데 '운동장')으로
 * 남아 있던 자료는 지금 종류의 이름으로 보여준다.
 */
export function landmarkLabel(lm) {
    const own = (LANDMARK_TYPES[lm.type] || LANDMARK_TYPES.etc).label;
    const label = lm.label || '';
    const stale = Object.entries(LANDMARK_TYPES).some(([key, t]) => key !== lm.type && t.label === label);
    return !label || stale ? own : label;
}

export function roomTypeOf(room) {
    return ROOM_TYPES[room && room.type] || ROOM_TYPES.etc;
}

export function buildingColor(building) {
    return building.color || (BUILDING_KINDS[building.kind] || BUILDING_KINDS.etc).color;
}

export function topFloor(building) {
    return building.baseFloor + building.floors - 1;
}

export function hasFloor(building, floor) {
    return floor >= building.baseFloor && floor <= topFloor(building);
}

export function maxFloor(layout) {
    return (layout.buildings || []).reduce((max, b) => Math.max(max, topFloor(b)), 1);
}

/** 화면에 보여줄 교실 이름: "음악실 (201호)" */
export function roomTitle(room) {
    const no = room.no ? `${room.no}${/^\d+$/.test(room.no) ? '호' : ''}` : '';
    if (room.name && no) return `${room.name} (${no})`;
    return room.name || no || roomTypeOf(room).label;
}

export function roomShortLabel(room) {
    return room.no || room.name || '';
}

/** 교실을 건물·층 정보와 함께 평평한 목록으로 돌려준다. */
export function listRooms(layout) {
    const result = [];
    for (const building of layout.buildings || []) {
        for (const room of building.rooms || []) {
            result.push({ room, building });
        }
    }
    return result;
}

export function findRoom(layout, roomId) {
    return listRooms(layout).find(item => item.room.id === roomId) || null;
}

/** 교실명·호수·강좌명·강사명으로 찾는다. 길찾기 도착지 검색에 쓴다. */
export function searchRooms(layout, keyword) {
    const q = String(keyword || '').replace(/\s+/g, '').toLowerCase();
    const items = listRooms(layout).filter(({ room }) => !ROOM_TYPES[room.type]?.walk);
    if (!q) return items;
    const scored = [];
    for (const item of items) {
        const { room, building } = item;
        const fields = [
            [room.name, 5], [room.no, 5], [`${building.name}${room.floor}층`, 1],
            ...room.courses.flatMap(c => [[c.name, 6], [c.teacher, 3], [c.grade, 1]]),
        ];
        let score = 0;
        for (const [text, weight] of fields) {
            const t = String(text || '').replace(/\s+/g, '').toLowerCase();
            if (!t) continue;
            if (t === q) score = Math.max(score, weight * 3);
            else if (t.startsWith(q)) score = Math.max(score, weight * 2);
            else if (t.includes(q)) score = Math.max(score, weight);
        }
        if (score) scored.push({ ...item, score });
    }
    return scored.sort((a, b) => b.score - a.score);
}

let idSeq = 0;
export function newId(prefix) {
    idSeq += 1;
    return `${prefix}${Date.now().toString(36)}${idSeq.toString(36)}${Math.random().toString(36).slice(2, 5)}`;
}

export function makeGate(site, kind, label, side, at, w = 3) {
    return fitGate(site, { id: newId('g'), kind, label, side, at, w });
}

export function makeDoor(label, side, at, w = 2) {
    return { id: newId('d'), label, side, at, w };
}

export function emptyLayout() {
    return {
        version: 1, cell: 3, north: 0,
        site: { cols: 64, rows: 44 },
        gates: [makeGate({ cols: 64, rows: 44 }, 'main', '교문', 's', 30, 4)],
        landmarks: [],
        buildings: [],
    };
}
