(() => {
  const select = document.querySelector('#export-config');
  const panel = document.querySelector('#export-parameters');
  const fields = panel?.querySelector('div');
  if (!select || !panel || !fields) return;
  const render = () => {
    let names = [];
    try { names = JSON.parse(select.selectedOptions[0]?.dataset.placeholders || '[]'); } catch (_) {}
    fields.replaceChildren(...names.map(name => {
      const label = document.createElement('label'); label.textContent = name;
      const input = document.createElement('input'); input.name = `param_${name}`;
      input.required = true; input.autocomplete = 'off'; input.placeholder = `{{${name}}}`;
      label.append(input); return label;
    }));
    panel.hidden = names.length === 0;
  };
  select.addEventListener('change', render); render();
})();
