/* Feedback widget — fixed bottom-right button + small modal.
 *
 * Auto-injected by render_page on every authenticated HTML page.
 * window.__FEEDBACK_CONFIG__ = {userHandle, userTier} is set by the server.
 */
(function () {
  try {
    if (window.__FEEDBACK_WIDGET_LOADED__) return;
    window.__FEEDBACK_WIDGET_LOADED__ = true;

    var cfg = window.__FEEDBACK_CONFIG__ || {};
    if (!cfg.userHandle) return;

    var css =
      '#fb-btn{position:fixed;bottom:56px;right:20px;background:var(--bg-surface,#141414);border:1px solid var(--border-default,#2a2a2a);border-radius:999px;padding:8px 16px;font-size:12px;font-weight:500;color:var(--text-primary,#f0f0f0);cursor:pointer;z-index:9995;font-family:inherit;box-shadow:0 4px 12px rgba(0,0,0,0.3);transition:all .15s}' +
      '#fb-btn:hover{background:var(--bg-raised,#161616);border-color:var(--text-primary,#f0f0f0)}' +
      '#fb-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9996;align-items:center;justify-content:center;padding:24px;font-family:inherit}' +
      '#fb-modal.open{display:flex}' +
      '#fb-dialog{background:var(--bg-surface,#141414);border:1px solid var(--border-default,#2a2a2a);border-radius:12px;max-width:440px;width:100%;padding:24px;color:var(--text-primary,#f0f0f0)}' +
      '#fb-dialog h3{margin:0 0 14px;font-size:16px;font-weight:600}' +
      '#fb-dialog label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:0.08em;color:var(--text-tertiary,#555);margin:12px 0 6px}' +
      '#fb-dialog select,#fb-dialog textarea{width:100%;padding:10px;background:var(--bg-raised,#161616);color:var(--text-primary,#f0f0f0);border:1px solid var(--border-default,#2a2a2a);border-radius:6px;font-size:13px;font-family:inherit}' +
      '#fb-dialog textarea{resize:vertical;min-height:120px}' +
      '#fb-meta{font-size:11px;color:var(--text-tertiary,#555);margin-top:12px;padding-top:12px;border-top:1px solid var(--border-ghost,#141414)}' +
      '#fb-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:16px}' +
      '#fb-actions button{padding:9px 18px;border-radius:6px;font-size:13px;font-weight:500;cursor:pointer;font-family:inherit}' +
      '#fb-cancel{background:transparent;color:var(--text-secondary,#909090);border:1px solid var(--border-default,#2a2a2a)}' +
      '#fb-send{background:var(--text-primary,#f0f0f0);color:var(--interactive-text,#0d0d0d);border:1px solid var(--text-primary,#f0f0f0)}' +
      '#fb-send:disabled{opacity:0.5;cursor:wait}' +
      '#fb-prio-row{display:none}' +
      '#fb-dialog.type-bug #fb-prio-row{display:block}';
    var style = document.createElement('style');
    style.textContent = css;
    document.head.appendChild(style);

    var btn = document.createElement('button');
    btn.id = 'fb-btn';
    btn.type = 'button';
    btn.textContent = '? Feedback';
    btn.addEventListener('click', openModal);
    document.body.appendChild(btn);

    var modal = document.createElement('div');
    modal.id = 'fb-modal';
    modal.innerHTML =
      '<div id="fb-dialog">' +
      '<h3>Send feedback</h3>' +
      '<label for="fb-type">Type</label>' +
      '<select id="fb-type">' +
      '<option value="bug">Bug report</option>' +
      '<option value="feature">Feature request</option>' +
      '<option value="data">Something\'s wrong with data</option>' +
      '<option value="general">General feedback</option>' +
      '</select>' +
      '<label for="fb-msg">Describe it</label>' +
      '<textarea id="fb-msg" placeholder="What happened, or what would you like to see?"></textarea>' +
      '<div id="fb-prio-row">' +
      '<label for="fb-prio">Priority</label>' +
      '<select id="fb-prio"><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option><option value="critical">Critical</option></select>' +
      '</div>' +
      '<div id="fb-meta">' +
      'Page: <span id="fb-page"></span><br>' +
      'User: ' + escapeHtml(cfg.userHandle) + ' · ' + escapeHtml(cfg.userTier || 'none') +
      '</div>' +
      '<div id="fb-actions"><button id="fb-cancel" type="button">Cancel</button><button id="fb-send" type="button">Send feedback</button></div>' +
      '</div>';
    document.body.appendChild(modal);

    modal.addEventListener('click', function (e) {
      if (e.target === modal) closeModal();
    });
    document.getElementById('fb-cancel').addEventListener('click', closeModal);
    document.getElementById('fb-send').addEventListener('click', sendFeedback);
    document.getElementById('fb-type').addEventListener('change', function () {
      var dialog = document.getElementById('fb-dialog');
      dialog.className = 'type-' + this.value;
    });

    function openModal() {
      document.getElementById('fb-page').textContent = window.location.pathname;
      document.getElementById('fb-msg').value = '';
      document.getElementById('fb-type').value = 'bug';
      document.getElementById('fb-dialog').className = 'type-bug';
      document.getElementById('fb-prio').value = 'medium';
      modal.classList.add('open');
      setTimeout(function () { document.getElementById('fb-msg').focus(); }, 50);
    }
    function closeModal() {
      modal.classList.remove('open');
    }
    function sendFeedback() {
      var msg = document.getElementById('fb-msg').value.trim();
      if (msg.length < 3) {
        (window.narveToastError || window.alert)('Please enter a short description.');
        return;
      }
      var sendBtn = document.getElementById('fb-send');
      sendBtn.disabled = true;
      sendBtn.textContent = 'Sending…';
      var csrf = (document.cookie.match(/(?:^|;\s*)_csrf=([^;]*)/) || [])[1] || '';
      fetch('/api/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'x-csrf-token': csrf },
        body: JSON.stringify({
          type: document.getElementById('fb-type').value,
          message: msg,
          priority: document.getElementById('fb-prio').value,
          page_url: window.location.pathname
        })
      }).then(function (r) {
        sendBtn.disabled = false;
        sendBtn.textContent = 'Send feedback';
        if (r.ok) {
          sendBtn.textContent = 'Thanks!';
          setTimeout(closeModal, 900);
        } else {
          r.text().then(function (t) { (window.narveToastError || window.alert)('Failed: ' + t); });
        }
      }).catch(function () {
        sendBtn.disabled = false;
        sendBtn.textContent = 'Send feedback';
        (window.narveToastError || window.alert)('Network error.');
      });
    }
    function escapeHtml(s) {
      return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
      });
    }
  } catch (e) { /* never break the page */ }
})();
