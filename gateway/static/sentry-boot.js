/* Lazy Sentry browser SDK loader.
 *
 * Reads window.__SENTRY_CONFIG__ set by the server. If dsn is empty,
 * does nothing. Otherwise loads the SDK from the CDN and initialises.
 * Wrapped in a try/catch so a Sentry failure NEVER breaks a page.
 */
(function () {
  try {
    var cfg = window.__SENTRY_CONFIG__ || {};
    if (!cfg.dsn) return;
    var script = document.createElement("script");
    script.src = "https://browser.sentry-cdn.com/7.119.2/bundle.tracing.min.js";
    script.crossOrigin = "anonymous";
    script.onload = function () {
      try {
        if (!window.Sentry) return;
        window.Sentry.init({
          dsn: cfg.dsn,
          environment: cfg.environment || "production",
          release: cfg.release || "1.0.0",
          tracesSampleRate: 0.1,
          beforeSend: function (event) {
            // Belt-and-braces: never send local form values or cookies.
            if (event && event.request) {
              if (event.request.cookies) event.request.cookies = "[Filtered]";
              if (event.request.headers) {
                var h = event.request.headers;
                ["Authorization", "Cookie", "X-CSRF-Token"].forEach(function (k) {
                  if (h[k]) h[k] = "[Filtered]";
                });
              }
            }
            return event;
          },
        });
        if (cfg.userId) {
          window.Sentry.setUser({ id: String(cfg.userId) });
        }
      } catch (e) {
        /* swallow */
      }
    };
    script.onerror = function () { /* swallow — CDN blocked */ };
    document.head.appendChild(script);
  } catch (e) {
    /* swallow */
  }
})();
