// 교실안내 길찾기
//
// 학교 부지를 (층, x, z) 격자로 보고 최단 경로를 찾는다.
//  - 건물 밖 1층 칸은 모두 걸을 수 있는 바깥 길이다.
//  - 건물 안에서는 복도(교실이 없는 빈 칸 포함)·현관·계단·엘리베이터만 걸을 수 있다.
//  - 바깥 ↔ 건물 안은 건물출입문(외벽 위에 놓은 문)으로만, 문이 놓인 벽 방향으로 드나든다.
//    출입문이 없는 예전 배치는 현관(로비) 칸으로, 현관도 없으면 1층 가장자리 복도 어디로든 드나든다.
//  - 계단·엘리베이터 칸은 바로 위·아래층의 같은 종류 칸과 이어진다.
//  - 서로 맞닿은 두 건물의 복도(연결통로 포함)는 그대로 이어진다.
import { ROOM_TYPES, doorCells, gateSpan, roomTitle } from './model.js';

const STAIRS_COST = 6;       // 한 층 오르내리는 비용(칸 단위)
const TURN_COST = 0.35;      // 건물 안에서 꺾을 때 더하는 비용(지그재그 대신 곧게 가도록)
const STAIRWELL_WALK = 6;    // 같은 층에서 계단·승강기 칸을 가로질러 걷는 한 칸 비용(지름길로 쓰지 않게)
const ELEVATOR_COST = 8;     // 기다리는 시간을 포함한 한 층 비용
const WALK_SPEED = 1.1;      // m/s — 아이와 함께 걷는 속도
const SECONDS_PER_FLOOR = { stairs: 15, elevator: 25 };

// ------------------------------------------------------------------ 격자
export function buildGrid(layout) {
    const cols = layout.site.cols;
    const rows = layout.site.rows;
    const floors = Math.max(1, ...layout.buildings.map(b => b.baseFloor + b.floors - 1));
    const size = cols * rows;
    const indoor = new Map();          // index → { building, room, kind, walk, entry }
    const footprintAtGround = new Uint8Array(size);

    const index = (f, x, z) => (f - 1) * size + z * cols + x;

    for (const building of layout.buildings) {
        const connector = building.kind === 'connector';
        const doors = building.baseFloor === 1 ? (building.doors || []) : [];
        const hasDoors = doors.length > 0;
        const hasEntrance = building.rooms.some(r => r.type === 'entrance');
        for (let f = building.baseFloor; f < building.baseFloor + building.floors; f += 1) {
            for (let lz = 0; lz < building.rows; lz += 1) {
                for (let lx = 0; lx < building.cols; lx += 1) {
                    const gx = building.x + lx;
                    const gz = building.z + lz;
                    if (gx >= cols || gz >= rows) continue;
                    const edge = lx === 0 || lz === 0 || lx === building.cols - 1 || lz === building.rows - 1;
                    indoor.set(index(f, gx, gz), {
                        building, room: null, kind: 'corridor', walk: true,
                        // 현관을 그리지 않은 건물은 1층 가장자리 복도로 드나든다.
                        entry: !connector && !hasDoors && !hasEntrance && f === 1 && edge,
                    });
                    if (f === 1) footprintAtGround[gz * cols + gx] = 1;
                }
            }
            if (connector) continue;
            for (const room of building.rooms) {
                if (room.floor !== f) continue;
                const type = ROOM_TYPES[room.type] || ROOM_TYPES.etc;
                for (let lz = room.z; lz < room.z + room.d; lz += 1) {
                    for (let lx = room.x; lx < room.x + room.w; lx += 1) {
                        const gx = building.x + lx;
                        const gz = building.z + lz;
                        if (gx >= cols || gz >= rows) continue;
                        indoor.set(index(f, gx, gz), {
                            building, room, kind: room.type, walk: !!type.walk,
                            entry: room.type === 'entrance' && !hasDoors,
                        });
                    }
                }
            }
        }
        // 건물출입문: 문 안쪽 칸에 '어느 벽 쪽으로 드나드는지'(n/e/s/w)를 적어 둔다.
        for (const door of doors) {
            for (const c of doorCells(building, door)) {
                if (c.x < 0 || c.z < 0 || c.x >= cols || c.z >= rows) continue;
                const idx = index(1, c.x, c.z);
                const rec = indoor.get(idx);
                if (!rec) continue;
                const sides = typeof rec.entry === 'string' ? rec.entry : '';
                indoor.set(idx, { ...rec, entry: sides + c.side, door });
            }
        }
    }

    function cell(f, x, z) {
        if (x < 0 || z < 0 || x >= cols || z >= rows || f < 1 || f > floors) return null;
        const inside = indoor.get(index(f, x, z));
        if (inside) return inside;
        if (f === 1 && !footprintAtGround[z * cols + x]) return OUTDOOR;
        return null;
    }

    return { cols, rows, floors, size, index, cell, layout };
}

