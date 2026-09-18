state.selected = new Set();
state.customGroups = [];
state.filter.root = '';
state.filter.tag = '';
state.tags = [];
state.libraryPage = 1;
state.libraryQueryKey = '';
const LIBRARY_PAGE_SIZE = 50;
// Remove the obsolete global button if an older cached HTML shell is still open.
$('#runSimilar')?.remove();
let pendingDeleteComic = null;
let pendingDeleteResultRow = null;
let duplicateReviewContext = null;
let detailTags = [];
const originalFileAction = fileAction;
const originalStartReader = startReader;

function rememberDuplicatePosition() {
  duplicateReviewContext = {
    dialogScroll: $('#duplicatesDialog').scrollTop,
    resultsScroll: $('#duplicateResults').scrollTop
  };
}

function returnToDuplicateComparison() {
  if (!duplicateReviewContext) return false;
  const position = duplicateReviewContext;
  duplicateReviewContext = null;
  $('#reader').classList.add('hidden');
  if ($('#detailDialog').open) $('#detailDialog').close();
  if (!$('#duplicatesDialog').open) $('#duplicatesDialog').showModal();
  requestAnimationFrame(() => {
    $('#duplicatesDialog').scrollTop = position.dialogScroll;
    $('#duplicateResults').scrollTop = position.resultsScroll;
  });
  $('#closeReader').textContent = '← 返回書庫';
  return true;
}

startReader = async function () {
  await originalStartReader();
  $('#thumbStrip').classList.add('hidden');
  $('#reader').classList.remove('thumbs-open');
  $('#toggleThumbs').textContent = '縮圖總覽';
  $('#closeReader').textContent = duplicateReviewContext ? '← 返回重複比較' : '← 返回書庫';
};

$('#toggleThumbs').onclick = () => {
  const opening = $('#thumbStrip').classList.contains('hidden');
  $('#thumbStrip').classList.toggle('hidden', !opening);
  $('#reader').classList.toggle('thumbs-open', opening);
  $('#toggleThumbs').textContent = opening ? '關閉縮圖' : '縮圖總覽';
  if (opening) {
    requestAnimationFrame(() => $('#thumbStrip img.active')?.scrollIntoView({ block: 'center' }));
  }
};

showDetail = async function (id) {
  const c = await api(`/api/comics/${id}`);
  state.comic = c;
  detailTags = [...(c.tags || [])];
  const authors = [...new Set((c.author || '').split(/[,，、]+/).map(value => value.trim()).filter(Boolean))];
  const sourceLabels = {
    manual: ['已確認', 'confirmed'], guessed: ['系統推測', 'guessed'], legacy: ['待確認', 'legacy']
  };
  const [sourceLabel, sourceClass] = sourceLabels[c.author_source] || ['未設定', 'empty'];
  const authorButtons = authors.length
    ? authors.map(author => `<button type="button" class="detail-author-search" data-search-detail-author="${esc(author)}" title="搜尋 ${esc(author)} 的其他作品">${esc(author)}</button>`).join('')
    : '<span class="detail-author-empty">尚未設定作者</span>';
  $('#detailBody').innerHTML = `<div class="detail-grid"><div class="detail-cover-column"><img class="detail-cover" src="${c.cover_name ? imageUrl(c.id, c.cover_name) : ''}"><div class="detail-author-panel"><div class="detail-author-heading"><span>作者</span><span class="author-source ${sourceClass}">${sourceLabel}</span></div><div class="detail-author-buttons">${authorButtons}</div><button type="button" class="ghost detail-author-edit" id="editDetailAuthor">${authors.length ? '修改作者' : '設定作者'}</button></div></div><div class="detail-info"><p class="eyebrow">${esc(c.extension.slice(1).toUpperCase())} · ${c.image_count} 頁</p><h2>${esc(c.name)}</h2><dl><dt>推測作品</dt><dd>${esc(c.title_guess || '—')}</dd><dt>集數</dt><dd>${esc(c.volume_guess || '—')}</dd><dt>大小</dt><dd>${fmt(c.size_bytes)}</dd><dt>完整路徑</dt><dd>${esc(c.path)}</dd>${c.error ? `<dt>錯誤</dt><dd>${esc(c.error)}</dd>` : ''}</dl><div class="detail-primary-actions"><button class="primary" id="readComic">開始閱讀</button><button class="danger" id="deleteFile">刪除</button></div><details class="detail-settings"><summary>⚙ 設定</summary><div class="detail-fields"><label>作者<input id="authorInput" value="${esc(c.author)}" placeholder="可留空"></label><label>分組（可多個，以逗號分隔）<input id="groupsInput" value="${esc((c.groups || []).join(', '))}" placeholder="例如：最愛, 待整理"></label><label class="wide-field">標籤<div id="tagEditor" class="tag-editor"><div id="tagChips" class="tag-chips"></div><input id="tagChipInput" autocomplete="off" placeholder="輸入標籤後按 Enter"></div><small>按 Enter 或輸入逗號建立標籤；點 × 移除</small></label></div><div class="detail-actions"><button class="primary" id="saveMetadata">儲存設定</button><button class="ghost" id="openFolder">開啟所在資料夾</button><button class="ghost" id="renameFile">重新命名</button><button class="ghost" id="moveFile">移動</button></div></details></div></div>`;
  renderDetailTags();
  $('#detailDialog').showModal();
};

function commaValues(value) {
  return [...new Set(value.split(/[,，、]+/).map(item => item.trim()).filter(Boolean))];
}

function renderDetailTags() {
  const chips = $('#tagChips');
  if (!chips) return;
  chips.innerHTML = detailTags.map((tag, index) => `<span class="editable-tag"><span>${esc(tag)}</span><button type="button" data-remove-detail-tag="${index}" aria-label="移除標籤 ${esc(tag)}">×</button></span>`).join('');
}

function commitDetailTagInput() {
  const input = $('#tagChipInput');
  if (!input) return;
  const additions = commaValues(input.value);
  additions.forEach(tag => {
    if (!detailTags.some(existing => existing.toLocaleLowerCase() === tag.toLocaleLowerCase())) detailTags.push(tag);
  });
  input.value = '';
  renderDetailTags();
}

$('#detailBody').addEventListener('keydown', event => {
  if (event.target.id !== 'tagChipInput' || event.isComposing) return;
  if (event.key === 'Enter' || event.key === ',' || event.key === '，') {
    event.preventDefault();
    commitDetailTagInput();
  }
});

