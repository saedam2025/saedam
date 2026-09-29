// 새담 방과후학교 학부모 알림 도우미 (수신 확인 v2 · 홈 화면 앱 v1)
const APP_ICON = '/static/images/parent_app/icon-192.png';
const OFFLINE_PAGE = '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
    + '<meta name="viewport" content="width=device-width,initial-scale=1">'
    + '<title>새담알림</title></head>'
    + '<body style="margin:0;font-family:sans-serif;background:#fff9f0;color:#172033;'
    + 'display:grid;place-items:center;min-height:100vh;text-align:center;padding:24px">'
    + '<div><h1 style="font-size:22px">인터넷 연결을 확인해주세요</h1>'
    + '<p style="color:#70798b;line-height:1.6">연결된 뒤 다시 열면 알림 내역을 볼 수 있습니다.</p>'
    + '<button onclick="location.reload()" style="border:0;border-radius:12px;padding:14px 22px;'
    + 'font-size:16px;font-weight:800;background:#e78100;color:#fff">다시 시도</button></div></body></html>';

self.addEventListener('install', event => {
    event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', event => {
    event.waitUntil(self.clients.claim());
});

// 홈 화면 아이콘으로 열었는데 인터넷이 끊겨 있으면 빈 화면 대신 안내를 보여준다.
// (삼성 인터넷 등은 이 처리가 있어야 '홈 화면에 추가'를 앱 아이콘으로 인정한다.)
self.addEventListener('fetch', event => {
    if (event.request.mode !== 'navigate') return;
    event.respondWith(fetch(event.request).catch(() => new Response(OFFLINE_PAGE, {
        headers: {'Content-Type': 'text/html; charset=utf-8'}
    })));
});

// 알림을 실제로 띄웠거나(received) 학부모가 눌렀을 때(opened) 서버에 알린다.
// 확인이 실패해도 알림 표시에는 영향이 없어야 하므로 오류는 삼킨다.
function sendReceipt(receipt, kind) {
    if (!receipt) return Promise.resolve();
    return fetch('/parent/api/push/receipt', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({receipt, event: kind}),
        credentials: 'omit',
        keepalive: true
    }).catch(() => {});
}

self.addEventListener('push', event => {
    let data = {};
    try {
        data = event.data ? event.data.json() : {};
    } catch (error) {
        data = {body: event.data ? event.data.text() : '새담 방과후학교 알림이 도착했습니다.'};
    }
    event.waitUntil(self.registration.showNotification(data.title || '새담 방과후학교', {
        body: data.body || '새로운 알림이 도착했습니다.',
        tag: data.tag || 'saedam-parent-notice',
        renotify: true,
        icon: APP_ICON,
        badge: '/static/favicon.ico',
        data: {url: data.url || '/parent/', receipt: data.receipt || ''}
    }).then(() => sendReceipt(data.receipt, 'received')));
});

self.addEventListener('notificationclick', event => {
    event.notification.close();
    const info = event.notification.data || {};
    const target = new URL(info.url || '/parent/', self.location.origin).href;
    event.waitUntil(Promise.all([
        sendReceipt(info.receipt, 'opened'),
        (async () => {
            const windows = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
            for (const client of windows) {
                if (client.url === target && 'focus' in client) return client.focus();
            }
            if (self.clients.openWindow) return self.clients.openWindow(target);
        })()
    ]));
});