const OUTDOOR = Object.freeze({ building: null, room: null, kind: 'outdoor', walk: true, outdoor: true });

function decode(grid, idx) {
    const f = Math.floor(idx / grid.size) + 1;
    const rest = idx % grid.size;
    return { f, x: rest % grid.cols, z: Math.floor(rest / grid.cols) };
}

/** a → b 한 칸 이동(dx, dz)이 가능한가. 바깥 ↔ 건물 안은 출입구로만 드나든다. */
function canStep(a, b, dx, dz) {
    if (!a || !b || !a.walk || !b.walk) return false;
    const aOut = !!a.outdoor;
    const bOut = !!b.outdoor;
    if (aOut === bOut) return true;
    const inside = aOut ? b : a;
    if (!inside.entry) return false;
    if (inside.entry === true) return true;
    if (dx && dz) return false;
    // 건물 안 칸에서 바깥을 바라보는 방향이 문이 놓인 벽과 같아야 한다.
    const ox = aOut ? -dx : dx;
    const oz = aOut ? -dz : dz;
    const side = ox > 0 ? 'e' : ox < 0 ? 'w' : oz > 0 ? 's' : 'n';
    return inside.entry.includes(side);
}

// ------------------------------------------------------------------ 힙
class MinHeap {
    constructor() { this.items = []; }
    get size() { return this.items.length; }
    push(node, cost) {
        const items = this.items;
        items.push([cost, node]);
        let i = items.length - 1;
        while (i > 0) {
            const p = (i - 1) >> 1;
            if (items[p][0] <= items[i][0]) break;
            [items[p], items[i]] = [items[i], items[p]];
            i = p;
        }
    }
    pop() {
        const items = this.items;
        const top = items[0];
        const last = items.pop();
        if (items.length) {
            items[0] = last;
            let i = 0;
            for (;;) {
                const l = i * 2 + 1;
                const r = l + 1;
                let m = i;
                if (l < items.length && items[l][0] < items[m][0]) m = l;
                if (r < items.length && items[r][0] < items[m][0]) m = r;
                if (m === i) break;
                [items[m], items[i]] = [items[i], items[m]];
                i = m;
            }
        }
        return top;
    }
}

// ------------------------------------------------------------------ 출발·도착
/** 길찾기 출발지·도착지 후보(정문·현관·교실). */
export function endpointOptions(layout) {
    const options = layout.gates.map(g => ({ key: `gate:${g.id}`, label: g.label, group: '학교 출입문' }));
    for (const building of layout.buildings) {
        for (const room of building.rooms) {
            if (room.type === 'corridor' || room.type === 'stairs' || room.type === 'elevator') continue;
            options.push({
                key: `room:${room.id}`,
                label: `${building.name} ${room.floor}층 · ${roomTitle(room)}`,
                group: room.type === 'entrance' ? '건물 현관' : `${building.name}`,
            });
        }
    }
    return options;
}

function roomOf(layout, id) {
    for (const building of layout.buildings) {
        const room = building.rooms.find(r => r.id === id);
        if (room) return { building, room };
    }
    return null;
}

