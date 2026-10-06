(() => {
  'use strict';
  const form = document.querySelector('#config-editor-form');
  const source = document.querySelector('#config-editor-content');
  if (!form || !source) return;

  const key = `sas-config-draft:${form.dataset.configId}`;
  const originalContent = source.value;
  const saved = localStorage.getItem(key);
  if (saved) {
    try {
      const draft = JSON.parse(saved);
      if (typeof draft.content === 'string') source.value = draft.content;
      if (draft.buildId) form.elements.build_id.value = draft.buildId;
      if (typeof draft.copyName === 'string') form.elements.copy_name.value = draft.copyName;
      form.dataset.draftRestored = 'true';
    } catch (_error) {
      localStorage.removeItem(key);
    }
  }

  const saveDraft = () => {
    const changed = source.value !== originalContent || Boolean(form.elements.copy_name?.value);
    if (!changed) {
      localStorage.removeItem(key);
      return;
    }
    localStorage.setItem(key, JSON.stringify({
      content: source.value,
      buildId: form.elements.build_id?.value || '',
      copyName: form.elements.copy_name?.value || '',
      savedAt: new Date().toISOString(),
    }));
  };
  window.setInterval(saveDraft, 1000);
  window.addEventListener('pagehide', saveDraft);

  window.setTimeout(() => {
    const state = document.querySelector('#config-save-state');
    if (form.dataset.draftRestored === 'true' && state) {
      state.textContent = window.sasTr("ui.68b088c4e277");
      state.className = 'config-save-state ok';
    }

    const contentButtons = [...form.querySelectorAll(
      'button[name="save_mode"][value="replace"], button[name="save_mode"][value="copy"]'
    )];
    form.addEventListener('submit', async event => {
      const mode = event.submitter?.value;
      if (!['replace', 'copy'].includes(mode)) return;
      event.preventDefault();

      const buildId = form.elements.build_id?.value || '';
      const copyName = form.elements.copy_name?.value.trim() || '';
      if (!buildId) {
        state.textContent = window.sasTr("ui.11e4be8911c4");
        state.className = 'config-save-state error';
        form.elements.build_id?.focus();
        return;
      }
      if (mode === 'copy' && !copyName) {
        state.textContent = window.sasTr("ui.f26f9eb2375a");
        state.className = 'config-save-state error';
        form.elements.copy_name?.focus();
        return;
      }

      saveDraft();
      contentButtons.forEach(button => { button.disabled = true; });
      state.textContent = window.sasTr("ui.a41efd203d13");
      state.className = 'config-save-state pending';
      try {
        const data = new FormData(form);
        data.set('save_mode', mode);
        const response = await fetch(form.action || location.href, {
          method: 'POST', body: data, cache: 'no-store',
          headers: {'X-Requested-With': 'task-fetch'},
        });
        const queued = await response.json();
        if (!response.ok) throw new Error(queued.error || `HTTP ${response.status}`);
        document.dispatchEvent(new CustomEvent('task-queued'));
        const taskId = queued.task_id;
        while (true) {
          await new Promise(resolve => window.setTimeout(resolve, 1000));
          const tasksResponse = await fetch('/api/tasks', {cache: 'no-store'});
          const tasksPayload = await tasksResponse.json();
          if (!tasksResponse.ok) throw new Error(tasksPayload.error || `HTTP ${tasksResponse.status}`);
          const task = (tasksPayload.tasks || []).find(item => item.task_id === taskId);
          if (!task || ['pending', 'running'].includes(task.state)) continue;
          if (task.state !== 'succeeded') throw new Error(task.error || 'Validace selhala.');
          localStorage.removeItem(key);
          location.assign(`/tasks/${encodeURIComponent(taskId)}/result`);
          return;
        }
      } catch (error) {
        saveDraft();
        state.textContent = window.sasTr("ui.de8936cf8cff", {error: error.message || error});
        state.className = 'config-save-state error';
        contentButtons.forEach(button => { button.disabled = false; });
      }
    });
  }, 0);
})();
