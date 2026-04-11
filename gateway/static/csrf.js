/**
 * CSRF protection — auto-attaches CSRF token to all state-changing requests.
 *
 * Reads the _csrf cookie and includes it as:
 *   - X-CSRF-Token header on fetch() and XMLHttpRequest
 *   - htmx:configRequest header for HTMX requests
 *   - Hidden form field for plain HTML form submissions
 *
 * Also handles 429 (rate limit) and 403 CSRF errors gracefully.
 */
(function () {
  'use strict';

  var CSRF_COOKIE = '_csrf';
  var CSRF_HEADER = 'X-CSRF-Token';
  var STATE_METHODS = ['POST', 'PUT', 'PATCH', 'DELETE'];

  function getCsrfToken() {
    var cookies = document.cookie.split(';');
    for (var i = 0; i < cookies.length; i++) {
      var parts = cookies[i].trim().split('=');
      if (parts[0] === CSRF_COOKIE) {
        return decodeURIComponent(parts.slice(1).join('='));
      }
    }
    return null;
  }

  function isStateChanging(method) {
    return STATE_METHODS.indexOf((method || 'GET').toUpperCase()) !== -1;
  }

  // ── Monkey-patch fetch() ────────────────────────────────────────────────
  var originalFetch = window.fetch;
  window.fetch = function (url, options) {
    options = options || {};
    if (isStateChanging(options.method)) {
      var token = getCsrfToken();
      if (token) {
        if (options.headers instanceof Headers) {
          if (!options.headers.has(CSRF_HEADER)) {
            options.headers.set(CSRF_HEADER, token);
          }
        } else if (typeof options.headers === 'object' && options.headers !== null) {
          if (!options.headers[CSRF_HEADER]) {
            options.headers[CSRF_HEADER] = token;
          }
        } else {
          options.headers = {};
          options.headers[CSRF_HEADER] = token;
        }
      }
    }
    return originalFetch.call(this, url, options);
  };

  // ── Monkey-patch XMLHttpRequest ─────────────────────────────────────────
  var originalOpen = XMLHttpRequest.prototype.open;
  var originalSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method) {
    this._csrfMethod = method;
    return originalOpen.apply(this, arguments);
  };

  XMLHttpRequest.prototype.send = function () {
    if (isStateChanging(this._csrfMethod)) {
      var token = getCsrfToken();
      if (token) {
        try {
          this.setRequestHeader(CSRF_HEADER, token);
        } catch (e) {
          // Headers already sent — ignore
        }
      }
    }
    return originalSend.apply(this, arguments);
  };

  // ── HTMX integration ───────────────────────────────────────────────────
  document.addEventListener('htmx:configRequest', function (event) {
    if (isStateChanging(event.detail.verb)) {
      var token = getCsrfToken();
      if (token) {
        event.detail.headers[CSRF_HEADER] = token;
      }
    }
  });

  // ── HTML form injection ─────────────────────────────────────────────────
  // Inject hidden CSRF field into all POST forms on DOMContentLoaded
  function injectCsrfFields() {
    var forms = document.querySelectorAll('form');
    for (var i = 0; i < forms.length; i++) {
      var form = forms[i];
      var method = (form.getAttribute('method') || 'GET').toUpperCase();
      if (isStateChanging(method) && !form.querySelector('[name="_csrf"]')) {
        var token = getCsrfToken();
        if (token) {
          var input = document.createElement('input');
          input.type = 'hidden';
          input.name = '_csrf';
          input.value = token;
          form.appendChild(input);
        }
      }
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', injectCsrfFields);
  } else {
    injectCsrfFields();
  }

  // Also re-inject when HTMX swaps content
  document.addEventListener('htmx:afterSwap', injectCsrfFields);

  // ── Error handling: 403 CSRF and 429 rate limit ─────────────────────────

  // Toast notification system
  function showToast(message, type) {
    type = type || 'error';
    var toast = document.createElement('div');
    toast.className = 'security-toast security-toast-' + type;
    toast.textContent = message;
    toast.style.cssText =
      'position:fixed;top:20px;right:20px;z-index:99999;padding:12px 20px;' +
      'border-radius:8px;font-size:13px;font-weight:500;max-width:400px;' +
      'box-shadow:0 4px 12px rgba(0,0,0,0.3);transition:opacity 0.3s;' +
      'color:#fff;background:' + (type === 'error' ? '#dc2626' : '#f59e0b') + ';';
    document.body.appendChild(toast);
    setTimeout(function () {
      toast.style.opacity = '0';
      setTimeout(function () {
        if (toast.parentNode) toast.parentNode.removeChild(toast);
      }, 300);
    }, 5000);
  }

  // Handle HTMX error responses
  document.addEventListener('htmx:responseError', function (event) {
    var xhr = event.detail.xhr;
    if (!xhr) return;

    if (xhr.status === 429) {
      var retryAfter = xhr.getResponseHeader('Retry-After') || '60';
      showToast('Too many requests. Try again in ' + retryAfter + ' seconds.', 'error');
      event.preventDefault();
    }

    if (xhr.status === 403) {
      var csrfError = xhr.getResponseHeader('X-CSRF-Error');
      if (csrfError) {
        showToast('Session expired. Refreshing page...', 'error');
        setTimeout(function () {
          window.location.reload();
        }, 1500);
        event.preventDefault();
      }
    }
  });

  // Expose for use in custom fetch wrappers
  window.__csrf = {
    getToken: getCsrfToken,
    showToast: showToast,
  };
})();