$('#detailBody').addEventListener('input', event => {
  if (event.target.id === 'tagChipInput' && !event.isComposing && /[,，、]/.test(event.target.value)) commitDetailTagInput();
});

$('#detailBody').addEventListener('focusout', event => {
  if (event.target.id === 'tagChipInput') setTimeout(commitDetailTagInput, 0);
});

$('#detailBody').addEventListener('click', event => {
  const authorSearch = event.target.closest('[data-search-detail-author]');
  if (authorSearch) {
    const author = authorSearch.dataset.searchDetailAuthor;
    $('#detailDialog').close();
    $('#search').value = author;
    state.filter = { author, group: '', tag: '', root: '' };
    state.libraryPage = 1;
    loadLibrary().then(() => toast(`正在顯示 ${author} 的作品`)).catch(error => toast(error.message, true));
    return;
  }
  if (event.target.closest('#editDetailAuthor')) {
    const settings = $('#detailBody').querySelector('.detail-settings');
    settings.open = true;
    requestAnimationFrame(() => {
      $('#authorInput').focus();
      $('#authorInput').select();
    });
    return;
  }
  const removeButton = event.target.closest('[data-remove-detail-tag]');
  if (!removeButton) {
    if (event.target.closest('#tagEditor')) $('#tagChipInput')?.focus();
    return;
  }
  detailTags.splice(Number(removeButton.dataset.removeDetailTag), 1);
  renderDetailTags();
  $('#tagChipInput')?.focus();
});

saveMetadata = async function () {
  const groups = commaValues($('#groupsInput').value);
  commitDetailTagInput();
  const tags = [...detailTags];
  await Promise.all([
    api(`/api/comics/${state.comic.id}/metadata`, {
      method: 'PATCH', body: JSON.stringify({ author: $('#authorInput').value, group_name: groups[0] || '' })
    }),
    api(`/api/comics/${state.comic.id}/groups`, {
      method: 'PUT', body: JSON.stringify({ groups })
    }),
    api(`/api/comics/${state.comic.id}/tags`, {
      method: 'PUT', body: JSON.stringify({ tags })
    })
  ]);
  toast('作者、分組與標籤已儲存，原始檔案未變更');
  $('#detailDialog').close();
  await loadLibrary();
};

$('#detailDialog').addEventListener('click', event => {
  if (event.target !== $('#detailDialog')) return;
  if (!returnToDuplicateComparison()) $('#detailDialog').close();
});

$('#detailDialog').addEventListener('click', event => {
  const closeButton = event.target.closest('[data-close="detailDialog"]');
  if (!closeButton || !duplicateReviewContext) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  returnToDuplicateComparison();
}, true);

$('#detailDialog').addEventListener('cancel', event => {
  if (!duplicateReviewContext) return;
  event.preventDefault();
  returnToDuplicateComparison();
});

$('#closeReader').onclick = () => {
  if (returnToDuplicateComparison()) return;
  $('#reader').classList.add('hidden');
};

document.addEventListener('keydown', event => {
  if ($('#reader').classList.contains('hidden')) return;
  if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
    event.preventDefault();
    event.stopImmediatePropagation();
    movePage(event.key === 'ArrowDown' ? 1 : -1);
    return;
  }
  if (event.key === 'Escape' && duplicateReviewContext) {
    event.preventDefault();
    event.stopImmediatePropagation();
    returnToDuplicateComparison();
  }
}, true);

$('#pageImage').onclick = event => {
  const zoomed = event.currentTarget.classList.toggle('zoom');
  const stage = event.currentTarget.closest('.reader-stage');
  stage.classList.toggle('zoomed', zoomed);
  if (!zoomed) stage.scrollTo({ top: 0, left: 0 });
};

function openDeleteDialog(comic, resultRow = null) {
  pendingDeleteComic = comic;
  pendingDeleteResultRow = resultRow;
  $('#deleteFileName').textContent = pendingDeleteComic.name;
  $('#deleteFilePath').textContent = pendingDeleteComic.path;
  $('#deleteDialog').showModal();
}

fileAction = async function (kind) {
  if (kind !== 'delete') return originalFileAction(kind);
  openDeleteDialog(state.comic);
};

$('#cancelDelete').onclick = () => {
  pendingDeleteComic = null;
  pendingDeleteResultRow = null;
  $('#deleteDialog').close();
};

