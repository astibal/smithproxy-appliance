(() => {
  'use strict';
  const countdowns = [...document.querySelectorAll('.td-countdown')];

  function duration(seconds) {
    const value = Math.max(0, Math.ceil(seconds));
    const days = Math.floor(value / 86400);
    const hours = Math.floor((value % 86400) / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    const secs = value % 60;
    if (days) return `${days} d ${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
  }

  function updateCountdowns() {
    const now = Date.now();
    countdowns.forEach(element => {
      const deadline = Date.parse(element.dataset.deadline || '');
      const expiredAt = Date.parse(element.dataset.expiredAt || '');
      const expired = element.dataset.state === 'expired';
      if (expired && Number.isFinite(expiredAt)) {
        const remaining = (expiredAt + (3 * 3600 * 1000) - now) / 1000;
        element.textContent = remaining > 0 ? `recovery ${duration(remaining)}` : window.sasTr("ui.d86af7b852cb");
        element.classList.toggle('urgent', remaining <= 900);
        return;
      }
      if (!Number.isFinite(deadline)) {
        element.textContent = window.sasTr("programs.unlimited");
        return;
      }
      const remaining = (deadline - now) / 1000;
      element.textContent = remaining > 0 ? duration(remaining) : window.sasTr("ui.bb8a0de65e0f");
      element.classList.toggle('urgent', remaining <= 300);
      element.classList.toggle('elapsed', remaining <= 0);
    });
  }

  updateCountdowns();
  if (countdowns.length) window.setInterval(updateCountdowns, 1000);

  const root = document.querySelector('.test-drive-terminals');
  if (!root || typeof Terminal === 'undefined' || typeof FitAddon === 'undefined') return;
  const driveId = root.dataset.driveId;
  const csrf = root.dataset.csrf;
  const terminals = new Map();

  function open(kind) {
    let state = terminals.get(kind);
    if (!state) {
      const element = document.querySelector(`#td-${kind}`);
      const term = new Terminal({
        cursorBlink: true, scrollback: 5000, fontSize: 13,
        fontFamily: 'ui-monospace,SFMono-Regular,Menlo,Consolas,monospace',
        theme: {background: '#000', foreground: '#c9eee0', cursor: '#59e1ae',
          selectionBackground: '#2463eb', selectionForeground: '#fff'},
      });
      const fit = new FitAddon.FitAddon(); term.loadAddon(fit); term.open(element); fit.fit();
      state = {term, fit, socket: null}; terminals.set(kind, state);
      term.onData(data => { if (state.socket?.readyState === WebSocket.OPEN) state.socket.send(data); });
      new ResizeObserver(() => requestAnimationFrame(() => fit.fit())).observe(element);
    }
    if (state.socket) { state.socket.onclose = null; state.socket.close(); }
    state.term.reset(); state.fit.fit();
    const status = document.querySelector(`#td-${kind}-status`); status.textContent = 'connecting…';
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(`${protocol}//${location.host}/ws/test-drives/${encodeURIComponent(driveId)}/${kind}?csrf=${encodeURIComponent(csrf)}`);
    state.socket = socket;
    socket.onopen = () => { status.textContent = 'connected'; state.term.focus(); };
    socket.onmessage = event => state.term.write(event.data);
    socket.onerror = () => status.textContent = 'chyba';
    socket.onclose = event => { status.textContent = 'odpojeno'; if (event.reason) state.term.write(`\r\n\x1b[31m${event.reason}\x1b[0m\r\n`); };
  }

  document.querySelectorAll('[data-terminal][data-action]').forEach(button => button.addEventListener('click', () => {
    const state = terminals.get(button.dataset.terminal);
    if (button.dataset.action === 'clear') state?.term.clear();
    else open(button.dataset.terminal);
  }));
  const shellPanel = document.querySelector('#td-shell-panel');
  shellPanel?.addEventListener('toggle', () => {
    if (shellPanel.open) {
      open('shell');
      return;
    }
    const state = terminals.get('shell');
    if (state?.socket) {
      state.socket.onclose = null;
      state.socket.close();
      state.socket = null;
    }
    const status = document.querySelector('#td-shell-status');
    if (status) status.textContent = 'odpojeno';
  });
  open('cli');
})();
