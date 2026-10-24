// Keeps the page and the last data it loaded, so the installed app opens without a connection.
// Network first: online it's always the live Pi; the saved copy is only used when the Pi can't be
// reached. Saved copies carry X-HeartWatch-Saved (when they were fetched) so the page can say so.
const CACHE = "heartwatch-1";
const SAVED = ["/", "/api/series", "/api/workouts", "/api/settings"];
const WAIT_MS = 6000;  // weak signal: give up on the Pi after this and show the saved copy

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(["/manifest.webmanifest", "/icon-192.png", "/icon-512.png"])));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

async function save(cache, path, r) {
  const headers = new Headers(r.headers);
  headers.set("X-HeartWatch-Saved", new Date().toISOString());
  await cache.put(path, new Response(await r.blob(), { status: r.status, headers }));
}

async function networkFirst(req, path) {
  const cache = await caches.open(CACHE);
  try {
    const r = await Promise.race([fetch(req),
      new Promise((_, no) => setTimeout(() => no(new Error("timed out")), WAIT_MS))]);
    if (r.ok) await save(cache, path, r.clone());
    return r;
  } catch (err) {
    const saved = await cache.match(path);
    if (saved) return saved;
    throw err;
  }
}

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  // Only the plain views: a settings preview (?max_hr=...) isn't worth keeping.
  if (SAVED.includes(url.pathname) && !url.search) return e.respondWith(networkFirst(e.request, url.pathname));
  if (e.request.mode === "navigate") return e.respondWith(networkFirst(e.request, "/"));
  e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
});