$('#confirmDelete').onclick = async () => {
  if (!pendingDeleteComic) return;
  const button = $('#confirmDelete');
  button.disabled = true;
  try {
    const deletedComicId = pendingDeleteComic.id;
    const result = await api(`/api/comics/${deletedComicId}/delete`, {
      method: 'POST', body: JSON.stringify({ value: '', confirmation: 'confirm-delete' })
    });
    $('#deleteDialog').close();
    $('#detailDialog').close();
    const affectedGroups = new Set();
    document.querySelectorAll(`[data-review-id="${deletedComicId}"], [data-delete-duplicate-id="${deletedComicId}"]`).forEach(action => {
      const row = action.closest('.duplicate-file');
      if (!row) return;
      const group = row.closest('.duplicate-group');
      if (group) affectedGroups.add(group);
      row.remove();
    });
    if (pendingDeleteResultRow?.isConnected) {
      const group = pendingDeleteResultRow.closest('.duplicate-group');
      if (group) affectedGroups.add(group);
      pendingDeleteResultRow.remove();
    }
    affectedGroups.forEach(group => {
      const remainingIds = [...group.querySelectorAll('[data-review-id]')].map(action => Number(action.dataset.reviewId));
      if (remainingIds.length < 2) {
        group.remove();
        return;
      }
      const controls = group.querySelector('.review-actions');
      if (controls) controls.outerHTML = reviewControls(remainingIds, 'duplicate');
    });
    // If deletion started from the detail/reader flow, return to the exact
    // duplicate position the user was reviewing instead of falling back to the library.
    returnToDuplicateComparison();
    if (!$('#duplicateResults').querySelector('.duplicate-group') && $('#duplicatesDialog').open) {
      $('#duplicateResults').innerHTML = '<div class="empty"><h3>目前沒有仍成立的重複群組</h3><p>已刪除的檔案不會再次列入比較。</p></div>';
    }
    pendingDeleteComic = null;
    pendingDeleteResultRow = null;
    toast(result.status === 'already_deleted' ? '檔案先前已經刪除，索引已同步' : '檔案已刪除，重複結果已同步');
    await loadLibrary();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
};

// The base UI originally searched while typing. Stop that event and search only on Enter.
$('#search').addEventListener('input', event => event.stopImmediatePropagation(), true);
$('#search').addEventListener('keydown', event => {
  if (event.key !== 'Enter' || event.isComposing) return;
  event.preventDefault();
  loadLibrary().catch(error => toast(error.message, true));
});

async function loadGroups() {
  state.customGroups = await api('/api/groups');
  const used = new Map(state.customGroups.map(group => [group.name, group.comic_count]));
  for (const item of state.items) {
    for (const group of item.groups || []) if (!used.has(group)) used.set(group, 1);
  }
  $('#groups').innerHTML = '<option value="">全部分組</option>' + [...used.entries()].map(([name, count]) =>
    `<option value="${esc(name)}" ${state.filter.group === name ? 'selected' : ''}>${esc(name)}（${count} 本）</option>`
  ).join('');
}

async function loadTags() {
  state.tags = await api('/api/tags');
  $('#tags').innerHTML = '<option value="">全部標籤</option>' + state.tags.map(tag =>
    `<option value="${esc(tag.name)}" ${state.filter.tag === tag.name ? 'selected' : ''}>${esc(tag.name)}（${tag.comic_count} 本）</option>`
  ).join('');
}

loadRoots = async function () {
  const roots = await api('/api/roots');
  const total = roots.length ? roots[0].total_count : 0;
  $('#roots').className = 'root-nav';
  $('#roots').innerHTML = `<button class="${state.filter.root ? '' : 'active'}" data-root=""><strong><span>全部位置</span><span>${total}</span></strong><small>所有掃描資料夾</small></button>` + roots.map(root => {
    const label = root.path.split(/[\\/]/).filter(Boolean).pop() || root.path;
    return `<button class="${state.filter.root === root.path ? 'active' : ''}" data-root="${esc(root.path)}"><strong><span>${esc(label)}</span><span>${root.comic_count}</span></strong><small title="${esc(root.path)}">${esc(root.path)}</small></button>`;
  }).join('');
};

loadLibrary = async function () {
  const q = new URLSearchParams({
    q: $('#search').value,
    sort: $('#sort').value,
    direction: $('#direction').value,
    author: state.filter.author,
    group: state.filter.group,
    tag: state.filter.tag,
    root: state.filter.root
  });
  const queryKey = q.toString();
  if (state.libraryQueryKey && state.libraryQueryKey !== queryKey) state.libraryPage = 1;
  state.libraryQueryKey = queryKey;
  const data = await api('/api/comics?' + q);
  state.items = data.items;
  const pageCount = Math.max(1, Math.ceil(state.items.length / LIBRARY_PAGE_SIZE));
  state.libraryPage = Math.min(Math.max(1, state.libraryPage), pageCount);
  $('#summary').textContent = `${data.items.length} 個檔案${state.filter.root ? ' · 指定掃描位置' : ' · 全部位置'}${state.filter.author ? ` · 作者：${state.filter.author}` : ''}${state.filter.group ? ` · 分組：${state.filter.group}` : ''}${state.filter.tag ? ` · 標籤：${state.filter.tag}` : ''}`;
  renderNav('authors', data.authors, 'author');
  $('#authors').insertAdjacentHTML('afterbegin', `<button class="${state.filter.author ? '' : 'active'}" data-filter="author" data-value=""><span>全部作者</span><span>${data.authors.length}</span></button>`);
  $('#authorCount').textContent = data.authors.length;
  renderCards();
  await loadGroups();
  await loadTags();
  await loadRoots();
};

$('#groups').addEventListener('change', event => {
  state.filter.group = event.target.value;
  loadLibrary().catch(error => toast(error.message, true));
});

$('#tags').addEventListener('change', event => {
  state.filter.tag = event.target.value;
  loadLibrary().catch(error => toast(error.message, true));
});

$('#direction').addEventListener('change', () => {
  loadLibrary().catch(error => toast(error.message, true));
});

$('#authors').addEventListener('click', event => {
  const button = event.target.closest('[data-filter="author"]');
  if (!button) return;
  event.stopImmediatePropagation();
  state.filter.author = button.dataset.value;
  state.filter.group = '';
  loadLibrary().catch(error => toast(error.message, true));
}, true);

$('#roots').addEventListener('click', event => {
  const button = event.target.closest('[data-root]');
  if (!button) return;
  state.filter.root = button.dataset.root;
  state.selected.clear();
  loadLibrary().catch(error => toast(error.message, true));
});

$('#clearFilter').onclick = () => {
  state.filter = { author: '', group: '', tag: '', root: '' };
  $('#search').value = '';
  loadLibrary().catch(error => toast(error.message, true));
};

$('#addRoot').onclick = async () => {
  const button = $('#addRoot');
  button.disabled = true;
  button.textContent = '請在視窗中選擇…';
  try {
    const picked = await api('/api/pick-folder', { method: 'POST', body: '{}' });
    if (!picked.selected) return;
    await api('/api/roots', { method: 'POST', body: JSON.stringify({ path: picked.path }) });
    button.textContent = '正在掃描新位置…';
    const result = await api('/api/scan', { method: 'POST', body: JSON.stringify({ path: picked.path }) });
    state.filter.root = picked.path;
    toast(`新位置掃描完成：找到 ${result.found}、更新 ${result.updated}、失敗 ${result.failed}`);
    await loadLibrary();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = '＋ 新增掃描資料夾';
  }
};

renderCards = function () {
  const lib = $('#library');
  $('#empty').classList.toggle('hidden', state.items.length > 0);
  const start = (state.libraryPage - 1) * LIBRARY_PAGE_SIZE;
  const visibleComics = state.items.slice(start, start + LIBRARY_PAGE_SIZE);
  lib.innerHTML = visibleComics.map(c => `<article class="card ${state.selected.has(c.id) ? 'selected' : ''}" data-id="${c.id}"><div class="cover"><input class="select-comic" type="checkbox" aria-label="選取 ${esc(c.name)}" ${state.selected.has(c.id) ? 'checked' : ''}>${c.cover_name ? `<img loading="lazy" src="${imageUrl(c.id, c.cover_name)}">` : ''}<span class="format">${esc(c.extension.slice(1).toUpperCase())}</span><span class="pages">${c.image_count} 頁</span></div><div class="card-body"><h3 title="${esc(c.name)}">${esc(c.name)}</h3><div class="meta">${c.author ? `<span class="tag">${esc(c.author)}</span>` : ''}<span>${fmt(c.size_bytes)}</span></div><div class="card-path" title="${esc(c.path)}">${esc(c.path)}</div><div class="card-tags">${(c.tags || []).map(tag => `<span class="card-tag">#${esc(tag)}</span>`).join('')}</div></div></article>`).join('');
  renderLibraryPagination();
  updateBatchBar();
};

function paginationTokens(current, total) {
  if (total <= 8) return Array.from({ length: total }, (_, index) => index + 1);
  if (current <= 5) return [1, 2, 3, 4, 5, 6, 7, 'ellipsis', total];
  if (current >= total - 4) return [1, 'ellipsis', ...Array.from({ length: 7 }, (_, index) => total - 6 + index)];
  return [1, 'ellipsis', current - 2, current - 1, current, current + 1, current + 2, 'ellipsis', total];
}

function renderLibraryPagination() {
  const pagination = $('#libraryPagination');
  const totalPages = Math.ceil(state.items.length / LIBRARY_PAGE_SIZE);
  pagination.classList.toggle('hidden', totalPages <= 1);
  if (totalPages <= 1) {
    pagination.innerHTML = '';
    return;
  }
  const pageButtons = paginationTokens(state.libraryPage, totalPages).map(token =>
    token === 'ellipsis'
      ? '<span class="pagination-ellipsis">…</span>'
      : `<button class="pagination-page ${token === state.libraryPage ? 'active' : ''}" data-library-page="${token}" aria-label="第 ${token} 頁" ${token === state.libraryPage ? 'aria-current="page"' : ''}>${token}</button>`
  ).join('');
  pagination.innerHTML = `${state.libraryPage > 1 ? '<button data-library-page="previous">上一頁</button>' : ''}${pageButtons}${state.libraryPage < totalPages ? '<button class="pagination-next" data-library-page="next">下一頁</button><button data-library-page="last">尾頁</button>' : ''}`;
}

$('#libraryPagination').onclick = event => {
  const button = event.target.closest('[data-library-page]');
  if (!button) return;
  const totalPages = Math.max(1, Math.ceil(state.items.length / LIBRARY_PAGE_SIZE));
  const target = button.dataset.libraryPage;
  if (target === 'previous') state.libraryPage -= 1;
  else if (target === 'next') state.libraryPage += 1;
  else if (target === 'last') state.libraryPage = totalPages;
  else state.libraryPage = Number(target);
  state.libraryPage = Math.min(Math.max(1, state.libraryPage), totalPages);
  renderCards();
  document.querySelector('main header')?.scrollIntoView({ block: 'start', behavior: 'smooth' });
};

function updateBatchBar() {
  $('#batchBar').classList.toggle('hidden', state.selected.size === 0);
  $('#selectedCount').textContent = `已選 ${state.selected.size} 本`;
}

$('#library').addEventListener('click', event => {
  const checkbox = event.target.closest('.select-comic');
  if (!checkbox) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  const card = checkbox.closest('.card');
  const id = Number(card.dataset.id);
  if (state.selected.has(id)) state.selected.delete(id); else state.selected.add(id);
  renderCards();
}, true);

$('#clearSelection').onclick = () => { state.selected.clear(); renderCards(); };

async function createGroupFromName(name) {
  name = name.trim();
  if (!name) return null;
  try {
    return await api('/api/groups', { method: 'POST', body: JSON.stringify({ name }) });
  } catch (error) {
    if (!error.message.includes('已經存在')) throw error;
    return state.customGroups.find(group => group.name.toLowerCase() === name.toLowerCase()) || { name };
  }
}

$('#addGroup').onclick = async () => {
  const name = prompt('輸入新分組名稱\n例如：待整理、最愛、同系列');
  if (!name) return;
  try {
    await createGroupFromName(name);
    await loadGroups();
    toast(`已建立分組「${name.trim()}」`);
  } catch (error) { toast(error.message, true); }
};

$('#addTag').onclick = async () => {
  const name = prompt('輸入新標籤名稱\n例如：彩色、短篇、待確認');
  if (!name) return;
  try {
    await api('/api/tags', { method: 'POST', body: JSON.stringify({ name }) });
    await loadTags();
    toast(`已建立標籤「${name.trim()}」`);
  } catch (error) {
    if (error.message.includes('已經存在')) toast('這個標籤已經存在'); else toast(error.message, true);
  }
};

$('#batchGroup').onclick = async () => {
  await loadGroups();
  $('#groupSelectionSummary').textContent = `目前選取 ${state.selected.size} 本漫畫`;
  $('#groupSelect').innerHTML = '<option value="">請選擇…</option>' + state.customGroups.map(group => `<option value="${esc(group.name)}">${esc(group.name)}（${group.comic_count} 本）</option>`).join('');
  $('#newGroupName').value = '';
  $('#groupDialog').showModal();
};

$('#saveBatchGroup').onclick = async () => {
  try {
    const typedGroups = commaValues($('#newGroupName').value);
    const groups = typedGroups.length ? typedGroups : commaValues($('#groupSelect').value);
    if (!groups.length) return toast('請選擇或輸入分組名稱', true);
    if (typedGroups.length) await Promise.all(groups.map(createGroupFromName));
    const result = await api('/api/comics/batch-groups', {
      method: 'PATCH',
      body: JSON.stringify({ comic_ids: [...state.selected], groups })
    });
    $('#groupDialog').close();
    state.selected.clear();
    toast(`已將 ${result.updated_comics} 本漫畫加入 ${groups.length} 個分組`);
    await loadLibrary();
  } catch (error) { toast(error.message, true); }
};

$('#batchTag').onclick = async () => {
  const value = prompt(`替選取的 ${state.selected.size} 本漫畫加入標籤\n可用逗號分隔多個標籤`);
  if (!value) return;
  const tags = commaValues(value);
  if (!tags.length) return;
  try {
    const result = await api('/api/comics/batch-tags', {
      method: 'PATCH', body: JSON.stringify({ comic_ids: [...state.selected], tags })
    });
    state.selected.clear();
    toast(`已替 ${result.updated_comics} 本漫畫加入 ${tags.length} 個標籤`);
    await loadLibrary();
  } catch (error) { toast(error.message, true); }
};

$('#batchAuthor').onclick = async () => {
  const author = prompt(`替選取的 ${state.selected.size} 本漫畫設定作者`);
  if (author === null) return;
  try {
    const result = await api('/api/comics/batch-metadata', {
      method: 'PATCH', body: JSON.stringify({ comic_ids: [...state.selected], author })
    });
    state.selected.clear();
    toast(`已更新 ${result.updated} 本漫畫的作者`);
    await loadLibrary();
  } catch (error) { toast(error.message, true); }
};

function formatModifiedTime(timestamp) {
  if (!timestamp) return '時間不明';
  return new Intl.DateTimeFormat('zh-TW', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false
  }).format(new Date(timestamp * 1000));
}

function duplicateFile(file, ageLabel = '') {
  return `<div class="duplicate-file"><div><strong>${esc(file.name)}</strong><small>${esc(file.path)}</small><small>${file.image_count} 頁 · ${fmt(file.size_bytes)}</small><small class="duplicate-modified">修改時間：${formatModifiedTime(file.modified_at)}${ageLabel ? `<span class="age-label">${ageLabel}</span>` : ''}</small></div><div class="duplicate-file-actions"><button class="ghost" data-review-id="${file.id}">開啟確認</button><button class="danger duplicate-delete" data-delete-duplicate-id="${file.id}">刪除此檔</button></div></div>`;
}

function reviewControls(ids, verdict = '') {
  return `<div class="review-actions" data-review-ids="${ids.join(',')}"><span class="muted">請選擇上方要刪除的檔案；若全部保留，可忽視這一組。</span><button class="ghost" data-verdict="not_duplicate">忽視此組</button></div>`;
}

function renderExactDuplicates(groups) {
  $('#duplicateResults').innerHTML = groups.length ? groups.map((group, index) =>
    `<section class="duplicate-group"><h3>完全相同群組 ${index + 1}<span class="similar-score">SHA-256 相同</span></h3>${group.files.map((file, fileIndex) => duplicateFile(file, fileIndex === 0 ? '最新' : fileIndex === group.files.length - 1 ? '最舊' : '')).join('')}${reviewControls(group.files.map(file => file.id), group.verdict)}<div class="deep-compare-actions"><button class="ghost" data-deep-compare-ids="${group.files.map(file => file.id).join(',')}">深入比較此組圖片內容</button><span class="group-deep-result muted"></span></div></section>`
  ).join('') : '<div class="empty"><h3>沒有找到完全相同的檔案</h3><p>這代表目前沒有整個 ZIP 位元完全一致的副本。</p></div>';
}

function renderSimilarDuplicates(matches) {
  $('#duplicateResults').innerHTML = matches.length ? matches.map((match, index) =>
    `<section class="duplicate-group"><h3>疑似內容相同 ${index + 1}<span class="similar-score">相似度 ${match.score}%</span></h3>${duplicateFile(match.left, '較新')}${duplicateFile(match.right, '較舊')}${reviewControls([match.left.id, match.right.id], match.verdict)}</section>`
  ).join('') : '<div class="empty"><h3>沒有找到高相似候選</h3><p>抽樣圖片與頁數沒有達到提示門檻。</p></div>';
}

$('#hashDuplicates').onclick = () => {
  $('#duplicateStatus').textContent = '準備檢查…';
  $('#duplicateResults').innerHTML = '';
  $('#duplicatesDialog').showModal();
  runExactDuplicateCheck();
};

async function runExactDuplicateCheck() {
  const button = $('#runExact'); button.disabled = true;
  button.textContent = '更新中…';
  $('#duplicateStatus').textContent = '正在計算同大小候選檔的 SHA-256…';
  try {
    const result = await api('/api/duplicates/hash', { method: 'POST' });
    $('#duplicateStatus').textContent = `本次計算 ${result.calculated} 個候選檔，找到 ${result.groups.length} 組完全相同。`;
    renderExactDuplicates(result.groups);
  } catch (error) {
    $('#duplicateStatus').textContent = `檢查失敗：${error.message}`;
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = '重新整理';
  }
}

$('#runExact').onclick = runExactDuplicateCheck;

$('#duplicateResults').onclick = event => {
  const deepCompareButton = event.target.closest('[data-deep-compare-ids]');
  if (deepCompareButton) {
    const group = deepCompareButton.closest('.duplicate-group');
    const resultLabel = group.querySelector('.group-deep-result');
    const comicIds = deepCompareButton.dataset.deepCompareIds.split(',').map(Number);
    deepCompareButton.disabled = true;
    deepCompareButton.textContent = '正在比較此組…';
    resultLabel.textContent = '';
    api('/api/duplicates/similar-selected', {
      method: 'POST', body: JSON.stringify({ comic_ids: comicIds })
    }).then(result => {
      if (result.errors.length) {
        resultLabel.textContent = `有 ${result.errors.length} 本無法分析`;
      } else if (result.matches.length) {
        const lowestScore = Math.min(...result.matches.map(match => match.score));
        resultLabel.textContent = `圖片抽樣完成：${result.matches.length} 組配對符合，最低相似度 ${lowestScore}%`;
      } else {
        resultLabel.textContent = '圖片抽樣完成：此組沒有達到相似門檻';
      }
    }).catch(error => {
      resultLabel.textContent = '比較失敗';
      toast(error.message, true);
    }).finally(() => {
      deepCompareButton.disabled = false;
      deepCompareButton.textContent = '重新比較此組圖片內容';
    });
    return;
  }
  const deleteButton = event.target.closest('[data-delete-duplicate-id]');
  if (deleteButton) {
    const resultRow = deleteButton.closest('.duplicate-file');
    api(`/api/comics/${deleteButton.dataset.deleteDuplicateId}`)
      .then(comic => openDeleteDialog(comic, resultRow))
      .catch(error => toast(error.message, true));
    return;
  }
  const verdictButton = event.target.closest('[data-verdict]');
  if (verdictButton) {
    const controls = verdictButton.closest('[data-review-ids]');
    const group = controls.closest('.duplicate-group');
    const comicIds = controls.dataset.reviewIds.split(',').map(Number);
    api('/api/duplicates/review', { method: 'POST', body: JSON.stringify({ comic_ids: comicIds, verdict: verdictButton.dataset.verdict }) })
      .then(() => {
        if (verdictButton.dataset.verdict === 'not_duplicate') {
          group.remove();
          if (!$('#duplicateResults').querySelector('.duplicate-group')) {
            $('#duplicateResults').innerHTML = '<div class="empty"><h3>沒有待比較項目</h3><p>標記為「不是重複」的候選不會再次出現。</p></div>';
          }
          toast('已忽視此組，所有檔案都會保留，之後不再列入比較');
          return;
        }
      }).catch(error => toast(error.message, true));
    return;
  }
  const button = event.target.closest('[data-review-id]');
  if (!button) return;
  rememberDuplicatePosition();
  $('#duplicatesDialog').close();
  showDetail(button.dataset.reviewId);
};

let webDownloadPreviewData = null;
const webDownloadJobs = new Map();
const webDownloadBatchEntries = new Map();
let webDownloadBatchSequence = 0;
let webDownloadBatchChecking = false;
let webDownloadCompletedNotice = false;
let webDownloadFailedNotice = false;

function updateWebDownloadIndicator() {
  const indicator = $('#webDownloadIndicator');
  if (!indicator) return;
  const active = [...webDownloadJobs.values()].filter(job => !['completed', 'failed', 'cancelled'].includes(job.status));
  const failed = [...webDownloadJobs.values()].some(job => job.status === 'failed');
  $('#retryFailedWebDownloads')?.classList.toggle('hidden', !failed);
  indicator.className = 'web-download-indicator';
  if (active.length) {
    indicator.classList.add('downloading');
    if (active.length === 1) {
      indicator.classList.add('single');
      indicator.textContent = '↓';
      indicator.title = '有 1 本漫畫正在背景下載';
    } else {
      indicator.textContent = String(active.length);
      indicator.title = `有 ${active.length} 本漫畫正在背景下載`;
    }
    indicator.classList.remove('hidden');
    return;
  }
  if (webDownloadFailedNotice) {
    indicator.classList.add('failed');
    indicator.textContent = '!';
    indicator.title = '有下載失敗，點開查看';
    indicator.classList.remove('hidden');
  } else if (webDownloadCompletedNotice) {
    indicator.classList.add('completed');
    indicator.textContent = '✓';
    indicator.title = '背景下載已完成';
    indicator.classList.remove('hidden');
  } else {
    indicator.classList.add('hidden');
    indicator.textContent = '';
  }
}

function isWebDownloadBatchMode() {
  return $('#webDownloadBatchMode').checked;
}

function setWebDownloadControls(busy) {
  if (isWebDownloadBatchMode()) return;
  $('#webDownloadUrl').disabled = busy;
  $('#webDownloadRoot').disabled = busy;
  $('#webDownloadBatchMode').disabled = busy;
  $('#previewWebDownload').disabled = busy;
  $('#startWebDownload').disabled = busy || !webDownloadPreviewData;
}

function resetWebDownloadEntry() {
  webDownloadPreviewData = null;
  $('#webDownloadUrl').value = '';
  $('#webDownloadPreview').classList.add('hidden');
  $('#webDownloadPreview').innerHTML = '';
  $('#startWebDownload').disabled = true;
}

function refreshWebDownloadMode() {
  const batch = isWebDownloadBatchMode();
  $('#webDownloadSingleField').classList.toggle('hidden', batch);
  $('#webDownloadBatchField').classList.toggle('hidden', !batch);
  $('#previewWebDownload').classList.toggle('hidden', batch);
  $('#startWebDownload').classList.toggle('hidden', batch);
  $('#previewWebDownload').textContent = '檢查名稱與頁數';
  $('#startWebDownload').textContent = '開始下載並建立 ZIP';
  webDownloadPreviewData = null;
  $('#webDownloadUrl').value = '';
  $('#startWebDownload').disabled = true;
  if (batch) renderWebDownloadBatchEntries();
  else {
    $('#webDownloadPreview').classList.add('hidden');
    $('#webDownloadPreview').innerHTML = '';
  }
  (batch ? $('#webDownloadUrls') : $('#webDownloadUrl')).focus();
}

function renderWebDownloadBatchEntries() {
  if (!isWebDownloadBatchMode()) return;
  const entries = [...webDownloadBatchEntries.values()];
  if (!entries.length) {
    $('#webDownloadPreview').classList.add('hidden');
    $('#webDownloadPreview').innerHTML = '';
    return;
  }
  const labels = { waiting: '等待檢查', checking: '正在檢查名稱與重複', starting: '檢查通過，正在排入下載', paused: '重複・已暫停', error: '檢查失敗' };
  $('#webDownloadPreview').innerHTML = `<p class="web-download-preview-summary"><strong>已接收 ${entries.length} 筆網址</strong></p><div class="web-download-preview-list">${entries.map(entry => {
    const editable = ['paused', 'error'].includes(entry.status);
    return `<div class="web-download-preview-item ${entry.status}" data-batch-entry="${entry.id}"><strong>${esc(entry.title || labels[entry.status])}</strong><span>${esc(labels[entry.status])}${entry.reason ? `｜${esc(entry.reason)}` : ''}</span>${editable ? `<div class="web-download-entry-edit"><input value="${esc(entry.url)}" aria-label="修改漫畫網址"></div><div class="web-download-entry-actions"><button class="ghost" data-recheck-batch-entry="${entry.id}">修改後重新檢查</button><button class="danger" data-delete-batch-entry="${entry.id}">刪除</button></div>` : `<small>${esc(entry.url)}</small>`}</div>`;
  }).join('')}</div>`;
  $('#webDownloadPreview').classList.remove('hidden');
}

async function populateWebDownloadRoots() {
  const roots = await api('/api/roots');
  const select = $('#webDownloadRoot');
  select.innerHTML = '';
  if (!roots.length) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = '請先新增掃描資料夾';
    select.append(option);
    return;
  }
  roots.forEach(root => {
    const option = document.createElement('option');
    option.value = root.path;
    option.textContent = root.path;
    select.append(option);
  });
}

