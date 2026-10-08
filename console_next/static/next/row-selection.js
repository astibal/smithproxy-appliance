// Keep native table semantics and text selection; nested controls own their clicks.
export function bindRowSelection(row, activate, hasSelection) {
  row.tabIndex = 0;
  row.addEventListener('click', event => {
    if (event.defaultPrevented || event.button !== 0 || hasSelection()) return;
    if (event.target.closest('button,a,input,select,textarea,[contenteditable="true"]')) return;
    activate();
  });
  row.addEventListener('keydown', event => {
    if (event.target !== row || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || hasSelection()) return;
    const targets = {ArrowDown: row.nextElementSibling, ArrowUp: row.previousElementSibling,
      Home: row.parentElement?.firstElementChild, End: row.parentElement?.lastElementChild};
    if (Object.hasOwn(targets, event.key)) {
      event.preventDefault();
      targets[event.key]?.focus();
      return;
    }
    if (event.repeat) return;
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    activate();
  });
}
