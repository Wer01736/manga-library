/* Edit image names in place; the new name remains staged until Apply is pressed. */
(() => {
  const grid = document.querySelector('#editGrid');
  if (!grid) return;

  const refreshSummary = () => {
    const c = changes();
    $('#editSummary').textContent = c.total
      ? `目前有 ${c.moved} 個順序變更、${c.renamed} 個改名、${c.deleted} 個刪除`
      : '尚未修改';
  };

  grid.addEventListener('dragstart', event => {
    if (event.target.matches('.inline-name-input')) event.preventDefault();
  }, true);

  grid.addEventListener('dblclick', event => {
    const name = event.target.closest('.item-name');
    if (!name || name.querySelector('input')) return;
    const card = name.closest('.edit-item');
    const item = state.working.find(candidate => candidate.source === card.dataset.source);
    if (!item) return;

    const input = document.createElement('input');
    input.className = 'inline-name-input';
    input.value = item.name;
    input.setAttribute('aria-label', '圖片名稱');
    input.addEventListener('mousedown', innerEvent => innerEvent.stopPropagation());
    name.replaceChildren(input);
    input.focus();
    input.select();

    let finished = false;
    const finish = save => {
      if (finished) return;
      finished = true;
      const value = input.value.trim();
      if (save && value) item.name = value;
      name.textContent = item.name;
      name.title = item.name;
      refreshSummary();
    };
    input.addEventListener('keydown', keyEvent => {
      if (keyEvent.key === 'Enter') {
        keyEvent.preventDefault();
        finish(true);
      } else if (keyEvent.key === 'Escape') {
        keyEvent.preventDefault();
        finish(false);
      }
    });
    input.addEventListener('blur', () => finish(true));
  });
})();