async function restoreActiveWebDownloads() {
  const jobs = await api('/api/web-download');
  jobs.forEach(job => {
    if (webDownloadJobs.has(job.id)) return;
    webDownloadJobs.set(job.id, job);
    renderWebDownloadJob(job, job);
    if (!['completed', 'failed', 'cancelled'].includes(job.status)) pollWebDownload(job.id);
  });
  updateWebDownloadIndicator();
}

$('#webDownload').onclick = async () => {
  try {
    await Promise.all([populateWebDownloadRoots(), restoreActiveWebDownloads()]);
    webDownloadCompletedNotice = false;
    webDownloadFailedNotice = false;
    updateWebDownloadIndicator();
    if (isWebDownloadBatchMode()) renderWebDownloadBatchEntries();
    else resetWebDownloadEntry();
    setWebDownloadControls(false);
    $('#webDownloadDialog').showModal();
    (isWebDownloadBatchMode() ? $('#webDownloadUrls') : $('#webDownloadUrl')).focus();
  } catch (error) {
    toast(error.message, true);
  }
};

$('#minimizeWebDownload').onclick = () => $('#webDownloadDialog').close();

$('#webDownloadBatchMode').addEventListener('change', refreshWebDownloadMode);

function invalidateWebDownloadPreview() {
  webDownloadPreviewData = null;
  $('#webDownloadPreview').classList.add('hidden');
  $('#startWebDownload').disabled = true;
}

