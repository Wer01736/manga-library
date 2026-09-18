/* Keep drag-and-drop entirely in the browser.  The ZIP is only touched by Apply. */
(() => {
  const grid = document.querySelector('#editGrid');
  if (!grid) return;

  let dragSource = '';
  let dropTarget = null;
  const cards = () => [...grid.querySelectorAll(':scope > .edit-item')];
  const clearTarget = () => {
    if (!dropTarget) return;
    dropTarget.classList.remove('drop-target', 'drop-after');
    dropTarget = null;
    grid.querySelector('.grid-drop-marker')?.remove();
  };
  const showDropMarker = (target, after) => {
    let marker = grid.querySelector('.grid-drop-marker');
    if (!marker) {
      marker = document.createElement('div');
      marker.className = 'grid-drop-marker';
      grid.append(marker);
    }
    const gap = 14;
    const x = after ? target.offsetLeft + target.offsetWidth + gap / 2 : target.offsetLeft - gap / 2;
    marker.style.left = `${x - 2}px`;
    marker.style.top = `${target.offsetTop}px`;
    marker.style.height = `${target.offsetHeight}px`;
  };
  const updateSummary = () => {
    const c = changes();
    document.querySelector('#editSummary').textContent = c.total
      ? `目前有 ${c.moved} 個順序變更、${c.renamed} 個改名、${c.deleted} 個刪除`
      : '尚未修改';
  };
  const syncPositions = () => {
    cards().forEach((card, index) => {
      card.dataset.index = index;
      card.querySelector('.number').textContent = index + 1;
    });
    updateSummary();
  };

  grid.addEventListener('dragstart', event => {
    if (grid.classList.contains('virtual-edit-grid')) return;
    const card = event.target.closest('.edit-item');
    if (!card) return;
    dragSource = card.dataset.source;
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', dragSource);
    const preview = document.createElement('canvas');
    preview.className = 'drag-preview';
    preview.width = 1;
    preview.height = 1;
    document.body.append(preview);
    event.dataTransfer.setDragImage(preview, 12, 12);
    requestAnimationFrame(() => preview.remove());
  }, true);

  grid.addEventListener('dragover', event => {
    if (grid.classList.contains('virtual-edit-grid')) return;
    const target = event.target.closest('.edit-item');
    if (!target || target.dataset.source === dragSource) return;
    event.preventDefault();
    if (dropTarget !== target) {
      clearTarget();
      dropTarget = target;
      target.classList.add('drop-target');
    }
    const bounds = target.getBoundingClientRect();
    const after = event.clientX > bounds.left + bounds.width / 2;
    target.classList.toggle('drop-after', after);
    showDropMarker(target, after);
    event.dataTransfer.dropEffect = 'move';
  }, true);

  grid.addEventListener('drop', event => {
    if (grid.classList.contains('virtual-edit-grid')) return;
    const target = event.target.closest('.edit-item');
    const dragged = cards().find(card => card.dataset.source === dragSource);
    if (!target || !dragged || target === dragged) return;
    event.preventDefault();
    event.stopImmediatePropagation();

    const bounds = target.getBoundingClientRect();
    const after = event.clientX > bounds.left + bounds.width / 2;
    const from = state.working.findIndex(item => item.source === dragSource);
    const targetIndex = state.working.findIndex(item => item.source === target.dataset.source);
    let insertAt = targetIndex + (after ? 1 : 0);
    const [moved] = state.working.splice(from, 1);
    if (from < insertAt) insertAt--;
    state.working.splice(insertAt, 0, moved);

    target.parentNode.insertBefore(dragged, after ? target.nextSibling : target);
    clearTarget();
    syncPositions();
  }, true);

  grid.addEventListener('dragend', event => {
    if (grid.classList.contains('virtual-edit-grid')) return;
    clearTarget();
  }, true);

  // Revert the staged state by reordering existing cards instead of rebuilding every thumbnail.
  document.querySelector('#discard').onclick = () => {
    if (!confirm('放棄所有尚未儲存的修改？')) return;
    state.working = structuredClone(state.original);
    if (grid.classList.contains('virtual-edit-grid')) {
      renderEditor();
      toast('已放棄尚未儲存的修改');
      return;
    }
    const bySource = new Map(cards().map(card => [card.dataset.source, card]));
    state.working.forEach(item => {
      const card = bySource.get(item.source);
      if (!card) return;
      card.classList.toggle('deleted', item.deleted);
      card.querySelector('.item-name').textContent = item.name;
      card.querySelector('.item-name').title = item.name;
      grid.appendChild(card);
    });
    syncPositions();
    toast('已放棄尚未儲存的修改');
  };
})();
