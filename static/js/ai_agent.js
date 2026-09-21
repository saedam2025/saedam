(function () {
    'use strict';

    const app = document.getElementById('aiAgentApp');
    if (!app) return;

    const messages = document.getElementById('aiAgentMessages');
    const welcome = document.getElementById('aiAgentWelcome');
    const form = document.getElementById('aiAgentForm');
    const input = document.getElementById('aiAgentInput');
    const send = document.getElementById('aiAgentSend');
    const reset = document.getElementById('aiAgentReset');
    const sidebar = document.getElementById('aiAgentSidebar');
    const sidebarToggle = document.getElementById('aiAgentSidebarToggle');
    const sidebarBackdrop = document.getElementById('aiAgentSidebarBackdrop');
    const historyList = document.getElementById('aiAgentHistoryList');
    const historyEmpty = document.getElementById('aiAgentHistoryEmpty');
    const state = {busy: false, activeHistoryId: null};

    function aiAvatarMarkup() {
        return '<span class="ai-avatar-cube-scene" aria-hidden="true">'
            + '<span class="ai-avatar-cube">'
            + '<span class="ai-avatar-cube-face front"></span>'
            + '<span class="ai-avatar-cube-face back"></span>'
            + '<span class="ai-avatar-cube-face right"></span>'
            + '<span class="ai-avatar-cube-face left"></span>'
            + '<span class="ai-avatar-cube-face top"></span>'
            + '<span class="ai-avatar-cube-face bottom"></span>'
            + '</span>'
            + '</span>';
    }

    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function internalUrl(value) {
        if (!value || typeof value !== 'string') return '';
        try {
            const parsed = new URL(value, window.location.origin);
            if (parsed.origin !== window.location.origin) return '';
            return parsed.pathname + parsed.search + parsed.hash;
        } catch (_) {
            return '';
        }
    }

    function decodedFileName(value) {
        let text = String(value || '').trim();
        if (!text) return '';
        if (text.startsWith('~e~')) text = text.slice(3);
        try {
            return decodeURIComponent(text);
        } catch (_) {
            return text;
        }
    }

    function scrollToBottom() {
        window.requestAnimationFrame(function () {
            messages.scrollTop = messages.scrollHeight;
        });
    }

    function resizeInput() {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 130) + 'px';
        send.disabled = state.busy || !input.value.trim();
    }

    function appendUser(text) {
        const row = element('div', 'ai-message ai-message-user');
        row.appendChild(element('div', 'ai-message-content', text));
        messages.appendChild(row);
        scrollToBottom();
    }

    function assistantRow() {
        const row = element('div', 'ai-message ai-message-assistant');
        const avatar = element('div', 'ai-message-avatar');
        avatar.innerHTML = aiAvatarMarkup();
        const content = element('div', 'ai-message-content');
        row.append(avatar, content);
        messages.appendChild(row);
        return {row, content};
    }

    function appendLoading() {
        const parts = assistantRow();
        parts.row.id = 'aiAgentLoading';
        const bubble = element('div', 'ai-loading-bubble');
        bubble.appendChild(element('span', '', '새담 AI가 데이터를 확인하고 있습니다...'));
        const dots = element('span', 'ai-loading-dots');
        dots.append(element('i'), element('i'), element('i'));
        bubble.appendChild(dots);
        parts.content.appendChild(bubble);
        scrollToBottom();
    }

    function removeLoading() {
        const loading = document.getElementById('aiAgentLoading');
        if (loading) loading.remove();
    }

    function resultHead(payload) {
        const head = element('div', 'ai-result-head');
        head.appendChild(element('h3', '', payload.title || '조회 결과'));
        if (payload.message) head.appendChild(element('p', '', payload.message));
        return head;
    }

    function renderActions(container, actions) {
        if (!Array.isArray(actions) || !actions.length) return;
        const wrap = element('div', 'ai-actions');
        actions.forEach(function (action) {
            const url = internalUrl(action && action.url);
            if (!url) return;
            const link = element('a', 'ai-action' + (action.style === 'primary' ? ' ai-action-primary' : ''), action.label || '이동');
            link.href = url;
            wrap.appendChild(link);
        });
        if (wrap.childElementCount) container.appendChild(wrap);
    }

    function columnClass(column) {
        // 본문 미리보기처럼 긴 문장은 줄바꿈 컬럼으로 표시해 가로 스크롤을 줄인다.
        if (column && column.wrap) return 'ai-col-wrap';
        return column && column.align === 'right' ? 'ai-col-right' : '';
    }

    function renderTable(payload) {
        const card = element('div', 'ai-result-card');
        card.appendChild(resultHead(payload));
        const columns = Array.isArray(payload.columns) ? payload.columns : [];
        const rows = Array.isArray(payload.rows) ? payload.rows : [];
        if (!columns.length || !rows.length) {
            card.appendChild(element('div', 'ai-result-empty', '표시할 결과가 없습니다.'));
        } else {
            const scroll = element('div', 'ai-table-scroll');
            const table = element('table', 'ai-result-table');
            const thead = element('thead');
            const headerRow = element('tr');
            columns.forEach(function (column) {
                const th = element('th', columnClass(column), column.label || column.key || '');
                headerRow.appendChild(th);
            });
            thead.appendChild(headerRow);
            const tbody = element('tbody');
            rows.forEach(function (row) {
                const tr = element('tr');
                columns.forEach(function (column) {
                    const value = row && Object.prototype.hasOwnProperty.call(row, column.key) ? row[column.key] : '';
                    tr.appendChild(element('td', columnClass(column), value));
                });
                tbody.appendChild(tr);
            });
            table.append(thead, tbody);
            scroll.appendChild(table);
            card.appendChild(scroll);
        }
        renderActions(card, payload.actions);
        return card;
    }

    function itemTitle(item) {
        return item.title || item.name || item.school || '결과';
    }

    function itemDescription(item) {
        const ignored = new Set(['title', 'name', 'school', 'link', 'url', 'thumbnail', 'image_url']);
        const values = [];
        Object.keys(item || {}).forEach(function (key) {
            if (ignored.has(key) || item[key] === '' || item[key] === null || item[key] === undefined) return;
            values.push(String(item[key]));
        });
        return values.join(' · ');
    }

    function renderList(payload) {
        const card = element('div', 'ai-result-card');
        card.appendChild(resultHead(payload));
        const items = Array.isArray(payload.items) ? payload.items : [];
        if (!items.length) {
            card.appendChild(element('div', 'ai-result-empty', '표시할 결과가 없습니다.'));
        } else {
            const list = element('ol', 'ai-list');
            items.forEach(function (item) {
                const li = element('li', 'ai-list-item');
                const url = internalUrl(item.link || item.url);
                const host = url ? element('a') : element('div');
                if (url) host.href = url;
                host.appendChild(element('strong', '', itemTitle(item)));
                const detail = itemDescription(item);
                if (detail) host.appendChild(element('span', '', detail));
                li.appendChild(host);
                list.appendChild(li);
            });
            card.appendChild(list);
        }
        renderActions(card, payload.actions);
        return card;
    }

    function renderGallery(payload) {
        const card = element('div', 'ai-result-card');
        card.appendChild(resultHead(payload));
        const items = Array.isArray(payload.items) ? payload.items : [];
        if (!items.length) {
            card.appendChild(element('div', 'ai-result-empty', '표시할 사진이 없습니다.'));
        } else {
            const grid = element('div', 'ai-gallery-grid');
            items.forEach(function (item) {
                const imageUrl = internalUrl(item.thumbnail || item.image_url);
                const largeUrl = internalUrl(item.image_url || item.thumbnail);
                if (!imageUrl || !largeUrl) return;
                const link = element('a', 'ai-gallery-item');
                link.href = largeUrl;
                link.target = '_blank';
                link.rel = 'noopener';
                const image = element('img');
                image.src = imageUrl;
                image.alt = item.title || '갤러리 사진';
                image.loading = 'lazy';
                const caption = element('div', 'ai-gallery-caption');
                caption.appendChild(element('strong', '', item.title || '사진'));
                if (item.meta) caption.appendChild(element('span', '', item.meta));
                link.append(image, caption);
                grid.appendChild(link);
            });
            card.appendChild(grid);
        }
        renderActions(card, payload.actions);
        return card;
    }

    function renderFiles(payload) {
        const card = element('div', 'ai-result-card');
        card.appendChild(resultHead(payload));
        const items = Array.isArray(payload.items) ? payload.items : [];
        if (!items.length) {
            card.appendChild(element('div', 'ai-result-empty', '표시할 파일이 없습니다.'));
        } else {
            const list = element('div', 'ai-file-list');
            items.forEach(function (item) {
                const url = internalUrl(item.link || item.url);
                const row = url ? element('a', 'ai-file-item') : element('div', 'ai-file-item');
                if (url) row.href = url;
                const icon = element('span', 'ai-file-icon');
                icon.appendChild(element('i', 'fa-regular fa-file-lines'));
                const meta = element('span', 'ai-file-meta');
                const fileLabel = decodedFileName(item.name);
                meta.appendChild(element('strong', '', item.title || fileLabel || '파일'));
                meta.appendChild(element('span', '', [item.source, fileLabel, item.date].filter(Boolean).join(' · ')));
                row.append(icon, meta);
                list.appendChild(row);
            });
            card.appendChild(list);
        }
        renderActions(card, payload.actions);
        return card;
    }

    function renderCard(payload) {
        const card = element('div', 'ai-result-card');
        card.appendChild(resultHead(payload));
        const items = Array.isArray(payload.items) ? payload.items : [];
        if (items.length) {
            const body = element('div', 'ai-file-list');
            items.forEach(function (item) { body.appendChild(element('div', 'ai-file-item', itemDescription(item))); });
            card.appendChild(body);
        }
        renderActions(card, payload.actions);
        return card;
    }

    function renderPayload(container, payload, includeSummary) {
        payload = payload && typeof payload === 'object' ? payload : {type: 'text', message: String(payload || '')};
        if (includeSummary && payload.summary) container.appendChild(element('p', 'ai-answer-summary', payload.summary));
        if (payload.type === 'sections') {
            if (payload.message) container.appendChild(element('p', 'ai-answer-summary', payload.message));
            (payload.sections || []).forEach(function (section) {
                const wrap = element('div', 'ai-section');
                renderPayload(wrap, section, false);
                container.appendChild(wrap);
            });
            return;
        }
        if (payload.type === 'table') container.appendChild(renderTable(payload));
        else if (payload.type === 'gallery') container.appendChild(renderGallery(payload));
        else if (payload.type === 'files') container.appendChild(renderFiles(payload));
        else if (payload.type === 'list') container.appendChild(renderList(payload));
        else if (payload.type === 'card') container.appendChild(renderCard(payload));
        else {
            container.appendChild(element('p', 'ai-answer-summary', payload.message || '답변을 확인하지 못했습니다.'));
            renderActions(container, payload.actions);
        }
    }

    function appendAssistant(payload) {
        const parts = assistantRow();
        renderPayload(parts.content, payload, true);
        scrollToBottom();
    }

    function formatTime(value) {
        if (!value) return '';
        const normalized = String(value).includes('T') ? value : String(value).replace(' ', 'T');
        const dt = new Date(normalized + (normalized.endsWith('Z') ? '' : 'Z'));
        if (Number.isNaN(dt.getTime())) return String(value);
        return dt.toLocaleString('ko-KR', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'});
    }

    function renderHistoryList(items) {
        historyList.innerHTML = '';
        historyEmpty.hidden = !!(items && items.length);
        (items || []).forEach(function (item) {
            const li = element('li', 'ai-agent-history-item' + (item.pinned ? ' is-pinned' : '') + (String(item.id) === String(state.activeHistoryId) ? ' is-active' : ''));
            li.dataset.id = item.id;
            const open = element('button', 'ai-agent-history-open');
            open.type = 'button';
            open.appendChild(element('span', 'ai-agent-history-question', item.question || '(빈 질문)'));
            open.appendChild(element('span', 'ai-agent-history-meta', formatTime(item.created_at)));
            open.addEventListener('click', function () { openHistoryItem(item.id); });
            const pin = element('button', 'ai-agent-history-pin');
            pin.type = 'button';
            pin.title = item.pinned ? '고정 해제' : '고정하여 삭제 방지';
            pin.innerHTML = '<i class="fa-solid fa-thumbtack"></i>';
            pin.addEventListener('click', function (event) {
                event.stopPropagation();
                togglePin(item.id, !item.pinned);
            });
            li.append(open, pin);
            historyList.appendChild(li);
        });
    }

    async function loadHistoryData() {
        try {
            const response = await fetch(app.dataset.historyUrl, {headers: {'Accept': 'application/json'}});
            const data = await response.json().catch(function () { return {}; });
            if (!response.ok || data.status !== 'success') return;
            renderHistoryList(data.history);
        } catch (_) { /* 기록 로딩 실패는 채팅 사용을 막지 않는다 */ }
    }

    async function togglePin(itemId, pinned) {
        try {
            const response = await fetch(app.dataset.historyUrl + '/' + itemId + '/pin', {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRF-Token': app.dataset.csrfToken},
                body: JSON.stringify({pinned: pinned})
            });
            if (response.ok) loadHistoryData();
        } catch (_) { /* noop */ }
    }

    async function openHistoryItem(itemId) {
        try {
            const response = await fetch(app.dataset.historyUrl + '/' + itemId, {headers: {'Accept': 'application/json'}});
            const data = await response.json().catch(function () { return {}; });
            if (!response.ok || data.status !== 'success') return;
            state.activeHistoryId = itemId;
            if (welcome) welcome.hidden = true;
            Array.from(messages.children).forEach(function (child) { if (child !== welcome) child.remove(); });
            appendUser(data.item.question);
            appendAssistant(data.item.payload);
            Array.from(historyList.children).forEach(function (child) {
                child.classList.toggle('is-active', String(child.dataset.id) === String(itemId));
            });
            closeSidebar();
        } catch (_) { /* noop */ }
    }

    function openSidebar() { app.classList.add('is-sidebar-open'); }
    function closeSidebar() { app.classList.remove('is-sidebar-open'); }

    if (sidebarToggle) {
        sidebarToggle.addEventListener('click', function () {
            app.classList.contains('is-sidebar-open') ? closeSidebar() : openSidebar();
        });
    }
    if (sidebarBackdrop) sidebarBackdrop.addEventListener('click', closeSidebar);

    function setBusy(value) {
        state.busy = !!value;
        input.disabled = state.busy;
        resizeInput();
    }

    async function submitQuestion(question) {
        question = String(question || '').trim();
        if (!question || state.busy) return;
        state.activeHistoryId = null;
        if (welcome) welcome.hidden = true;
        appendUser(question);
        input.value = '';
        resizeInput();
        setBusy(true);
        appendLoading();
        try {
            const response = await fetch(app.dataset.chatUrl, {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRF-Token': app.dataset.csrfToken},
                body: JSON.stringify({question: question})
            });
            const data = await response.json().catch(function () { return {}; });
            removeLoading();
            if (!response.ok || data.status !== 'success') {
                appendAssistant({type: 'card', title: 'AI 응답 안내', message: data.message || 'AI 응답을 처리하는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.'});
            } else {
                appendAssistant(data.answer);
                loadHistoryData();
            }
        } catch (_) {
            removeLoading();
            appendAssistant({type: 'card', title: '연결 안내', message: 'AI 응답을 처리하는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.'});
        } finally {
            setBusy(false);
            input.focus();
        }
    }

    form.addEventListener('submit', function (event) {
        event.preventDefault();
        submitQuestion(input.value);
    });

    input.addEventListener('input', resizeInput);
    input.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            submitQuestion(input.value);
        }
    });

    document.querySelectorAll('.ai-agent-suggestions [data-question]').forEach(function (button) {
        button.addEventListener('click', function () { submitQuestion(button.dataset.question); });
    });

    reset.addEventListener('click', async function () {
        if (state.busy) return;
        setBusy(true);
        try {
            const response = await fetch(app.dataset.resetUrl, {
                method: 'POST', headers: {'Accept': 'application/json', 'X-CSRF-Token': app.dataset.csrfToken}
            });
            if (!response.ok) throw new Error('reset failed');
            state.activeHistoryId = null;
            Array.from(messages.children).forEach(function (child) { if (child !== welcome) child.remove(); });
            if (welcome) welcome.hidden = false;
            messages.scrollTop = 0;
            Array.from(historyList.children).forEach(function (child) { child.classList.remove('is-active'); });
        } catch (_) {
            appendAssistant({type: 'card', title: '새 대화', message: '새 대화를 시작하지 못했습니다. 잠시 후 다시 시도해 주세요.'});
        } finally {
            input.value = '';
            setBusy(false);
            input.focus();
        }
    });

    resizeInput();
    loadHistoryData();
})();