$('#webDownloadUrl').addEventListener('input', invalidateWebDownloadPreview);
$('#webDownloadRoot').addEventListener('change', () => {
  if (!isWebDownloadBatchMode()) {
    invalidateWebDownloadPreview();
    return;
  }
  for (const entry of webDownloadBatchEntries.values()) {
    if (['paused', 'error'].includes(entry.status)) {
      entry.status = 'waiting';
      entry.reason = '';
    }
  }
  renderWebDownloadBatchEntries();
  drainWebDownloadBatchChecks();
});

function webDownloadUrlsFromText(text) {
  return text.split(/[\s,，]+/).map(item => item.trim()).filter(Boolean);
}

function addWebDownloadBatchText(text) {
  const urls = webDownloadUrlsFromText(text);
  if (!urls.length) return;
  if (!$('#webDownloadRoot').value) return toast('請先選擇儲存位置', true);
  for (const url of urls) {
    const id = String(++webDownloadBatchSequence);
    webDownloadBatchEntries.set(id, { id, url, title: '', status: 'waiting', reason: '' });
  }
  $('#webDownloadUrls').value = '';
  renderWebDownloadBatchEntries();
  drainWebDownloadBatchChecks();
}

$('#webDownloadUrls').addEventListener('paste', event => {
  event.preventDefault();
  addWebDownloadBatchText(event.clipboardData.getData('text'));
});

