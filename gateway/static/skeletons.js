/* Skeleton component library — window.narveSkel.
 *
 * Usage:
 *   narveSkel.show('feed-table', 'prediction-row', 8);
 *   fetch('/api/feed').then(r => r.json()).then(data => {
 *     narveSkel.hide('feed-table');
 *     renderFeed(data);
 *   }).catch(err => {
 *     narveSkel.error('feed-table', 'Could not load predictions.', () => reload());
 *   });
 *
 * Components built-in: prediction-row, bet-card, source-row, market-row,
 * stat-card, chart, detail-panel, chat-message.
 */
(function () {
  var templates = {
    'prediction-row': function () {
      return '<tr class="skeleton-row">' +
        '<td><div class="skeleton skeleton-text" style="width:65%"></div></td>' +
        '<td><div class="skeleton skeleton-text-sm" style="width:80px"></div></td>' +
        '<td><div class="skeleton skeleton-badge"></div></td>' +
        '<td><div class="skeleton skeleton-text-sm" style="width:50px"></div></td>' +
        '<td><div class="skeleton skeleton-text-sm" style="width:50px"></div></td>' +
        '<td><div class="skeleton skeleton-badge" style="width:40px"></div></td>' +
        '</tr>';
    },
    'bet-card': function () {
      return '<div class="skeleton-card">' +
        '<div class="skeleton skeleton-text-lg" style="width:85%;margin-bottom:8px"></div>' +
        '<div class="skeleton skeleton-text" style="width:60%;margin-bottom:20px"></div>' +
        '<div class="skeleton skeleton-text-sm" style="width:40px;margin-bottom:8px"></div>' +
        '<div class="skeleton skeleton-bar" style="width:100%;margin-bottom:16px"></div>' +
        '<div class="skeleton skeleton-text-sm" style="width:70%"></div>' +
        '</div>';
    },
    'source-row': function () {
      return '<tr class="skeleton-row">' +
        '<td><div style="display:flex;align-items:center;gap:12px">' +
          '<div class="skeleton skeleton-circle" style="width:32px;height:32px"></div>' +
          '<div class="skeleton skeleton-text" style="width:140px"></div>' +
        '</div></td>' +
        '<td><div class="skeleton skeleton-text-sm" style="width:60px"></div></td>' +
        '<td><div class="skeleton skeleton-badge"></div></td>' +
        '<td><div class="skeleton skeleton-text-sm" style="width:40px"></div></td>' +
        '</tr>';
    },
    'market-row': function () {
      return '<tr class="skeleton-row">' +
        '<td><div class="skeleton skeleton-text" style="width:72%"></div></td>' +
        '<td><div class="skeleton skeleton-badge"></div></td>' +
        '<td><div class="skeleton skeleton-text-sm" style="width:50px"></div></td>' +
        '<td><div class="skeleton skeleton-text-sm" style="width:70px"></div></td>' +
        '</tr>';
    },
    'stat-card': function () {
      return '<div class="skeleton-card">' +
        '<div class="skeleton skeleton-text-sm" style="width:60%;margin-bottom:12px"></div>' +
        '<div class="skeleton skeleton-text-lg" style="width:50%;height:32px"></div>' +
        '</div>';
    },
    'chart': function () {
      return '<div class="skeleton" style="width:100%;height:220px;border-radius:8px"></div>';
    },
    'detail-panel': function () {
      return '<div style="padding:24px">' +
        '<div class="skeleton skeleton-text-lg" style="width:90%;margin-bottom:16px"></div>' +
        '<div class="skeleton skeleton-text" style="width:70%;margin-bottom:32px"></div>' +
        '<div class="skeleton skeleton-bar" style="width:100%;margin-bottom:24px"></div>' +
        '<div class="skeleton skeleton-text" style="width:95%;margin-bottom:10px"></div>' +
        '<div class="skeleton skeleton-text" style="width:80%;margin-bottom:10px"></div>' +
        '<div class="skeleton skeleton-text" style="width:88%"></div>' +
        '</div>';
    },
    'chat-message': function () {
      return '<div style="margin-bottom:24px">' +
        '<div class="skeleton skeleton-text-sm" style="width:80px;margin-bottom:10px"></div>' +
        '<div class="skeleton skeleton-text" style="width:96%;margin-bottom:8px"></div>' +
        '<div class="skeleton skeleton-text" style="width:90%;margin-bottom:8px"></div>' +
        '<div class="skeleton skeleton-text" style="width:72%"></div>' +
        '</div>';
    },
  };

  function show(containerId, component, count) {
    var el = document.getElementById(containerId);
    if (!el) return;
    var fn = templates[component];
    if (!fn) {
      console.warn('narveSkel: unknown component', component);
      return;
    }
    var html = '';
    for (var i = 0; i < (count || 1); i++) html += fn();
    el.innerHTML = html;
    el.classList.remove('skeleton-fade-out');
  }

  function hide(containerId) {
    var el = document.getElementById(containerId);
    if (!el) return;
    el.classList.add('skeleton-fade-out');
    setTimeout(function () {
      el.innerHTML = '';
      el.classList.remove('skeleton-fade-out');
    }, 200);
  }

  function fadeInContent(containerId, html) {
    var el = document.getElementById(containerId);
    if (!el) return;
    el.innerHTML = html;
    el.classList.add('content-fade-in');
    setTimeout(function () { el.classList.remove('content-fade-in'); }, 350);
  }

  function error(containerId, message, retryFn) {
    var el = document.getElementById(containerId);
    if (!el) return;
    var btn = retryFn ? '<button onclick="(' + retryFn.toString() + ')()">Retry</button>' : '';
    el.innerHTML = '<div class="skeleton-error">' + (message || 'Something went wrong.') + btn + '</div>';
  }

  function registerTemplate(name, fn) {
    templates[name] = fn;
  }

  window.narveSkel = {
    show: show,
    hide: hide,
    fadeInContent: fadeInContent,
    error: error,
    registerTemplate: registerTemplate,
    templates: templates,
  };
})();
