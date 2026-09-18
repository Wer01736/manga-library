/* Keep ordering metadata for every page, but only render nearby thumbnail cards. */
(() => {
  const grid = $('#editGrid');
  const editor = $('#editor');
  const thumbnailUrl = item => `/api/comics/${state.comic.id}/thumbnail?name=${encodeURIComponent(item.source)}&width=160`;
  const virtual = { start: -1, end: -1, frame: 0, dragging: '', dropTarget: null, dropAfter: false, pendingTarget: '', committed: false, autoScroll: 0 };

  const layout = () => {
    const gap = 14;
    const width = Math.max(grid.clientWidth, 150);
    const columns = Math.max(1, Math.floor((width + gap) / (150 + gap)));
    const cardWidth = (width - gap * (columns - 1)) / columns;
    return { gap, columns, cardWidth, rowHeight: Math.ceil(cardWidth * 4 / 3 + 69) };
  };
  const summary = () => {
    const c = changes();
    $('#editSummary').textContent = c.total
      ? `目前有 ${c.moved} 個順序變更、${c.renamed} 個改名、${c.deleted} 個刪除`
      : '尚未修改';
  };
  const range = info => {
    const gridTop = grid.getBoundingClientRect().top - editor.getBoundingClientRect().top + editor.scrollTop;
    const first = Math.max(0, Math.floor((editor.scrollTop - gridTop) / info.rowHeight) - 2) * info.columns;
    const last = Math.min(state.working.length, (Math.ceil((editor.scrollTop + editor.clientHeight - gridTop) / info.rowHeight) + 3) * info.columns);
    return { first, last };
  };
  const card = (item, index, info) => {
    const column = index % info.columns;
    const row = Math.floor(index / info.columns);
    return `<article draggable="true" class="edit-item ${item.deleted ? 'deleted' : ''}" data-index="${index}" data-source="${esc(item.source)}" style="left:${column * (info.cardWidth + info.gap)}px;top:${row * info.rowHeight}px;width:${info.cardWidth}px;height:${info.rowHeight - info.gap}px"><span class="number">${index + 1}</span><img loading="lazy" src="${thumbnailUrl(item)}" onerror="this.onerror=null;this.src='${imageUrl(state.comic.id, item.source)}'"><div class="item-info"><div class="item-name" title="${esc(item.name)}">${esc(item.name)}</div><div class="item-actions"><button data-action="delete">${item.deleted ? '復原' : '刪除'}</button></div></div></article>`;
  };
  const renderVirtual = (force = false) => {
    if (!state.comic) return;
    const info = layout();
    const pages = state.working.length;
    const rows = Math.ceil(pages / info.columns);
    grid.classList.add('virtual-edit-grid');
    grid.style.height = `${Math.max(1, rows * info.rowHeight - info.gap)}px`;
    const next = range(info);
    if (!force && next.first === virtual.start && next.last === virtual.end && grid.dataset.layout === `${info.columns}:${Math.round(info.cardWidth)}`) return;
    virtual.start = next.first;
    virtual.end = next.last;
    grid.dataset.layout = `${info.columns}:${Math.round(info.cardWidth)}`;
    grid.innerHTML = state.working.slice(next.first, next.last).map((item, offset) => card(item, next.first + offset, info)).join('');
    summary();
  };
  const scheduleRender = () => {
    if (virtual.frame) return;
    virtual.frame = requestAnimationFrame(() => {
      virtual.frame = 0;
      renderVirtual();
    });
  };
  const clearDrop = () => {
    if (virtual.dropTarget) virtual.dropTarget.classList.remove('drop-target', 'drop-after');
    virtual.dropTarget = null;
    grid.querySelector('.virtual-drop-marker')?.remove();
  };
  const showDropMarker = (target, after) => {
    let marker = grid.querySelector('.virtual-drop-marker');
    if (!marker) {
      marker = document.createElement('div');
      marker.className = 'virtual-drop-marker';
      grid.append(marker);
    }
    const info = layout();
    const x = after
      ? target.offsetLeft + target.offsetWidth + info.gap / 2
      : target.offsetLeft - info.gap / 2;
    marker.style.left = `${x - 2}px`;
    marker.style.top = `${target.offsetTop}px`;
    marker.style.height = `${target.offsetHeight}px`;
  };
  const commitMove = (targetSource, after) => {
    if (virtual.committed || !virtual.dragging || !targetSource || targetSource === virtual.dragging) return false;
    const from = state.working.findIndex(item => item.source === virtual.dragging);
    const targetIndex = state.working.findIndex(item => item.source === targetSource);
    if (from < 0 || targetIndex < 0) return false;
    let insertAt = targetIndex + (after ? 1 : 0);
    if (from < insertAt) insertAt--;
    virtual.committed = true;
    if (insertAt === from) {
      toast('這張圖片原本就在這個位置');
      return false;
    }
    const [moved] = state.working.splice(from, 1);
    state.working.splice(insertAt, 0, moved);
    renderVirtual(true);
    toast(`已移動到第 ${insertAt + 1} 頁`);
    return true;
  };
  const stopAutoScroll = () => {
    virtual.autoScroll = 0;
  };
  const autoScroll = () => {
    if (!virtual.autoScroll) return;
    editor.scrollTop += virtual.autoScroll;
    renderVirtual();
    requestAnimationFrame(autoScroll);
  };
  const setAutoScroll = clientY => {
    const bounds = editor.getBoundingClientRect();
    const edge = 90;
    const topDistance = clientY - bounds.top;
    const bottomDistance = bounds.bottom - clientY;
    const next = topDistance < edge ? -Math.ceil((edge - topDistance) / 8) : bottomDistance < edge ? Math.ceil((edge - bottomDistance) / 8) : 0;
    if (!virtual.autoScroll && next) requestAnimationFrame(autoScroll);
    virtual.autoScroll = next;
  };

  renderEditor = () => renderVirtual(true);
  openEditor = () => {
    const current = visibleItems()[state.page];
    $('#reader').classList.add('hidden');
    editor.classList.remove('hidden');
    const index = Math.max(0, state.working.findIndex(item => item.source === current?.source));
    const info = layout();
    editor.scrollTop = Math.floor(index / info.columns) * info.rowHeight;
    renderVirtual(true);
  };
  $('#editOrder').onclick = openEditor;

  const originalStartReader = startReader;
  startReader = async () => {
    await originalStartReader();
    state.items = [];
    $('#library').innerHTML = '';
  };
  const releaseCurrentComic = () => {
    state.comic = null;
    state.original = [];
    state.working = [];
    grid.innerHTML = '';
    grid.style.height = '';
  };
  $('#closeReader').onclick = () => {
    $('#reader').classList.add('hidden');
    $('#editor').classList.add('hidden');
    releaseCurrentComic();
    loadLibrary().catch(error => toast(error.message, true));
  };
  const originalApplyChanges = applyChanges;
  applyChanges = async event => {
    await originalApplyChanges(event);
    if ($('#editor').classList.contains('hidden')) releaseCurrentComic();
  };
  $('#confirmApply').onclick = applyChanges;
  renderThumbs = () => {
    const strip = $('#thumbStrip');
    if (strip.classList.contains('hidden')) {
      strip.innerHTML = '';
      return;
    }
    const list = visibleItems();
    const start = Math.max(0, state.page - 7);
    const end = Math.min(list.length, state.page + 8);
    strip.innerHTML = list.slice(start, end).map((item, offset) => {
      const page = start + offset;
      return `<img class="${page === state.page ? 'active' : ''}" data-page="${page}" loading="lazy" src="${thumbnailUrl(item)}" title="${esc(item.name)}">`;
    }).join('');
  };
  $('#toggleThumbs').onclick = () => {
    $('#thumbStrip').classList.toggle('hidden');
    renderThumbs();
  };

  editor.addEventListener('scroll', scheduleRender, { passive: true });
  new ResizeObserver(() => renderVirtual(true)).observe(grid);

  grid.addEventListener('dragstart', event => {
    if (!grid.classList.contains('virtual-edit-grid')) return;
    const item = event.target.closest('.edit-item');
    if (!item) return;
    event.stopImmediatePropagation();
    virtual.dragging = item.dataset.source;
    virtual.pendingTarget = '';
    virtual.dropAfter = false;
    virtual.committed = false;
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', virtual.dragging);
    const preview = document.createElement('canvas');
    preview.className = 'drag-preview';
    preview.width = 1;
    preview.height = 1;
    document.body.append(preview);
    event.dataTransfer.setDragImage(preview, 12, 12);
    requestAnimationFrame(() => preview.remove());
  }, true);
  grid.addEventListener('dragover', event => {
    if (!grid.classList.contains('virtual-edit-grid')) return;
    const target = event.target.closest('.edit-item');
    if (!target || target.dataset.source === virtual.dragging) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    setAutoScroll(event.clientY);
    if (virtual.dropTarget !== target) {
      clearDrop();
      virtual.dropTarget = target;
    }
    const bounds = target.getBoundingClientRect();
    virtual.dropAfter = event.clientX > bounds.left + bounds.width / 2;
    virtual.pendingTarget = target.dataset.source;
    showDropMarker(target, virtual.dropAfter);
  }, true);
  editor.addEventListener('dragover', event => {
    if (!virtual.dragging) return;
    event.preventDefault();
    setAutoScroll(event.clientY);
  });
  grid.addEventListener('drop', event => {
    if (!grid.classList.contains('virtual-edit-grid')) return;
    const target = event.target.closest('.edit-item');
    event.preventDefault();
    event.stopImmediatePropagation();
    if (!target || target.dataset.source === virtual.dragging) return;
    const bounds = target.getBoundingClientRect();
    commitMove(target.dataset.source, event.clientX > bounds.left + bounds.width / 2);
    clearDrop();
    stopAutoScroll();
  }, true);
  editor.addEventListener('drop', event => {
    if (!grid.classList.contains('virtual-edit-grid') || !virtual.dragging) return;
    const target = event.target.closest('.edit-item');
    event.preventDefault();
    event.stopImmediatePropagation();
    if (!target || target.dataset.source === virtual.dragging) return;
    const bounds = target.getBoundingClientRect();
    commitMove(target.dataset.source, event.clientX > bounds.left + bounds.width / 2);
    clearDrop();
    stopAutoScroll();
  }, true);
  grid.addEventListener('dragend', () => {
    if (!virtual.committed && virtual.pendingTarget) commitMove(virtual.pendingTarget, virtual.dropAfter);
    clearDrop();
    stopAutoScroll();
    virtual.dragging = '';
    virtual.pendingTarget = '';
  }, true);
})();
