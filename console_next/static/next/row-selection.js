// Keep native table semantics and text selection; nested controls own their clicks.
export function bindRowSelection(row, activate, hasSelection) {
  row.tabIndex = 0;
  row.addEventListener('click', event => {
    if (event.defaultPrevented || event.button !== 0 || hasSelection()) return;
    if (event.target.closest('button,a,input,select,textarea,[contenteditable="true"]')) return;
    activate();
  });
  row.addEventListener('keydown', event => {
    if (event.target !== row || event.repeat || hasSelection()) return;
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    activate();
  });
}