$('#webDownloadUrls').addEventListener('keydown', event => {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  addWebDownloadBatchText(event.currentTarget.value);
});

$('#webDownloadUrls').addEventListener('blur', event => {
  if (event.currentTarget.value.trim()) addWebDownloadBatchText(event.currentTarget.value);
});

async function startCheckedWebDownloadEntry(entry) {
  entry.status = 'starting';
  renderWebDownloadBatchEntries();
  try {
    const job = await api('/api/web-download/start', {
      method: 'POST',
      body: JSON.stringify({ url: entry.url, root_path: $('#webDownloadRoot').value })
    });
    webDownloadJobs.set(job.id, entry);
    renderWebDownloadJob(job, entry);
    webDownloadBatchEntries.delete(entry.id);
    renderWebDownloadBatchEntries();
    pollWebDownload(job.id);
  } catch (error) {
    entry.status = 'error';
    entry.reason = error.message;
    renderWebDownloadBatchEntries();
  }
}

async function drainWebDownloadBatchChecks() {
  if (webDownloadBatchChecking) return;
  webDownloadBatchChecking = true;
  try {
    while (true) {
      const entries = [...webDownloadBatchEntries.values()].filter(entry => entry.status === 'waiting');
      if (!entries.length) break;
      entries.forEach(entry => { entry.status = 'checking'; });
      renderWebDownloadBatchEntries();
      try {
        const result = await api('/api/web-download/batch-preview', {
          method: 'POST',
          body: JSON.stringify({ urls: entries.map(entry => entry.url), root_path: $('#webDownloadRoot').value })
        });
        const starts = [];
        result.items.forEach((item, index) => {
          const entry = entries[index];
          Object.assign(entry, item);
          if (item.status === 'ready') starts.push(startCheckedWebDownloadEntry(entry));
        });
        renderWebDownloadBatchEntries();
        await Promise.all(starts);
      } catch (error) {
        const message = error.message === 'Method Not Allowed'
          ? '背景服務仍是舊版本，請關閉漫畫網頁版後重新啟動'
          : error.message;
        entries.forEach(entry => Object.assign(entry, { status: 'error', reason: message }));
        renderWebDownloadBatchEntries();
      }
    }
  } finally {
    webDownloadBatchChecking = false;
  }
}