/**
 * 교실 문: 복도와 맞닿은 변 중 가장 길게 이어진 구간의 가운데 칸에 낸다.
 * 복도·현관이 없을 때만 계단 쪽으로 낸다(승강기 쪽으로는 내지 않는다).
 * 길찾기와 3D 화면이 같은 문을 쓰도록 여기 한 곳에서 정한다.
 * 결과: { side, start, end, center(변 위 위치), cell:{x,z}(문 앞 칸), point:{x,z}(벽 위 문 가운데) } 또는 null
 */
export function roomDoor(grid, building, room) {
    const sides = [
        { side: 'n', len: room.w, at: i => [room.x + i, room.z - 1] },
        { side: 's', len: room.w, at: i => [room.x + i, room.z + room.d] },
        { side: 'w', len: room.d, at: i => [room.x - 1, room.z + i] },
        { side: 'e', len: room.d, at: i => [room.x + room.w, room.z + i] },
    ];
    const inside = c => c && c.walk && !c.outdoor && c.kind !== 'elevator' && c.room !== room;
    const hallway = c => inside(c) && c.kind !== 'stairs';
    const pick = (ok) => {
        let best = null;
        for (const side of sides) {
            let run = null;
            for (let i = 0; i <= side.len; i += 1) {
                let open = false;
                if (i < side.len) {
                    const [lx, lz] = side.at(i);
                    open = ok(grid.cell(room.floor, building.x + lx, building.z + lz));
                }
                if (open && !run) run = { side, start: i, end: i };
                else if (open) run.end = i;
                if (!open && run) {
                    if (!best || run.end - run.start > best.end - best.start) best = run;
                    run = null;
                }
            }
        }
        return best;
    };
    const best = pick(hallway) || pick(inside);
    if (!best) return null;
    const k = Math.floor((best.start + best.end) / 2);
    const [lx, lz] = best.side.at(k);
    const bx = building.x + room.x;
    const bz = building.z + room.z;
    const point = {
        n: { x: bx + k + 0.5, z: bz },
        s: { x: bx + k + 0.5, z: bz + room.d },
        w: { x: bx, z: bz + k + 0.5 },
        e: { x: bx + room.w, z: bz + k + 0.5 },
    }[best.side.side];
    // 문 바로 안쪽 점: 문을 곧게 통과하도록 경로에 끼워 넣는다.
    const inset = Math.min(0.8, (best.side.side === 'n' || best.side.side === 's' ? room.d : room.w) / 2);
    const inward = { n: [0, 1], s: [0, -1], w: [1, 0], e: [-1, 0] }[best.side.side];
    return {
        side: best.side.side, start: best.start, end: best.end, center: k + 0.5,
        cell: { x: building.x + lx, z: building.z + lz }, point,
        inside: { x: point.x + inward[0] * inset, z: point.z + inward[1] * inset },
    };
}

/** 출발지/도착지를 격자 칸 목록으로 바꾼다. */
function resolveEndpoint(grid, key) {
    const [kind, id] = String(key || '').split(':');
    const layout = grid.layout;
    if (kind === 'gate') {
        const gate = layout.gates.find(g => g.id === id);
        if (!gate) return null;
        const span = gateSpan(gate, layout.site);
        const open = span.cells
            .filter(c => grid.cell(1, c.x, c.z)?.outdoor)
            .map(c => grid.index(1, c.x, c.z));
        const mid = span.cells[Math.floor(span.cells.length / 2)];
        const cells = open.length ? open : nearestOutdoor(grid, mid.x, mid.z);
        return cells && {
            kind, label: gate.label, cells, floor: 1,
            anchor: { f: 1, ...span.anchor },
        };
    }
    if (kind === 'room') {
        const found = roomOf(layout, id);
        if (!found) return null;
        const { building, room } = found;
        const type = ROOM_TYPES[room.type] || ROOM_TYPES.etc;
        const cells = [];
        let door = null;
        if (type.walk) {
            // 현관·계단처럼 걸어 들어가는 곳은 그 칸 자체가 출발·도착 칸이다.
            for (let lz = room.z; lz < room.z + room.d; lz += 1) {
                for (let lx = room.x; lx < room.x + room.w; lx += 1) {
                    const c = grid.cell(room.floor, building.x + lx, building.z + lz);
                    if (c && c.walk && !c.outdoor) cells.push(grid.index(room.floor, building.x + lx, building.z + lz));
                }
            }
        } else {
            // 교실은 문(3D 화면에 뚫린 문과 같은 곳)으로만 드나든다. 벽을 뚫고 지나가지 않게 한다.
            const found = roomDoor(grid, building, room);
            if (found) {
                cells.push(grid.index(room.floor, found.cell.x, found.cell.z));
                door = { f: room.floor, ...found.point, inside: found.inside };
            }
        }
        return {
            kind, label: roomTitle(room), room, building, cells, floor: room.floor, door,
            anchor: { f: room.floor, x: building.x + room.x + room.w / 2, z: building.z + room.z + room.d / 2 },
        };
    }
    return null;
}

