(() => {
  const panel = document.querySelector('#netns-terminal');
  if (!panel) return;
  const screen = panel.querySelector('#netns-screen');
  const status = panel.querySelector('#netns-status');
  let terminal, fit, socket, id = '', size = 13;
  const send = value => { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(value)); };
  const resize = () => {
    if (!terminal || panel.hidden || !screen.getBoundingClientRect().width) return;
    fit.fit(); send({type: 'resize', cols: terminal.cols, rows: terminal.rows});
  };
  const disconnect = () => {
    const previous = socket; socket = null;
    if (previous) { previous.onclose = null; previous.close(); }
    status.textContent = '○';
  };
  const close = () => { disconnect(); panel.hidden = true; id = ''; };
  const connect = () => {
    if (!id) return;
    disconnect();
    if (!terminal) {
      terminal = new Terminal({fontSize: size, cursorBlink: true, scrollback: 10000,
        fontFamily: 'ui-monospace,monospace', theme: {background: '#000000', foreground: '#d8e8e2', selectionBackground: '#2463eb'}});
      fit = new FitAddon.FitAddon(); terminal.loadAddon(fit); terminal.open(screen);
      terminal.onData(data => send({type: 'input', data}));
      new ResizeObserver(resize).observe(screen);
    }
    terminal.reset(); resize(); status.textContent = '…';
    const current = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/instances/${encodeURIComponent(id)}/netns?csrf=${encodeURIComponent(panel.dataset.csrf)}`);
    socket = current;
    current.onopen = () => { if (socket !== current) return; status.textContent = '●'; resize(); terminal.focus(); };
    current.onmessage = event => { if (socket === current) terminal.write(event.data); };
    current.onclose = event => { if (socket !== current) return; socket = null; status.textContent = `○ (${event.code})`; if (event.reason) terminal.writeln('\r\n' + event.reason); };
    current.onerror = () => { if (socket === current) status.textContent = '⚠'; };
  };
  window.addEventListener('sas-netns-open', event => {
    if (!confirm(window.sasTr('netns.warning'))) return;
    close(); id = event.detail;
    document.querySelector('[data-pane="diag"]').append(panel);
    panel.querySelector('#netns-instance').textContent = id.slice(0, 12);
    panel.hidden = false; connect(); panel.scrollIntoView({block: 'nearest'});
  });
  window.addEventListener('sas-netns-close', close);
  window.addEventListener('pagehide', close);
  panel.addEventListener('click', event => {
    const action = event.target.dataset.netns;
    if (action === 'close') close();
    if (action === 'reconnect') connect();
    if (action === 'clear') terminal?.clear();
    if (action === 'smaller' || action === 'larger') {
      size = Math.max(10, Math.min(24, size + (action === 'larger' ? 1 : -1)));
      if (terminal) { terminal.options.fontSize = size; resize(); }
    }
  });
})();
