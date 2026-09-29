// 새담 방과후학교 학부모 페이지 공통 도우미
// 1) 카카오톡 등 앱 안의 브라우저에서는 알림 등록(푸시)이 되지 않으므로 Safari·Chrome으로 다시 연다.
// 2) '홈 화면에 추가': 안드로이드는 설치 창을 바로 띄우고, 아이폰은 추가 방법을 순서대로 안내한다.
(function () {
    'use strict';

    const ua = navigator.userAgent || '';
    const isIOS = /iPhone|iPad|iPod/i.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    const isAndroid = /Android/i.test(ua);
    const standalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;

    const IN_APPS = [
        ['kakao', /KAKAOTALK/i, '카카오톡'],
        ['line', /\bLine\//i, '라인'],
        ['naver', /NAVER\(inapp/i, '네이버 앱'],
        ['band', /\bBAND\//i, '밴드'],
        ['daum', /DaumApps/i, '다음 앱'],
        ['instagram', /Instagram/i, '인스타그램'],
        ['facebook', /FBAN|FBAV|FB_IAB/i, '페이스북'],
    ];

    function detectInApp() {
        if (standalone) return null;
        for (const [key, pattern, label] of IN_APPS) {
            if (pattern.test(ua)) return {key, label};
        }
        // 이름을 모르는 앱 안의 브라우저: 안드로이드 WebView, 아이폰에서 Safari 표시가 없는 화면
        if (isAndroid && /; wv\)/.test(ua)) return {key: 'webview', label: '앱'};
        if (isIOS && !/Safari\//.test(ua) && !/CriOS|FxiOS|EdgiOS|Whale/i.test(ua)) return {key: 'webview', label: '앱'};
        return null;
    }

    const inApp = detectInApp();
    // 카카오톡·라인은 기본 브라우저로 열어 준다. 그 밖의 안드로이드 앱은 Chrome으로 연다.
    const browserName = isIOS ? 'Safari' : (inApp && (inApp.key === 'kakao' || inApp.key === 'line') ? '인터넷 브라우저' : 'Chrome');
    const openLabel = browserName === 'Chrome' ? 'Chrome으로 열기' : `${browserName}로 열기`;

    function openExternal() {
        const url = location.href;
        if (inApp && inApp.key === 'kakao') {
            location.href = 'kakaotalk://web/openExternal?url=' + encodeURIComponent(url);
        } else if (inApp && inApp.key === 'line') {
            const next = new URL(url);
            next.searchParams.set('openExternalBrowser', '1');
            location.href = next.href;
        } else if (isAndroid) {
            location.href = 'intent://' + url.replace(/^https?:\/\//, '') + '#Intent;scheme='
                + location.protocol.replace(':', '') + ';package=com.android.chrome;end';
        } else if (isIOS) {
            location.href = 'x-safari-' + url;   // iOS 17 이상은 이 주소로 Safari가 열린다.
        }
    }

    async function copyLink() {
        const url = location.href;
        try {
            await navigator.clipboard.writeText(url);
            return true;
        } catch (error) { /* 앱 안의 브라우저는 클립보드를 막는 경우가 있어 아래 방법으로 한 번 더 시도한다. */ }
        const area = document.createElement('textarea');
        area.value = url;
        area.setAttribute('readonly', '');
        area.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
        document.body.appendChild(area);
        area.select();
        let copied = false;
        try { copied = document.execCommand('copy'); } catch (error) { copied = false; }
        area.remove();
        return copied;
    }

    const STYLE = `
.pa-inapp{background:#fff;border:2px solid #ffc46f;border-radius:20px;padding:20px;margin-bottom:15px;box-shadow:0 12px 30px rgba(231,129,0,.16)}
.pa-inapp strong{display:block;font-size:18px;line-height:1.4;margin-bottom:6px}
.pa-inapp p{margin:0;color:#70798b;line-height:1.6;font-size:14px}
.pa-inapp .pa-hint{margin-top:12px;font-size:13px}
.pa-actions{display:flex;gap:8px;margin-top:14px}
.pa-btn{flex:1 1 auto;width:auto;white-space:nowrap;border:0;border-radius:14px;margin:0;padding:15px 12px;font:inherit;font-weight:900;font-size:16px;cursor:pointer;background:linear-gradient(135deg,#e78100,#ff9d1b);color:#fff}
.pa-btn.ghost{flex:0 0 auto;padding:15px 16px;background:#fff4df;color:#9a5a00;box-shadow:none}
.pa-sheet{border:0;border-radius:22px;padding:24px 20px 20px;width:min(440px,calc(100% - 24px));color:#172033;box-shadow:0 20px 50px rgba(23,32,51,.28)}
.pa-sheet::backdrop{background:rgba(23,32,51,.45)}
.pa-sheet h2{font-size:20px;margin:0 0 4px}
.pa-sheet .pa-sub{color:#70798b;font-size:14px;margin:0 0 14px;line-height:1.55}
.pa-sheet ol{margin:0;padding:0;list-style:none;counter-reset:pa}
.pa-sheet li{counter-increment:pa;position:relative;padding:10px 0 10px 40px;line-height:1.6;font-size:15px;border-top:1px solid #e7e9ee}
.pa-sheet li::before{content:counter(pa);position:absolute;left:0;top:10px;width:28px;height:28px;border-radius:50%;background:#fff4df;color:#9a5a00;font-weight:900;display:grid;place-items:center}
.pa-sheet .pa-icon{display:inline-block;width:20px;height:20px;vertical-align:-4px;color:#1a73e8}
.pa-sheet .pa-btn{width:100%;margin-top:16px}
`;

    function injectStyle() {
        if (document.getElementById('pa-style')) return;
        const style = document.createElement('style');
        style.id = 'pa-style';
        style.textContent = STYLE;
        document.head.appendChild(style);
    }

    // 앱 안의 브라우저라면 안내 카드를 anchor 뒤에 붙이고, 카카오톡·라인은 한 번 자동으로 바깥 브라우저를 연다.
    function guardInApp(anchor) {
        if (!inApp) return false;
        injectStyle();
        const card = document.createElement('section');
        card.className = 'pa-inapp';
        card.setAttribute('role', 'alert');
        const menuHint = isIOS
            ? `화면의 <b>⋯</b> 또는 <b>공유</b> 메뉴에서 <b>‘Safari로 열기’</b>를 눌러주세요.`
            : `화면 오른쪽 위 <b>⋮</b> 메뉴에서 <b>‘다른 브라우저로 열기’</b>를 눌러주세요.`;
        card.innerHTML = `
            <strong>${inApp.label} 안에서는 알림 등록이 되지 않아요</strong>
            <p>아래 버튼을 눌러 <b>${browserName}</b>에서 이 페이지를 다시 열어주세요.</p>
            <div class="pa-actions">
                <button type="button" class="pa-btn" data-pa="open">${openLabel}</button>
                <button type="button" class="pa-btn ghost" data-pa="copy">링크 복사</button>
            </div>
            <p class="pa-hint">버튼이 되지 않으면 ${menuHint}<br>링크를 복사해 ${isIOS ? 'Safari' : 'Chrome'} 주소창에 붙여넣어도 됩니다.</p>`;
        card.querySelector('[data-pa="open"]').addEventListener('click', openExternal);
        card.querySelector('[data-pa="copy"]').addEventListener('click', async event => {
            const copied = await copyLink();
            event.currentTarget.textContent = copied ? '복사됨' : '복사 실패';
        });
        anchor.insertAdjacentElement('afterend', card);

        if (inApp.key === 'kakao' || inApp.key === 'line') {
            // 뒤로 가기·새로고침 때마다 바깥 브라우저가 다시 열리지 않도록 페이지마다 한 번만 자동으로 연다.
            const key = 'saedam-parent-open:' + location.pathname;
            let tried = false;
            try {
                tried = sessionStorage.getItem(key) === '1';
                sessionStorage.setItem(key, '1');
            } catch (error) { tried = false; }
            if (!tried) openExternal();
        }
        return true;
    }

    // ---------------------------------------------------------------- 홈 화면에 추가

    let installPrompt = null;
    let installed = false;
    const listeners = [];

    function notify() {
        listeners.forEach(fn => {
            try { fn(); } catch (error) { /* 화면 갱신 실패가 설치 흐름을 막지 않게 한다. */ }
        });
    }

    window.addEventListener('beforeinstallprompt', event => {
        event.preventDefault();   // 브라우저 기본 배너 대신 페이지의 '홈 화면에 추가' 버튼으로 띄운다.
        installPrompt = event;
        notify();
    });
    window.addEventListener('appinstalled', () => {
        installPrompt = null;
        installed = true;
        notify();
    });

    function canAddToHome() {
        if (standalone || installed || inApp) return false;
        return Boolean(installPrompt) || isIOS || isAndroid;
    }

    const SHARE_ICON = '<svg class="pa-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        + 'stroke-linecap="round" stroke-linejoin="round" aria-label="공유 버튼"><path d="M12 3v12"/>'
        + '<path d="M8 7l4-4 4 4"/><path d="M6 11H5a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-8a1 1 0 0 0-1-1h-1"/></svg>';

    function guideSteps() {
        if (isIOS) {
            const shareStep = /CriOS/i.test(ua)
                ? `주소창 오른쪽의 <b>공유 버튼</b> ${SHARE_ICON}을 누르세요.`
                : `화면 아래쪽(아이패드는 위쪽)의 <b>공유 버튼</b> ${SHARE_ICON}을 누르세요. 안 보이면 주소창 옆 <b>⋯</b>을 먼저 누르세요.`;
            return [
                shareStep,
                `목록을 위로 올려 <b>‘홈 화면에 추가’</b>를 누르세요.`,
                `<b>‘웹 앱으로 열기’</b>가 보이면 켠 채로 오른쪽 위 <b>‘추가’</b>를 누르세요.`,
                `홈 화면에 생긴 <b>‘새담알림’</b> 아이콘으로 열어 <b>‘출결 알림 받기’</b>를 누르면 끝입니다.`,
            ];
        }
        if (/SamsungBrowser/i.test(ua)) {
            return [
                `화면 아래쪽 <b>≡ 메뉴</b>를 누르세요.`,
                `<b>‘현재 페이지 추가’</b>(또는 ‘페이지 추가’)를 누르세요.`,
                `<b>‘홈 화면’</b>을 고르면 ‘새담알림’ 아이콘이 생깁니다.`,
            ];
        }
        return [
            `화면 오른쪽 위 <b>⋮ 메뉴</b>를 누르세요.`,
            `<b>‘홈 화면에 추가’</b> 또는 <b>‘앱 설치’</b>를 누르세요.`,
            `<b>‘추가’</b>(설치)를 누르면 ‘새담알림’ 아이콘이 생깁니다.`,
        ];
    }

    function showGuide() {
        injectStyle();
        let sheet = document.getElementById('pa-guide');
        if (!sheet) {
            sheet = document.createElement('dialog');
            sheet.id = 'pa-guide';
            sheet.className = 'pa-sheet';
            sheet.innerHTML = `
                <h2>홈 화면에 아이콘 추가</h2>
                <p class="pa-sub">${isIOS ? '아이폰은 홈 화면에 추가한 아이콘에서만 알림을 받을 수 있어요.' : '아이콘을 누르면 이 페이지가 앱처럼 바로 열립니다.'}</p>
                <ol>${guideSteps().map(step => `<li>${step}</li>`).join('')}</ol>
                <button type="button" class="pa-btn">확인</button>`;
            sheet.querySelector('.pa-btn').addEventListener('click', () => closeGuide(sheet));
            // 바깥 어두운 곳을 눌러도 닫힌다.
            sheet.addEventListener('click', event => { if (event.target === sheet) closeGuide(sheet); });
            document.body.appendChild(sheet);
        }
        if (typeof sheet.showModal === 'function') sheet.showModal();
        else sheet.setAttribute('open', '');
    }

    function closeGuide(sheet) {
        if (typeof sheet.close === 'function') sheet.close();
        else sheet.removeAttribute('open');
    }

    // 결과: 'accepted' | 'dismissed' | 'guide' | 'inapp'
    async function addToHome() {
        if (inApp) {
            openExternal();
            return 'inapp';
        }
        if (installPrompt) {
            const promptEvent = installPrompt;
            installPrompt = null;   // 설치 창은 한 번만 쓸 수 있다.
            promptEvent.prompt();
            const choice = await promptEvent.userChoice.catch(() => null);
            notify();
            return choice && choice.outcome === 'accepted' ? 'accepted' : 'dismissed';
        }
        showGuide();
        return 'guide';
    }

    window.SaedamParentApp = {
        isIOS, isAndroid, standalone, inApp, browserName, openLabel,
        guardInApp, openExternal, canAddToHome, addToHome, showGuide,
        onChange(fn) { listeners.push(fn); },
    };
})();
