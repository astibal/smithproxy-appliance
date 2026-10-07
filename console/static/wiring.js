(() => {
  const filter = document.querySelector('[data-wiring-filter]');
  if (!filter) return;
  filter.addEventListener('input', () => {
    const query = filter.value.trim().toLocaleLowerCase();
    let count = 0;
    document.querySelectorAll('[data-wiring-segment]').forEach(element => {
      element.hidden = !element.dataset.wiringSegment.toLocaleLowerCase().includes(query);
      if (!element.hidden) count++;
    });
    document.querySelector('[data-wiring-no-match]').hidden = count !== 0;
  });
  const tr = key => window.sasTr ? window.sasTr(key) : key;
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href^="#endpoint-"]');
    if (!link) return;
    const target = document.getElementById(link.hash.slice(1));
    if (!target) return;
    filter.value = '';
    filter.dispatchEvent(new Event('input'));
    const editor = target.querySelector('.wiring-address-editor');
    if (editor) editor.open = true;
  });
  document.querySelectorAll('[data-wiring-address-form]').forEach(form => {
    let timer, generation = 0, request;
    const warnings = form.querySelector('[data-wiring-warnings]');
    const preview = async () => {
      const current = ++generation;
      request?.abort();
      request = new AbortController();
      const data = new FormData(form);
      try {
        const response = await fetch(form.dataset.previewUrl, {
          method: 'POST', signal: request.signal,
          headers: {'Content-Type':'application/json', 'X-CSRF-Token':data.get('csrf_token')},
          body: JSON.stringify({addresses:String(data.get('addresses')).split(/\s+/).filter(Boolean),
            segment_id:data.get('segment_id'), endpoint_id:data.get('endpoint_id')})
        });
        const result = await response.json();
        if (current !== generation) return;
        warnings.replaceChildren();
        if (!response.ok) throw new Error(result.error || response.statusText);
        for (const warning of result.warnings) {
          const item = document.createElement('div');
          item.className = 'wiring-warning';
          const usage = warning.usage;
          item.append(`⚠ ${tr('wiring.' + warning.kind)}: ${warning.requested} — `);
          const label = `${usage.segment_name || tr('wiring.disconnected')} · ${usage.prefix} · ${usage.instance_id} / ${usage.interface}`;
          if (usage.endpoint_id) {
            const link = document.createElement('a');
            link.href = '#endpoint-' + usage.endpoint_id;
            link.textContent = label;
            item.append(link);
          } else item.append(label);
          warnings.append(item);
        }
      } catch (error) {
        if (error.name !== 'AbortError' && current === generation) warnings.textContent = error.message;
      }
    };
    form.elements.addresses.addEventListener('input', () => {
      clearTimeout(timer);
      generation++;
      request?.abort();
      timer = setTimeout(preview, 350);
    });
    form.closest('details').addEventListener('toggle', event => {
      if (event.target.open) preview();
    });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const button = form.querySelector('button');
      const output = form.querySelector('[data-wiring-result]');
      button.disabled = true;
      try {
        // A field named "action" shadows HTMLFormElement.action.
        const response = await fetch(form.getAttribute('action') || location.href, {
          method:'POST', headers:{Accept:'application/json'}, body:new FormData(form)
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || response.statusText);
        output.textContent = `${tr('microservices.queued')}: ${result.task_id}. ${tr('wiring.refresh_after')}`;
        // Keep all input intact: a queued task is not successful validation/application.
      } catch (error) { output.textContent = error.message; }
      finally { button.disabled = false; }
    });
  });
})();