function nearestOutdoor(grid, x, z) {
    for (let radius = 0; radius < 12; radius += 1) {
        const found = [];
        for (let dz = -radius; dz <= radius; dz += 1) {
            for (let dx = -radius; dx <= radius; dx += 1) {
                if (Math.max(Math.abs(dx), Math.abs(dz)) !== radius) continue;
                const c = grid.cell(1, x + dx, z + dz);
                if (c && c.outdoor) found.push(grid.index(1, x + dx, z + dz));
            }
        }
        if (found.length) return found;
    }
    return null;
}

// ------------------------------------------------------------------ 최단 경로
function search(grid, sources, targets, { noStairs = false, stairsCost = STAIRS_COST } = {}) {
    const dist = new Map();
    const prev = new Map();
    const heap = new MinHeap();
    for (const s of sources) {
        dist.set(s, 0);
        heap.push(s, 0);
    }
    const targetSet = new Set(targets);
    const DIRS = [[1, 0, 1], [-1, 0, 1], [0, 1, 1], [0, -1, 1],
        [1, 1, Math.SQRT2], [1, -1, Math.SQRT2], [-1, 1, Math.SQRT2], [-1, -1, Math.SQRT2]];

    while (heap.size) {
        const [cost, idx] = heap.pop();
        if (cost > (dist.get(idx) ?? Infinity)) continue;
        if (targetSet.has(idx)) {
            const path = [idx];
            let cur = idx;
            while (prev.has(cur)) { cur = prev.get(cur); path.push(cur); }
            return path.reverse();
        }
        const { f, x, z } = decode(grid, idx);
        const here = grid.cell(f, x, z);
        const from = prev.has(idx) ? decode(grid, prev.get(idx)) : null;
        const came = from && from.f === f ? [x - from.x, z - from.z] : null;
        const relax = (nf, nx, nz, step) => {
            const nIdx = grid.index(nf, nx, nz);
            const next = cost + step;
            if (next < (dist.get(nIdx) ?? Infinity)) {
                dist.set(nIdx, next);
                prev.set(nIdx, idx);
                heap.push(nIdx, next);
            }
        };
        for (const [dx, dz, len] of DIRS) {
            const there = grid.cell(f, x + dx, z + dz);
            if (!canStep(here, there, dx, dz)) continue;
            if (dx && dz) {
                // 대각선은 바깥 길에서만, 모서리를 깎지 않을 때 허용한다.
                // 복도는 반듯하게 걸어야 '왼쪽/오른쪽으로 돌아' 안내가 정확해진다.
                if (!here.outdoor || !there.outdoor) continue;
                const side1 = grid.cell(f, x + dx, z);
                const side2 = grid.cell(f, x, z + dz);
                if (!side1?.outdoor || !side2?.outdoor) continue;
            }
            // 바깥길은 조금 더 비싸게 쳐서, 가능하면 건물 안 복도로 안내한다.
            // 계단실·승강기 칸은 층을 오르내릴 때만 들어가도록 가로지르는 비용을 크게 둔다.
            const vertical = there.kind === 'stairs' || there.kind === 'elevator';
            const turn = came && !here.outdoor && (came[0] !== dx || came[1] !== dz) ? TURN_COST : 0;
            relax(f, x + dx, z + dz, (here.outdoor ? len * 1.15 : vertical ? len * STAIRWELL_WALK : len) + turn);
        }
        if (here.kind === 'stairs' || here.kind === 'elevator') {
            if (here.kind === 'stairs' && noStairs) continue;
            for (const df of [1, -1]) {
                const there = grid.cell(f + df, x, z);
                if (there && there.kind === here.kind && there.building === here.building) {
                    relax(f + df, x, z, here.kind === 'stairs' ? stairsCost : ELEVATOR_COST);
                }
            }
        }
    }
    return null;
}

