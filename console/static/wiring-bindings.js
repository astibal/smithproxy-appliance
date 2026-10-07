(() => {
  const tr = key => window.sasTr(key);
  document.querySelectorAll('[data-wiring-bindings]').forEach(box => {
    if (box.dataset.targetForm) {
      const form = document.getElementById(box.dataset.targetForm);
      form.insertBefore(box, form.querySelector('.dialog-actions'));
      box.hidden = false;
    }
    const spawn = box.dataset.spawn === 'true';
    const ready = document.createElement('input');
    ready.type = 'hidden'; ready.name = 'wiring_editor_ready'; ready.value = '1';
    const choices = JSON.parse(box.dataset.choices);
    const rows = box.querySelector('[data-binding-rows]');
    const override = box.querySelector('[name=wiring_override]');
    const enabled = () => !override || override.checked;
    function update() {
      rows.querySelectorAll('input,select,textarea,button').forEach(el => el.disabled = !enabled());
      box.querySelector('[data-binding-add]').disabled = !enabled() || rows.children.length >= 16;
    }
    function add(binding = {}) {
      const row = document.createElement('fieldset');
      row.className = 'stack';
      const field = (text, input) => {
        const label = document.createElement('label');
        label.append(text, input); row.append(label); return input;
      };
      const select = field(tr('wiring.connections'), document.createElement('select'));
      select.name = 'wiring_segment'; select.required = true;
      select.add(new Option(tr('runtime.choose'), ''));
      choices.forEach(choice => select.add(new Option(`${choice.name} · ${choice.endpoints.length}/${choice.kind === 'virtual-cable' ? 2 : '∞'}`, choice.id)));
      if (binding.segment_id && !choices.some(c => c.id === binding.segment_id)) {
        select.add(new Option(`⚠ ${binding.segment_id}`, binding.segment_id));
      }
      select.value = binding.segment_id || '';
      const port = field(tr('l2.port'), document.createElement('input'));
      port.name = 'wiring_interface'; port.required = true; port.maxLength = 15;
      port.pattern = '[A-Za-z][A-Za-z0-9_-]{0,14}'; port.value = binding.interface || 'cable' + rows.children.length;
      if (spawn) {
        const mode = field(tr('wiring.mode'), document.createElement('select'));
        mode.name = 'wiring_mode';
        ['none', 'sas', 'guest'].forEach(value => mode.add(new Option(tr('wiring.' + value), value)));
        const addresses = field(tr('wiring.addresses'), document.createElement('textarea'));
        addresses.name = 'wiring_addresses'; addresses.rows = 2;
      }
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'secondary compact'; remove.textContent = tr('wiring.remove_binding');
      remove.onclick = () => { row.remove(); update(); };
      row.append(remove); rows.append(row); update();
    }
    JSON.parse(box.dataset.existing).forEach(add);
    box.querySelector('[data-binding-add]').onclick = () => add();
    override?.addEventListener('change', update);
    update();
    box.append(ready);
  });
})();
