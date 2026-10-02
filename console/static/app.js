(() => {
  const q = selector => document.querySelector(selector);
  const text = (selector, value) => { const node = q(selector); if (node) node.textContent = value; };
  const formatBytes = bytes => bytes ? `${(bytes / 1048576).toFixed(1)} MiB` : '0 MiB';
  const activeState = state => ['starting', 'running', 'orphaned'].includes(state);
  const problemState = state => ['failed', 'expired', 'orphaned'].includes(state);
  const short = value => value ? String(value).slice(0, 12) : '—';
  const ttl = deadline => {
    if (!deadline) return 'bez limitu';
    const seconds = Math.max(0, Math.round((new Date(deadline).getTime() - Date.now()) / 1000));
    if (seconds < 60) return `${seconds} s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min ${seconds % 60} s`;
    return `${Math.floor(seconds / 3600)} h ${Math.floor((seconds % 3600) / 60)} min`;
  };

  function setupSpawnForm() {
    const configSelect = q('#instance-config');
    const runtimeSelection = q('#runtime-selection');
    const profileSelect = q('#runtime-profile');
    const profilePanel = q('#profile-selection');
    const manualPanel = q('#manual-selection');
    const placeholderFields = q('#placeholder-fields');
    if (configSelect && runtimeSelection && profileSelect && placeholderFields) {
      const updatePlaceholderFields = () => {
        const source = runtimeSelection.value === 'profile' ? profileSelect : configSelect;
        const placeholders = (source.selectedOptions[0]?.dataset.placeholders || '')
          .split(',').map(value => value.trim()).filter(Boolean);
        placeholderFields.replaceChildren(...placeholders.map(name => {
          const label = document.createElement('label');
          label.textContent = `{{${name}}}`;
          const input = document.createElement('input');
          input.name = `placeholder__${name}`;
          input.required = true;
          input.maxLength = 512;
          input.autocomplete = 'off';
          label.appendChild(input);
          return label;
        }));
      };
      const updateSelection = () => {
        const useProfile = runtimeSelection.value === 'profile';
        profilePanel.hidden = !useProfile;
        manualPanel.hidden = useProfile;
        profileSelect.disabled = !useProfile;
        profileSelect.required = useProfile;
        manualPanel.querySelectorAll('select').forEach(select => {
          select.disabled = useProfile;
          select.required = !useProfile;
        });
        updatePlaceholderFields();
      };
      configSelect.addEventListener('change', updatePlaceholderFields);
      profileSelect.addEventListener('change', updatePlaceholderFields);
      runtimeSelection.addEventListener('change', updateSelection);
      if (profileSelect.options.length <= 1) runtimeSelection.value = 'manual';
      updateSelection();
    }
    const dialog = q('#spawn-dialog');
    if (!dialog) return;
    const form = q('#spawn-form');
    const submit = q('#spawn-submit');
    const state = q('#spawn-submit-state');
    const close = () => { dialog.close(); if (state) { state.textContent = ''; state.className = 'spawn-submit-state'; } };
    q('#spawn-open')?.addEventListener('click', () => dialog.showModal());
    q('#spawn-close')?.addEventListener('click', close);
    q('#spawn-cancel')?.addEventListener('click', close);
    dialog.addEventListener('click', event => { if (event.target === dialog) close(); });
    form?.addEventListener('submit', async event => {
      event.preventDefault();
      if (submit?.disabled) return;
      if (submit) { submit.disabled = true; submit.textContent = 'Zařazuji…'; }
      if (state) { state.textContent = 'Předávám požadavek do fronty…'; state.className = 'spawn-submit-state'; }
      try {
        const response = await fetch(form.action, {
          method: 'POST', body: new FormData(form),
          headers: {'X-Requested-With': 'task-fetch'}, cache: 'no-store',
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        if (state) {
          state.textContent = `${data.message || 'Spuštění zařazeno'} · task ${short(data.task_id)}`;
          state.className = 'spawn-submit-state ok';
        }
        document.dispatchEvent(new CustomEvent('task-queued'));
        setTimeout(close, 450);
      } catch (error) {
        if (state) { state.textContent = error.message || String(error); state.className = 'spawn-submit-state error'; }
      } finally {
        if (submit) { submit.disabled = false; submit.textContent = 'Zařadit spuštění'; }
      }
    });
  }

  function setupProfileBuildUpgrade() {
    document.querySelectorAll('[data-use-profile-build]').forEach(button => {
      button.addEventListener('click', () => {
        const select = q('#profile-build-id');
        if (!select) return;
        select.value = button.dataset.useProfileBuild;
        select.dispatchEvent(new Event('change', {bubbles: true}));
        button.textContent = 'Vybráno';
        button.disabled = true;
      });
    });
  }

  function setupBuildPolling() {
    const build = q('#build');
    if (!build || build.dataset.state !== 'running') return;
    const poll = async () => {
      try {
        const response = await fetch('/api/build', {cache: 'no-store'});
        const data = await response.json();
        text('#build-state', data.state);
        text('#build-revision', (data.revision || '').slice(0, 12));
        const log = q('#build-log');
        if (log) log.textContent = data.log || data.error || '';
        if (data.state === 'running') setTimeout(poll, 2000);
        else setTimeout(() => location.reload(), 700);
      } catch (_error) { setTimeout(poll, 3000); }
    };
    setTimeout(poll, 1000);
  }

  function setupProfileTtlControls() {
    document.querySelectorAll('input[name="ttl_unlimited"]').forEach(toggle => {
      const field = toggle.form?.querySelector('input[name="ttl_seconds"]');
      if (!field) return;
      const update = () => {
        field.disabled = toggle.checked;
        field.setAttribute('aria-disabled', String(toggle.checked));
      };
      toggle.addEventListener('change', update);
      update();
    });
  }

  function setupBuildForm() {
    const form = q('#build-form');
    const submit = q('#build-submit');
    const state = q('#build-enqueue-state');
    if (!form || !state) return;
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (submit?.disabled) return;
      const ref = form.elements.ref?.value || 'master';
      if (submit) { submit.disabled = true; submit.textContent = 'Zařazuji…'; }
      state.textContent = `'${ref}' build task is being enqueued…`;
      state.className = 'build-enqueue-state';
      try {
        const response = await fetch(form.action, {
          method: 'POST', body: new FormData(form),
          headers: {'X-Requested-With': 'task-fetch'}, cache: 'no-store',
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        state.textContent = `${data.message} · task ${short(data.task_id)}`;
        state.className = 'build-enqueue-state ok';
        document.dispatchEvent(new CustomEvent('task-queued'));
      } catch (error) {
        state.textContent = error.message || String(error);
        state.className = 'build-enqueue-state error';
      } finally {
        if (submit) { submit.disabled = false; submit.textContent = 'Build / rebuild'; }
      }
    });
  }

  function setupTaskDock() {
    const dock = q('#task-dock');
    const drawer = q('#task-drawer');
    const list = q('#task-list');
    if (!dock || !drawer || !list) return;
    document.body.classList.add('has-task-dock');
    q('#task-dock-toggle')?.addEventListener('click', () => {
      drawer.hidden = !drawer.hidden;
      dock.classList.toggle('open', !drawer.hidden);
    });
    const stamp = value => value ? new Date(value).toLocaleTimeString() : '—';
    const render = tasks => {
      const active = tasks.filter(task => ['pending', 'running'].includes(task.state));
      text('#task-active-count', active.length);
      const running = active.filter(task => task.state === 'running').length;
      const pending = active.length - running;
      text('#task-dock-summary', active.length ? `${running} běží · ${pending} čeká` : 'nic neběží');
      const ordered = [...active, ...tasks.filter(task => !['pending', 'running'].includes(task.state))].slice(0, 30);
      if (!ordered.length) {
        const empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = 'Zatím žádné úlohy.';
        list.replaceChildren(empty); return;
      }
      list.replaceChildren(...ordered.map(task => {
        const row = document.createElement('div'); row.className = `task-row task-${task.state}`;
        const state = document.createElement('span'); state.className = 'task-state'; state.textContent = task.state;
        const body = document.createElement('span'); body.className = 'task-label';
        const label = document.createElement('b'); label.textContent = task.label;
        const detail = document.createElement('small');
        detail.textContent = task.error || `${task.kind} · ${stamp(task.started_at || task.created_at)}`;
        body.append(label, detail);
        const resultId = task.result?.id;
        const viewable = task.state === 'succeeded' && [
          'config-preview', 'build-config-preview', 'instance-config-preview', 'config-observer'
        ].includes(task.kind);
        let tail;
        if (resultId) {
          tail = document.createElement('a'); tail.href = `/?instance=${encodeURIComponent(resultId)}`; tail.textContent = short(resultId);
        } else if (viewable) {
          tail = document.createElement('a');
          tail.href = `/tasks/${encodeURIComponent(task.task_id)}/result`;
          tail.textContent = 'Otevřít výsledek →';
        } else {
          tail = document.createElement('code'); tail.textContent = short(task.task_id);
        }
        row.append(state, body, tail); return row;
      }));
    };
    let pollTimer;
    let pollInFlight = false;
    const schedulePoll = delay => {
      clearTimeout(pollTimer);
      pollTimer = setTimeout(poll, delay);
    };
    const poll = async () => {
      if (pollInFlight) return;
      pollInFlight = true;
      try {
        const response = await fetch('/api/tasks', {cache: 'no-store'});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        render(data.tasks || []);
      } catch (error) {
        text('#task-dock-summary', `chyba: ${error.message || error}`);
      } finally {
        pollInFlight = false;
        schedulePoll(1500);
      }
    };
    document.addEventListener('task-queued', () => schedulePoll(0));
    poll();
  }

  function setupRuntimeWorkspace() {
    const workspace = q('#runtime-workspace');
    if (!workspace) return;
    const list = q('#instance-list');
    const detail = q('#instance-detail');
    const csrf = workspace.dataset.csrf;
    const initialParams = new URLSearchParams(location.search);
    const focusMode = initialParams.get('focus') === '1';
    let instances = [];
    let selectedId = initialParams.get('instance') || '';
    let currentView = initialParams.get('view') || 'overview';
    let filter = 'all';
    let query = '';
    let polling = false;
    let logsRequest = 0;
    let diagnosticsRequest = 0;
    if (focusMode) document.body.classList.add('instance-focus-mode');

    function focusedInstanceUrl(instanceId, view = currentView) {
      const url = new URL('/', location.origin);
      url.searchParams.set('instance', instanceId);
      if (view !== 'overview') url.searchParams.set('view', view);
      url.searchParams.set('focus', '1');
      return url.toString();
    }

    function updatePopoutLink() {
      const link = q('#detail-popout');
      if (!link || !selectedId) return;
      link.href = focusedInstanceUrl(selectedId);
    }

    const updateUrl = () => {
      const url = new URL(location.href);
      if (selectedId) url.searchParams.set('instance', selectedId); else url.searchParams.delete('instance');
      if (selectedId && currentView !== 'overview') url.searchParams.set('view', currentView); else url.searchParams.delete('view');
      history.replaceState({}, '', url);
      updatePopoutLink();
    };

    function renderMetrics() {
      text('#metric-running', instances.filter(item => ['running', 'starting'].includes(item.state)).length);
      text('#metric-orphaned', instances.filter(item => item.state === 'orphaned').length);
      text('#metric-total', instances.length);
      text('#metric-rss', formatBytes(instances.reduce((sum, item) => sum + (item.rss_bytes || 0), 0)));
    }

    function visibleInstances() {
      const needle = query.toLowerCase();
      return instances.filter(item => {
        if (filter === 'active' && !activeState(item.state)) return false;
        if (filter === 'problem' && !problemState(item.state)) return false;
        if (!needle) return true;
        return [item.id, item.source_ip, item.user_id, item.profile, item.namespace, item.state]
          .some(value => String(value || '').toLowerCase().includes(needle));
      });
    }

    function makeInstanceCard(item) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `instance-card state-${item.state}${item.id === selectedId ? ' selected' : ''}`;
      button.dataset.id = item.id;
      button.title = item.id;
      const state = document.createElement('span'); state.className = 'instance-cell instance-cell-state';
      const stateDot = document.createElement('i');
      const stateText = document.createElement('em'); stateText.textContent = item.state;
      state.append(stateDot, stateText);
      const source = document.createElement('b'); source.className = 'instance-cell instance-cell-source'; source.textContent = item.source_ip || '—';
      const identity = document.createElement('code'); identity.className = 'instance-cell instance-cell-id'; identity.textContent = short(item.id);
      const owner = document.createElement('span'); owner.className = 'instance-cell instance-card-owner';
      const user = document.createElement('strong'); user.textContent = item.user_id || 'unknown';
      const profile = document.createElement('small'); profile.textContent = `${item.profile || 'custom'}${item.persistent ? ' · persistent' : ''}`;
      owner.append(user, profile);
      const pid = document.createElement('code'); pid.className = 'instance-cell'; pid.textContent = item.pid || '—';
      const rss = document.createElement('span'); rss.className = 'instance-cell'; rss.textContent = formatBytes(item.rss_bytes || 0);
      const deadline = document.createElement('span'); deadline.className = 'instance-cell instance-cell-ttl'; deadline.textContent = ttl(item.deadline);
      button.append(state, source, identity, owner, pid, rss, deadline);
      button.addEventListener('click', () => selectInstance(item.id));
      const popout = document.createElement('a');
      popout.className = 'instance-card-popout';
      popout.href = focusedInstanceUrl(item.id, 'overview');
      popout.target = '_blank'; popout.rel = 'noopener';
      popout.title = `Otevřít ${short(item.id)} v samostatném tabu`;
      popout.setAttribute('aria-label', popout.title);
      popout.textContent = '↗';
      const row = document.createElement('div'); row.className = 'instance-row';
      row.append(button, popout);
      return row;
    }

    function renderList() {
      const items = visibleInstances();
      if (!items.length) {
        const empty = document.createElement('p'); empty.className = 'empty';
        empty.textContent = instances.length ? 'Filtru neodpovídá žádná instance.' : 'Žádné instance.';
        list.replaceChildren(empty); return;
      }
      list.replaceChildren(...items.map(makeInstanceCard));
    }

    function setForm(formSelector, path, visible) {
      const form = q(formSelector);
      if (!form) return;
      form.action = path;
      form.hidden = !visible;
      const token = form.querySelector('[name=csrf_token]'); if (token) token.value = csrf;
    }

    function renderDetail() {
      const item = instances.find(candidate => candidate.id === selectedId);
      q('#detail-empty').hidden = Boolean(item);
      q('#detail-content').hidden = !item;
      detail.classList.toggle('has-selection', Boolean(item));
      if (!item) return;
      text('#detail-state', item.state);
      q('#detail-state-dot').className = `state-${item.state}`;
      text('#detail-short-id', short(item.id)); text('#detail-full-id', item.id);
      text('#detail-pid', item.pid ? `PID ${item.pid}` : 'PID —');
      text('#detail-rss', `RSS ${formatBytes(item.rss_bytes || 0)} · CLI ${item.cli_port ? `:${item.cli_port}` : '—'}`);
      text('#detail-source', item.source_ip || '—'); text('#detail-user', item.user_id || 'unknown user');
      text('#detail-namespace', item.namespace || '—'); text('#detail-profile', `${item.profile || 'custom'} · ${item.unit || 'unit unknown'}`);
      text('#detail-ttl', ttl(item.deadline)); text('#detail-deadline', item.deadline ? new Date(item.deadline).toLocaleString() : 'bez deadline');
      text('#detail-build', short(item.build_id)); text('#detail-runtime-profile', `${item.runtime_profile_id ? `profil ${short(item.runtime_profile_id)}` : 'ruční výběr'}${item.persistent ? ' · persistent' : ''}`);
      text('#detail-config-id', short(item.config_id)); text('#detail-config-mode', `${String(item.config_mode || 'ro').toUpperCase()} · cert ${short(item.cert_bundle_id)}`);
      if (focusMode) document.title = `Smithproxy ${short(item.id)} · ${item.source_ip || 'instance'}`;
      const result = q('#detail-result'); result.hidden = !item.result; result.textContent = item.result || '';
      q('#detail-config').href = `/instances/${encodeURIComponent(item.id)}/config/download`;
      setForm('#detail-restart', `/instances/${encodeURIComponent(item.id)}/restart`, item.state === 'running');
      setForm('#detail-extend', `/instances/${encodeURIComponent(item.id)}/extend`, activeState(item.state));
      setForm('#detail-stop', `/instances/${encodeURIComponent(item.id)}/stop`, activeState(item.state));
      setForm('#detail-delete', `/instances/${encodeURIComponent(item.id)}/delete`, !activeState(item.state));
      const consoleTab = q('[data-view=console]'); consoleTab.disabled = item.state !== 'running';
      updatePopoutLink();
      if (currentView === 'console' && item.state !== 'running') switchView('overview');
    }

    function selectInstance(id) {
      if (selectedId !== id) {
        disconnectTerminal();
        hideGdbTerminal();
      }
      selectedId = id;
      renderList(); renderDetail(); updateUrl();
      if (currentView === 'console') openTerminal(id);
      if (currentView === 'logs') loadLogs();
      if (currentView === 'diag') loadDiagnostics();
      if (matchMedia('(max-width: 820px)').matches) detail.scrollIntoView({behavior: 'smooth', block: 'start'});
    }

    async function loadLogs() {
      const output = q('#terminal-log-output');
      if (!selectedId || !output) return;
      const instanceId = selectedId;
      const requestId = ++logsRequest;
      output.textContent = 'Načítám…';
      try {
        const response = await fetch(`/api/instances/${encodeURIComponent(instanceId)}/logs`, {cache: 'no-store'});
        const data = await response.json();
        if (requestId !== logsRequest || instanceId !== selectedId) return;
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        output.textContent = data.output || 'Žádné logy.';
      } catch (error) {
        if (requestId === logsRequest && instanceId === selectedId) {
          output.textContent = `Logy nelze načíst: ${error.message || error}`;
        }
      }
    }

    function appendDiagRow(root, label, value) {
      const name = document.createElement('span'); name.textContent = label;
      const code = document.createElement('code'); code.textContent = value || '—';
      root.append(name, code);
    }

    function formatInterfaces(items) {
      return (items || []).map(item => {
        const addresses = (item.addr_info || []).map(address =>
          `${address.local || '?'}${address.prefixlen === undefined ? '' : `/${address.prefixlen}`} (${address.family || '?'}, ${address.scope || '?'})`
        );
        return `${item.ifname || '?'} [${item.operstate || 'unknown'}] ${addresses.join(', ') || 'bez adresy'}`;
      }).join('\n') || '—';
    }

    function formatRoutes(items) {
      return (items || []).map(route => {
        const parts = [route.dst || 'default'];
        if (route.gateway) parts.push(`via ${route.gateway}`);
        if (route.dev) parts.push(`dev ${route.dev}`);
        if (route.table) parts.push(`table ${route.table}`);
        if (route.protocol) parts.push(`proto ${route.protocol}`);
        return parts.join(' ');
      }).join('\n') || '—';
    }

    function appendDebugControls(root, data) {
      const execution = data.execution || {};
      const debug = execution.debug || {};
      const instance = data.instance || {};
      const panel = document.createElement('section'); panel.className = 'debug-panel';
      const heading = document.createElement('div'); heading.className = 'pane-heading';
      const copy = document.createElement('div');
      const title = document.createElement('h3'); title.textContent = 'Remote GDB';
      const hint = document.createElement('small');
      hint.textContent = 'gdbserver sdílí network namespace instance a dostane pouze CAP_SYS_PTRACE.';
      copy.append(title, hint); heading.append(copy);

      if (execution.build_type === 'Debug' && instance.state === 'running') {
        const form = document.createElement('form'); form.method = 'post';
        form.action = `/instances/${encodeURIComponent(selectedId)}/debug/${debug.unit ? 'stop' : 'start'}`;
        const csrf = document.createElement('input');
        csrf.type = 'hidden'; csrf.name = 'csrf_token';
        csrf.value = q('#runtime-workspace')?.dataset.csrf || '';
        const button = document.createElement('button');
        button.className = debug.unit ? 'danger compact' : 'secondary compact';
        button.textContent = debug.unit ? 'Zastavit gdbserver' : 'Spustit gdbserver';
        form.append(csrf, button); heading.append(form);
        const terminalButton = document.createElement('button');
        terminalButton.type = 'button'; terminalButton.className = 'secondary compact';
        terminalButton.textContent = 'Otevřít GDB terminal';
        terminalButton.addEventListener('click', () => openGdbTerminal(instance.id));
        heading.append(terminalButton);
      }
      panel.append(heading);

      if (execution.build_type !== 'Debug') {
        const note = document.createElement('p'); note.className = 'empty';
        note.textContent = 'Attach je povolen jen pro instanci spuštěnou z archivovaného Debug buildu.';
        panel.append(note);
      } else if (!debug.unit) {
        const note = document.createElement('p'); note.className = 'empty';
        note.textContent = instance.state === 'running' ? 'GDB helper není spuštěn.' : 'Instance musí být spuštěná.';
        panel.append(note);
      } else {
        const commands = document.createElement('div'); commands.className = 'diag-grid';
        appendDiagRow(commands, 'Helper unit', debug.unit);
        appendDiagRow(commands, '1. SSH tunel', debug.ssh_tunnel);
        appendDiagRow(commands, '2. Binárka se symboly', debug.copy_binary);
        appendDiagRow(commands, '3. GDB', debug.gdb_commands);
        panel.append(commands);
      }
      root.append(panel);
    }

    async function loadDiagnostics() {
      const output = q('#diag-content');
      if (!selectedId || !output) return;
      const instanceId = selectedId;
      const requestId = ++diagnosticsRequest;
      output.replaceChildren(Object.assign(document.createElement('p'), {className: 'empty', textContent: 'Načítám…'}));
      try {
        const response = await fetch(`/api/instances/${encodeURIComponent(instanceId)}/diagnostics`, {cache: 'no-store'});
        const data = await response.json();
        if (requestId !== diagnosticsRequest || instanceId !== selectedId) return;
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        const execution = data.execution || {};
        const grid = document.createElement('div'); grid.className = 'diag-grid';
        appendDiagRow(grid, 'Model', execution.model);
        appendDiagRow(grid, 'Systemd unit', execution.unit);
        appendDiagRow(grid, 'Root filesystem', `${execution.rootfs || ''} · ${execution.rootfs_mode || ''}`);
        appendDiagRow(grid, 'Network namespace', `${execution.network_namespace || ''} · ${execution.network_namespace_path || ''}`);
        const network = execution.network || {};
        appendDiagRow(grid, 'Síťový rozsah', network.subnet);
        appendDiagRow(grid, 'Host veth', `${network.host_interface || '—'} · ${network.expected_host_address || '—'}`);
        appendDiagRow(grid, 'Namespace veth', `${network.guest_interface || '—'} · ${network.expected_guest_address || '—'}`);
        appendDiagRow(grid, 'Host interface — skutečnost', formatInterfaces(network.host_interfaces));
        appendDiagRow(grid, 'Namespace interface — skutečnost', formatInterfaces(network.namespace_interfaces));
        appendDiagRow(grid, 'Namespace routy', formatRoutes(network.namespace_routes));
        appendDiagRow(grid, 'Host policy routy', formatRoutes(network.host_routes));
        appendDiagRow(grid, 'Policy routing', `table ${network.route_table || '—'} · fwmark ${network.packet_mark || '—'} · ${network.present ? 'active' : 'not present'}`);
        appendDiagRow(grid, 'Runtime directory', execution.runtime_dir);
        appendDiagRow(grid, 'Live config', execution.live_config);
        appendDiagRow(grid, 'Binary', `${execution.binary || ''} · ${execution.build_type || ''}`);
        const instance = data.instance || {};
        setGdbTerminalAvailable(
          instance.id,
          execution.build_type === 'Debug' && instance.state === 'running',
        );
        appendDiagRow(grid, 'Auto-restart', instance.auto_restart ? `ano · ${instance.restart_count || 0}/5 pokusů` : 'ne');
        appendDiagRow(grid, 'Poslední restart', instance.last_restart_at || '—');
        output.replaceChildren(grid);
        if (instance.crash_trace) {
          const crash = document.createElement('section'); crash.className = 'debug-panel';
          const title = document.createElement('h3'); title.textContent = 'Poslední automatický stack trace';
          const meta = document.createElement('p'); meta.className = 'hint';
          meta.textContent = `PID ${instance.crash_pid || '—'} · ${instance.crash_at || 'čas neznámý'}`;
          const trace = document.createElement('pre'); trace.className = 'runtime-log crash-trace';
          trace.textContent = instance.crash_trace;
          crash.append(title, meta, trace); output.append(crash);
        }
        appendDebugControls(output, data);
      } catch (error) {
        if (requestId === diagnosticsRequest && instanceId === selectedId) {
          output.replaceChildren(Object.assign(document.createElement('p'), {className: 'flash error', textContent: String(error.message || error)}));
        }
      }
    }

    function switchView(view, changeUrl = true) {
      if (!['overview', 'console', 'logs', 'diag'].includes(view)) view = 'overview';
      currentView = view;
      document.querySelectorAll('.detail-tabs [data-view]').forEach(button => button.classList.toggle('active', button.dataset.view === view));
      document.querySelectorAll('.detail-pane').forEach(pane => { pane.hidden = pane.dataset.pane !== view; });
      if (view === 'console') openTerminal(selectedId);
      else {
        disconnectTerminal();
        if (view === 'logs') loadLogs();
        if (view === 'diag') loadDiagnostics();
      }
      if (changeUrl) updateUrl();
    }

    async function pollInstances(manual = false) {
      if (polling) return;
      polling = true;
      text('#instance-poll-state', manual ? 'ověřuji…' : 'aktualizuji…');
      try {
        const response = await fetch('/api/instances', {cache: 'no-store'});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        instances = (data.instances || []).sort((a, b) => Number(activeState(b.state)) - Number(activeState(a.state)) || String(b.created_at).localeCompare(String(a.created_at)));
        if (selectedId && !instances.some(item => item.id === selectedId)) selectedId = '';
        renderMetrics(); renderList(); renderDetail();
        text('#instance-poll-state', `ověřeno ${new Date().toLocaleTimeString()}`);
        q('#runner-status').textContent = 'ok'; q('#runner-status').classList.add('ok');
      } catch (error) {
        text('#instance-poll-state', `chyba: ${error.message || error}`);
        q('#runner-status').textContent = 'unavailable'; q('#runner-status').classList.remove('ok');
      } finally { polling = false; }
    }

    q('#instance-refresh')?.addEventListener('click', () => pollInstances(true));
    q('#instance-search')?.addEventListener('input', event => { query = event.target.value.trim(); renderList(); });
    document.querySelectorAll('.instance-filters [data-filter]').forEach(button => button.addEventListener('click', () => {
      filter = button.dataset.filter;
      document.querySelectorAll('.instance-filters [data-filter]').forEach(candidate => candidate.classList.toggle('active', candidate === button));
      renderList();
    }));
    document.querySelectorAll('.detail-tabs [data-view]').forEach(button => button.addEventListener('click', () => switchView(button.dataset.view)));
    q('#terminal-refresh-logs')?.addEventListener('click', loadLogs);
    q('#diag-refresh')?.addEventListener('click', loadDiagnostics);
    q('#detail-back')?.addEventListener('click', () => q('.instance-browser')?.scrollIntoView({behavior: 'smooth'}));
    switchView(currentView, false);
    pollInstances();
    setInterval(pollInstances, 3000);
    setInterval(() => { if (selectedId) { renderList(); renderDetail(); } }, 1000);
  }

  const panel = q('#terminal');
  const screen = q('#terminal-screen');
  const terminalStatus = q('#terminal-status');
  const fontSizeValue = q('#font-size-value');
  let terminalId = '';
  let socket = null;
  let term = null;
  let fit = null;
  let fontSize = Math.min(22, Math.max(10, Number(localStorage.getItem('smithproxy-terminal-font-size')) || 13));
  if (fontSizeValue) fontSizeValue.textContent = `${fontSize} px`;

  function redrawTerminal() {
    if (!term || !panel || panel.hidden) return;
    const atBottom = term.buffer.active.viewportY >= term.buffer.active.baseY;
    requestAnimationFrame(() => {
      fit.fit();
      requestAnimationFrame(() => {
        term.refresh(0, Math.max(0, term.rows - 1));
        if (atBottom) term.scrollToBottom();
      });
    });
  }
  function ensureTerminal() {
    if (term || !screen) return;
    term = new Terminal({cursorBlink: true, convertEol: false, scrollback: 5000,
      fontFamily: 'ui-monospace,SFMono-Regular,Menlo,Consolas,monospace', fontSize,
      theme: {background: '#000000', foreground: '#c9eee0', cursor: '#59e1ae', selectionBackground: '#2463eb', selectionInactiveBackground: '#1d4ed8', selectionForeground: '#ffffff'}});
    fit = new FitAddon.FitAddon(); term.loadAddon(fit); term.open(screen);
    term.onData(data => { if (socket?.readyState === WebSocket.OPEN) socket.send(data); });
    new ResizeObserver(redrawTerminal).observe(screen); redrawTerminal();
  }
  function showTerminalError(message) { if (term) term.write(`\r\n\x1b[31m${message}\x1b[0m\r\n`); }
  function disconnectTerminal() {
    if (socket) { socket.onclose = null; socket.close(); socket = null; }
    terminalId = '';
    if (terminalStatus) terminalStatus.textContent = 'odpojeno';
  }
  function reconnectTerminal() {
    if (!terminalId || !panel) return;
    ensureTerminal();
    if (socket) { socket.onclose = null; socket.close(); socket = null; }
    term.reset(); redrawTerminal(); terminalStatus.textContent = 'connecting…';
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${protocol}//${location.host}/ws/instances/${encodeURIComponent(terminalId)}/cli?csrf=${encodeURIComponent(panel.dataset.csrf)}`);
    socket.onopen = () => { terminalStatus.textContent = 'connected'; term.focus(); };
    socket.onmessage = event => term.write(typeof event.data === 'string' ? event.data : new Uint8Array(event.data));
    socket.onerror = () => showTerminalError('WebSocket CLI transport error');
    socket.onclose = event => { terminalStatus.textContent = `disconnected (${event.code})`; if (event.reason) showTerminalError(event.reason); socket = null; };
  }
  function openTerminal(id) {
    if (!id || !panel) return;
    if (terminalId !== id) { disconnectTerminal(); terminalId = id; reconnectTerminal(); }
    else { ensureTerminal(); redrawTerminal(); }
  }
  function setTerminalFont(value) {
    fontSize = Math.min(22, Math.max(10, value));
    if (fontSizeValue) fontSizeValue.textContent = `${fontSize} px`;
    localStorage.setItem('smithproxy-terminal-font-size', fontSize);
    if (term) { term.options.fontSize = fontSize; term.clearTextureAtlas(); redrawTerminal(); term.focus(); }
  }
  q('#terminal-reconnect')?.addEventListener('click', reconnectTerminal);
  q('#terminal-clear')?.addEventListener('click', () => { if (term) { term.clear(); term.focus(); } });
  q('#terminal-close')?.addEventListener('click', disconnectTerminal);
  q('#font-decrease')?.addEventListener('click', () => setTerminalFont(fontSize - 1));
  q('#font-increase')?.addEventListener('click', () => setTerminalFont(fontSize + 1));

  const gdbPanel = q('#gdb-terminal');
  const gdbScreen = q('#gdb-terminal-screen');
  const gdbStatus = q('#gdb-terminal-status');
  const gdbFontSizeValue = q('#gdb-font-size-value');
  let gdbInstanceId = '';
  let gdbSocket = null;
  let gdbTerm = null;
  let gdbFit = null;
  let gdbFontSize = Math.min(22, Math.max(10, Number(localStorage.getItem('smithproxy-gdb-font-size')) || 13));
  if (gdbFontSizeValue) gdbFontSizeValue.textContent = `${gdbFontSize} px`;

  function redrawGdbTerminal() {
    if (!gdbTerm || !gdbPanel || gdbPanel.hidden) return;
    requestAnimationFrame(() => {
      gdbFit.fit();
      requestAnimationFrame(() => gdbTerm.refresh(0, Math.max(0, gdbTerm.rows - 1)));
    });
  }

  function ensureGdbTerminal() {
    if (gdbTerm || !gdbScreen) return;
    gdbTerm = new Terminal({cursorBlink: true, convertEol: false, scrollback: 10000,
      fontFamily: 'ui-monospace,SFMono-Regular,Menlo,Consolas,monospace', fontSize: gdbFontSize,
      theme: {background: '#000000', foreground: '#d8e8e2', cursor: '#59e1ae', selectionBackground: '#2463eb', selectionInactiveBackground: '#1d4ed8', selectionForeground: '#ffffff'}});
    gdbFit = new FitAddon.FitAddon(); gdbTerm.loadAddon(gdbFit); gdbTerm.open(gdbScreen);
    gdbTerm.onData(data => { if (gdbSocket?.readyState === WebSocket.OPEN) gdbSocket.send(data); });
    new ResizeObserver(redrawGdbTerminal).observe(gdbScreen);
  }

  function disconnectGdbTerminal() {
    if (gdbSocket) { gdbSocket.onclose = null; gdbSocket.close(); gdbSocket = null; }
    if (gdbStatus) gdbStatus.textContent = 'odpojeno';
  }

  function hideGdbTerminal() {
    disconnectGdbTerminal();
    gdbInstanceId = '';
    if (gdbPanel) gdbPanel.hidden = true;
  }

  function setGdbTerminalAvailable(instanceId, available) {
    if (!available || (gdbInstanceId && gdbInstanceId !== instanceId)) hideGdbTerminal();
  }

  function reconnectGdbTerminal() {
    if (!gdbInstanceId || !gdbPanel) return;
    ensureGdbTerminal();
    disconnectGdbTerminal();
    gdbTerm.reset(); redrawGdbTerminal();
    gdbStatus.textContent = 'connecting…';
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    gdbSocket = new WebSocket(`${protocol}//${location.host}/ws/instances/${encodeURIComponent(gdbInstanceId)}/gdb?csrf=${encodeURIComponent(gdbPanel.dataset.csrf)}`);
    gdbSocket.onopen = () => { gdbStatus.textContent = 'connected'; gdbTerm.focus(); };
    gdbSocket.onmessage = event => gdbTerm.write(typeof event.data === 'string' ? event.data : new Uint8Array(event.data));
    gdbSocket.onerror = () => gdbTerm.write('\r\n\x1b[31mGDB WebSocket transport error\x1b[0m\r\n');
    gdbSocket.onclose = event => {
      gdbStatus.textContent = `disconnected (${event.code})`;
      if (event.reason) gdbTerm.write(`\r\n\x1b[31m${event.reason}\x1b[0m\r\n`);
      gdbSocket = null;
    };
  }

  function openGdbTerminal(instanceId) {
    if (!instanceId || !gdbPanel) return;
    const changed = gdbInstanceId !== instanceId;
    if (changed) disconnectGdbTerminal();
    gdbInstanceId = instanceId;
    gdbPanel.hidden = false;
    ensureGdbTerminal(); redrawGdbTerminal();
    if (changed || gdbSocket?.readyState !== WebSocket.OPEN) reconnectGdbTerminal();
    gdbPanel.scrollIntoView({behavior: 'smooth', block: 'start'});
  }

  function setGdbTerminalFont(value) {
    gdbFontSize = Math.min(22, Math.max(10, value));
    if (gdbFontSizeValue) gdbFontSizeValue.textContent = `${gdbFontSize} px`;
    localStorage.setItem('smithproxy-gdb-font-size', gdbFontSize);
    if (gdbTerm) { gdbTerm.options.fontSize = gdbFontSize; gdbTerm.clearTextureAtlas(); redrawGdbTerminal(); gdbTerm.focus(); }
  }

  q('#gdb-terminal-reconnect')?.addEventListener('click', reconnectGdbTerminal);
  q('#gdb-terminal-clear')?.addEventListener('click', () => { if (gdbTerm) { gdbTerm.clear(); gdbTerm.focus(); } });
  q('#gdb-terminal-close')?.addEventListener('click', hideGdbTerminal);
  q('#gdb-font-decrease')?.addEventListener('click', () => setGdbTerminalFont(gdbFontSize - 1));
  q('#gdb-font-increase')?.addEventListener('click', () => setGdbTerminalFont(gdbFontSize + 1));

  setupSpawnForm();
  setupProfileBuildUpgrade();
  setupProfileTtlControls();
  setupBuildForm();
  setupBuildPolling();
  setupTaskDock();
  setupRuntimeWorkspace();
})();