/**
 * 편집 화면 점검용: 교문·후문에서 걸어서 닿지 않는 교실을 한 번의 탐색으로 찾는다.
 * (교실마다 findRoute 를 부르면 교실 수만큼 격자를 다시 훑어 느리다.)
 * 결과: [{ building, room, reason: 'nodoor' | 'cut' }] — nodoor: 교실이 복도에 닿지 않음, cut: 길이 끊김
 */
export function unreachableRooms(layout) {
    const grid = buildGrid(layout);
    const seen = new Uint8Array(grid.size * grid.floors);
    const queue = [];
    for (const gate of layout.gates || []) {
        const end = resolveEndpoint(grid, `gate:${gate.id}`);
        for (const idx of end?.cells || []) {
            if (!seen[idx]) { seen[idx] = 1; queue.push(idx); }
        }
    }
    const DIRS = [[1, 0], [-1, 0], [0, 1], [0, -1]];
    for (let head = 0; head < queue.length; head += 1) {
        const idx = queue[head];
        const { f, x, z } = decode(grid, idx);
        const here = grid.cell(f, x, z);
        const visit = (nf, nx, nz) => {
            const n = grid.index(nf, nx, nz);
            if (!seen[n]) { seen[n] = 1; queue.push(n); }
        };
        for (const [dx, dz] of DIRS) {
            if (canStep(here, grid.cell(f, x + dx, z + dz), dx, dz)) visit(f, x + dx, z + dz);
        }
        if (here.kind === 'stairs' || here.kind === 'elevator') {
            for (const df of [1, -1]) {
                const there = grid.cell(f + df, x, z);
                if (there && there.kind === here.kind && there.building === here.building) visit(f + df, x, z);
            }
        }
    }
    const out = [];
    for (const building of layout.buildings) {
        if (building.kind === 'connector') continue;
        for (const room of building.rooms) {
            if (ROOM_TYPES[room.type]?.walk) continue;
            const door = roomDoor(grid, building, room);
            if (!door) out.push({ building, room, reason: 'nodoor' });
            else if (!seen[grid.index(room.floor, door.cell.x, door.cell.z)]) out.push({ building, room, reason: 'cut' });
        }
    }
    return out;
}

/** 엘리베이터 우선 길에서 계단으로 오르내리는 구간을 '본관 1층 → 2층'처럼 알려주는 문구. */
function stairsWarning(grid, raw) {
    const legs = [];
    for (let i = 1; i < raw.length; i += 1) {
        const a = decode(grid, raw[i - 1]);
        const b = decode(grid, raw[i]);
        const info = grid.cell(a.f, a.x, a.z);
        if (a.f === b.f || info?.kind !== 'stairs') continue;
        const last = legs[legs.length - 1];
        if (last && last.to === a.f && last.building === info.building) last.to = b.f;
        else legs.push({ building: info.building, from: a.f, to: b.f });
    }
    if (!legs.length) return '엘리베이터만으로 갈 수 있는 길이 없어 계단을 포함한 경로로 안내합니다.';
    const parts = legs.map(l => `${l.building.name} ${l.from}층 → ${l.to}층`).join(', ');
    return `엘리베이터가 이어지지 않는 구간(${parts})은 계단으로 안내합니다. 배치도에서 엘리베이터가 층마다 같은 자리에 있는지 확인해 주세요.`;
}

/**
 * 길찾기. fromKey/toKey 는 "gate:<id>" 또는 "room:<id>".
 * 결과: { ok, message, path:[{f,x,z}], steps:[...], meters, seconds, from, to }
 */
