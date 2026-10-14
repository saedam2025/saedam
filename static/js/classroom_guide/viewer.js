// 교실안내 보기 화면(인트라넷 · 학부모 공유 링크 공용)
import { GuideScene } from './scene.js';
import {
    ROOM_TYPES, findRoom, hasFloor, maxFloor, roomTitle, searchRooms,
} from './model.js';
import { endpointOptions, findRoute, formatDuration } from './route.js';

function esc(text) {
    return String(text ?? '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
}

function toast(root, message) {
    let box = root.querySelector('.cg-toast');
    if (!box) {
        box = document.createElement('div');
        box.className = 'cg-toast';
        box.setAttribute('role', 'status');
        root.appendChild(box);
    }
    box.textContent = message;
    box.classList.add('is-on');
    clearTimeout(box._t);
    box._t = setTimeout(() => box.classList.remove('is-on'), 2600);
}

async function copyText(text) {
    try {
        await navigator.clipboard.writeText(text);
        return true;
    } catch (_) {
        const area = document.createElement('textarea');
        area.value = text;
        document.body.appendChild(area);
        area.select();
        const ok = document.execCommand('copy');
        area.remove();
        return ok;
    }
}

export function mountViewer(root, { layout, publicLink = '', mode = 'internal' } = {}) {
    const $ = sel => root.querySelector(sel);
    const $$ = sel => [...root.querySelectorAll(sel)];
    const state = { view: 'all', roomId: null, route: null, publicLink };

    const scene = new GuideScene($('[data-cg-stage]'), {
        onPickRoom: id => selectRoom(id, { focus: false }),
        onPickBuilding: (_id, floor) => { if (state.view === 'all' && floor) setFloor(floor); },
        onCamera: theta => {
            const needle = $('[data-cg-compass]');
            if (needle) needle.style.transform = `rotate(${(theta * 180 / Math.PI) + (layout.north || 0)}deg)`;
        },
        onInteract: () => { if (scene.autoRotate) toggleRotate(false); },
    });
    scene.setLayout(layout);

    // ------------------------------------------------------------ 층
    function renderFloors() {
        const box = $('[data-cg-floors]');
        if (!box) return;
        const top = maxFloor(layout);
        const items = [['all', '전체']];
        for (let f = 1; f <= top; f += 1) items.push([String(f), `${f}층`]);
        box.innerHTML = items.map(([v, label]) =>
            `<button type="button" class="cg-floor-tab${String(state.view) === v ? ' is-active' : ''}" data-floor="${v}" aria-pressed="${String(state.view) === v}">${label}</button>`,
        ).join('');
    }

    function setFloor(view) {
        state.view = view === 'all' ? 'all' : Number(view);
        scene.setFloor(state.view);
        renderFloors();
        renderFloorCourses();
        renderFloorInfo();
    }

    function renderFloorInfo() {
        const box = $('[data-cg-floorinfo]');
        if (!box) return;
        if (state.view === 'all') {
            box.innerHTML = layout.buildings.filter(b => b.kind !== 'connector').map(b =>
                `<div class="cg-floorinfo-row"><b>${esc(b.name)}</b><span>${b.floors}층 건물${b.note ? ` · ${esc(b.note)}` : ''}</span></div>`,
            ).join('') || '<p class="cg-muted">건물 정보가 없습니다.</p>';
            return;
        }
        const rows = layout.buildings
            .filter(b => b.kind !== 'connector' && hasFloor(b, state.view))
            .map(b => {
                const info = b.floorInfo?.[String(state.view)] || b.rooms
                    .filter(r => r.floor === state.view && !ROOM_TYPES[r.type]?.walk && r.type !== 'restroom')
                    .map(r => r.name || r.no).filter(Boolean).join(' · ');
                return `<div class="cg-floorinfo-row"><b>${esc(b.name)} ${state.view}층</b><span>${esc(info || '-')}</span></div>`;
            });
        box.innerHTML = rows.join('') || '<p class="cg-muted">이 층이 있는 건물이 없습니다.</p>';
    }

    // ------------------------------------------------------------ 교실 정보
    function selectRoom(roomId, { focus = true } = {}) {
        const found = roomId && findRoom(layout, roomId);
        state.roomId = found ? roomId : null;
        // 전체 보기에서는 교실이 외벽에 가려지므로 교실이 있는 층의 단면으로 넘어간다.
        if (found && state.view !== found.room.floor) setFloor(found.room.floor);
        scene.selectRoom(state.roomId);
        if (found && focus) scene.focusRoom(roomId);
        renderInfo();
        $$('[data-room-chip]').forEach(el => el.classList.toggle('is-active', el.dataset.roomChip === state.roomId));
    }

    function renderInfo() {
        const box = $('[data-cg-info]');
        if (!box) return;
        const found = state.roomId && findRoom(layout, state.roomId);
        if (!found) {
            box.innerHTML = `<div class="cg-info-empty"><i class="fa-solid fa-hand-pointer"></i>
                <p>3D 안내도에서 교실을 누르거나<br>강좌·교실 이름으로 검색해 보세요.</p></div>`;
            return;
        }
        const { room, building } = found;
        const type = ROOM_TYPES[room.type] || ROOM_TYPES.etc;
        const courses = room.courses || [];
        const courseHtml = courses.length ? courses.map(c => `
            <div class="cg-course">
                <div class="cg-course-name"><i class="fa-solid fa-book-open"></i>${esc(c.name || '강좌')}</div>
                <dl>
                    ${c.teacher ? `<div><dt><i class="fa-regular fa-user"></i>강사명</dt><dd>${esc(c.teacher)}</dd></div>` : ''}
                    ${c.time ? `<div><dt><i class="fa-regular fa-clock"></i>수업시간</dt><dd>${esc(c.time)}</dd></div>` : ''}
                    ${c.grade ? `<div><dt><i class="fa-solid fa-children"></i>대상학년</dt><dd>${esc(c.grade)}</dd></div>` : ''}
                </dl>
            </div>`).join('') : (type.course ? '<p class="cg-muted">등록된 방과후 강좌가 없습니다.</p>' : '');
        box.innerHTML = `
            <div class="cg-info-head" style="--room:${type.color};--room-edge:${type.edge}">
                <span class="cg-chip">${esc(type.label)}</span>
                <h3>${esc(roomTitle(room))}</h3>
                <p><i class="fa-solid fa-building"></i> ${esc(building.name)} ${room.floor}층</p>
            </div>
            ${courseHtml}
            ${room.note ? `<p class="cg-note"><i class="fa-solid fa-circle-info"></i> ${esc(room.note)}</p>` : ''}
            <div class="cg-info-actions">
                <button type="button" class="cg-btn cg-btn-primary" data-cg-go="${esc(room.id)}"><i class="fa-solid fa-location-arrow"></i> 교실 찾아가기</button>
                ${state.publicLink ? `<button type="button" class="cg-btn" data-cg-copy-room="${esc(room.id)}"><i class="fa-solid fa-link"></i> 학부모 안내 링크 복사</button>` : ''}
            </div>`;
    }

    function renderFloorCourses() {
        const box = $('[data-cg-floor-courses]');
        if (!box) return;
        const title = $('[data-cg-floor-courses-title]');
        if (title) title.textContent = state.view === 'all' ? '전체 방과후 강좌' : `${state.view}층의 강좌`;
        const chips = [];
        for (const building of layout.buildings) {
            for (const room of building.rooms) {
                if (state.view !== 'all' && room.floor !== state.view) continue;
                for (const course of room.courses || []) {
                    if (!course.name) continue;
                    const type = ROOM_TYPES[room.type] || ROOM_TYPES.etc;
                    chips.push(`<button type="button" class="cg-course-chip${state.roomId === room.id ? ' is-active' : ''}" data-room-chip="${esc(room.id)}" style="--room:${type.color};--room-edge:${type.edge}">
                        <b>${esc(room.no || room.name)}</b><span>${esc(course.name)}</span></button>`);
                }
            }
        }
        box.innerHTML = chips.join('') || '<p class="cg-muted">등록된 강좌가 없습니다.</p>';
    }

    // ------------------------------------------------------------ 검색
    const searchInput = $('[data-cg-search]');
    const searchResults = $('[data-cg-search-results]');
    function renderSearch() {
        if (!searchInput || !searchResults) return;
        const q = searchInput.value.trim();
        if (!q) { searchResults.hidden = true; searchResults.innerHTML = ''; return; }
        const items = searchRooms(layout, q).slice(0, 8);
        searchResults.hidden = false;
        searchResults.innerHTML = items.length ? items.map(({ room, building }) => {
            const courses = (room.courses || []).map(c => c.name).filter(Boolean).join(', ');
            return `<button type="button" role="option" data-cg-result="${esc(room.id)}">
                <b>${esc(roomTitle(room))}</b>
                <span>${esc(building.name)} ${room.floor}층${courses ? ` · ${esc(courses)}` : ''}</span></button>`;
        }).join('') : '<p class="cg-muted">찾는 교실·강좌가 없습니다.</p>';
    }
    searchInput?.addEventListener('input', renderSearch);
    searchInput?.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
            e.preventDefault();
            const first = searchResults?.querySelector('[data-cg-result]');
            if (first) first.click();
        } else if (e.key === 'Escape') {
            searchInput.value = '';
            renderSearch();
        }
    });

    // ------------------------------------------------------------ 길찾기
    const fromSelect = $('[data-cg-from]');
    const toSelect = $('[data-cg-to]');
    function fillEndpoints() {
        const options = endpointOptions(layout);
        const groups = new Map();
        for (const opt of options) {
            if (!groups.has(opt.group)) groups.set(opt.group, []);
            groups.get(opt.group).push(opt);
        }
        const html = [...groups.entries()].map(([group, opts]) =>
            `<optgroup label="${esc(group)}">${opts.map(o => `<option value="${esc(o.key)}">${esc(o.label)}</option>`).join('')}</optgroup>`,
        ).join('');
        if (fromSelect) fromSelect.innerHTML = html;
        if (toSelect) toSelect.innerHTML = `<option value="">도착 교실을 선택하세요</option>${html}`;
        if (fromSelect && layout.gates[0]) fromSelect.value = `gate:${layout.gates[0].id}`;
    }

    function runRoute(toKey, { scroll = true } = {}) {
        const fromKey = fromSelect?.value || (layout.gates[0] ? `gate:${layout.gates[0].id}` : '');
        if (toSelect && toKey) toSelect.value = toKey;
        const target = toKey || toSelect?.value;
        const stepsBox = $('[data-cg-steps]');
        const summary = $('[data-cg-route-summary]');
        if (!target) { toast(root, '도착할 교실을 선택해 주세요.'); return; }
        if (!fromKey) { toast(root, '출발지를 선택해 주세요.'); return; }
        const route = findRoute(layout, fromKey, target, { preferElevator: !!$('[data-cg-elevator]')?.checked });
        root.classList.toggle('has-route', !!route.ok);
        if (!route.ok) {
            state.route = null;
            scene.setRoute(null);
            if (summary) summary.innerHTML = `<p class="cg-error"><i class="fa-solid fa-triangle-exclamation"></i> ${esc(route.message)}</p>`;
            if (stepsBox) stepsBox.innerHTML = '';
            return;
        }
        state.route = route;
        const toRoom = route.to.kind === 'room' ? route.to.room.id : null;
        if (toRoom) selectRoom(toRoom, { focus: false });
        setFloor(route.to.floor);
        scene.setRoute(route);
        scene.focusPath(route.path);
        if (summary) {
            summary.innerHTML = `
                <div class="cg-route-total">
                    <span><i class="fa-solid fa-route"></i> 약 ${route.meters}m</span>
                    <span><i class="fa-regular fa-clock"></i> ${formatDuration(route.seconds)}</span>
                </div>
                ${route.warning ? `<p class="cg-warn"><i class="fa-solid fa-circle-exclamation"></i> ${esc(route.warning)}</p>` : ''}`;
        }
        if (stepsBox) {
            stepsBox.innerHTML = route.steps.map((step, i) => `
                <li><button type="button" data-cg-step="${i}" class="cg-step cg-step-${step.kind}">
                    <span class="cg-step-no">${i + 1}</span>
                    <span class="cg-step-text"><i class="fa-solid ${step.icon}"></i> ${esc(step.text)}</span>
                    <span class="cg-step-floor">${step.floor}층</span>
                </button></li>`).join('');
        }
        if (scroll) $('[data-cg-route-panel]')?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
    }

    function clearRoute() {
        state.route = null;
        scene.setRoute(null);
        root.classList.remove('has-route');
        const stepsBox = $('[data-cg-steps]');
        const summary = $('[data-cg-route-summary]');
        if (stepsBox) stepsBox.innerHTML = '';
        if (summary) summary.innerHTML = '';
    }

    function focusStep(index) {
        const route = state.route;
        const step = route?.steps[index];
        if (!step) return;
        $$('[data-cg-step]').forEach(el => el.classList.toggle('is-active', Number(el.dataset.cgStep) === index));
        setFloor(step.floor);
        const slice = route.path.slice(step.from, step.to + 1);
        if (step.kind === 'start') scene.focusPath([route.path[0]]);
        else if (step.kind === 'arrive' && route.to.kind === 'room') scene.focusRoom(route.to.room.id);
        else scene.focusPath(slice.length ? slice : route.path);
    }

    // ------------------------------------------------------------ 도구
    function toggleRotate(force) {
        const on = typeof force === 'boolean' ? force : !scene.autoRotate;
        scene.setAutoRotate(on);
        $('[data-cg-action="rotate"]')?.classList.toggle('is-active', on);
    }

    root.addEventListener('click', async (e) => {
        const target = e.target.closest('button, [data-cg-result]');
        if (!target || !root.contains(target)) return;
        const d = target.dataset;
        if (d.floor) setFloor(d.floor);
        else if (d.cgAction === 'reset') { scene.resetView(); setFloor('all'); }
        else if (d.cgAction === 'zoom-in') scene.zoom(0.75);
        else if (d.cgAction === 'zoom-out') scene.zoom(1.33);
        else if (d.cgAction === 'rotate') toggleRotate();
        else if (d.roomChip) selectRoom(d.roomChip);
        else if (d.cgResult) {
            selectRoom(d.cgResult);
            if (toSelect) toSelect.value = `room:${d.cgResult}`;
            if (searchResults) searchResults.hidden = true;
            if (mode === 'public') runRoute(`room:${d.cgResult}`);
        } else if (d.cgGo) runRoute(`room:${d.cgGo}`);
        else if (d.cgStep !== undefined) focusStep(Number(d.cgStep));
        else if ('cgFind' in d) runRoute();
        else if ('cgClearRoute' in d) clearRoute();
        else if (d.cgCopyRoom) {
            const ok = await copyText(`${state.publicLink}?to=${encodeURIComponent(d.cgCopyRoom)}`);
            toast(root, ok ? '이 교실로 바로 안내하는 링크를 복사했습니다.' : '복사하지 못했습니다.');
        }
    });

    document.addEventListener('click', (e) => {
        if (searchResults && !searchResults.hidden && !e.target.closest('[data-cg-searchbox]')) searchResults.hidden = true;
    });

    // ------------------------------------------------------------ 시작
    renderFloors();
    renderFloorInfo();
    renderFloorCourses();
    renderInfo();
    fillEndpoints();

    const params = new URLSearchParams(window.location.search);
    const deepTo = params.get('to');
    if (deepTo && findRoom(layout, deepTo)) {
        const from = params.get('from');
        if (from && fromSelect && [...fromSelect.options].some(o => o.value === from)) fromSelect.value = from;
        // 링크로 들어온 경우에는 3D 안내도부터 보이도록 화면을 내리지 않는다.
        setTimeout(() => runRoute(`room:${deepTo}`, { scroll: false }), 350);
    }

    return {
        scene,
        setPublicLink(link) { state.publicLink = link; renderInfo(); },
        selectRoom,
        setFloor,
    };
}