$('#webDownloadPreview').addEventListener('click', event => {
  const deleteButton = event.target.closest('[data-delete-batch-entry]');
  if (deleteButton) {
    webDownloadBatchEntries.delete(deleteButton.dataset.deleteBatchEntry);
    renderWebDownloadBatchEntries();
    return;
  }
  const recheckButton = event.target.closest('[data-recheck-batch-entry]');
  if (!recheckButton) return;
  const id = recheckButton.dataset.recheckBatchEntry;
  const entry = webDownloadBatchEntries.get(id);
  const input = recheckButton.closest('[data-batch-entry]')?.querySelector('input');
  if (!entry || !input?.value.trim()) return toast('請輸入漫畫網址', true);
  Object.assign(entry, { url: input.value.trim(), title: '', status: 'waiting', reason: '' });
  renderWebDownloadBatchEntries();
  drainWebDownloadBatchChecks();
});

$('#previewWebDownload').onclick = async () => {
  const batch = false;
  const url = $('#webDownloadUrl').value.trim();
  if (!url) return toast('請先貼上漫畫目錄網址', true);
  if (!$('#webDownloadRoot').value) return toast('請先選擇儲存位置', true);
  const button = $('#previewWebDownload');
  setWebDownloadControls(true);
  button.textContent = '正在檢查…';
  try {
    webDownloadPreviewData = await api('/api/web-download/preview', {
      method: 'POST', body: JSON.stringify({ url })
    });
    const pageNote = webDownloadPreviewData.page_count_adjusted
      ? `（網站標示 ${webDownloadPreviewData.reported_page_count}P，實際原圖清單 ${webDownloadPreviewData.page_count} 頁）`
      : '';
    $('#webDownloadPreview').innerHTML = `<strong>${esc(webDownloadPreviewData.title)}</strong><span>${webDownloadPreviewData.page_count} 頁 ${esc(pageNote)}· 將建立 ${esc(webDownloadPreviewData.archive_name)}</span>`;
    $('#webDownloadPreview').classList.remove('hidden');
  } catch (error) {
    webDownloadPreviewData = null;
    $('#startWebDownload').disabled = true;
    const message = batch && error.message === 'Method Not Allowed'
      ? '背景服務仍是舊版本，請關閉漫畫網頁版後重新啟動'
      : error.message;
    toast(message, true);
  } finally {
    setWebDownloadControls(false);
    button.textContent = batch ? '檢查整批名稱與重複' : '檢查名稱與頁數';
    $('#startWebDownload').disabled = !webDownloadPreviewData || (batch && webDownloadPreviewData.ready_count === 0);
  }
};