export function findRoute(layout, fromKey, toKey, { preferElevator = false } = {}) {
    const grid = buildGrid(layout);
    const from = resolveEndpoint(grid, fromKey);
    const to = resolveEndpoint(grid, toKey);
    if (!from) return { ok: false, message: '출발지를 찾을 수 없습니다.' };
    if (!to) return { ok: false, message: '도착지를 찾을 수 없습니다.' };
    if (fromKey === toKey) return { ok: false, message: '출발지와 도착지가 같습니다.' };
    if (!from.cells.length) return { ok: false, message: `${from.label}이(가) 복도와 이어져 있지 않습니다. 배치도를 확인해 주세요.` };
    if (!to.cells.length) return { ok: false, message: `${to.label}이(가) 복도와 이어져 있지 않습니다. 배치도를 확인해 주세요.` };

    let warning = '';
    let raw = search(grid, from.cells, to.cells, { noStairs: preferElevator });
    if (!raw && preferElevator) {
        // 엘리베이터가 모든 층을 잇지 않으면 계단 구간을 가장 적게 쓰는 길로 안내하고, 그 구간을 알려준다.
        raw = search(grid, from.cells, to.cells, { stairsCost: STAIRS_COST * 40 });
        if (raw) warning = stairsWarning(grid, raw);
    }
    if (!raw) {
        return {
            ok: false,
            message: '이어진 길을 찾지 못했습니다. 현관·복도·계단이 끊겨 있지 않은지 배치도를 확인해 주세요.',
        };
    }

    const path = raw.map(idx => {
        const p = decode(grid, idx);
        return { ...p, info: grid.cell(p.f, p.x, p.z) };
    });
    const steps = describe(layout, path, from, to);
    let meters = 0;
    let seconds = 0;
    for (let i = 1; i < path.length; i += 1) {
        const a = path[i - 1];
        const b = path[i];
        if (a.f !== b.f) {
            seconds += SECONDS_PER_FLOOR[a.info.kind] || 15;
            continue;
        }
        const len = Math.hypot(a.x - b.x, a.z - b.z) * layout.cell;
        meters += len;
        seconds += len / WALK_SPEED;
    }
    return {
        ok: true,
        warning,
        from, to,
        path: path.map(({ f, x, z, info }) => ({ f, x: x + 0.5, z: z + 0.5, kind: info.kind })),
        steps,
        meters: Math.round(meters),
        seconds: Math.round(seconds),
    };
}

// ------------------------------------------------------------------ 안내 문장
function hasBatchim(word) {
    const text = String(word || '').replace(/[\s)\]]+$/, '');
    const ch = text.charCodeAt(text.length - 1);
    if (ch >= 0xac00 && ch <= 0xd7a3) return (ch - 0xac00) % 28 !== 0;
    if (ch >= 48 && ch <= 57) return '013678'.includes(text[text.length - 1]);
    return false;
}

export function josa(word, withBatchim, without) {
    return `${word}${hasBatchim(word) ? withBatchim : without}`;
}

function eulo(word) {
    // (으)로: ㄹ 받침은 '로'
    const text = String(word || '').replace(/[\s)\]]+$/, '');
    const ch = text.charCodeAt(text.length - 1);
    if (ch >= 0xac00 && ch <= 0xd7a3 && (ch - 0xac00) % 28 === 8) return `${word}로`;
    return josa(word, '으로', '로');
}

function placeKey(p) {
    return p.info.outdoor ? 'out' : `${p.info.building.id}:${p.f}`;
}

function runsOf(points, cell) {
    // 같은 방향 이동을 묶는다.
    const runs = [];
    for (let i = 1; i < points.length; i += 1) {
        const dx = Math.sign(points[i].x - points[i - 1].x);
        const dz = Math.sign(points[i].z - points[i - 1].z);
        const len = Math.hypot(points[i].x - points[i - 1].x, points[i].z - points[i - 1].z) * cell;
        const last = runs[runs.length - 1];
        if (last && last.dx === dx && last.dz === dz) last.len += len;
        else runs.push({ dx, dz, len });
    }
    // 대각선 계단 모양의 짧은 꺾임(3m 미만)은 앞 구간에 합친다.
    const merged = [];
    for (const run of runs) {
        const last = merged[merged.length - 1];
        if (last && run.len < cell * 1.01 && merged.length) { last.len += run.len; continue; }
        if (last && angleBetween(last, run) < 50) { last.len += run.len; continue; }
        merged.push({ ...run });
    }
    return merged;
}

