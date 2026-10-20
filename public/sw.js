// Сервис-воркер нужен браузеру, чтобы предложить «Установить приложение». Ничего не кэширует:
// все запросы идут в сеть как обычно, поэтому после выкладки сайт сразу обновляется.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
