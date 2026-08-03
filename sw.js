// HEIMDALL 서비스 워커
// 앱 껍데기만 캐시합니다. 회의 내용은 절대 캐시하지 않습니다 —
// 오래된 회의록이 남거나, 기기에 회사 자료가 저장되면 안 되기 때문입니다.
const V = 'heimdall-v1';
const SHELL = ['./', './index.html', './manifest.webmanifest',
               './icons/icon-192.png', './icons/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(V).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(ks => Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // 서버 통신(Supabase·CDN)은 캐시하지 않고 항상 네트워크로
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  // 껍데기는 네트워크 우선, 실패하면 캐시 (오프라인이어도 화면은 뜹니다)
  e.respondWith(
    fetch(e.request)
      .then(r => {
        const copy = r.clone();
        caches.open(V).then(c => c.put(e.request, copy));
        return r;
      })
      .catch(() => caches.match(e.request).then(r => r || caches.match('./index.html')))
  );
});

self.addEventListener('message', e => {
  if (e.data && e.data.type === 'SKIP_WAITING') self.skipWaiting();
});