function angleBetween(a, b) {
    const dot = a.dx * b.dx + a.dz * b.dz;
    const mag = Math.hypot(a.dx, a.dz) * Math.hypot(b.dx, b.dz) || 1;
    return Math.acos(Math.max(-1, Math.min(1, dot / mag))) * 180 / Math.PI;
}

function turnWord(a, b) {
    const angle = angleBetween(a, b);
    if (angle > 150) return '뒤로 돌아';
    // z가 아래(남쪽)로 커지는 화면 좌표라 외적이 양수면 오른쪽(시계 방향)이다.
    const cross = a.dx * b.dz - a.dz * b.dx;
    return cross > 0 ? '오른쪽으로 돌아' : '왼쪽으로 돌아';
}

function meters(len) {
    return `약 ${Math.max(1, Math.round(len))}m`;
}

/** "약 20m 직진 → 왼쪽으로 돌아 약 15m 이동" 처럼 '…하다'로 이어 붙일 수 있게 끝낸다. */
function runsText(runs, prevRun = null) {
    if (!runs.length) return '';
    // 앞 구간(다른 건물·바깥)에서 이어질 때 꺾이는 방향도 알려준다.
    const turnFirst = prevRun && angleBetween(prevRun, runs[0]) >= 50;
    const parts = [turnFirst
        ? `${turnWord(prevRun, runs[0])} ${meters(runs[0].len)} 이동`
        : `${meters(runs[0].len)} 직진`];
    for (let i = 1; i < runs.length && i < 5; i += 1) {
        parts.push(`${turnWord(runs[i - 1], runs[i])} ${meters(runs[i].len)} 이동`);
    }
    if (runs.length > 5) parts.push('길을 따라 계속 이동');
    return parts.join(' → ');
}

function sideOf(lastRun, from, target) {
    if (!lastRun) return '';
    const vx = target.x - from.x;
    const vz = target.z - from.z;
    const cross = lastRun.dx * vz - lastRun.dz * vx;
    const dot = lastRun.dx * vx + lastRun.dz * vz;
    if (Math.abs(cross) < 0.35 * Math.hypot(vx, vz)) return dot >= 0 ? '정면' : '뒤쪽';
    return cross > 0 ? '오른쪽' : '왼쪽';
}

