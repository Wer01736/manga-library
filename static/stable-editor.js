/* Reliable full-grid ordering using lightweight, lazy-loaded local thumbnails. */
(() => {
  const grid = $('#editGrid');
  const editor = $('#editor');
  const thumbnailUrl = item => `/api/comics/${state.comic.id}/thumbnail?name=${encodeURIComponent(item.source)}&width=160`;

  const updateSummary = () => {
    const c = changes();
    $('#editSummary').textContent = c.total
      ? `目前有 ${c.moved} 個順序變更、${c.renamed} 個改名、${c.deleted} 個刪除`
      : '尚未修改';
  };

  renderEditor = () => {
    grid.classList.remove('virtual-edit-grid');
    grid.style.height = '';
    grid.innerHTML = state.working.map((item, index) =>
      `<article draggable="true" class="edit-item ${item.deleted ? 'deleted' : ''}" data-index="${index}" data-source="${esc(item.source)}"><span class="number">${index + 1}</span><img loading="lazy" draggable="false" src="${thumbnailUrl(item)}" onerror="this.onerror=null;this.src='${imageUrl(state.comic.id, item.source)}'"><div class="item-info"><div class="item-name" title="${esc(item.name)}">${esc(item.name)}</div><div class="item-actions"><button data-action="delete">${item.deleted ? '復原' : '刪除'}</button></div></div></article>`
    ).join('');
    updateSummary();
  };

  openEditor = () => {
    const current = visibleItems()[state.page];
    $('#reader').classList.add('hidden');
    editor.classList.remove('hidden');
    renderEditor();
    requestAnimationFrame(() => grid.querySelector(`[data-source="${CSS.escape(current?.source || '')}"]`)?.scrollIntoView({ block: 'center' }));
  };
  $('#editOrder').onclick = openEditor;

  const actions = document.querySelector('.editor-actions');
  if (actions && !$('#renameAllPages')) {
    const batchRow = document.createElement('div');
    batchRow.className = 'editor-batch-actions';
    batchRow.innerHTML = '<button id="renameAllPages" class="ghost" type="button">全部重新命名（000 起）</button>';
    actions.insertBefore(batchRow, actions.querySelector('.editor-commit-actions'));
    $('#renameAllPages').onclick = () => {
      const activeItems = state.working.filter(item => !item.deleted);
      const digits = Math.max(3, String(Math.max(0, activeItems.length - 1)).length);
      let sequence = 0;
      state.working.forEach(item => {
        if (item.deleted) return;
        const extensionMatch = item.name.match(/(\.[^./\\]+)$/);
        const extension = extensionMatch ? extensionMatch[1].toLowerCase() : '';
        item.name = `${String(sequence).padStart(digits, '0')}${extension}`;
        sequence++;
        const nameElement = grid.querySelector(`[data-source="${CSS.escape(item.source)}"] .item-name`);
        if (nameElement) {
          nameElement.textContent = item.name;
          nameElement.title = item.name;
        }
      });
      updateSummary();
    };
  }

  const releaseCurrentComic = () => {
    state.comic = null;
    state.original = [];
    state.working = [];
    grid.innerHTML = '';
  };
  let returningToLibrary = false;
  const returnToLibrary = async ({ refresh = false } = {}) => {
    if (returningToLibrary) return;
    returningToLibrary = true;
    $('#reader').classList.add('hidden');
    editor.classList.add('hidden');
    if (!refresh && typeof returnToDuplicateComparison === 'function' && returnToDuplicateComparison()) {
      releaseCurrentComic();
      returningToLibrary = false;
      return;
    }
    releaseCurrentComic();
    if (!refresh) {
      returningToLibrary = false;
      return;
    }
    try {
      await loadLibrary();
    } catch (error) {
      toast(error.message, true);
    } finally {
      returningToLibrary = false;
    }
  };
  $('#closeReader').onclick = () => returnToLibrary();
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || $('#reader').classList.contains('hidden')) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    returnToLibrary();
  }, true);

  const confirmationInput = $('#confirmText');
  const confirmationLabel = confirmationInput?.closest('label');
  if (confirmationLabel) confirmationLabel.replaceChildren('請輸入「確認」以套用變更', confirmationInput);
  applyChanges = async event => {
    event.preventDefault();
    if ($('#confirmText').value.trim() !== '確認') {
      toast('請輸入「確認」', true);
      return;
    }
    const button = $('#confirmApply');
    button.disabled = true;
    try {
      await api(`/api/comics/${state.comic.id}/apply`, {
        method: 'POST',
        body: JSON.stringify({ items: state.working, confirmation: '確認' })
      });
      $('#confirmDialog').close();
      editor.classList.add('hidden');
      await returnToLibrary({ refresh: true });
      toast('已套用變更');
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  };
  $('#confirmApply').onclick = applyChanges;
  confirmationInput?.addEventListener('keydown', event => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    event.stopPropagation();
    if (!$('#confirmApply').disabled) applyChanges(event);
  });

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
})();