function friendlyWebDownloadMessage(message = '') {
  return message.replace(
    '網站回應 HTTP 429',
    '來源網站暫時限制下載（HTTP 429：請求太頻繁），請稍後再重新下載'
  );
}

function renderWebDownloadJob(job, fallback = {}) {
  let card = $(`[data-web-download-job="${job.id}"]`);
  if (!card) {
    card = document.createElement('div');
    card.className = 'web-download-progress';
    card.dataset.webDownloadJob = job.id;
    $('#webDownloadJobs').append(card);
  }
  const total = job.total || fallback.page_count || 1;
  const title = job.title || fallback.title || '等待開始';
  const retryable = ['failed', 'cancelled'].includes(job.status);
  const action = retryable
    ? `<button class="ghost" data-retry-web-download="${job.id}">重新下載整個項目</button><button class="danger" data-delete-web-download="${job.id}">刪除紀錄</button>`
    : `<button class="ghost" data-cancel-web-download="${job.id}">取消下載</button>`;
  card.classList.toggle('failed', retryable);
  const message = friendlyWebDownloadMessage(job.message || '等待開始');
  card.innerHTML = `<div class="progress-heading"><strong>${esc(title)}</strong><span>${job.completed || 0} / ${job.total || fallback.page_count || '—'}</span></div><progress value="${job.completed || 0}" max="${total}"></progress><p class="muted">${esc(message)}</p><div class="job-actions">${action}</div>`;
  $('#webDownloadJobs').classList.remove('hidden');
  updateWebDownloadIndicator();
}

function removeWebDownloadJob(jobId) {
  $(`[data-web-download-job="${jobId}"]`)?.remove();
  webDownloadJobs.delete(jobId);
  if (!$('#webDownloadJobs').children.length) $('#webDownloadJobs').classList.add('hidden');
  updateWebDownloadIndicator();
}

async function pollWebDownload(jobId) {
  const fallback = webDownloadJobs.get(jobId) || {};
  try {
    const job = await api(`/api/web-download/${jobId}`);
    webDownloadJobs.set(jobId, { ...fallback, ...job });
    renderWebDownloadJob(job, fallback);
    if (!['completed', 'failed', 'cancelled'].includes(job.status)) {
      setTimeout(() => pollWebDownload(jobId), 750);
      return;
    }
    if (job.status === 'completed') {
      webDownloadCompletedNotice = true;
      toast(`漫畫下載完成：${job.title}`);
      removeWebDownloadJob(jobId);
      await loadLibrary();
    } else {
      webDownloadFailedNotice = true;
      const card = $(`[data-web-download-job="${jobId}"]`);
      card?.classList.add('failed');
      updateWebDownloadIndicator();
      toast(friendlyWebDownloadMessage(job.message || '下載失敗，已取消整批下載'), true);
    }
  } catch (error) {
    const card = $(`[data-web-download-job="${jobId}"]`);
    card?.classList.add('failed');
    const status = card?.querySelector('p');
    if (status) status.textContent = `無法取得下載狀態：${error.message}`;
    toast(error.message, true);
  }
}

$('#startWebDownload').onclick = async () => {
  if (!webDownloadPreviewData) return toast('請先檢查名稱與頁數', true);
  const rootPath = $('#webDownloadRoot').value;
  if (!rootPath) return toast('請先選擇儲存位置', true);
  const entry = { ...webDownloadPreviewData, url: $('#webDownloadUrl').value.trim() };
  setWebDownloadControls(true);
  try {
    const job = await api('/api/web-download/start', {
      method: 'POST', body: JSON.stringify({ url: entry.url, root_path: rootPath })
    });
    webDownloadJobs.set(job.id, entry);
    renderWebDownloadJob(job, entry);
    pollWebDownload(job.id);
    resetWebDownloadEntry();
  } catch (error) {
    toast(error.message, true);
  } finally {
    setWebDownloadControls(false);
    $('#startWebDownload').disabled = true;
    $('#webDownloadUrl').focus();
  }
};

async function retryWebDownload(jobId) {
  const job = await api(`/api/web-download/${jobId}/retry`, { method: 'POST' });
  webDownloadJobs.set(jobId, job);
  renderWebDownloadJob(job, job);
  pollWebDownload(jobId);
}

$('#retryFailedWebDownloads').onclick = async () => {
  const button = $('#retryFailedWebDownloads');
  button.disabled = true;
  try {
    const jobs = await api('/api/web-download/retry-failed', { method: 'POST' });
    jobs.forEach(job => {
      webDownloadJobs.set(job.id, job);
      renderWebDownloadJob(job, job);
      pollWebDownload(job.id);
    });
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    updateWebDownloadIndicator();
  }
};

$('#webDownloadJobs').onclick = async event => {
  const deleteButton = event.target.closest('[data-delete-web-download]');
  if (deleteButton) {
    const jobId = deleteButton.dataset.deleteWebDownload;
    deleteButton.disabled = true;
    try {
      await api(`/api/web-download/${jobId}`, { method: 'DELETE' });
      removeWebDownloadJob(jobId);
      toast('下載紀錄已刪除');
    } catch (error) {
      deleteButton.disabled = false;
      toast(error.message, true);
    }
    return;
  }
  const retryButton = event.target.closest('[data-retry-web-download]');
  if (retryButton) {
    retryButton.disabled = true;
    try {
      await retryWebDownload(retryButton.dataset.retryWebDownload);
    } catch (error) {
      retryButton.disabled = false;
      toast(error.message, true);
    }
    return;
  }
  const button = event.target.closest('[data-cancel-web-download]');
  if (!button) return;
  const jobId = button.dataset.cancelWebDownload;
  button.disabled = true;
  try {
    await api(`/api/web-download/${jobId}/cancel`, { method: 'POST' });
    const status = button.closest('.web-download-progress')?.querySelector('p');
    if (status) status.textContent = '正在取消下載，等待目前連線結束…';
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
};

Promise.all([loadLibrary(), loadRoots(), restoreActiveWebDownloads()]).catch(error => toast(error.message, true));