function describe(layout, path, from, to) {
    const cell = layout.cell;
    const steps = [];
    const startPlace = from.kind === 'gate'
        ? from.label
        : `${from.building.name} ${from.floor}층 ${from.label}`;
    steps.push({ kind: 'start', icon: 'fa-location-dot', floor: from.floor, from: 0, to: 0,
        text: `${startPlace}에서 출발합니다.` });

    // 경로를 [같은 장소(바깥/건물·층)] 구간과 [층 이동] 구간으로 자른다.
    const legs = [];
    let i = 0;
    while (i < path.length) {
        let j = i;
        while (j + 1 < path.length && path[j + 1].f === path[j].f
               && placeKey(path[j + 1]) === placeKey(path[i])) j += 1;
        legs.push({ kind: 'walk', from: i, to: j });
        if (j + 1 >= path.length) break;
        if (path[j + 1].f !== path[j].f) {
            let k = j + 1;
            while (k + 1 < path.length && path[k + 1].f !== path[k].f) k += 1;
            legs.push({ kind: 'vertical', from: j, to: k });
            i = k;
        } else {
            i = j + 1;
        }
    }

    let prevRun = null;
    for (let li = 0; li < legs.length; li += 1) {
        const leg = legs[li];
        const first = path[leg.from];
        const last = path[leg.to];
        if (leg.kind === 'vertical') {
            prevRun = null;
            const kindName = last.info.kind === 'elevator' ? '엘리베이터' : '계단';
            const name = first.info.room?.name || kindName;
            const up = last.f > first.f;
            steps.push({
                kind: 'vertical', icon: up ? 'fa-arrow-up' : 'fa-arrow-down', floor: last.f,
                from: leg.from, to: leg.to,
                text: `${josa(name, '을', '를')} 이용해 ${last.f}층으로 ${up ? '올라갑니다' : '내려갑니다'}.`,
            });
            continue;
        }
        // 다음 구간이 이어지는 곳까지 한 칸 포함해 방향을 계산한다.
        const nextLeg = legs[li + 1];
        const endIdx = nextLeg && nextLeg.kind === 'walk' ? nextLeg.from : leg.to;
        const points = path.slice(leg.from, endIdx + 1);
        const runs = runsOf(points, cell);
        const next = nextLeg ? path[nextLeg.from] : null;
        const move = runsText(runs, prevRun);
        if (runs.length) prevRun = runs[runs.length - 1];

        if (first.info.outdoor) {
            if (!move) continue;
            let tail = '';
            if (next && !next.info.outdoor) {
                const door = next.info.door?.label || next.info.room?.name || '출입구';
                const bname = next.info.building.name;
                tail = ` ${eulo(door.startsWith(bname) ? door : `${bname} ${door}`)} 들어갑니다`;
            }
            steps.push({ kind: 'walk', icon: 'fa-person-walking', floor: 1, from: leg.from, to: endIdx,
                text: `바깥 길을 따라 ${move}${tail ? `한 뒤${tail}` : '합니다'}.` });
            continue;
        }

        const building = first.info.building;
        const where = building.kind === 'connector'
            ? `${first.f}층 ${building.name}`
            : `${building.name} ${first.f}층 복도`;
        let tail = '';
        if (next && next.info.outdoor) tail = ` 밖으로 나갑니다`;
        else if (next && next.info.building !== building) tail = ` ${eulo(next.info.building.name)} 넘어갑니다`;
        else if (nextLeg && nextLeg.kind === 'vertical') {
            const kindName = last.info.kind === 'elevator' ? '엘리베이터' : '계단';
            tail = ` ${last.info.room?.name || kindName} 앞까지 갑니다`;
        }
        if (!move && !tail) continue;
        steps.push({ kind: 'walk', icon: 'fa-shoe-prints', floor: first.f, from: leg.from, to: endIdx,
            text: `${josa(where, '을', '를')} 따라 ${move || '조금 이동'}${tail ? `한 뒤${tail}` : '합니다'}.` });
    }

    // 도착 — 교실이 진행 방향의 어느 쪽에 있는지 알려준다.
    const lastPoint = path[path.length - 1];
    const tailRuns = runsOf(path.slice(-6).filter(p => p.f === lastPoint.f), cell);
    const side = to.kind === 'room' && !(ROOM_TYPES[to.room.type] || {}).walk
        ? sideOf(tailRuns[tailRuns.length - 1], { x: lastPoint.x + 0.5, z: lastPoint.z + 0.5 }, to.anchor)
        : '';
    const place = to.kind === 'gate' ? to.label : `${to.building.name} ${to.floor}층 ${to.label}`;
    let where = side ? ` 교실은 진행 방향 ${side}에 있습니다.` : '';
    if (!where && to.kind === 'room' && (lastPoint.info.kind === 'stairs' || lastPoint.info.kind === 'elevator')) {
        const name = lastPoint.info.room?.name || (lastPoint.info.kind === 'stairs' ? '계단' : '엘리베이터');
        where = ` 교실은 ${name} 바로 옆에 있습니다.`;
    }
    steps.push({
        kind: 'arrive', icon: 'fa-flag-checkered', floor: to.floor, from: path.length - 1, to: path.length - 1,
        text: `${place}에 도착합니다.${where}`,
    });
    return steps;
}

export function formatDuration(seconds) {
    if (seconds < 60) return '1분 이내';
    return `약 ${Math.round(seconds / 60)}분`;
}

