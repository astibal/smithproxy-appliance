(() => {
  const q = selector => document.querySelector(selector);
  const text = (selector, value) => { const node = q(selector); if (node) node.textContent = value; };
  const locale = ['cs', 'en', 'fr'].includes(document.documentElement.lang) ? document.documentElement.lang : 'en';
  const messages = {
    cs: {unlimited:'bez limitu', enqueuing:'Zařazuji…', queueing:'Předávám požadavek do fronty…', queued:'Spuštění zařazeno', enqueueStart:'Zařadit spuštění', selected:'Vybráno', running:'běží', pending:'čeká', idle:'nic neběží', noTasks:'Zatím žádné úlohy.', openResult:'Otevřít výsledek →', noMatch:'Filtru neodpovídá žádná instance.', noInstances:'Žádné instance.', loading:'Načítám…', noLogs:'Žádné logy.', logsFailed:'Logy nelze načíst', checking:'ověřuji…', updating:'aktualizuji…', checked:'ověřeno', error:'chyba', profile:'profil', manual:'ruční výběr', noDeadline:'bez deadline', copy:'Kliknutím zkopírovat', copied:'Zkopírováno', copyFailed:'Kopírování selhalo', expired:'expirováno', topologyEmpty:'Žádné autorizované source IP ani živé instance.', source:'SOURCE', hostServices:'služby na appliance', blocked:'BLOKOVÁNO', inputPolicy:'globální INPUT policy', forwardPolicy:'globální FORWARD policy', noRoute:'BEZ ŽIVÉ ROUTY', spawnFree:'spawn slot je volný', noInstance:'žádná odpovídající instance', attachSource:'Připojit source k další živé instanci', buildQueue:'Build se zařazuje…', ingressPath:'SOURCE → runner policy → di0', egressPath:'namespace → uplink / route', viaLocksSide:'Zamčeno profilem VIA: vlastní společně di0 i do0.'},
    en: {unlimited:'unlimited', enqueuing:'Enqueuing…', queueing:'Submitting request to the queue…', queued:'Start enqueued', enqueueStart:'Enqueue start', selected:'Selected', running:'running', pending:'pending', idle:'nothing running', noTasks:'No tasks yet.', openResult:'Open result →', noMatch:'No instance matches the filter.', noInstances:'No instances.', loading:'Loading…', noLogs:'No logs.', logsFailed:'Cannot load logs', checking:'checking…', updating:'updating…', checked:'checked', error:'error', profile:'profile', manual:'manual selection', noDeadline:'no deadline', copy:'Click to copy', copied:'Copied', copyFailed:'Copy failed', expired:'expired', topologyEmpty:'No authorized source IP or live instance.', source:'SOURCE', hostServices:'appliance services', blocked:'BLOCKED', inputPolicy:'global INPUT policy', forwardPolicy:'global FORWARD policy', noRoute:'NO LIVE ROUTE', spawnFree:'spawn slot available', noInstance:'no matching instance', attachSource:'Attach source to another live instance', buildQueue:'Build is being enqueued…', ingressPath:'SOURCE → runner policy → di0', egressPath:'namespace → uplink / route', viaLocksSide:'Locked by the VIA profile: it owns both di0 and do0.'},
    fr: {unlimited:'sans limite', enqueuing:'Planification…', queueing:'Envoi de la demande dans la file…', queued:'Démarrage planifié', enqueueStart:'Planifier le démarrage', selected:'Sélectionné', running:'actives', pending:'en attente', idle:'aucune tâche active', noTasks:'Aucune tâche.', openResult:'Ouvrir le résultat →', noMatch:'Aucune instance ne correspond au filtre.', noInstances:'Aucune instance.', loading:'Chargement…', noLogs:'Aucun journal.', logsFailed:'Impossible de charger les journaux', checking:'vérification…', updating:'actualisation…', checked:'vérifié', error:'erreur', profile:'profil', manual:'sélection manuelle', noDeadline:'sans échéance', copy:'Cliquer pour copier', copied:'Copié', copyFailed:'Échec de la copie', expired:'expiré', topologyEmpty:'Aucune IP source autorisée ni instance active.', source:'SOURCE', hostServices:'services de l’appliance', blocked:'BLOQUÉ', inputPolicy:'politique INPUT globale', forwardPolicy:'politique FORWARD globale', noRoute:'AUCUNE ROUTE ACTIVE', spawnFree:'slot de lancement disponible', noInstance:'aucune instance correspondante', attachSource:'Associer la source à une autre instance active', buildQueue:'Mise en file du build…', ingressPath:'SOURCE → politique runner → di0', egressPath:'namespace → uplink / route', viaLocksSide:'Verrouillé par le profil VIA : il possède di0 et do0.'}
  };
  const tr = key => messages[locale]?.[key] || messages.en[key] || key;
  const formatBytes = bytes => bytes ? `${(bytes / 1048576).toFixed(1)} MiB` : '0 MiB';
  const activeState = state => ['starting', 'running', 'orphaned'].includes(state);
  const problemState = state => ['failed', 'expired', 'orphaned'].includes(state);
  const short = value => value ? String(value).slice(0, 12) : '—';
  const ttl = deadline => {
    if (!deadline) return tr('unlimited');
    const seconds = Math.max(0, Math.round((new Date(deadline).getTime() - Date.now()) / 1000));
    if (seconds < 60) return `${seconds} s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min ${seconds % 60} s`;
    return `${Math.floor(seconds / 3600)} h ${Math.floor((seconds % 3600) / 60)} min`;
  };

  function setupNavigation() {
    const menus = [...document.querySelectorAll('header details.nav-menu')];
    if (!menus.length) return;
    menus.forEach(menu => menu.addEventListener('toggle', () => {
      if (!menu.open) return;
      menus.forEach(other => { if (other !== menu) other.open = false; });
    }));
    document.addEventListener('click', event => {
      if (event.target.closest?.('header details.nav-menu')) return;
      menus.forEach(menu => { menu.open = false; });
    });
    document.addEventListener('keydown', event => {
      if (event.key !== 'Escape') return;
      menus.forEach(menu => { menu.open = false; });
    });
  }

  function setupSpawnForm() {
    const configSelect = q('#instance-config');
    const runtimeSelection = q('#runtime-selection');
    const profileSelect = q('#runtime-profile');
    const profilePanel = q('#profile-selection');
    const manualPanel = q('#manual-selection');
    const placeholderFields = q('#placeholder-fields');
    const networkRuntimeFields = q('#network-runtime-fields');
    if (configSelect && runtimeSelection && profileSelect && placeholderFields) {
      const updatePlaceholderFields = () => {
        const sourceIP = q('#spawn-form select[name="source_ip"]');
        const ingressDriver = runtimeSelection.value === 'profile'
          ? profileSelect.selectedOptions[0]?.dataset.ingressDriver || 'authorized-veth'
          : 'authorized-veth';
        if (sourceIP) {
          const required = !['none', 'unlimited-veth'].includes(ingressDriver);
          sourceIP.required = required;
          sourceIP.disabled = !required;
          if (sourceIP.closest('label')) sourceIP.closest('label').hidden = !required;
        }
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
        const networkParameters = runtimeSelection.value === 'profile'
          ? (profileSelect.selectedOptions[0]?.dataset.networkParameters || '')
            .split(',').map(value => value.trim()).filter(Boolean)
          : [];
        const labels = {
          tuntom_local_ip: 'Tuntom local IP', tuntom_peer_ip: 'Tuntom peer IP',
          tuntom_peer_host: 'Tuntom peer host',
          tuntom_secret: 'Tuntom secret (32 hex)',
        };
        networkRuntimeFields?.replaceChildren(...networkParameters.map(name => {
          const label = document.createElement('label');
          label.textContent = name === 'headless_endpoint_id'
            ? 'Fabric endpoint package · exclusive Slice binding'
            : labels[name] || name;
          let input;
          if (name === 'headless_endpoint_id') {
            input = document.createElement('select');
            const empty = document.createElement('option');
            empty.value = '';
            empty.textContent = 'Choose one available package…';
            input.appendChild(empty);
            const template = document.querySelector('#headless-endpoint-options');
            if (template) input.appendChild(template.content.cloneNode(true));
          } else {
            input = document.createElement('input');
          }
          input.name = `network__${name}`;
          input.required = true;
          input.autocomplete = 'off';
          input.spellcheck = false;
          if (name === 'tuntom_secret') {
            input.type = 'password';
            input.pattern = '[0-9a-fA-F]{32}';
            input.maxLength = 32;
          } else if (name !== 'headless_endpoint_id') {
            input.maxLength = 255;
          }
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
      if (submit) { submit.disabled = true; submit.textContent = tr('enqueuing'); }
      if (state) { state.textContent = tr('queueing'); state.className = 'spawn-submit-state'; }
      try {
        const response = await fetch(form.action, {
          method: 'POST', body: new FormData(form),
          headers: {'X-Requested-With': 'task-fetch'}, cache: 'no-store',
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        if (state) {
          state.textContent = `${data.message || tr('queued')} · task ${short(data.task_id)}`;
          state.className = 'spawn-submit-state ok';
        }
        document.dispatchEvent(new CustomEvent('task-queued'));
        setTimeout(close, 450);
      } catch (error) {
        if (state) { state.textContent = error.message || String(error); state.className = 'spawn-submit-state error'; }
      } finally {
        if (submit) { submit.disabled = false; submit.textContent = tr('enqueueStart'); }
      }
    });
  }

  function setupRuntimeProfileNetworkBindings() {
    document.querySelectorAll('form').forEach(form => {
      const ingress = form.querySelector('select[name="ingress_network_profile_id"]');
      const egress = form.querySelector('select[name="egress_network_profile_id"]');
      if (!ingress || !egress) return;
      const controls = {ingress, egress};
      let owner = null;
      const isDuplex = select => {
        const consumes = (select.selectedOptions[0]?.dataset.consumes || '').split(',');
        return consumes.includes('ingress') && consumes.includes('egress');
      };
      const render = () => {
        for (const [side, select] of Object.entries(controls)) {
          const locked = Boolean(owner) && side !== owner;
          select.disabled = locked;
          select.setAttribute('aria-disabled', String(locked));
          const label = select.closest('label');
          label?.classList.toggle('network-binding-consumed', locked);
          label?.classList.toggle('network-binding-owner', side === owner);
          const hint = label?.querySelector('small');
          if (hint) hint.textContent = locked
            ? tr('viaLocksSide')
            : tr(side === 'ingress' ? 'ingressPath' : 'egressPath');
        }
      };
      const selectSide = side => {
        const selected = controls[side];
        if (isDuplex(selected)) {
          owner = side;
          const other = side === 'ingress' ? egress : ingress;
          other.value = selected.value;
        } else if (owner === side) {
          owner = null;
        }
        render();
      };
      ingress.addEventListener('change', () => selectSide('ingress'));
      egress.addEventListener('change', () => selectSide('egress'));
      // Persisted duplex bindings repeat the same ID in both fields. VIA is
      // currently authored as an egress profile, so retain egress as the
      // editable owner on initial render; a new selection can originate from
      // either side.
      owner = isDuplex(egress) ? 'egress' : isDuplex(ingress) ? 'ingress' : null;
      if (owner) {
        const other = owner === 'ingress' ? egress : ingress;
        other.value = controls[owner].value;
      }
      render();
    });
  }

  function setupProfileBuildUpgrade() {
    document.querySelectorAll('[data-use-profile-build]').forEach(button => {
      button.addEventListener('click', () => {
        const select = q('#profile-build-id');
        if (!select) return;
        select.value = button.dataset.useProfileBuild;
        select.dispatchEvent(new Event('change', {bubbles: true}));
        button.textContent = tr('selected');
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

  function setupFirewallCountdowns() {
    const countdowns = [...document.querySelectorAll('.firewall-countdown')];
    if (!countdowns.length) return;
    const duration = rawSeconds => {
      const seconds = Math.max(0, Math.ceil(rawSeconds));
      const days = Math.floor(seconds / 86400);
      const hours = Math.floor((seconds % 86400) / 3600);
      const minutes = Math.floor((seconds % 3600) / 60);
      const rest = seconds % 60;
      const clock = `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
      return days ? `${days} d ${clock}` : clock;
    };
    const update = () => {
      const now = Date.now();
      countdowns.forEach(element => {
        const expiry = Date.parse(element.closest('.firewall-expiry')?.dataset.expiresAt || '');
        const remaining = (expiry - now) / 1000;
        const elapsed = !Number.isFinite(expiry) || remaining <= 0;
        element.textContent = elapsed ? 'expired' : duration(remaining);
        element.classList.toggle('urgent', !elapsed && remaining <= 300);
        element.classList.toggle('elapsed', elapsed);
        const row = element.closest('.firewall-row');
        row?.classList.toggle('expired', elapsed);
        const extendButton = row?.querySelector('.firewall-extend-button');
        if (extendButton) extendButton.textContent = elapsed ? window.sasTr("ui.6d81c50b8e54") : '+ Čas';
      });
    };
    update();
    window.setInterval(update, 1000);
  }

  function setupFirewallExplainers() {
    const cards = [...document.querySelectorAll('.firewall-status-card')];
    const toggle = card => {
      const open = !card.classList.contains('explain-open');
      cards.forEach(candidate => {
        candidate.classList.remove('explain-open');
        candidate.setAttribute('aria-expanded', 'false');
      });
      if (open) {
        card.classList.add('explain-open');
        card.setAttribute('aria-expanded', 'true');
      }
    };
    cards.forEach(card => {
      card.addEventListener('click', () => toggle(card));
      card.addEventListener('keydown', event => {
        if (['Enter', ' '].includes(event.key)) { event.preventDefault(); toggle(card); }
        if (event.key === 'Escape') {
          card.classList.remove('explain-open'); card.setAttribute('aria-expanded', 'false');
        }
      });
    });
  }

  function setupFirewallTopology() {
    const root = q('#firewall-topology');
    const state = q('#firewall-topology-state');
    if (!root) return;
    const attachDialog = q('#firewall-attach-dialog');
    const attachForm = q('#firewall-attach-form');
    const attachSource = q('#firewall-attach-source');
    const attachInstance = q('#firewall-attach-instance');
    const authorizationForm = q('.firewall-add-form');
    const protocolSelect = authorizationForm?.querySelector('[name="protocol"]');
    const portsInput = authorizationForm?.querySelector('[name="ports"]');
    const existingSelect = authorizationForm?.querySelector('[name="instance_id"]');
    const profileSelect = authorizationForm?.querySelector('[name="runtime_profile_id"]');
    const registerSource = authorizationForm?.querySelector('[name="register_source"]');
    const updatePortSelector = () => {
      if (!portsInput || !protocolSelect) return;
      const enabled = ['tcp', 'udp'].includes(protocolSelect.value);
      portsInput.disabled = !enabled;
      if (!enabled) portsInput.value = '';
    };
    protocolSelect?.addEventListener('change', updatePortSelector);
    updatePortSelector();
    existingSelect?.addEventListener('change', () => {
      if (existingSelect.value && profileSelect) profileSelect.value = '';
      if (existingSelect.value && registerSource) registerSource.checked = true;
    });
    profileSelect?.addEventListener('change', () => {
      if (profileSelect.value && existingSelect) existingSelect.value = '';
      if (profileSelect.value && registerSource) registerSource.checked = true;
    });
    attachDialog?.querySelectorAll('[data-close]').forEach(button => {
      button.addEventListener('click', () => attachDialog.close());
    });
    attachForm?.addEventListener('submit', event => {
      if (!attachInstance?.value) { event.preventDefault(); return; }
      attachForm.action = `/firewall/instances/${encodeURIComponent(attachInstance.value)}/sources`;
    });
    const make = (tag, className = '', value = '') => {
      const element = document.createElement(tag);
      if (className) element.className = className;
      if (value !== '') element.textContent = value;
      return element;
    };
    const topologyDuration = rawSeconds => {
      const seconds = Math.max(0, Math.ceil(rawSeconds));
      const days = Math.floor(seconds / 86400);
      const hours = Math.floor((seconds % 86400) / 3600);
      const minutes = Math.floor((seconds % 3600) / 60);
      const rest = seconds % 60;
      const clock = `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
      return days ? `${days} d ${clock}` : clock;
    };
    const updateTopologyCountdowns = () => {
      const now = Date.now();
      root.querySelectorAll('.topology-source-countdown').forEach(element => {
        const expiry = Date.parse(element.dataset.expiresAt || '');
        const remaining = (expiry - now) / 1000;
        const elapsed = !Number.isFinite(expiry) || remaining <= 0;
        element.textContent = elapsed ? tr('expired') : topologyDuration(remaining);
        element.classList.toggle('urgent', !elapsed && remaining <= 300);
        element.classList.toggle('elapsed', elapsed);
      });
    };
    const renderTarget = instance => {
      const target = make('a', `topology-target instance-target state-${instance.state}`);
      target.href = `/?instance=${encodeURIComponent(instance.id)}`;
      const heading = make('span', 'topology-target-heading');
      heading.append(make('b', '', instance.namespace || `instance ${instance.id.slice(0, 8)}`));
      heading.append(make('span', 'badge ok', instance.state.toUpperCase()));
      target.append(heading);
      const network = instance.network || {};
      target.append(make('code', '', `${network.guest_ip || 'IP ?'} / ${network.guest_interface || '?'}  ←  ${network.host_interface || '?'}`));
      if (network.guest_ip_v6) target.append(make('code', 'topology-ipv6', `${network.guest_ip_v6} / ${network.guest_interface || '?'}`));
      const smithproxy = (instance.members || []).find(member => member.role === 'smithproxy');
      target.append(make('small', '', `${instance.profile || 'custom'} · ${instance.user_id || 'user ?'} · PID ${smithproxy?.pid || '—'} · ${instance.id.slice(0, 12)}`));
      return target;
    };
    const renderLane = (label, effective, mode, targets) => {
      const lane = make('div', `topology-lane ${effective ? 'allowed' : 'blocked'}`);
      const gate = make('span', 'topology-gate', mode || label);
      gate.title = effective ? `${label}: provoz projde` : `${label}: provoz je zahozen`;
      const gateStack = make('div', 'topology-gate-stack');
      gateStack.append(gate);
      lane.append(gateStack, make('span', 'topology-connector'));
      const destination = make('div', 'topology-destinations');
      targets.forEach(target => destination.append(target));
      lane.append(destination);
      return lane;
    };
    const render = payload => {
      root.replaceChildren();
      const entries = Array.isArray(payload.topology) ? payload.topology : [];
      if (!entries.length) {
        root.append(make('p', 'empty', tr('topologyEmpty')));
      }
      const sourceGroups = new Map();
      entries.forEach(entry => {
        const inputEffective = !payload.input_enforced || entry.input_allowed;
        const forwardEffective = !payload.forward_enforced || entry.forward_allowed;
        const targetIds = (entry.instances || []).map(instance => instance.id).sort();
        const signature = JSON.stringify([inputEffective, forwardEffective, targetIds, entry.selectors || []]);
        if (!sourceGroups.has(signature)) sourceGroups.set(signature, []);
        sourceGroups.get(signature).push(entry);
      });
      [...sourceGroups.values()].forEach(sourceEntries => {
        const entry = sourceEntries[0];
        const group = make('article', `topology-group${sourceEntries.some(item => item.active) ? '' : ' inactive'}${sourceEntries.length > 1 ? ' sources-merged' : ''}`);
        const source = make('div', 'topology-source');
        sourceEntries.forEach((sourceEntry, index) => {
          const sourceItem = make('div', 'topology-source-item');
          sourceItem.append(make('small', '', sourceEntries.length > 1 ? `${tr('source')} ${index + 1}/${sourceEntries.length}` : tr('source')));
          sourceItem.append(make('code', '', sourceEntry.source));
          const flags = make('div', 'topology-source-flags');
          if (sourceEntry.input_allowed) flags.append(make('span', 'badge ok', 'INPUT'));
          if (sourceEntry.forward_allowed) flags.append(make('span', 'badge ok', 'FORWARD'));
          if (sourceEntry.spawn_allowed) flags.append(make('span', 'badge', 'SPAWN POOL'));
          sourceItem.append(flags);
          (sourceEntry.selectors || []).forEach(selector => {
            const destination = selector.destination || '*';
            const ports = (selector.ports || []).length ? `:${selector.ports.join(',')}` : '';
            const chains = (selector.chains || []).join('+').toUpperCase();
            sourceItem.append(make('code', 'topology-selector', `${chains} · ${String(selector.protocol || 'any').toUpperCase()} → ${destination}${ports}`));
          });
          (sourceEntry.expirations || []).forEach(expiration => {
            const expiry = make('div', 'topology-source-expiry');
            expiry.title = `${(expiration.chains || []).join(' + ').toUpperCase()} · ${expiration.system || 'authorization'} · ${expiration.expires_at}`;
            expiry.append(make('span', 'topology-clock', '⏱'));
            const countdown = make('b', 'topology-source-countdown', window.sasTr("ui.345ce6f0116e"));
            countdown.dataset.expiresAt = expiration.expires_at || '';
            expiry.append(countdown);
            expiry.append(make('small', '', (expiration.chains || []).join('+').toUpperCase()));
            sourceItem.append(expiry);
          });
          sourceItem.append(make('small', '', (sourceEntry.systems || []).join(', ') || 'runtime routing'));
          source.append(sourceItem);
        });
        const lanes = make('div', 'topology-lanes');
        const inputEffective = !payload.input_enforced || entry.input_allowed;
        const inputSelected = (entry.selectors || []).some(selector => (selector.chains || []).includes('input'));
        const inputMode = payload.input_enforced ? (entry.input_allowed ? (inputSelected ? 'INPUT SELECTED' : 'INPUT ALLOW') : 'INPUT DROP') : 'INPUT AUDIT';
        const host = make('div', `topology-target host-target${inputEffective ? '' : ' denied'}`);
        host.append(make('b', '', inputEffective ? 'SAS HOST' : tr('blocked')));
        host.append(make('small', '', inputEffective ? tr('hostServices') : tr('inputPolicy')));
        lanes.append(renderLane('INPUT', inputEffective, inputMode, [host]));
        const forwardEffective = !payload.forward_enforced || entry.forward_allowed;
        const forwardSelected = (entry.selectors || []).some(selector => (selector.chains || []).includes('forward'));
        const forwardMode = payload.forward_enforced ? (entry.forward_allowed ? (forwardSelected ? 'FWD SELECTED' : 'FWD ALLOW') : 'FWD DROP') : 'FWD AUDIT';
        let targets = [];
        if (!forwardEffective) {
          const blocked = make('div', 'topology-target denied');
          blocked.append(make('b', '', tr('blocked')));
          blocked.append(make('small', '', tr('forwardPolicy')));
          targets = [blocked];
        } else if ((entry.instances || []).length) {
          targets = entry.instances.map(renderTarget);
        } else {
          const empty = make('div', 'topology-target empty-route');
          empty.append(make('b', '', tr('noRoute')));
          empty.append(make('small', '', sourceEntries.some(item => item.spawn_allowed) ? tr('spawnFree') : tr('noInstance')));
          targets = [empty];
        }
        const forwardLane = renderLane('FORWARD', forwardEffective, forwardMode, targets);
        const attachableSources = sourceEntries.map(item => item.source).filter(source => !source.includes('/'));
        const currentTargets = new Set((entry.instances || []).map(instance => instance.id));
        const attachableInstances = (payload.active_instances || []).filter(instance => !currentTargets.has(instance.id));
        if (forwardEffective && attachableSources.length && attachableInstances.length && attachDialog) {
          const attach = make('button', 'topology-route-add', '+');
          attach.type = 'button';
          attach.title = tr('attachSource');
          attach.addEventListener('click', () => {
            attachSource.replaceChildren(...attachableSources.map(source => {
              const option = make('option', '', source); option.value = source; return option;
            }));
            attachInstance.replaceChildren(...attachableInstances.map(instance => {
              const option = make('option', '', `${instance.namespace || instance.id.slice(0, 8)} · ${instance.profile} · ${instance.id.slice(0, 12)}`);
              option.value = instance.id; return option;
            }));
            attachDialog.showModal();
          });
          forwardLane.querySelector('.topology-gate-stack')?.append(attach);
        }
        lanes.append(forwardLane);
        group.append(source, lanes);
        root.append(group);
      });
      const egressGroups = Array.isArray(payload.egress_groups) ? payload.egress_groups : [];
      if (egressGroups.length) {
        const heading = make('div', 'topology-egress-heading');
        heading.append(make('b', '', 'INSTANCE OUTPUT'));
        heading.append(make('span', '', window.sasTr("ui.da3e49ee3e4d")));
        root.append(heading);
      }
      egressGroups.forEach(group => {
        const diagram = make('article', 'topology-egress-group');
        const members = make('div', 'topology-egress-members');
        (group.instances || []).forEach(instance => {
          const member = make('a', 'topology-egress-member');
          member.href = `/?instance=${encodeURIComponent(instance.id)}`;
          member.append(make('b', '', instance.namespace || instance.id.slice(0, 8)));
          member.append(make('code', '', `${instance.guest_ip || 'IP ?'} · ${instance.guest_interface || '?'}`));
          if (instance.guest_ip_v6) member.append(make('code', 'topology-ipv6', `${instance.guest_ip_v6} · IPv6`));
          member.append(make('small', '', `${instance.profile || 'custom'} · ${instance.id.slice(0, 12)}`));
          members.append(member);
        });
        const connector = make('div', 'topology-egress-connector');
        connector.append(make('span', '', `${(group.instances || []).length}×`));
        const output = make('div', `topology-egress-output mode-${group.mode}`);
        const title = group.mode === 'masquerade' ? 'MASQUERADE' : group.mode === 'routed' ? 'ROUTED' : String(group.mode || 'UNKNOWN').toUpperCase();
        output.title = window.sasTr("ui.aa51c793d3bb");
        output.append(make('small', 'effect-indication', 'INDICATION ONLY · SHARED EGRESS EFFECT'));
        output.append(make('b', '', title));
        output.append(make('code', '', group.interface || 'interface dle host route'));
        output.append(make('span', 'effect-description', group.effect || ''));
        if (group.route_via) output.append(make('small', '', `return route via ${group.route_via}`));
        diagram.append(members, connector, output);
        root.append(diagram);
      });
      if (state) {
        const updated = Date.parse(payload.updated_at || '');
        state.textContent = Number.isFinite(updated)
          ? `${window.sasTr("ui.34bc5c168e50")} · ${new Date(updated).toLocaleTimeString(locale)}` : window.sasTr("ui.34bc5c168e50");
        state.classList.remove('state-failed');
      }
      updateTopologyCountdowns();
    };
    try {
      render(JSON.parse(q('#firewall-topology-initial')?.textContent || '{}'));
    } catch (_) { /* the first poll will recover */ }
    let refreshing = false;
    const refresh = async () => {
      if (refreshing || document.hidden) return;
      refreshing = true;
      try {
        const response = await fetch(root.dataset.url, {headers: {'Accept': 'application/json'}});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        render(await response.json());
      } catch (error) {
        if (state) {
          state.textContent = `obnova selhala · ${error.message}`;
          state.classList.add('state-failed');
        }
      } finally {
        refreshing = false;
      }
    };
    refresh();
    window.setInterval(refresh, 5000);
    window.setInterval(updateTopologyCountdowns, 1000);
  }

  function setupBuildForm() {
    const form = q('#build-form');
    const state = q('#build-enqueue-state');
    if (!form || !state) return;
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const submit = event.submitter || q('#build-submit');
      const buttons = [...form.querySelectorAll('button[type="submit"], button:not([type])')];
      if (submit?.disabled) return;
      const ref = form.elements.ref?.value || 'master';
      const originalLabels = buttons.map(button => button.textContent);
      buttons.forEach(button => { button.disabled = true; });
      if (submit) submit.textContent = tr('enqueuing');
      state.textContent = `'${ref}' · ${tr('buildQueue')}`;
      state.className = 'build-enqueue-state';
      try {
        const body = new FormData(form, submit);
        const response = await fetch(form.action, {
          method: 'POST', body,
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
        buttons.forEach((button, index) => { button.disabled = false; button.textContent = originalLabels[index]; });
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
      text('#task-dock-summary', active.length ? `${running} ${tr('running')} · ${pending} ${tr('pending')}` : tr('idle'));
      const ordered = [...active, ...tasks.filter(task => !['pending', 'running'].includes(task.state))].slice(0, 30);
      if (!ordered.length) {
        const empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = tr('noTasks');
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
        const exportId = task.result?.export_id;
        const viewable = task.state === 'succeeded' && [
          'config-preview', 'build-config-preview', 'instance-config-preview', 'config-observer'
        ].includes(task.kind);
        let tail;
        if (exportId && task.kind === 'appliance-export') {
          tail = document.createElement('a');
          tail.href = `/appliance-exports/${encodeURIComponent(exportId)}/download`;
          tail.textContent = locale === 'cs' ? window.sasTr("ui.c710338ea02e") : locale === 'fr' ? 'Télécharger' : 'Download';
        } else if (resultId) {
          tail = document.createElement('a'); tail.href = `/?instance=${encodeURIComponent(resultId)}`; tail.textContent = short(resultId);
        } else if (viewable) {
          tail = document.createElement('a');
          tail.href = `/tasks/${encodeURIComponent(task.task_id)}/result`;
          tail.textContent = tr('openResult');
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
        text('#task-dock-summary', `${tr('error')}: ${error.message || error}`);
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
    const memberHeading = q('.instance-list-head span:nth-child(5)');
    if (memberHeading) memberHeading.textContent = 'Slice members';
    const sliceHeading = q('#detail-pid')?.closest('div')?.querySelector('span');
    if (sliceHeading) sliceHeading.textContent = 'Slice';

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
      text('#metric-rss', formatBytes(instances.reduce((sum, item) => sum + (item.slice_rss_bytes || 0), 0)));
    }

    function visibleInstances() {
      const needle = query.toLowerCase();
      return instances.filter(item => {
        if (filter === 'active' && !activeState(item.state)) return false;
        if (filter === 'problem' && !problemState(item.state)) return false;
        if (!needle) return true;
        return [item.id, item.alias, item.source_ip, item.user_id, item.profile, item.namespace, item.state]
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
      const identity = document.createElement('code'); identity.className = 'instance-cell instance-cell-id'; identity.textContent = item.alias || short(item.id); identity.title = item.id;
      const owner = document.createElement('span'); owner.className = 'instance-cell instance-card-owner';
      const user = document.createElement('strong'); user.textContent = item.user_id || 'unknown';
      const profile = document.createElement('small'); profile.textContent = `${item.profile || 'custom'}${item.persistent ? ' · persistent' : ''}`;
      owner.append(user, profile);
      const memberPids = (item.members || []).filter(member => member.pid).map(member => `${member.role}:${member.pid}`);
      const pid = document.createElement('code'); pid.className = 'instance-cell'; pid.textContent = memberPids.join(' · ') || '—';
      const rss = document.createElement('span'); rss.className = 'instance-cell'; rss.textContent = formatBytes(item.slice_rss_bytes || 0);
      const deadline = document.createElement('span'); deadline.className = 'instance-cell instance-cell-ttl'; deadline.textContent = ttl(item.deadline);
      button.append(state, source, identity, owner, pid, rss, deadline);
      button.addEventListener('click', () => selectInstance(item.id));
      const popout = document.createElement('a');
      popout.className = 'instance-card-popout';
      popout.href = focusedInstanceUrl(item.id, 'overview');
      popout.target = '_blank'; popout.rel = 'noopener';
      popout.title = window.sasTr("ui.d1736ed66b9e", {id: short(item.id)});
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
        empty.textContent = instances.length ? tr('noMatch') : tr('noInstances');
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
      text('#detail-short-id', item.alias || short(item.id)); text('#detail-full-id', item.id);
      const members = (item.members || []).filter(member => member.pid);
      text('#detail-pid', item.slice_unit || 'Slice —');
      text('#detail-rss', `${members.map(member => `${member.role} PID ${member.pid}`).join(' · ') || window.sasTr("ui.c667d4a42895")} · RSS ${formatBytes(item.slice_rss_bytes || 0)} · CLI ${item.cli_port ? `:${item.cli_port}` : '—'}`);
      text('#detail-source', item.source_ip || '—'); text('#detail-user', item.user_id || 'unknown user');
      text('#detail-namespace', item.namespace || '—'); text('#detail-profile', `${item.profile || 'custom'} · ${item.unit || 'unit unknown'}`);
      text('#detail-ttl', ttl(item.deadline)); text('#detail-deadline', item.deadline ? new Date(item.deadline).toLocaleString() : tr('noDeadline'));
      text('#detail-build', short(item.build_id)); text('#detail-runtime-profile', `${item.runtime_profile_id ? `${tr('profile')} ${short(item.runtime_profile_id)}` : tr('manual')}${item.persistent ? ' · persistent' : ''}`);
      text('#detail-config-id', short(item.config_id)); text('#detail-config-mode', `${String(item.config_mode || 'ro').toUpperCase()} · ${item.filesystem_mode === 'rootfs' ? 'ROOTFS' : 'HOST FS'} · cert ${short(item.cert_bundle_id)}`);
      if (focusMode) document.title = `Smithproxy ${short(item.id)} · ${item.source_ip || 'instance'}`;
      const result = q('#detail-result'); result.hidden = !item.result; result.textContent = item.result || '';
      q('#detail-config').href = `/instances/${encodeURIComponent(item.id)}/config/download`;
      setForm('#detail-restart', `/instances/${encodeURIComponent(item.id)}/restart`, item.state === 'running');
      setForm('#detail-extend', `/instances/${encodeURIComponent(item.id)}/extend`, activeState(item.state));
      setForm('#detail-stop', `/instances/${encodeURIComponent(item.id)}/stop`, activeState(item.state));
      setForm('#detail-delete', `/instances/${encodeURIComponent(item.id)}/delete`, !activeState(item.state));
      const consoleTab = q('[data-view=console]'); consoleTab.disabled = item.state !== 'running' || (item.application && item.application !== 'smithproxy');
      updatePopoutLink();
      if (currentView === 'console' && consoleTab.disabled) switchView('overview');
    }

    function selectInstance(id) {
      if (selectedId !== id) {
        window.dispatchEvent(new Event('sas-netns-close'));
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
      output.textContent = tr('loading');
      try {
        const response = await fetch(`/api/instances/${encodeURIComponent(instanceId)}/logs`, {cache: 'no-store'});
        const data = await response.json();
        if (requestId !== logsRequest || instanceId !== selectedId) return;
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        output.textContent = data.output || tr('noLogs');
      } catch (error) {
        if (requestId === logsRequest && instanceId === selectedId) {
          output.textContent = `${tr('logsFailed')}: ${error.message || error}`;
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
      hint.textContent = window.sasTr("ui.ef96b5f42982");
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
        terminalButton.textContent = window.sasTr("ui.91de12d1a96a");
        terminalButton.addEventListener('click', () => openGdbTerminal(instance.id));
        heading.append(terminalButton);
      }
      panel.append(heading);

      if (execution.build_type !== 'Debug') {
        const note = document.createElement('p'); note.className = 'empty';
        note.textContent = window.sasTr("ui.528183e345de");
        panel.append(note);
      } else if (!debug.unit) {
        const note = document.createElement('p'); note.className = 'empty';
        note.textContent = instance.state === 'running' ? window.sasTr("ui.b52f009d2aa3") : window.sasTr("ui.bc111e59ea84");
        panel.append(note);
      } else {
        const commands = document.createElement('div'); commands.className = 'diag-grid';
        appendDiagRow(commands, 'Helper unit', debug.unit);
        appendDiagRow(commands, '1. SSH tunel', debug.ssh_tunnel);
        appendDiagRow(commands, window.sasTr("ui.2479467f9e1e"), debug.copy_binary);
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
      output.replaceChildren(Object.assign(document.createElement('p'), {className: 'empty', textContent: tr('loading')}));
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
        const networkProfiles = execution.network_profiles || {};
        const ingressProfile = networkProfiles.ingress || {};
        const egressProfile = networkProfiles.egress || {};
        appendDiagRow(grid, 'Ingress profil', `${ingressProfile.name || 'global/default'} · ${ingressProfile.driver || 'split-veth'} · ${ingressProfile.selector || 'source'}`);
        appendDiagRow(grid, 'Egress profil', `${egressProfile.name || 'global/default'} · ${egressProfile.driver || 'split-veth'} · ${egressProfile.mode || 'global route'}`);
        const ingress = network.ingress || {};
        const egress = network.egress || {};
        appendDiagRow(grid, 'Ingress pool / subnet', ingress.subnet);
        appendDiagRow(grid, 'Ingress host → namespace', `${ingress.host_interface || '—'} ${ingress.expected_host_address || '—'} → ${ingress.guest_interface || '—'} ${ingress.expected_guest_address || '—'}`);
        appendDiagRow(grid, window.sasTr("ui.3113f9cbd186"), formatInterfaces(ingress.host_interfaces));
        appendDiagRow(grid, 'Egress pool / subnet', egress.subnet);
        appendDiagRow(grid, 'Egress namespace → host', `${egress.guest_interface || '—'} ${egress.expected_guest_address || '—'} → ${egress.host_interface || '—'} ${egress.expected_host_address || '—'}`);
        appendDiagRow(grid, window.sasTr("ui.077293545507"), formatInterfaces(egress.host_interfaces));
        appendDiagRow(grid, window.sasTr("ui.3ec55ec78625"), formatInterfaces(network.namespace_interfaces));
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
        appendDiagRow(grid, 'Auto-restart', instance.auto_restart ? window.sasTr("ui.cccb4e4ad1ba", {count: instance.restart_count || 0}) : window.sasTr("ui.cb58e4600bf0"));
        appendDiagRow(grid, window.sasTr("ui.e1d2d128701a"), instance.last_restart_at || '—');
        output.replaceChildren(grid);
        if (instance.crash_trace) {
          const crash = document.createElement('section'); crash.className = 'debug-panel';
          const title = document.createElement('h3'); title.textContent = window.sasTr("ui.6550183db229");
          const meta = document.createElement('p'); meta.className = 'hint';
          meta.textContent = `PID ${instance.crash_pid || '—'} · ${instance.crash_at || window.sasTr("ui.8c82ddc7c82f")}`;
          const trace = document.createElement('pre'); trace.className = 'runtime-log crash-trace';
          trace.textContent = instance.crash_trace;
          crash.append(title, meta, trace); output.append(crash);
        }
        appendDebugControls(output, data);
        const netnsButton = document.createElement('button');
        netnsButton.type = 'button'; netnsButton.className = 'secondary';
        netnsButton.textContent = 'NetNS shell';
        netnsButton.disabled = data.instance?.state !== 'running';
        netnsButton.onclick = () => window.dispatchEvent(new CustomEvent('sas-netns-open', {detail: instanceId}));
        output.append(netnsButton);
        const checkServices = document.createElement('button');
        checkServices.type = 'button'; checkServices.className = 'secondary';
        checkServices.textContent = window.sasTr('microservices.check');
        checkServices.onclick = async () => {
          checkServices.disabled = true;
          try {
            const response = await fetch(`/api/instances/${encodeURIComponent(instanceId)}/microservices/check`, {
              method: 'POST', headers: {'X-CSRF-Token': csrf}
            });
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || response.statusText);
            checkServices.textContent = `${window.sasTr('microservices.queued')} · ${result.task_id.slice(0, 8)}`;
          } catch (error) {
            checkServices.textContent = String(error.message || error);
          } finally { checkServices.disabled = false; }
        };
        output.append(checkServices);
        if (data.system_start) {
          const status = document.createElement('p');
          status.textContent = `00-start · ${data.system_start.state} · ${data.system_start.checked_at || '—'}`;
          if (data.system_start.error) status.textContent += ` · ${data.system_start.error}`;
          output.append(status);
          const toggle = document.createElement('button');
          toggle.type = 'button'; toggle.className = 'secondary';
          toggle.textContent = window.sasTr(data.system_start.enabled ? 'microservices.disable00' : 'microservices.enable00');
          toggle.onclick = async () => {
            toggle.disabled = true;
            try {
              const response = await fetch(`/api/instances/${encodeURIComponent(instanceId)}/microservices/00/configure`, {
                method: 'POST', headers: {'X-CSRF-Token': csrf, 'Content-Type': 'application/json'},
                body: JSON.stringify({enabled: !data.system_start.enabled})
              });
              const result = await response.json();
              if (!response.ok) throw new Error(result.error || response.statusText);
              toggle.textContent = `${window.sasTr('microservices.queued')} · ${result.task_id.slice(0, 8)}`;
            } catch (error) { toggle.textContent = String(error.message || error); toggle.disabled = false; }
          };
          output.append(toggle);
        }
      } catch (error) {
        if (requestId === diagnosticsRequest && instanceId === selectedId) {
          output.replaceChildren(Object.assign(document.createElement('p'), {className: 'flash error', textContent: String(error.message || error)}));
        }
      }
    }

    function switchView(view, changeUrl = true) {
      if (view === 'console' && q('[data-view=console]')?.disabled) view = 'overview';
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
      text('#instance-poll-state', manual ? tr('checking') : tr('updating'));
      try {
        const response = await fetch('/api/instances', {cache: 'no-store'});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        instances = (data.instances || []).sort((a, b) => Number(activeState(b.state)) - Number(activeState(a.state)) || String(b.created_at).localeCompare(String(a.created_at)));
        if (selectedId && !instances.some(item => item.id === selectedId)) selectedId = '';
        renderMetrics(); renderList(); renderDetail();
        text('#instance-poll-state', `${tr('checked')} ${new Date().toLocaleTimeString()}`);
        q('#runner-status').textContent = 'ok'; q('#runner-status').classList.add('ok');
      } catch (error) {
        text('#instance-poll-state', `${tr('error')}: ${error.message || error}`);
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

  function setupWorkspaceWidth() {
    const main = q('main');
    const handles = [...document.querySelectorAll('[data-layout-resizer]')];
    if (!main || !handles.length) return;
    const storageKey = 'smithproxy-workspace-width';
    const defaultWidth = 1440;
    const clamp = value => Math.max(760, Math.min(window.innerWidth - 24, value));
    const apply = (value, persist = true) => {
      const width = Math.round(clamp(value));
      document.documentElement.style.setProperty('--workspace-width', `${width}px`);
      if (persist) localStorage.setItem(storageKey, String(width));
      requestAnimationFrame(position);
    };
    const position = () => {
      if (window.innerWidth <= 900) return;
      const rect = main.getBoundingClientRect();
      const left = q('[data-layout-resizer="left"]');
      const right = q('[data-layout-resizer="right"]');
      if (left) left.style.left = `${Math.max(3, rect.left - left.offsetWidth - 7)}px`;
      if (right) right.style.left = `${Math.min(window.innerWidth - right.offsetWidth - 3, rect.right + 7)}px`;
    };
    const stored = Number(localStorage.getItem(storageKey));
    apply(Number.isFinite(stored) && stored >= 760 ? stored : defaultWidth, false);
    handles.forEach(handle => {
      handle.setAttribute('aria-label', handle.dataset.layoutResizer === 'left' ? 'Resize workspace from left edge' : 'Resize workspace from right edge');
      handle.addEventListener('pointerdown', event => {
        if (window.innerWidth <= 900) return;
        event.preventDefault();
        handle.setPointerCapture(event.pointerId);
        handle.classList.add('dragging');
        document.body.classList.add('layout-resizing');
      });
      handle.addEventListener('pointermove', event => {
        if (!handle.hasPointerCapture(event.pointerId)) return;
        apply(2 * Math.abs(event.clientX - window.innerWidth / 2));
      });
      const finish = event => {
        if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
        handle.classList.remove('dragging');
        document.body.classList.remove('layout-resizing');
      };
      handle.addEventListener('pointerup', finish);
      handle.addEventListener('pointercancel', finish);
      handle.addEventListener('dblclick', () => {
        localStorage.removeItem(storageKey);
        apply(defaultWidth, false);
      });
      handle.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
        event.preventDefault();
        const current = main.getBoundingClientRect().width;
        const outward = handle.dataset.layoutResizer === 'left' ? event.key === 'ArrowLeft' : event.key === 'ArrowRight';
        apply(current + (outward ? 80 : -80));
      });
    });
    window.addEventListener('resize', () => {
      const current = Number(localStorage.getItem(storageKey)) || defaultWidth;
      apply(current, false);
    });
    new ResizeObserver(position).observe(main);
    position();
  }

  function setupCopyableValues() {
    const selector = [
      'main code', '.instance-cell-source', '#detail-full-id', '#detail-source',
      '#detail-namespace', '#detail-build', '#detail-config-id',
      '.lab-coordinates b', '.test-drive-ttl code', '[data-copy-value]'
    ].join(',');
    const excluded = 'a,button,summary,label,pre,textarea,input,select,.terminal-screen,.xterm';
    const toast = document.createElement('div');
    toast.className = 'copy-toast'; toast.setAttribute('role', 'status'); toast.hidden = true;
    document.body.append(toast);
    let toastTimer;

    const valueOf = node => (node.dataset.copyValue || node.textContent || '').trim();
    const decorate = root => {
      const nodes = [];
      if (root instanceof Element && root.matches(selector)) nodes.push(root);
      if (root.querySelectorAll) nodes.push(...root.querySelectorAll(selector));
      nodes.forEach(node => {
        if (node.dataset.copyReady || node.closest(excluded) || !valueOf(node)) return;
        node.dataset.copyReady = '1';
        node.classList.add('copyable-value');
        node.tabIndex = 0;
        node.setAttribute('role', 'button');
        node.setAttribute('aria-label', `${tr('copy')}: ${valueOf(node)}`);
        node.title = tr('copy');
      });
    };
    const fallbackCopy = value => {
      const area = document.createElement('textarea');
      area.value = value; area.readOnly = true;
      area.style.position = 'fixed'; area.style.opacity = '0';
      document.body.append(area); area.select();
      const copied = document.execCommand('copy'); area.remove();
      if (!copied) throw new Error('copy command rejected');
    };
    const copy = async node => {
      const value = valueOf(node);
      if (!value) return;
      try {
        if (navigator.clipboard?.writeText && window.isSecureContext) await navigator.clipboard.writeText(value);
        else fallbackCopy(value);
        node.classList.add('copied');
        toast.textContent = `${tr('copied')}: ${value.length > 80 ? `${value.slice(0, 77)}…` : value}`;
      } catch (_error) {
        toast.textContent = tr('copyFailed');
        toast.classList.add('error');
      }
      toast.hidden = false;
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => {
        toast.hidden = true; toast.classList.remove('error'); node.classList.remove('copied');
      }, 1300);
    };
    document.addEventListener('click', event => {
      const node = event.target.closest?.('.copyable-value');
      if (!node || window.getSelection()?.toString()) return;
      event.preventDefault(); copy(node);
    });
    document.addEventListener('keydown', event => {
      if (!event.target.matches?.('.copyable-value') || !['Enter', ' '].includes(event.key)) return;
      event.preventDefault(); copy(event.target);
    });
    decorate(document);
    new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(decorate)))
      .observe(document.body, {childList: true, subtree: true});
  }

  setupWorkspaceWidth();
  setupNavigation();
  setupCopyableValues();
  setupRuntimeProfileNetworkBindings();
  setupSpawnForm();
  setupProfileBuildUpgrade();
  setupProfileTtlControls();
  setupFirewallCountdowns();
  setupFirewallExplainers();
  setupFirewallTopology();
  setupBuildForm();
  setupBuildPolling();
  setupTaskDock();
  setupRuntimeWorkspace();
})();
