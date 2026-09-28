// 새담 방과후학교 학부모 알림 도우미 (수신 확인 v2)
self.addEventListener('install', event => {
    event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', event => {
    event.waitUntil(self.clients.claim());
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
        icon: '/static/favicon.ico',
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
