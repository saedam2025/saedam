/* 청구업무 공용 스크립트 — 본사 화면과 학교회원 포털이 함께 쓴다.
 *
 * Billing.init({
 *   viewer: 'staff' | 'member',
 *   csrf: '포털 CSRF 토큰(본사 화면은 null)',
 *   urls: { summary, tasks, task(id), messages(id), advance(id), remove(id) },
 *   summaryEl, listEl, getFilters(), onStatClick(stage)
 * })
 */
(function (global) {
    'use strict';

    var STAGES = ['start', 'progress', 'review', 'done'];
    var STAGE_COLOR = { start: '#64748b', progress: '#2563eb', review: '#d97706', done: '#16a34a' };
    var cfg = null;
    var modal = null;
    var currentTaskId = null;
    var uploading = false;
    var composerZone = null;
    // 본사 화면 전용: 마지막으로 그린 목록(상세의 이전/다음), 체크한 업무, 연 업무가 새 소식이었는지
    var lastTasks = [];
    var selected = {};
    var openedUnread = false;
    var bulkDialog = null;
    var lastDetail = null;        // 열려 있는 업무 창의 마지막 상세 데이터(실시간 갱신 비교용)
    var realtimeSocket = null;
    var SCHOOL_COLORS = ['#2563eb', '#db2777', '#0891b2', '#7c3aed', '#059669', '#ea580c', '#4f46e5', '#be123c', '#0d9488', '#9333ea'];

    // 학교(회원)마다 늘 같은 색 — 여러 학교 업무가 섞여도 한눈에 구분되게 한다.
    function schoolColor(memberId) {
        var n = Math.abs(parseInt(memberId, 10) || 0);
        return SCHOOL_COLORS[n % SCHOOL_COLORS.length];
    }

    function esc(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function fmtSize(bytes) {
        var n = Number(bytes || 0);
        if (n < 1024) return n + 'B';
        if (n < 1024 * 1024) return (n / 1024).toFixed(0) + 'KB';
        return (n / 1024 / 1024).toFixed(1) + 'MB';
    }

    // 시간은 모두 12시간제로 보여 준다: '14:24' → 'pm 02:24'
    function clock12(hhmm) {
        var m = /^(\d{1,2}):(\d{2})/.exec(String(hhmm || ''));
        if (!m) return String(hhmm || '');
        var h = Number(m[1]);
        return (h < 12 ? 'am ' : 'pm ') + String(h % 12 || 12).padStart(2, '0') + ':' + m[2];
    }

    // '2026-09-13 14:24:05' → '2026-09-13 pm 02:24'
    function shortTime(text) {
        var s = String(text || '');
        var m = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})/.exec(s);
        return m ? m[1] + ' ' + clock12(m[2]) : s.slice(0, 16);
    }

    var toastTimer = null;
    function toast(message) {
        var box = document.getElementById('blToast');
        if (!box) {
            box = document.createElement('div');
            box.id = 'blToast';
            box.className = 'bl-toast';
            document.body.appendChild(box);
        }
        box.textContent = message;
        box.classList.add('show');
        clearTimeout(toastTimer);
        toastTimer = setTimeout(function () { box.classList.remove('show'); }, 3200);
    }

    async function request(url, options) {
        options = options || {};
        options.credentials = 'same-origin';
        options.headers = Object.assign({}, options.headers || {});
        if (cfg && cfg.csrf) options.headers['X-Portal-CSRF'] = cfg.csrf;
        var res = await fetch(url, options);
        var data = await res.json().catch(function () {
            return { status: 'error', message: '서버 응답을 확인할 수 없습니다.' };
        });
        if (res.status === 401 && data.login_url) {
            location.href = data.login_url;
        }
        if (!res.ok || data.status !== 'success') {
            throw new Error(data.message || '처리 중 오류가 발생했습니다.');
        }
        return data;
    }

    function postJSON(url, body) {
        return request(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {})
        });
    }

    /* ------------------------------------------------------------ 파일 첨부(갤러리 · 조각 전송) */
    var MAX_FILES = 10;
    var MAX_FILE_BYTES = 30 * 1024 * 1024;
    var MAX_PARALLEL = 2;          // 동시에 보내는 파일 수
    var CHUNK_RETRIES = 5;         // 조각 하나당 재시도 횟수

    // 올릴 수 없는 형식 — 서버 BLOCKED_EXTENSIONS와 같게 유지한다(실행 · 스크립트 · 바로가기 · 웹페이지 · 매크로 문서 · 디스크 이미지).
    var BLOCKED_EXT = ('exe com bat cmd msi msp mst scr pif cpl dll sys drv ocx app apk deb rpm dmg pkg gadget application ' +
        'appref-ms msc ps1 psm1 psd1 vbs vbe js jse mjs wsf wsh wsc hta jar sh bash py pyw pl rb php asp aspx jsp cgi ' +
        'lnk url website scf reg inf ins isp html htm xhtml shtml mht mhtml svg chm ' +
        'docm dotm xlsm xltm xlam pptm potm ppam ppsm sldm iso img vhd vhdx').split(' ');

    function blockedExt(name) {
        var base = String(name || '').trim().toLowerCase().replace(/[. ]+$/, '');
        var dot = base.lastIndexOf('.');
        return dot > -1 && BLOCKED_EXT.indexOf(base.slice(dot + 1)) > -1;
    }

    function sleep(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

    function waitOnline() {
        if (navigator.onLine !== false) return Promise.resolve();
        return new Promise(function (resolve) {
            window.addEventListener('online', function once() {
                window.removeEventListener('online', once);
                resolve();
            });
        });
    }

    // 진행률·시간제한·취소가 필요한 요청은 XMLHttpRequest로 보낸다.
    function xhrJSON(method, url, body, opts) {
        opts = opts || {};
        return new Promise(function (resolve, reject) {
            var xhr = new XMLHttpRequest();
            xhr.open(method, url);
            xhr.withCredentials = true;
            xhr.timeout = opts.timeout || 60000;
            xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
            if (cfg && cfg.csrf) xhr.setRequestHeader('X-Portal-CSRF', cfg.csrf);
            if (opts.contentType) xhr.setRequestHeader('Content-Type', opts.contentType);
            if (opts.onProgress) {
                xhr.upload.onprogress = function (e) { if (e.lengthComputable) opts.onProgress(e.loaded, e.total); };
            }
            if (opts.onXhr) opts.onXhr(xhr);
            xhr.onload = function () {
                var data;
                try { data = JSON.parse(xhr.responseText); }
                catch (err) { data = { status: 'error', message: '서버 응답을 확인할 수 없습니다.' }; }
                if (xhr.status === 401 && data.login_url) location.href = data.login_url;
                if (xhr.status >= 200 && xhr.status < 300 && data.status === 'success') {
                    resolve(data);
                } else {
                    var failure = new Error(data.message || '전송하지 못했습니다.');
                    failure.status = xhr.status;
                    failure.data = data;
                    reject(failure);
                }
            };
            xhr.onerror = function () { var e = new Error('네트워크 연결이 끊겼습니다.'); e.network = true; reject(e); };
            xhr.ontimeout = function () { var e = new Error('응답이 늦어 전송하지 못했습니다.'); e.network = true; reject(e); };
            xhr.onabort = function () { var e = new Error('취소했습니다.'); e.aborted = true; reject(e); };
            xhr.send(body);
        });
    }

    var dropGuardInstalled = false;
    function installDropGuard() {
        // 첨부 칸을 살짝 벗어나 떨어뜨려도 브라우저가 파일을 열면서 화면을 떠나지 않게 한다.
        if (dropGuardInstalled) return;
        dropGuardInstalled = true;
        ['dragover', 'drop'].forEach(function (type) {
            window.addEventListener(type, function (e) {
                var inside = e.target && e.target.closest && e.target.closest('.bl-drop');
                if (!inside) e.preventDefault();
            });
        });
    }

    /* 끌어다 놓기 + [파일 선택] 첨부 칸.
     * 파일을 넣는 즉시 갤러리 카드로 보여주고, 2MB 조각으로 나눠 바로 전송한다.
     * 조각이 실패하면 최대 5번 다시 보내고, 끊긴 경우 서버가 받은 위치부터 이어서 보낸다. */
    function createDropZone(el, options) {
        options = options || {};
        installDropGuard();
        var items = [];
        var seq = 0;
        var depth = 0;
        var locked = false;
        var listeners = [];

        el.classList.add('bl-drop');
        if (options.compact) el.classList.add('compact');
        el.innerHTML =
            '<input type="file" multiple hidden>' +
            '<div class="bl-drop-hint"><i class="fa-solid fa-cloud-arrow-up"></i>' +
            '<span>파일을 여기로 끌어다 놓거나 <button type="button" class="bl-drop-pick">파일 선택</button></span>' +
            '<small>한 번에 10개, 파일당 30MB까지 · 실행파일 등 위험한 파일 제외 · 넣는 즉시 전송됩니다</small></div>' +
            '<div class="bl-drop-summary" hidden></div>' +
            '<div class="bl-drop-grid"></div>';
        var input = el.querySelector('input[type=file]');
        var grid = el.querySelector('.bl-drop-grid');
        var summary = el.querySelector('.bl-drop-summary');

        function base() { return options.uploadsUrl || (cfg && cfg.urls && cfg.urls.uploads); }
        function tokenUrl(it, suffix) { return base() + '/' + encodeURIComponent(it.token) + (suffix || ''); }

        function counts() {
            var c = { total: items.length, waiting: 0, uploading: 0, done: 0, error: 0 };
            items.forEach(function (it) { c[it.state] += 1; });
            return c;
        }

        function notify() {
            var c = counts();
            el.classList.toggle('has-files', c.total > 0);
            summary.hidden = !c.total;
            if (c.total) {
                var parts = ['파일 ' + c.total + '개'];
                if (c.waiting + c.uploading) parts.push('전송 중 ' + (c.waiting + c.uploading));
                if (c.done) parts.push('완료 ' + c.done);
                if (c.error) parts.push('실패 ' + c.error);
                summary.textContent = parts.join(' · ');
            }
            listeners.forEach(function (fn) { fn(c); });
        }

        function paint(it) {
            if (!it.el) return;
            var pct = it.size ? Math.min(100, Math.floor(it.loaded * 100 / it.size)) : 0;
            if (it.state === 'done') pct = 100;
            it.el.className = 'bl-tile ' + it.state;
            it.el.querySelector('.bl-tile-bar i').style.width = pct + '%';
            var meta = it.el.querySelector('.bl-tile-meta');
            if (it.state === 'waiting') {
                meta.textContent = fmtSize(it.size) + ' · 대기';
            } else if (it.state === 'uploading') {
                meta.textContent = pct + '% · ' + fmtSize(it.loaded) + ' / ' + fmtSize(it.size) +
                    (it.retrying ? ' · 재시도 ' + it.retrying + '/' + CHUNK_RETRIES : '');
            } else if (it.state === 'done') {
                meta.textContent = fmtSize(it.size) + ' · 완료';
            } else {
                meta.textContent = it.error || '전송 실패';
            }
        }

        function add(fileList) {
            if (locked) return;
            // 이미 넣은 파일과 같은 파일은 조용히 건너뛴다.
            var incoming = Array.from(fileList || []).filter(function (f) {
                return !items.some(function (x) {
                    return x.name === f.name && x.size === f.size && x.file.lastModified === f.lastModified;
                });
            });
            if (!incoming.length) return;

            // 한도를 넘는 파일이 하나라도 있으면 일부만 잘려 들어가지 않도록 이번에 넣은 파일 전체를 받지 않는다.
            var problems = [];
            var total = items.length + incoming.length;
            if (total > MAX_FILES) {
                problems.push('· 파일 개수 초과: 한 번에 ' + MAX_FILES + '개까지 첨부할 수 있습니다.' +
                    (items.length ? ' (이미 첨부 ' + items.length + '개 + 이번 ' + incoming.length + '개 = ' + total + '개)'
                                  : ' (선택한 파일 ' + incoming.length + '개)'));
            }
            var oversized = incoming.filter(function (f) { return f.size > MAX_FILE_BYTES; });
            if (oversized.length) {
                problems.push('· 용량 초과(파일당 30MB): ' + oversized.length + '개\n' + oversized.map(function (f) {
                    return '   - ' + f.name + ' (' + fmtSize(f.size) + ')';
                }).join('\n'));
            }
            var blocked = incoming.filter(function (f) { return blockedExt(f.name); });
            if (blocked.length) {
                problems.push('· 보안상 올릴 수 없는 파일 형식: ' + blocked.length + '개\n' + blocked.map(function (f) {
                    return '   - ' + f.name;
                }).join('\n') + '\n   (실행파일 · 스크립트 · 매크로 문서 · 웹페이지 파일 등)');
            }
            if (problems.length) {
                alert('첨부할 수 없습니다. 아래 내용을 확인한 뒤 다시 첨부해 주세요.\n\n' + problems.join('\n\n') +
                    '\n\n이번에 넣은 파일 ' + incoming.length + '개는 모두 첨부되지 않았습니다.');
                return;
            }

            incoming.forEach(function (f) {
                var it = { file: f, name: f.name, size: f.size, loaded: 0, received: 0, state: 'waiting',
                           token: null, chunkSize: 0, xhr: null, cancelled: false, retrying: 0, error: '' };
                it.id = ++seq;
                it.el = document.createElement('div');
                it.el.innerHTML =
                    '<div class="bl-tile-name" title="' + esc(f.name) + '">' + esc(f.name) + '</div>' +
                    '<div class="bl-tile-meta"></div>' +
                    '<div class="bl-tile-bar"><i></i></div>' +
                    '<button type="button" class="bl-tile-remove" aria-label="첨부 빼기"><i class="fa-solid fa-xmark"></i></button>' +
                    '<button type="button" class="bl-tile-retry"><i class="fa-solid fa-rotate-right"></i> 다시 시도</button>';
                it.el.querySelector('.bl-tile-remove').onclick = function () { remove(it); };
                it.el.querySelector('.bl-tile-retry').onclick = function () {
                    it.state = 'waiting';
                    it.error = '';
                    paint(it);
                    pump();
                };
                grid.appendChild(it.el);
                items.push(it);
                paint(it);
            });
            notify();
            pump();
        }

        function discard(it, cancelOnServer) {
            it.cancelled = true;
            if (it.xhr) { try { it.xhr.abort(); } catch (e) { /* 이미 끝난 요청 */ } }
            if (cancelOnServer && it.token && base()) {
                xhrJSON('POST', tokenUrl(it, '/cancel'), null, { timeout: 15000 }).catch(function () {});
            }
            if (it.el) it.el.remove();
        }

        function remove(it) {
            if (locked) return;
            discard(it, true);
            items = items.filter(function (x) { return x !== it; });
            notify();
            pump();
        }

        function pump() {
            var active = items.filter(function (x) { return x.state === 'uploading'; }).length;
            items.forEach(function (it) {
                if (active < MAX_PARALLEL && it.state === 'waiting' && !it.cancelled) {
                    active += 1;
                    transfer(it);
                }
            });
            notify();
        }

        async function syncFromServer(it) {
            var status = await xhrJSON('GET', tokenUrl(it), null, { timeout: 20000 });
            it.received = status.received;
            it.loaded = status.received;
            return status;
        }

        async function transfer(it) {
            it.state = 'uploading';
            it.retrying = 0;
            paint(it);
            try {
                if (!it.token) {
                    var init = await xhrJSON('POST', base(), JSON.stringify({ name: it.name, size: it.size }),
                        { contentType: 'application/json', timeout: 20000 });
                    it.token = init.upload_id;
                    it.chunkSize = init.chunk_size || 2 * 1024 * 1024;
                    it.received = 0;
                } else {
                    await syncFromServer(it);     // [다시 시도]는 서버가 받은 곳부터 이어서 보낸다
                }
                while (!it.cancelled && it.received < it.size) {
                    var from = it.received;
                    var to = Math.min(it.size, from + (it.chunkSize || 2 * 1024 * 1024));
                    var attempt = 0;
                    for (;;) {
                        if (it.cancelled) return;
                        try {
                            var res = await xhrJSON('POST', tokenUrl(it, '/chunk?offset=' + from), it.file.slice(from, to), {
                                contentType: 'application/octet-stream',
                                timeout: 90000,
                                onXhr: function (x) { it.xhr = x; },
                                onProgress: function (sent) { it.loaded = from + sent; paint(it); }
                            });
                            it.received = res.received;
                            it.loaded = res.received;
                            it.retrying = 0;
                            paint(it);
                            break;
                        } catch (err) {
                            if (it.cancelled || err.aborted) return;
                            if (err.status === 409 && err.data && typeof err.data.received === 'number') {
                                it.received = err.data.received;   // 서버 기준 위치로 맞춰 이어서 보낸다
                                it.loaded = it.received;
                                break;
                            }
                            var retriable = err.network || !err.status || err.status >= 500 || err.status === 429;
                            attempt += 1;
                            if (!retriable || attempt > CHUNK_RETRIES) throw err;
                            it.retrying = attempt;
                            it.loaded = from;
                            paint(it);
                            await waitOnline();
                            await sleep(Math.min(8000, 1000 * Math.pow(2, attempt - 1)));
                            try {
                                await syncFromServer(it);
                                if (it.received !== from) break;   // 응답만 못 받았고 서버엔 저장된 경우
                            } catch (syncErr) { /* 다음 재시도에서 다시 확인한다 */ }
                        }
                    }
                }
                if (it.cancelled) return;
                it.state = 'done';
                it.loaded = it.size;
            } catch (err) {
                if (it.cancelled) return;
                it.state = 'error';
                it.error = err.message || '전송 실패';
            } finally {
                it.xhr = null;
                it.retrying = 0;
                if (!it.cancelled) paint(it);
                pump();
            }
        }

        el.querySelector('.bl-drop-pick').onclick = function () { if (!locked) input.click(); };
        input.onchange = function () { add(input.files); input.value = ''; };
        el.addEventListener('dragenter', function (e) { e.preventDefault(); depth += 1; el.classList.add('over'); });
        el.addEventListener('dragover', function (e) {
            e.preventDefault();
            if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
        });
        el.addEventListener('dragleave', function () {
            depth = Math.max(0, depth - 1);
            if (!depth) el.classList.remove('over');
        });
        el.addEventListener('drop', function (e) {
            e.preventDefault();
            depth = 0;
            el.classList.remove('over');
            add(e.dataTransfer && e.dataTransfer.files);
        });

        var api = {
            add: add,
            count: function () { return items.length; },
            pending: function () { var c = counts(); return c.waiting + c.uploading; },
            errors: function () { return counts().error; },
            uploadIds: function () {
                return items.filter(function (x) { return x.state === 'done'; }).map(function (x) { return x.token; });
            },
            onChange: function (fn) { listeners.push(fn); fn(counts()); },
            setBusy: function (on) { locked = !!on; el.classList.toggle('busy', locked); },
            // keepServer: 등록이 끝나 서버가 파일을 가져간 경우 — 화면만 비운다
            clear: function (opts) {
                var keep = opts && opts.keepServer;
                items.forEach(function (it) { discard(it, !keep); });
                items = [];
                notify();
            }
        };
        return api;
    }

    /* ------------------------------------------------------------ 써머리 */
    function renderSummary(data, activeStage) {
        var el = cfg.summaryEl;
        if (!el) return;
        var labels = data.stage_labels || {};
        var cards = [
            { key: '', label: '전체 업무', count: data.total, dot: '#1f3a5f' }
        ].concat(STAGES.map(function (key) {
            return { key: key, label: labels[key] || key, count: data.by_stage[key] || 0, dot: STAGE_COLOR[key] };
        }));
        var html = '<div class="bl-summary">' + cards.map(function (card) {
            return '<button type="button" class="bl-stat' + (card.key === (activeStage || '') ? ' active' : '') +
                '" data-stage="' + card.key + '" style="--dot:' + card.dot + '">' +
                '<small>' + esc(card.label) + '</small><b>' + card.count + '<span>건</span></b></button>';
        }).join('') + '</div>';
        html += '<div class="bl-progressbar"><div class="bar"><i style="width:' + (data.completion_rate || 0) +
            '%"></i></div><strong>완료율 ' + (data.completion_rate || 0) + '% · 진행중 ' + (data.open || 0) + '건</strong></div>';
        if (data.cancel_requests) {
            var awaitingMe = cfg.viewer === 'staff' ? (data.cancel_by_member || 0) : (data.cancel_by_staff || 0);
            var otherLabel = cfg.viewer === 'staff' ? '학교' : '본사';
            html += '<div class="bl-summary-alert"><button type="button" class="bl-cancel-chip" data-stage="cancel">' +
                '<i class="fa-solid fa-ban"></i> 취소요청 ' + data.cancel_requests + '건' +
                (awaitingMe ? ' · 처리 필요 ' + awaitingMe + '건' : ' · ' + otherLabel + ' 확인 대기') + '</button></div>';
        }
        html += '<div class="bl-typerow">' + Object.keys(data.by_type || {}).map(function (key) {
            var t = data.by_type[key];
            return '<div class="bl-typecell">' + esc(t.label) + '<span>완료 ' + t.done + ' / 전체 ' + t.total + '</span></div>';
        }).join('') + '</div>';
        if (cfg.viewer === 'staff' && data.queue) {
            var q = data.queue;
            // 마지막 값이 false인 칸(가입 승인 · 접속 허용 요청)은 건수가 있을 때만 보인다.
            html = '<div class="bl-queue">' + [
                ['todo', 'fa-hand-point-right', '본사 처리 차례', q.todo, true],
                ['unread', 'fa-bell', '새 소식', q.unread, true],
                ['overdue', 'fa-hourglass-end', '처리 지연', q.overdue, true],
                ['unassigned', 'fa-user-slash', '담당자 미지정', q.unassigned, true],
                ['mine', 'fa-user-check', '내 담당 진행중', q.mine, true],
                ['pending', 'fa-user-clock', '가입 승인 대기', q.pending, false],
                ['unlock', 'fa-unlock-keyhole', '접속 허용 요청', q.unlock_requests, false]
            ].filter(function (item) { return item[4] || item[3]; }).map(function (item) {
                return '<button type="button" class="bl-queue-chip ' + item[0] + (item[3] ? ' on' : '') +
                    '" data-stage="' + item[0] + '"><i class="fa-solid ' + item[1] + '"></i>' + item[2] +
                    '<b>' + (item[3] || 0) + '</b></button>';
            }).join('') + '</div>' + html;
        }
        el.innerHTML = html;
        el.querySelectorAll('.bl-stat, .bl-cancel-chip, .bl-queue-chip').forEach(function (btn) {
            btn.onclick = function () { if (cfg.onStatClick) cfg.onStatClick(btn.dataset.stage); };
        });
        if (cfg.onSummary) cfg.onSummary(data);
    }

    /* ------------------------------------------------------------ 목록 */
    function miniStepper(stageIndex, stage) {
        var color = STAGE_COLOR[stage] || '#64748b';
        var html = '<div class="bl-mini" style="--c:' + color + '" title="' + (stageIndex + 1) + ' / 4 단계">';
        for (var i = 0; i < 4; i++) {
            if (i > 0) html += '<em class="' + (i <= stageIndex ? 'on' : '') + '"></em>';
            html += '<i class="' + (i <= stageIndex ? 'on' : '') + '"></i>';
        }
        return html + '</div>';
    }

    function renderList(tasks) {
        var el = cfg.listEl;
        lastTasks = tasks;
        if (cfg.viewer === 'staff') { renderStaffList(tasks); return; }
        if (!tasks.length) {
            el.innerHTML = '<div class="bl-empty"><i class="fa-regular fa-folder-open"></i>조건에 맞는 업무가 없습니다.</div>';
            return;
        }
        el.innerHTML = '<div class="bl-list">' + tasks.map(function (t) {
            var sub = cfg.viewer === 'staff'
                ? esc(t.school_name) + ' · ' + esc(t.member_name) + (t.assignee_name ? ' · 담당 ' + esc(t.assignee_name) : '')
                : (t.assignee_name ? '본사 담당 ' + esc(t.assignee_name) : '본사 접수 대기');
            if (t.message_count || t.file_count) {
                sub += ' · 글 ' + t.message_count + ' · 파일 ' + t.file_count;
            }
            var age = taskAgeHtml(t);
            if (age) sub += ' · ' + age;
            return '<div class="bl-row' + (t.cancel_requested ? ' cancel-req' : '') + '" data-id="' + t.id + '">' +
                '<span class="bl-type"><i class="fa-solid ' + esc(t.type_icon) + '"></i>' + esc(t.type_label) + '</span>' +
                '<span class="bl-row-title"><b>' + priorityBadge(t) + lateBadge(t) +
                    (t.cancel_requested ? '<span class="bl-cancel-badge" title="' +
                        (t.cancel_requested_side === 'staff' ? '본사' : '학교') + '에서 요청">취소요청</span>' : '') +
                    esc(t.title) + '</b><small>' + sub + '</small></span>' +
                miniStepper(t.stage_index, t.stage) +
                '<span class="bl-stage ' + esc(t.stage) + '">' + esc(t.stage_label) + '</span>' +
                '<span class="bl-row-time">' + esc(shortTime(t.updated_at)) + '</span>' +
                '</div>';
        }).join('') + '</div>';
        el.querySelectorAll('.bl-row').forEach(function (row) {
            row.onclick = function () { openTask(Number(row.dataset.id)); };
        });
    }

    /* ------------------------------------------------------------ 본사 목록 — 여러 학교 동시 처리 */
    // 날짜 차이는 달력 날짜로 센다(어제 밤에 올린 업무는 오늘 '1일째').
    function localDay(text) {
        var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(text || ''));
        return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
    }

    function today0() {
        var d = new Date();
        return new Date(d.getFullYear(), d.getMonth(), d.getDate());
    }

    function daysSince(text) {
        var d = localDay(text);
        return d ? Math.max(0, Math.round((today0() - d) / 86400000)) : null;
    }

    function daysUntil(text) {
        var d = localDay(text);
        return d ? Math.round((d - today0()) / 86400000) : null;
    }

    function dueText(dueDate) {
        var left = daysUntil(dueDate);
        if (left === null) return '';
        return left < 0 ? (-left) + '일 지남' : left === 0 ? '오늘까지' : 'D-' + left;
    }

    // 업무를 올린 지 며칠째인지 · 처리 희망일이 얼마나 남았는지 (완료된 업무는 표시하지 않는다)
    function taskAgeHtml(t) {
        if (t.stage === 'done') return '';
        var html = '';
        var days = daysSince(t.created_at);
        if (days !== null) {
            html += '<em class="bl-age' + (t.overdue ? ' late' : '') + '"><i class="fa-regular fa-clock"></i> ' +
                (days ? '등록 ' + days + '일째' : '오늘 등록') + '</em>';
        }
        if (t.due_date && !t.priority) {   // 처리 요청 선택 전에 '희망일'로 받은 업무
            var left = daysUntil(t.due_date);
            html += (html ? ' · ' : '') + '<em class="bl-due' + (left < 0 ? ' late' : left <= 1 ? ' soon' : '') + '">' +
                '<i class="fa-regular fa-calendar"></i> 희망일 ' + esc(String(t.due_date).slice(5).replace('-', '/')) +
                ' (' + dueText(t.due_date) + ')</em>';
        }
        return html;
    }

    // 학교가 고른 처리 요청(긴급 · 금일 이내)을 제목 앞에 표시한다. 순차적은 표시하지 않는다.
    function priorityBadge(t) {
        if (t.stage === 'done') return '';
        if (t.priority === 'urgent') return '<span class="bl-prio urgent"><i class="fa-solid fa-bolt"></i>긴급</span>';
        if (t.priority === 'today') return '<span class="bl-prio today"><i class="fa-solid fa-calendar-day"></i>금일 이내</span>';
        return '';
    }

    function lateBadge(t) {
        return t.overdue ? '<span class="bl-late-badge" title="금일 이내 · 긴급 요청의 기한이 지났거나 오래 걸리고 있는 업무">처리 지연</span>' : '';
    }

    function taskPosition(taskId) {
        for (var i = 0; i < lastTasks.length; i++) {
            if (lastTasks[i].id === taskId) return i;
        }
        return -1;
    }

    function neighborTask(taskId, step) {
        var pos = taskPosition(taskId);
        return pos > -1 ? (lastTasks[pos + step] || null) : null;
    }

    function staffRowHtml(t) {
        var sub = [t.assignee_name ? '담당 ' + esc(t.assignee_name) : '<span class="bl-unassigned">담당자 미지정</span>'];
        if (t.message_count || t.file_count) sub.push('글 ' + t.message_count + ' · 파일 ' + t.file_count);
        var age = taskAgeHtml(t);
        if (age) sub.push(age);
        return '<div class="bl-row staff' + (t.cancel_requested ? ' cancel-req' : '') + (t.unread ? ' unread' : '') +
            '" data-id="' + t.id + '" style="--sc:' + schoolColor(t.member_id) + '">' +
            '<label class="bl-row-check" title="여러 업무를 한 번에 처리하려면 체크하세요"><input type="checkbox"></label>' +
            '<span class="bl-school" title="' + esc(t.school_name + ' · ' + t.member_name) + '"><b>' + esc(t.school_name) +
                '</b><small>' + esc(t.member_name) + (t.member_status === 'replaced' ? ' · 교체된 담당자' : '') + '</small></span>' +
            '<span class="bl-type"><i class="fa-solid ' + esc(t.type_icon) + '"></i>' + esc(t.type_label) + '</span>' +
            '<span class="bl-row-title"><b>' +
                priorityBadge(t) + (t.unread ? '<span class="bl-new-badge">새 소식</span>' : '') + lateBadge(t) +
                (t.cancel_requested ? '<span class="bl-cancel-badge" title="' +
                    (t.cancel_requested_side === 'staff' ? '본사' : '학교') + '에서 요청">취소요청</span>' : '') +
                esc(t.title) + '</b><small>' + sub.join(' · ') + '</small></span>' +
            miniStepper(t.stage_index, t.stage) +
            '<span class="bl-stage ' + esc(t.stage) + '">' + esc(t.stage_label) + '</span>' +
            '<span class="bl-row-time">' + esc(shortTime(t.updated_at)) + '</span>' +
            '</div>';
    }

    function renderStaffList(tasks) {
        var el = cfg.listEl;
        var present = {};
        tasks.forEach(function (t) { present[t.id] = true; });
        Object.keys(selected).forEach(function (id) { if (!present[id]) delete selected[id]; });
        if (!tasks.length) {
            el.innerHTML = '<div class="bl-empty"><i class="fa-regular fa-folder-open"></i>조건에 맞는 업무가 없습니다.</div>';
            return;
        }
        el.innerHTML =
            '<div class="bl-listhead">' +
                '<label class="bl-check"><input type="checkbox" class="bl-select-all">전체 선택</label>' +
                '<span>' + tasks.length + '건</span>' +
                '<small>여러 학교 업무를 체크하면 단계 넘기기 · 같은 안내글 등록 · 담당 지정을 한 번에 할 수 있습니다.</small>' +
            '</div>' +
            '<div class="bl-bulk" hidden>' +
                '<b class="bl-bulk-count"></b><span class="bl-bulk-schools"></span>' +
                '<div class="bl-bulk-actions">' +
                    '<label class="bl-check"><input type="checkbox" class="bl-bulk-notify" checked>학교 담당자에게 문자 알림</label>' +
                    '<button type="button" class="bl-btn sm primary" data-bulk="advance"><i class="fa-solid fa-forward-step"></i>다음 단계로 넘기기</button>' +
                    '<button type="button" class="bl-btn sm" data-bulk="message"><i class="fa-solid fa-bullhorn"></i>같은 안내글 등록</button>' +
                    '<button type="button" class="bl-btn sm" data-bulk="assign"><i class="fa-solid fa-user-check"></i>내 담당으로 지정</button>' +
                    '<button type="button" class="bl-btn sm" data-bulk="clear">선택 해제</button>' +
                '</div>' +
            '</div>' +
            '<div class="bl-list">' + tasks.map(staffRowHtml).join('') + '</div>';

        el.querySelectorAll('.bl-row').forEach(function (row) {
            row.onclick = function (e) {
                if (e.target.closest('.bl-row-check')) return;
                openTask(Number(row.dataset.id));
            };
            row.querySelector('.bl-row-check input').onchange = function () {
                if (this.checked) selected[row.dataset.id] = true;
                else delete selected[row.dataset.id];
                syncSelection();
            };
        });
        el.querySelector('.bl-select-all').onchange = function () {
            var on = this.checked;
            tasks.forEach(function (t) { if (on) selected[t.id] = true; else delete selected[t.id]; });
            syncSelection();
        };
        el.querySelectorAll('[data-bulk]').forEach(function (btn) {
            btn.onclick = function () { runBulk(btn.dataset.bulk); };
        });
        syncSelection();
    }

    function selectedTasks() {
        return lastTasks.filter(function (t) { return selected[t.id]; });
    }

    function syncSelection() {
        var el = cfg.listEl;
        var picked = selectedTasks();
        el.querySelectorAll('.bl-row').forEach(function (row) {
            var on = !!selected[row.dataset.id];
            row.classList.toggle('selected', on);
            row.querySelector('.bl-row-check input').checked = on;
        });
        var all = el.querySelector('.bl-select-all');
        if (all) {
            all.checked = picked.length > 0 && picked.length === lastTasks.length;
            all.indeterminate = picked.length > 0 && picked.length < lastTasks.length;
        }
        var bar = el.querySelector('.bl-bulk');
        if (!bar) return;
        bar.hidden = !picked.length;
        if (!picked.length) return;
        var schools = {};
        picked.forEach(function (t) { schools[t.school_name] = (schools[t.school_name] || 0) + 1; });
        var names = Object.keys(schools);
        bar.querySelector('.bl-bulk-count').textContent = picked.length + '건 선택 · ' + names.length + '개 학교';
        bar.querySelector('.bl-bulk-schools').textContent = names.map(function (n) { return n + ' ' + schools[n] + '건'; }).join(', ');
    }

    function targetLines(list) {
        return list.map(function (t, i) {
            return (i + 1) + '. ' + t.school_name + ' · ' + t.member_name + ' — ' + t.title;
        }).join('\n');
    }

    function askBulkMessage(list, skipped) {
        return new Promise(function (resolve) {
            if (!bulkDialog) {
                bulkDialog = document.createElement('div');
                bulkDialog.className = 'bl-modal bl-scope bl-dialog';
                bulkDialog.innerHTML = '<div class="bl-dialog-box">' +
                    '<h3><i class="fa-solid fa-bullhorn"></i> 여러 학교에 같은 안내글 등록</h3>' +
                    '<p class="bl-help bl-dialog-desc"></p>' +
                    '<div class="bl-dialog-targets"></div>' +
                    '<textarea class="bl-input" maxlength="5000" placeholder="예: 9월 강사료 청구 서류를 확인했습니다. 계산서를 보내 주세요."></textarea>' +
                    '<div class="bl-dialog-actions"><button type="button" class="bl-btn" data-act="cancel">취소</button>' +
                    '<button type="button" class="bl-btn primary" data-act="ok"><i class="fa-solid fa-paper-plane"></i>등록</button></div></div>';
                document.body.appendChild(bulkDialog);
            }
            var area = bulkDialog.querySelector('textarea');
            area.value = '';
            bulkDialog.querySelector('.bl-dialog-desc').textContent =
                '아래 ' + list.length + '건의 업무마다 같은 글이 각각 등록됩니다. 학교마다 다른 문서는 업무를 열어 따로 첨부해 주세요.' +
                (skipped ? ' (완료된 업무 ' + skipped + '건 제외)' : '');
            bulkDialog.querySelector('.bl-dialog-targets').innerHTML = list.map(function (t) {
                return '<div style="--sc:' + schoolColor(t.member_id) + '"><b>' + esc(t.school_name) + '</b> ' + esc(t.member_name) +
                    '<span>' + esc(t.title) + '</span></div>';
            }).join('');
            function finish(value) { bulkDialog.classList.remove('open'); resolve(value); }
            bulkDialog.querySelector('[data-act=cancel]').onclick = function () { finish(null); };
            bulkDialog.querySelector('[data-act=ok]').onclick = function () {
                var body = area.value.trim();
                if (!body) { toast('등록할 내용을 입력해 주세요.'); area.focus(); return; }
                finish(body);
            };
            bulkDialog.classList.add('open');
            setTimeout(function () { area.focus(); }, 30);
        });
    }

    async function runBulk(action) {
        if (action === 'clear') { selected = {}; syncSelection(); return; }
        var picked = selectedTasks();
        if (!picked.length) return;
        var open = picked.filter(function (t) { return t.stage !== 'done'; });
        var skipped = picked.length - open.length;
        var skipNote = skipped ? '\n\n※ 완료된 업무 ' + skipped + '건은 제외합니다.' : '';
        var ids = function (list) { return list.map(function (t) { return t.id; }); };
        var payload = { action: action };

        if (action === 'advance') {
            var movable = open.filter(function (t) {
                return !t.cancel_requested && t.member_status !== 'replaced' && (t.next_actors || []).indexOf('staff') > -1;
            });
            if (!movable.length) { toast('선택한 업무 중 본사에서 넘길 수 있는 업무가 없습니다.'); return; }
            var byStage = {};
            movable.forEach(function (t) { (byStage[t.stage] = byStage[t.stage] || []).push(t); });
            var stages = Object.keys(byStage);
            if (stages.length > 1) {
                alert('서로 다른 단계의 업무가 섞여 있어 한 번에 넘길 수 없습니다.\n같은 단계의 업무만 선택해 주세요.\n\n' +
                    stages.map(function (k) { return '· [' + byStage[k][0].stage_label + '] ' + byStage[k].length + '건'; }).join('\n'));
                return;
            }
            var first = movable[0];
            var blocked = open.length - movable.length;
            var notify = cfg.listEl.querySelector('.bl-bulk-notify').checked;
            if (!confirm('아래 ' + movable.length + '건을 [' + first.stage_label + '] → [' + first.next_stage_label + '] 단계로 넘깁니다.\n\n' +
                    targetLines(movable) + skipNote +
                    (blocked ? '\n※ 취소요청 중인 업무 ' + blocked + '건은 제외합니다.' : '') +
                    (notify ? '\n\n학교 담당자마다 문자 알림을 보냅니다.' : '') + '\n\n계속할까요?')) return;
            payload.ids = ids(movable);
            payload.from_stage = first.stage;
            payload.notify = notify;
        } else {
            if (!open.length) { toast('완료되지 않은 업무를 선택해 주세요.'); return; }
            if (action === 'assign') {
                if (!confirm('아래 ' + open.length + '건의 본사 담당자를 나로 지정할까요?\n\n' + targetLines(open) + skipNote)) return;
            } else {
                var body = await askBulkMessage(open, skipped);
                if (body === null) return;
                payload.body = body;
            }
            payload.ids = ids(open);
        }

        cfg.listEl.querySelectorAll('[data-bulk]').forEach(function (b) { b.disabled = true; });
        try {
            var result = await postJSON(cfg.urls.bulk, payload);
            var lines = (result.results || []).map(function (r) {
                return (r.ok ? '✔ ' : '✖ ') + (r.school_name || '') + ' — ' + (r.title || '#' + r.id) + ' : ' + r.message;
            });
            alert(result.message + '\n\n' + lines.join('\n'));
            selected = {};
        } catch (e) {
            toast(e.message);
        }
        await refresh();
    }

    async function refresh(opts) {
        var quiet = !!(opts && opts.quiet);
        var filters = cfg.getFilters ? cfg.getFilters() : {};
        var qs = new URLSearchParams(filters).toString();
        try {
            var results = await Promise.all([
                request(cfg.urls.summary),
                request(cfg.urls.tasks + (qs ? '?' + qs : ''))
            ]);
            renderSummary(results[0], filters.stage || '');
            renderList(results[1].tasks || []);
        } catch (e) {
            if (!quiet) toast(e.message);   // 자동 새로고침 실패는 조용히 넘기고 다음에 다시 시도한다
        }
    }

    /* ------------------------------------------------------------ 상세 모달 */
    function ensureModal() {
        if (modal) return modal;
        modal = document.createElement('div');
        modal.className = 'bl-modal bl-scope';
        modal.innerHTML = '<div class="bl-modal-box"><button type="button" class="bl-modal-close" aria-label="닫기">' +
            '<i class="fa-solid fa-xmark"></i></button><div class="bl-modal-inner" style="display:contents"></div></div>';
        // 본문의 쌓임 맥락에 갇히지 않도록 body 바로 밑에 붙인다.
        document.body.appendChild(modal);
        modal.querySelector('.bl-modal-close').onclick = closeTask;
        // 바깥을 클릭하거나 드래그해도 닫히지 않는다(쓰던 글·첨부를 잃지 않게). X 또는 Esc로만 닫는다.
        document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeTask(); });
        return modal;
    }

    function closeTask() {
        if (!modal || uploading) return;
        if (composerZone && composerZone.count() &&
                !confirm('첨부한 파일이 아직 등록되지 않았습니다. 닫으면 첨부가 취소됩니다. 닫을까요?')) return;
        if (composerZone) { composerZone.clear(); composerZone = null; }
        modal.classList.remove('open');
        currentTaskId = null;
        if (openedUnread) { openedUnread = false; refresh({ quiet: true }); }   // [새 소식] 표시를 지운다
    }

    function stepperHtml(task, times) {
        return '<div class="bl-stepper">' + STAGES.map(function (key, index) {
            var reached = index <= task.stage_index;
            var current = index === task.stage_index;
            return '<div class="bl-step' + (reached ? ' reached' : '') + (current ? ' current' : '') +
                '" style="--c:' + STAGE_COLOR[key] + '">' +
                '<div class="bl-step-dot">' + (reached && !current ? '<i class="fa-solid fa-check"></i>' : (index + 1)) + '</div>' +
                '<div class="bl-step-label">' + ['시작', '진행', '확인', '완료'][index] + '</div>' +
                '<div class="bl-step-time">' + esc(shortTime(times[key])) + '</div></div>';
        }).join('') + '</div>';
    }

    function actionHtml(task) {
        if (task.stage === 'done') {
            return '<div class="bl-action done"><p><i class="fa-solid fa-circle-check" style="color:#16a34a"></i> ' +
                '완료된 업무입니다.<small>완료 ' + esc(shortTime(task.completed_at)) + ' · 이 업무는 더 이상 수정되지 않습니다.</small></p></div>';
        }
        if (task.cancel_requested) {
            return '<div class="bl-action"><p>다음 단계: [' + esc(task.next_stage_label) + ']' +
                '<small>취소 요청이 처리되기 전에는 단계를 넘길 수 없습니다.</small></p></div>';
        }
        var canAct = (task.next_actors || []).indexOf(cfg.viewer) > -1;
        var nextLabel = esc(task.next_stage_label);
        if (!canAct) {
            var who = cfg.viewer === 'staff' ? '학교' : '본사';
            return '<div class="bl-action"><p>다음 단계: [' + nextLabel + ']' +
                '<small>' + who + '에서 처리하면 다음 단계로 넘어갑니다. 필요한 내용은 아래에 글로 남겨 주세요.</small></p></div>';
        }
        var target = cfg.viewer === 'staff' ? '학교 담당자' : '본사 담당자';
        var hint = {
            progress: '업무를 접수하고 처리를 시작합니다.',
            review: '처리를 마치고 학교에 확인을 요청합니다. 필요한 문서를 먼저 올려 주세요.',
            done: '내용을 확인했으면 업무를 완료합니다. 완료하면 더 이상 수정할 수 없습니다.'
        }[task.next_stage] || '';
        return '<div class="bl-action"><p>다음 단계: [' + nextLabel + ']<small>' + hint + '</small></p>' +
            '<div class="bl-action-controls">' +
            '<label class="bl-check"><input type="checkbox" id="blNotify" checked>' + target + '에게 문자 알림</label>' +
            '<button type="button" class="bl-btn ' + (task.next_stage === 'done' ? 'success' : 'primary') +
            '" id="blAdvance"><i class="fa-solid fa-forward-step"></i>' + esc(task.next_action_label) + '</button>' +
            '</div></div>';
    }

    function threadHtml(messages, task) {
        if (!messages.length) {
            return '<div class="bl-empty" style="padding:24px"><i class="fa-regular fa-comments"></i>아직 주고받은 내용이 없습니다.</div>';
        }
        var mine = cfg.viewer === 'staff' ? 'staff' : 'member';
        return '<div class="bl-thread">' + messages.map(function (m) {
            if (m.author_type === 'system') {
                // 단계 변경 안내는 넘어간 단계의 색(요약 카드와 같은 색)으로 표시한다.
                var moved = /→ \[(.+?)\]/.exec(m.body || '');
                var toKey = moved && { '시작': 'start', '진행': 'progress', '확인': 'review', '완료': 'done' }[moved[1]];
                return '<div class="bl-sys"' + (toKey ? ' style="color:' + STAGE_COLOR[toKey] + '"' : '') +
                    '><i class="fa-solid fa-flag"></i> ' + esc(m.body) +
                    '<time>' + esc(shortTime(m.created_at)) + '</time></div>';
            }
            var side = m.author_type === 'staff' ? '본사' : (cfg.viewer === 'staff' && task ? task.school_name : '학교');
            var files = (m.files || []).length
                ? '<div class="bl-files">' + m.files.map(function (f) {
                    return '<a class="bl-file" href="' + esc(f.url) + '"><i class="fa-regular fa-file-lines"></i>' +
                        esc(f.original_name) + '<small>' + fmtSize(f.size) + '</small></a>';
                }).join('') + '</div>'
                : '';
            return '<div class="bl-msg' + (m.author_type === mine ? ' mine' : '') + '">' +
                '<div class="bl-msg-head"><b>' + side + ' · ' + esc(m.author_name) + '</b>' + esc(shortTime(m.created_at)) + '</div>' +
                (m.body ? '<div class="bl-msg-bubble">' + esc(m.body) + '</div>' : '') + files + '</div>';
        }).join('') + '</div>';
    }

    function renderDetail(data) {
        if (composerZone) { composerZone.clear(); composerZone = null; }
        lastDetail = data;
        var task = data.task;
        var box = ensureModal().querySelector('.bl-modal-inner');
        var staffView = cfg.viewer === 'staff';
        var schoolTint = staffView ? schoolColor(task.member_id) : '';
        modal.querySelector('.bl-modal-box').style.borderTop = staffView ? '5px solid ' + schoolTint : '';
        // 본사는 여러 학교를 상대하므로 확인창마다 어느 학교 업무인지 먼저 보여준다.
        var whoLine = staffView ? '[' + task.school_name + ' · ' + task.member_name + ']\n"' + task.title + '"\n\n' : '';
        var schoolBanner = '';
        if (staffView) {
            var pos = taskPosition(task.id);
            schoolBanner = '<div class="bl-school-banner" style="--sc:' + schoolTint + '">' +
                '<i class="fa-solid fa-school"></i>' +
                '<div class="bl-school-banner-text"><b>' + esc(task.school_name) + '</b>' +
                    '<small>학교 담당자 ' + esc(task.member_name) + ' · ' + esc(task.member_phone_display || task.member_phone || '-') + '</small></div>' +
                (cfg.onSchoolFilter ? '<button type="button" class="bl-btn sm" id="blSchoolOnly"><i class="fa-solid fa-filter"></i>이 학교 업무만</button>' : '') +
                (pos > -1
                    ? '<div class="bl-nav"><button type="button" class="bl-btn sm" id="blPrev"' + (pos === 0 ? ' disabled' : '') + '>' +
                        '<i class="fa-solid fa-chevron-left"></i>이전</button><span>' + (pos + 1) + ' / ' + lastTasks.length + '</span>' +
                        '<button type="button" class="bl-btn sm" id="blNext"' + (pos === lastTasks.length - 1 ? ' disabled' : '') + '>' +
                        '다음<i class="fa-solid fa-chevron-right"></i></button></div>'
                    : '') +
                '</div>';
        }
        var meta = [];
        if (cfg.viewer === 'staff') {
            meta.push(['학교', task.school_name], ['학교 담당자', task.member_name],
                ['휴대폰', task.member_phone_display || task.member_phone], ['전화', task.member_tel || '-'], ['이메일', task.member_email]);
        }
        if (task.task_type !== 'data_request') meta.push(['청구금액', task.amount || '-']);
        var age = task.stage !== 'done' ? daysSince(task.created_at) : null;
        meta.push(['본사 담당', task.assignee_name || '접수 대기'],
            ['시작일', shortTime(task.created_at) + (age === null ? '' : age ? ' (' + age + '일째)' : ' (오늘)')]);
        if (task.priority_label) {
            meta.push(['처리 요청', task.priority_label]);
        } else if (task.due_date) {
            meta.push(['처리 희망일', task.due_date + (task.stage !== 'done' ? ' (' + dueText(task.due_date) + ')' : '')]);
        }

        var footer = '';
        // 담당자가 교체된 학교의 업무: 전 담당자 계정은 정지 — 본사가 바로 취소만 할 수 있다.
        var orphan = staffView && task.member_status === 'replaced' && task.stage !== 'done';
        var canRequestCancel = task.stage !== 'done' && !task.cancel_requested && cfg.urls.cancelRequest && !orphan;
        if (cfg.viewer === 'staff') {
            if (canRequestCancel) {
                footer += '<button type="button" class="bl-btn sm danger" id="blCancelRequest"><i class="fa-solid fa-ban"></i>업무 취소 요청</button> ';
            }
            if (cfg.urls.remove) {
                footer += '<button type="button" class="bl-btn sm danger" id="blRemove"><i class="fa-regular fa-trash-can"></i>업무 삭제</button>';
            }
        } else if (canRequestCancel) {
            footer = '<button type="button" class="bl-btn sm danger" id="blCancelRequest"><i class="fa-solid fa-ban"></i>업무 취소</button>';
        }

        var cancelBox = '';
        if (task.cancel_requested) {
            var requesterSide = task.cancel_requested_side || 'member';
            var requesterLabel = requesterSide === 'staff' ? '본사' : '학교';
            var otherSideLabel = cfg.viewer === 'staff' ? '학교' : '본사';
            var info = '요청 ' + esc(shortTime(task.cancel_requested_at)) + ' · ' + esc(task.cancel_requested_by || requesterLabel) +
                (task.cancel_reason ? ' · 사유: ' + esc(task.cancel_reason) : '');
            if (requesterSide === cfg.viewer) {
                cancelBox = '<div class="bl-cancel-box"><p><i class="fa-solid fa-hourglass-half"></i> 업무 취소를 요청했습니다.' +
                    '<small>' + info + '<br>' + otherSideLabel + '에서 승인하면 업무와 첨부파일이 삭제됩니다.</small></p>' +
                    '<div class="bl-action-controls">' +
                    '<button type="button" class="bl-btn sm" id="blCancelWithdraw">요청 철회</button>' +
                    '</div></div>';
            } else {
                var approveLabel = cfg.viewer === 'staff' ? '요청 확인' : '승인';
                cancelBox = '<div class="bl-cancel-box"><p><i class="fa-solid fa-ban"></i> ' + requesterLabel + '에서 업무 취소를 요청했습니다.' +
                    '<small>' + info + '<br>[' + approveLabel + ']을 누르면 이 업무와 첨부파일이 모두 삭제됩니다.</small></p>' +
                    '<div class="bl-action-controls">' +
                    '<button type="button" class="bl-btn sm" id="blCancelReject">반려</button>' +
                    '<button type="button" class="bl-btn sm danger" id="blCancelApprove"><i class="fa-solid fa-check"></i>' +
                    approveLabel + ' · 삭제</button>' +
                    '</div></div>';
            }
        }
        if (orphan) {
            cancelBox = '<div class="bl-cancel-box"><p><i class="fa-solid fa-user-slash"></i> 학교 담당자가 교체된 업무입니다.' +
                '<small>' + esc(task.member_name) + ' 선생님 계정은 정지되었고, 이 업무와 대화 내용은 새 담당자에게 보이지 않습니다.<br>' +
                '더 진행할 수 없으니 [업무 취소]로 정리해 주세요. 취소하면 업무와 첨부파일이 삭제됩니다.</small></p>' +
                '<div class="bl-action-controls"><button type="button" class="bl-btn sm danger" id="blReplacedCancel">' +
                '<i class="fa-solid fa-ban"></i>업무 취소 · 삭제</button></div></div>';
        }

        box.innerHTML =
            '<div class="bl-modal-head">' + schoolBanner +
                '<div class="bl-modal-head-top"><span class="bl-type"><i class="fa-solid ' + esc(task.type_icon) + '"></i>' +
                esc(task.type_label) + '</span><span class="bl-stage ' + esc(task.stage) + '">' + esc(task.stage_label) + '</span>' +
                (task.cancel_requested ? '<span class="bl-cancel-badge">취소요청</span>' : '') +
                '<span style="margin-left:auto">' + footer + '</span></div>' +
                '<h3>' + esc(task.title) + '</h3>' +
            '</div>' +
            '<div class="bl-modal-body">' +
                cancelBox +
                stepperHtml(task, data.stage_times || {}) +
                (orphan ? '' : actionHtml(task)) +
                '<div class="bl-meta">' + meta.map(function (m) {
                    return '<div><span>' + esc(m[0]) + '</span><b>' + esc(m[1]) + '</b></div>';
                }).join('') + '</div>' +
                '<div class="bl-section-label">주고받은 내용 · 문서</div>' +
                '<div class="bl-thread-wrap">' + threadHtml(data.messages || [], task) + '</div>' +
            '</div>' +
            (task.stage === 'done' || orphan
                ? '<div class="bl-compose-locked"><i class="fa-solid fa-lock"></i> ' +
                  (orphan ? '학교 담당자가 교체된 업무라 글과 파일을 올릴 수 없습니다.' : '완료된 업무라 글과 파일을 더 올릴 수 없습니다.') + '</div>'
                : '<form class="bl-compose" id="blCompose">' +
                    '<textarea class="bl-input" name="body" placeholder="내용을 입력하고 Enter를 누르면 등록됩니다. (줄바꿈은 Shift+Enter) 문서는 아래 칸에 끌어다 놓거나 [파일 선택]으로 첨부해 주세요."></textarea>' +
                    '<div id="blComposeDrop" style="margin-top:8px"></div>' +
                    '<div class="bl-compose-bar">' +
                        '<span class="files-name">' + (staffView
                            ? '<b class="bl-to" style="color:' + schoolTint + '"><i class="fa-solid fa-school"></i> ' +
                              esc(task.school_name) + ' · ' + esc(task.member_name) + '</b>에게 보냅니다 · '
                            : '') + '한 번에 10개, 파일당 30MB까지 첨부할 수 있습니다.</span>' +
                        '<button type="submit" class="bl-btn primary sm"><i class="fa-solid fa-paper-plane"></i>등록</button>' +
                    '</div></form>');

        var body = box.querySelector('.bl-modal-body');
        body.scrollTop = body.scrollHeight;

        if (staffView) {
            var leaveOk = function () {
                if (uploading) return false;
                var text = box.querySelector('#blCompose textarea');
                if ((composerZone && composerZone.count()) || (text && text.value.trim())) {
                    return confirm('작성 중인 글이나 첨부한 파일이 아직 등록되지 않았습니다. 이동하면 사라집니다. 이동할까요?');
                }
                return true;
            };
            [['#blPrev', -1], ['#blNext', 1]].forEach(function (pair) {
                var btn = box.querySelector(pair[0]);
                if (!btn) return;
                btn.onclick = function () {
                    var target = neighborTask(task.id, pair[1]);
                    if (!target) { toast('목록의 ' + (pair[1] < 0 ? '처음' : '마지막') + ' 업무입니다.'); return; }
                    if (leaveOk()) openTask(target.id);
                };
            });
            var schoolOnly = box.querySelector('#blSchoolOnly');
            if (schoolOnly) {
                schoolOnly.onclick = function () {
                    if (!leaveOk()) return;
                    if (composerZone) { composerZone.clear(); composerZone = null; }
                    modal.classList.remove('open');
                    currentTaskId = null;
                    openedUnread = false;
                    cfg.onSchoolFilter(task.member_id, task.school_name + ' · ' + task.member_name);
                };
            }
        }

        var advance = box.querySelector('#blAdvance');
        if (advance) {
            advance.onclick = async function () {
                var notify = box.querySelector('#blNotify').checked;
                if (!confirm(whoLine + '[' + task.next_stage_label + '] 단계로 넘길까요?' +
                        (notify ? '\n상대방에게 문자 알림도 보냅니다.' : ''))) return;
                advance.disabled = true;
                try {
                    var result = await postJSON(cfg.urls.advance(task.id), { notify: notify });
                    toast(result.message + (notify && result.notify_result ? '\n' + result.notify_result : ''));
                    await openTask(task.id);
                    refresh();
                } catch (e) {
                    toast(e.message);
                    advance.disabled = false;
                }
            };
        }

        var replacedCancel = box.querySelector('#blReplacedCancel');
        if (replacedCancel && cfg.urls.replacedCancel) {
            replacedCancel.onclick = async function () {
                if (!confirm(whoLine + '담당자가 교체된 업무를 취소합니다.\n업무와 주고받은 글 · 첨부파일이 모두 삭제되며 되돌릴 수 없습니다. 계속할까요?')) return;
                replacedCancel.disabled = true;
                try {
                    var result = await postJSON(cfg.urls.replacedCancel(task.id), {});
                    toast(result.message);
                    closeTask();
                    refresh();
                } catch (e) {
                    toast(e.message);
                    replacedCancel.disabled = false;
                }
            };
        }

        var remove = box.querySelector('#blRemove');
        if (remove) {
            remove.onclick = async function () {
                var msg = cfg.viewer === 'staff'
                    ? '이 업무와 주고받은 글·첨부파일을 모두 삭제합니다. 되돌릴 수 없습니다. 계속할까요?'
                    : '이 업무를 취소할까요? 올린 내용과 파일이 함께 삭제됩니다.';
                var reason = prompt(whoLine + msg + '\n\n삭제 사유를 적어 주세요. 누가 · 언제 · 무엇을 지웠는지와 함께 기록에 남습니다.', '');
                if (reason === null) return;
                try {
                    var result = await postJSON(cfg.urls.remove(task.id), { reason: reason });
                    toast(result.message);
                    closeTask();
                    refresh();
                } catch (e) { toast(e.message); }
            };
        }

        function bindCancelAction(selector, urlFn, question, closeAfter, askReason) {
            var btn = box.querySelector(selector);
            if (!btn || !urlFn) return;
            question = whoLine + question;
            btn.onclick = async function () {
                var body = {};
                if (askReason) {
                    var reason = prompt(question + String.fromCharCode(10) + '취소 사유를 적어 주세요. (선택)', '');
                    if (reason === null) return;
                    body.reason = reason;
                } else if (!confirm(question)) {
                    return;
                }
                btn.disabled = true;
                try {
                    var result = await postJSON(urlFn(task.id), body);
                    toast(result.message);
                    if (closeAfter) closeTask();
                    else await openTask(task.id);
                    refresh();
                } catch (e) {
                    toast(e.message);
                    btn.disabled = false;
                }
            };
        }
        var isStaffView = cfg.viewer === 'staff';
        bindCancelAction('#blCancelRequest', cfg.urls.cancelRequest,
            isStaffView ? '이 업무의 취소를 학교에 요청할까요? 학교에서 승인하면 업무와 첨부파일이 삭제됩니다.'
                        : '이 업무의 취소를 본사에 요청할까요? 본사에서 확인하면 업무와 첨부파일이 삭제됩니다.', false, true);
        bindCancelAction('#blCancelWithdraw', cfg.urls.cancelWithdraw,
            '업무 취소 요청을 철회할까요?', false, false);
        bindCancelAction('#blCancelReject', cfg.urls.cancelReject,
            '취소 요청을 반려할까요? 업무는 그대로 진행됩니다.', false, false);
        bindCancelAction('#blCancelApprove', cfg.urls.cancelApprove,
            isStaffView ? '취소 요청을 확인하고 이 업무와 첨부파일을 모두 삭제합니다. 되돌릴 수 없습니다. 계속할까요?'
                        : '본사의 취소 요청을 승인하고 이 업무와 첨부파일을 모두 삭제합니다. 되돌릴 수 없습니다. 계속할까요?', true, false);

        var form = box.querySelector('#blCompose');
        if (form) {
            // Enter만 누르면 등록, Shift+Enter는 줄바꿈. 한글 조합 중 Enter는 글자 확정이라 등록하지 않는다.
            form.querySelector('textarea[name=body]').addEventListener('keydown', function (e) {
                if (e.key !== 'Enter' || e.shiftKey || e.isComposing || e.keyCode === 229) return;
                e.preventDefault();
                if (typeof form.requestSubmit === 'function') form.requestSubmit();
                else form.dispatchEvent(new Event('submit', { cancelable: true }));
            });
            var drop = createDropZone(box.querySelector('#blComposeDrop'), { compact: true });
            composerZone = drop;
            var submit = form.querySelector('button[type=submit]');
            var submitHtml = submit.innerHTML;
            drop.onChange(function (c) {
                var busy = c.waiting + c.uploading;
                submit.disabled = busy > 0 || uploading;
                submit.innerHTML = busy
                    ? '<i class="fa-solid fa-spinner fa-spin"></i>파일 전송 중 (' + (c.total - busy) + '/' + c.total + ')'
                    : submitHtml;
            });
            form.onsubmit = async function (e) {
                e.preventDefault();
                if (uploading) return;
                if (drop.pending()) { toast('파일 전송이 끝난 뒤 등록할 수 있습니다.'); return; }
                if (drop.errors()) { toast('전송에 실패한 파일을 [다시 시도]하거나 빼 주세요.'); return; }
                var body = form.querySelector('textarea[name=body]').value;
                var ids = drop.uploadIds();
                if (!body.trim() && !ids.length) { toast('내용을 입력하거나 파일을 첨부해 주세요.'); return; }
                var fd = new FormData();
                fd.append('body', body);
                ids.forEach(function (id) { fd.append('upload_ids', id); });
                uploading = true;
                submit.disabled = true;
                drop.setBusy(true);
                try {
                    await request(cfg.urls.messages(task.id), { method: 'POST', body: fd });
                    drop.clear({ keepServer: true });
                    uploading = false;
                    await openTask(task.id);
                    refresh();
                } catch (err) {
                    uploading = false;
                    drop.setBusy(false);
                    submit.disabled = drop.pending() > 0;
                    toast(err.message);
                }
            };
        }
    }

    async function openTask(taskId) {
        currentTaskId = taskId;
        var pos = taskPosition(taskId);
        if (cfg.viewer === 'staff' && pos > -1 && lastTasks[pos].unread) openedUnread = true;
        ensureModal().classList.add('open');
        try {
            var data = await request(cfg.urls.task(taskId));
            if (currentTaskId !== taskId) return;
            renderDetail(data);
        } catch (e) {
            toast(e.message);
            closeTask();
        }
    }

    /* ------------------------------------------------------------ 실시간 신호(Socket.IO)
     * 서버는 '업무 N번이 바뀜' 같은 신호만 보내고 내용은 싣지 않는다.
     * 신호를 받으면 권한을 확인하는 기존 조회 API로 다시 불러온다. 연결이 끊겨도 기존 주기적 확인이 보조로 동작한다. */
    function hasDraft() {
        if (!modal) return false;
        var text = modal.querySelector('#blCompose textarea');
        return !!(uploading || (composerZone && composerZone.count()) || (text && text.value.trim()));
    }

    async function reloadOpenTask(taskId) {
        var data;
        try { data = await request(cfg.urls.task(taskId)); } catch (e) { return; }
        if (currentTaskId !== taskId || !modal || !modal.classList.contains('open')) return;
        var prev = lastDetail && lastDetail.task;
        var next = data.task;
        var sameState = !!prev && prev.stage === next.stage &&
            !!prev.cancel_requested === !!next.cancel_requested &&
            (prev.cancel_requested_side || '') === (next.cancel_requested_side || '') &&
            prev.member_status === next.member_status;
        // 단계 등이 바뀌었고 작성 중인 글이 없으면 창 전체를 새로 그린다.
        if (!sameState && !hasDraft()) { renderDetail(data); return; }
        // 그 밖에는 대화만 바꿔 끼워서, 쓰던 글과 첨부를 지키고 스크롤도 유지한다.
        var body = modal.querySelector('.bl-modal-body');
        var wrap = modal.querySelector('.bl-thread-wrap');
        var nearBottom = body && body.scrollHeight - body.scrollTop - body.clientHeight < 80;
        if (wrap) wrap.innerHTML = threadHtml(data.messages || [], next);
        if (sameState) lastDetail = data;
        if (nearBottom && body) body.scrollTop = body.scrollHeight;
        if (!sameState && body && !modal.querySelector('.bl-live-note')) {
            var note = document.createElement('div');
            note.className = 'bl-live-note';
            note.innerHTML = '<i class="fa-solid fa-rotate"></i><span>업무 단계나 취소 요청이 바뀌었습니다.</span>' +
                '<button type="button" class="bl-btn sm">새로 보기</button>';
            note.querySelector('button').onclick = function () {
                if (hasDraft() && !confirm('작성 중인 글이나 첨부한 파일이 사라집니다. 새로 볼까요?')) return;
                if (composerZone) { composerZone.clear(); composerZone = null; }
                openTask(taskId);
            };
            body.insertBefore(note, body.firstChild);
        }
    }

    function connectRealtime() {
        if (!cfg.socketNamespace || typeof global.io !== 'function' || realtimeSocket) return;
        realtimeSocket = global.io(cfg.socketNamespace, { transports: ['polling'], upgrade: false });
        var listTimer = null;
        realtimeSocket.on('billing_task', function (data) {
            data = data || {};
            var taskId = Number(data.task_id) || null;
            clearTimeout(listTimer);
            listTimer = setTimeout(function () { refresh({ quiet: true }); }, 300);   // 신호가 몰려도 목록은 한 번만 다시 불러온다
            var viewing = taskId && currentTaskId === taskId && modal && modal.classList.contains('open');
            if (viewing && data.kind === 'deleted') {
                if (composerZone) { composerZone.clear(); composerZone = null; }
                modal.classList.remove('open');
                currentTaskId = null;
                toast('보고 있던 업무가 삭제되었습니다.');
            } else if (viewing) {
                reloadOpenTask(taskId);
            } else if (cfg.viewer === 'member' && data.by === 'staff' && (data.kind === 'message' || data.kind === 'stage')) {
                toast(data.kind === 'message' ? '본사에서 새 글을 남겼습니다. 업무 목록에서 확인해 주세요.'
                                              : '본사에서 업무 단계를 바꿨습니다. 업무 목록에서 확인해 주세요.');
            }
        });
        realtimeSocket.on('disconnect', function (reason) {
            // 서버가 연결을 끊은 경우(정지 · 교체 · 잠김 등) 화면을 다시 불러 로그인 상태를 확인한다.
            if (reason === 'io server disconnect') setTimeout(function () { location.reload(); }, 300);
        });
        if (cfg.onRealtime) cfg.onRealtime(realtimeSocket);
    }

    global.Billing = {
        init: function (config) { cfg = config; refresh(); connectRealtime(); },
        refresh: refresh,
        openTask: openTask,
        request: request,
        postJSON: postJSON,
        toast: toast,
        esc: esc,
        createDropZone: createDropZone,
        schoolColor: schoolColor,
        shortTime: shortTime,
        clock12: clock12
    };
})(window);
