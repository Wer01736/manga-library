state.selected = new Set();
state.selectionMode = false;
state.customGroups = [];
state.filter.root = '';
state.filter.tag = '';
state.filter.marker = false;
state.tags = [];
state.libraryPage = 1;
state.libraryQueryKey = '';
state.readingPosition = null;
state.markerViewReturn = null;
const LIBRARY_PAGE_SIZE = 54;
let webDownloadCompletedNoticeTimer = null;
const MIN_18COMIC_EXTENSION_VERSION = [0, 7, 2];
const MIN_NHENTAI_EXTENSION_VERSION = [0, 7, 0];
const SOURCE_RESOLVE_TIMEOUT_MS = 210000;
// Remove the obsolete global button if an older cached HTML shell is still open.
$('#runSimilar')?.remove();
let pendingDeleteComic = null;
let pendingDeleteResultRow = null;
let duplicateReviewContext = null;
let detailTags = [];
const READING_OPEN_MODE_KEY = 'comicLibrary.readingOpenMode';
let readingOpenMode = (() => {
  try {
    return localStorage.getItem(READING_OPEN_MODE_KEY) === 'same' ? 'same' : 'new';
  } catch {
    return 'new';
  }
})();
const originalFileAction = fileAction;
const originalStartReader = startReader;

function readingLinkTarget() {
  return readingOpenMode === 'new' ? ' target="_blank"' : '';
}

function comicSourceName(comic) {
  const sourceTag = (comic.tags || []).find(tag => /^來源[：:]/.test(tag));
  return sourceTag ? sourceTag.replace(/^來源[：:]\s*/, '').slice(0, 2) : '';
}

function hasSupportedExtension(version, minimumVersion) {
  const numbers = String(version || '').split('.').map(value => Number(value));
  for (let index = 0; index < minimumVersion.length; index += 1) {
    const actual = numbers[index] || 0;
    const minimum = minimumVersion[index];
    if (actual !== minimum) return actual > minimum;
  }
  return true;
}

function hasSupported18comicExtension(version) {
  return hasSupportedExtension(version, MIN_18COMIC_EXTENSION_VERSION);
}

function visibleComicTags(comic) {
  return (comic.tags || []).filter(tag => !/^來源[：:]/.test(tag));
}

function formatBadges(comic, detail = false) {
  const source = comicSourceName(comic);
  const prefix = detail ? 'detail-' : 'card-';
  const damaged = comic.integrity_status === 'damaged';
  return `<span class="${prefix}file-format">${esc(comic.extension.slice(1).toUpperCase())}</span>${source ? `<span class="${prefix}source-format" title="下載來源：${esc(source)}">${esc(source)}</span>` : ''}${damaged ? `<span class="${prefix}source-format" title="${esc(comic.integrity_error || 'ZIP 結構損壞')}">損壞</span>` : ''}`;
}

function readingModeMenu(comicId) {
  const sameDefault = readingOpenMode === 'same' ? '<span>預設</span>' : '';
  const newDefault = readingOpenMode === 'new' ? '<span>預設</span>' : '';
  return `<div class="read-mode-menu hidden" id="readModeMenu" role="menu"><button type="button" data-read-once="same" role="menuitem">在目前頁面閱讀${sameDefault}</button><a href="/reader.html?comic=${comicId}" target="_blank" rel="noopener" data-read-once="new" role="menuitem">在新分頁閱讀${newDefault}</a></div>`;
}

function updateReadingPositionButton() {
  const button = $('#returnReadingPosition');
  if (!button) return;
  const hasPosition = Boolean(state.readingPosition?.set);
  button.classList.toggle('hidden', !hasPosition);
  if (hasPosition) button.title = '回到上次看到的漫畫位置';
}

async function loadReadingPosition() {
  state.readingPosition = await api('/api/reading-position');
  updateReadingPositionButton();
}

function currentLibraryView() {
  const start = Math.max(0, (state.libraryPage - 1) * LIBRARY_PAGE_SIZE);
  const firstItem = libraryDisplayItems()[start];
  return {
    search: $('#search').value,
    filter: {
      author: state.filter.author || '',
      group: state.filter.group || '',
      tag: state.filter.tag || '',
      root: state.filter.root || '',
      marker: false
    },
    sort: $('#sort').value,
    direction: $('#direction').value,
    page: state.libraryPage,
    anchorId: libraryDisplayCardId(firstItem) || null,
    scrollTop: window.scrollY || 0
  };
}

async function restoreLibraryView(view) {
  const saved = view || {};
  const filter = saved.filter || {};
  state.filter = {
    author: filter.author || '',
    group: filter.group || '',
    tag: filter.tag || '',
    root: filter.root || '',
    marker: false
  };
  $('#search').value = saved.search || '';
  $('#sort').value = saved.sort || 'modified_at';
  $('#direction').value = saved.direction || 'desc';
  state.libraryPage = 1;
  state.libraryQueryKey = '';
  await loadLibrary();
  const displayItems = libraryDisplayItems();
  const anchorIndex = saved.anchorId == null
    ? -1
    : displayItems.findIndex(item => Number(libraryDisplayCardId(item)) === Number(saved.anchorId));
  const pageCount = Math.max(1, Math.ceil(displayItems.length / LIBRARY_PAGE_SIZE));
  state.libraryPage = anchorIndex >= 0
    ? Math.floor(anchorIndex / LIBRARY_PAGE_SIZE) + 1
    : Math.min(Math.max(1, Number(saved.page) || 1), pageCount);
  renderCards();
  requestAnimationFrame(() => {
    const target = anchorIndex >= 0 ? document.querySelector(`[data-id="${CSS.escape(String(saved.anchorId))}"]`) : null;
    if (target) target.scrollIntoView({ block: 'center', behavior: 'smooth' });
    else window.scrollTo({ top: Number(saved.scrollTop) || 0, behavior: 'smooth' });
  });
}

async function setReadingPosition(comicId) {
  const context = currentLibraryView();
  context.targetId = comicId;
  const result = await api('/api/reading-position', {
    method: 'PUT',
    body: JSON.stringify({ comic_id: Number(comicId), context })
  });
  state.readingPosition = result;
  updateReadingPositionButton();
  updateReadingPositionCards(Number(comicId));
}

async function clearReadingPosition() {
  await api('/api/reading-position', { method: 'DELETE' });
  state.readingPosition = { set: false };
  updateReadingPositionButton();
  updateReadingPositionCards(null);
}

function updateReadingPositionCards(comicId) {
  const selectedId = comicId == null ? null : Number(comicId);
  state.items.forEach(item => { item.reading_position = Number(item.id) === selectedId; });
  document.querySelectorAll('#library .card').forEach(card => {
    const marked = Number(card.dataset.id) === selectedId;
    const button = card.querySelector('[data-reading-position]');
    if (button) {
      button.classList.toggle('marked', marked);
      button.setAttribute('aria-label', `${marked ? '清除' : '設為'}上次閱讀位置`);
      button.title = marked ? '清除上次閱讀位置' : '設為上次看到這裡';
    }
  });
}

async function goToReadingPosition() {
  const position = state.readingPosition;
  if (!position?.set) {
    toast('目前還沒有設定上次閱讀位置', true);
    return;
  }
  state.markerViewReturn = null;
  const context = position.context || {};
  state.filter = { ...(context.filter || {}), marker: false };
  $('#search').value = context.search || '';
  $('#sort').value = context.sort || 'modified_at';
  $('#direction').value = context.direction || 'desc';
  state.libraryPage = 1;
  state.libraryQueryKey = '';
  await loadLibrary();
  const displayItems = libraryDisplayItems();
  const index = libraryDisplayIndexForComic(position.comic_id, displayItems);
  if (index < 0) {
    toast('上次閱讀的漫畫目前不存在，請重新設定閱讀位置', true);
    return;
  }
  state.libraryPage = Math.floor(index / LIBRARY_PAGE_SIZE) + 1;
  renderCards();
  requestAnimationFrame(() => {
    const targetId = libraryDisplayCardId(displayItems[index]);
    const target = document.querySelector(`[data-id="${CSS.escape(String(targetId))}"]`);
    target?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    target?.classList.add('reading-position-target');
    setTimeout(() => target?.classList.remove('reading-position-target'), 2200);
  });
}

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
  const chapters = Array.isArray(c.chapters) ? c.chapters : [];
  const chapterDirectory = chapters.length > 1
    ? `<section class="detail-chapter-directory"><h3>${esc(c.series_title || c.group_name || '章節目錄')} · 共 ${chapters.length} 話</h3><div>${chapters.map((chapter, index) => `<a class="${chapter.current ? 'current' : ''}" href="/reader.html?comic=${chapter.id}"${readingLinkTarget()} rel="noopener"><span>第 ${chapter.chapter_number || index + 1} 話</span><strong>${esc(chapter.name)}</strong><small>${chapter.image_count} 頁${chapter.current ? ' · 當前' : ''}</small></a>`).join('')}</div></section>`
    : '';
  $('#detailBody').innerHTML = `<div class="detail-grid"><div class="detail-cover-column"><img class="detail-cover" src="${c.cover_name ? imageUrl(c.id, c.cover_name) : ''}"><div class="detail-author-panel"><div class="detail-author-heading"><span>作者</span><span class="author-source ${sourceClass}">${sourceLabel}</span></div><div class="detail-author-buttons">${authorButtons}</div><button type="button" class="ghost detail-author-edit" id="editDetailAuthor">${authors.length ? '修改作者' : '設定作者'}</button></div></div><div class="detail-info"><p class="eyebrow detail-format-badges">${formatBadges(c, true)}</p><h2>${esc(c.name)}</h2><dl><dt>推測作品</dt><dd>${esc(c.title_guess || '—')}</dd><dt>集數</dt><dd>${c.chapter_number ? `第 ${c.chapter_number} / ${c.chapter_count || chapters.length} 話` : esc(c.volume_guess || '—')}</dd><dt>頁數</dt><dd>${c.image_count} 頁</dd><dt>大小</dt><dd>${fmt(c.size_bytes)}</dd><dt>完整路徑</dt><dd>${esc(c.path)}</dd>${c.error ? `<dt>錯誤</dt><dd>${esc(c.error)}</dd>` : ''}</dl><div class="detail-primary-actions"><div class="read-split"><a class="primary detail-read-link" id="readComic" href="/reader.html?comic=${c.id}"${readingLinkTarget()} rel="noopener">開始閱讀</a><button class="primary read-mode-toggle" id="readModeToggle" type="button" aria-label="其他閱讀方式" aria-expanded="false">▼</button>${readingModeMenu(c.id)}</div><button class="ghost icon-action" id="toggleReadingPosition" aria-label="${c.reading_position ? '清除上次位置' : '設為上次看到這裡'}" title="${c.reading_position ? '清除上次位置' : '設為上次看到這裡'}">🔖</button><button class="ghost icon-action" id="toggleReadingMarker" aria-label="${c.reading_marker ? '移除標記' : '設為標記'}" title="${c.reading_marker ? '移除標記' : '設為標記'}">📍</button><button class="danger" id="deleteFile">刪除</button></div>${chapterDirectory}<details class="detail-settings"><summary>⚙ 設定</summary><div class="detail-fields"><label>作者<input id="authorInput" value="${esc(c.author)}" placeholder="可留空"></label><label>分組（可多個，以逗號分隔）<input id="groupsInput" value="${esc((c.groups || []).join(', '))}" placeholder="例如：最愛, 待整理"></label><label class="wide-field">標籤<div id="tagEditor" class="tag-editor"><div id="tagChips" class="tag-chips"></div><input id="tagChipInput" autocomplete="off" placeholder="輸入標籤後按 Enter"></div><small>按 Enter 或輸入逗號建立標籤；點 × 移除</small></label></div><div class="detail-actions"><button class="primary" id="saveMetadata">儲存設定</button><button class="ghost" id="manageComicImages">調整圖片</button><button class="ghost" id="openFolder">開啟所在資料夾</button><button class="ghost" id="renameFile">重新命名</button><button class="ghost" id="moveFile">移動</button></div></details></div></div>`;
  renderDetailTags();
  $('#deleteFile')?.insertAdjacentHTML('beforebegin', '<button class="ghost" id="addDetailRepair" type="button">加入待補</button>');
  $('#detailDialog').showModal();
};

function commaValues(value) {
  return [...new Set(value.split(/[,，、]+/).map(item => item.trim()).filter(Boolean))];
}

function renderDetailTags() {
  const chips = $('#tagChips');
  if (!chips) return;
  chips.innerHTML = detailTags.map((tag, index) => ({ tag, index })).filter(item => !/^來源[：:]/.test(item.tag)).map(({ tag, index }) => `<span class="editable-tag"><span>${esc(tag)}</span><button type="button" data-remove-detail-tag="${index}" aria-label="移除標籤 ${esc(tag)}">×</button></span>`).join('');
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
  const readLink = event.target.closest('#readComic');
  if (readLink && readingOpenMode === 'same' && event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
    event.preventDefault();
    startReader().catch(error => toast(error.message, true));
    return;
  }
  const menuToggle = event.target.closest('#readModeToggle');
  if (menuToggle) {
    event.preventDefault();
    const menu = $('#readModeMenu');
    const opening = menu.classList.contains('hidden');
    menu.classList.toggle('hidden', !opening);
    menuToggle.setAttribute('aria-expanded', String(opening));
    return;
  }
  const readOnce = event.target.closest('[data-read-once]');
  if (readOnce) {
    $('#readModeMenu')?.classList.add('hidden');
    $('#readModeToggle')?.setAttribute('aria-expanded', 'false');
    if (readOnce.dataset.readOnce === 'same') {
      event.preventDefault();
      startReader().catch(error => toast(error.message, true));
    }
    return;
  }
  const positionButton = event.target.closest('#toggleReadingPosition');
  if (positionButton) {
    event.preventDefault();
    event.stopImmediatePropagation();
    const comicId = state.comic.id;
    const action = state.comic.reading_position ? clearReadingPosition() : setReadingPosition(comicId);
    action.then(async () => {
      state.comic.reading_position = !state.comic.reading_position;
      positionButton.textContent = '🔖';
      positionButton.setAttribute('aria-label', state.comic.reading_position ? '清除上次位置' : '設為上次看到這裡');
      positionButton.title = state.comic.reading_position ? '清除上次位置' : '設為上次看到這裡';
      toast(state.comic.reading_position ? '已記住上次看到這本漫畫的位置' : '已清除上次閱讀位置');
    }).catch(error => toast(error.message, true));
    return;
  }
  const markerButton = event.target.closest('#toggleReadingMarker');
  if (markerButton) {
    event.preventDefault();
    event.stopImmediatePropagation();
    api(`/api/comics/${state.comic.id}/reading-marker`, {
      method: 'PUT',
      body: JSON.stringify({ marked: !state.comic.reading_marker })
    }).then(async () => {
      const marked = !state.comic.reading_marker;
      state.comic.reading_marker = marked;
      markerButton.textContent = '📍';
      markerButton.setAttribute('aria-label', marked ? '移除標記' : '設為標記');
      markerButton.title = marked ? '移除標記' : '設為標記';
      toast(marked ? '已設為標記' : '已移除標記');
      await loadLibrary();
    }).catch(error => toast(error.message, true));
    return;
  }
  const authorSearch = event.target.closest('[data-search-detail-author]');
  if (authorSearch) {
    const author = authorSearch.dataset.searchDetailAuthor;
    $('#detailDialog').close();
    $('#search').value = author;
    state.filter = { author, group: '', tag: '', root: '', marker: false };
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

function updateReadingModeSummary() {
  $('#readingModeSummary').textContent = readingOpenMode === 'same' ? '目前頁面閱讀' : '新分頁閱讀';
}

function showSettingsHome() {
  $('#settingsHome').classList.remove('hidden');
  $('#readingSettingPanel').classList.add('hidden');
  updateReadingModeSummary();
}

$('#openSettings').onclick = () => {
  showSettingsHome();
  $('#settingsDialog').showModal();
};

$('#openReadingSetting').onclick = () => {
  $('#settingsHome').classList.add('hidden');
  $('#readingSettingPanel').classList.remove('hidden');
  const selected = document.querySelector(`input[name="readingOpenMode"][value="${readingOpenMode}"]`);
  if (selected) selected.checked = true;
};

$('#backToSettings').onclick = showSettingsHome;

$('#settingsDialog').addEventListener('cancel', showSettingsHome);

$('#saveReadingSetting').onclick = () => {
  const selected = document.querySelector('input[name="readingOpenMode"]:checked');
  if (!selected) return;
  readingOpenMode = selected.value === 'same' ? 'same' : 'new';
  try { localStorage.setItem(READING_OPEN_MODE_KEY, readingOpenMode); } catch { /* 本次仍套用 */ }
  showSettingsHome();
  toast(readingOpenMode === 'same' ? '預設改為在目前頁面閱讀' : '預設改為在新分頁閱讀');
};

document.addEventListener('click', event => {
  if (event.target.closest('.read-split')) return;
  $('#readModeMenu')?.classList.add('hidden');
  $('#readModeToggle')?.setAttribute('aria-expanded', 'false');
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

// A WNACG 合輯 can contain several physical ZIP/CBZ files.  Keep those
// records separate in the database, but present one library card for the
// source series so the home page represents a work rather than a file.
function libraryCollectionKey(comic) {
  const site = String(comic?.source_site || '').trim();
  const seriesId = String(comic?.source_series_id || '').trim();
  return site && seriesId ? `${site}:${seriesId}` : '';
}

function libraryDisplayItems() {
  const display = [];
  const seriesByKey = new Map();
  for (const comic of state.items || []) {
    const key = libraryCollectionKey(comic);
    if (!key) {
      display.push({ kind: 'comic', comic });
      continue;
    }
    let group = seriesByKey.get(key);
    if (!group) {
      group = { kind: 'series', key, comics: [] };
      seriesByKey.set(key, group);
      display.push(group);
    }
    group.comics.push(comic);
  }
  for (const group of display) {
    if (group.kind !== 'series') continue;
    group.comics.sort((a, b) => {
      const aNumber = Number(a.chapter_number);
      const bNumber = Number(b.chapter_number);
      if (Number.isFinite(aNumber) && Number.isFinite(bNumber) && aNumber !== bNumber) return aNumber - bNumber;
      if (Number.isFinite(aNumber) !== Number.isFinite(bNumber)) return Number.isFinite(aNumber) ? -1 : 1;
      return String(a.name || '').localeCompare(String(b.name || ''), 'zh-Hant');
    });
    group.representative = group.comics[0];
    group.downloadedCount = group.comics.length;
    group.chapterCount = Math.max(0, ...group.comics.map(comic => Number(comic.chapter_count) || 0));
    group.totalSize = group.comics.reduce((sum, comic) => sum + (Number(comic.size_bytes) || 0), 0);
    group.totalPages = group.comics.reduce((sum, comic) => sum + (Number(comic.image_count) || 0), 0);
  }
  return display;
}

function libraryDisplayIndexForComic(comicId, displayItems = libraryDisplayItems()) {
  const id = Number(comicId);
  return displayItems.findIndex(item => item.kind === 'series'
    ? item.comics.some(comic => Number(comic.id) === id)
    : Number(item.comic.id) === id);
}

function libraryDisplayCardId(item) {
  return item?.kind === 'series' ? item.representative?.id : item?.comic?.id;
}

function renderLibraryComicCard(comic) {
  return `<article class="card ${state.selected.has(comic.id) ? 'selected' : ''}" data-id="${comic.id}"><div class="cover"><input class="select-comic" type="checkbox" aria-label="選取 ${esc(comic.name)}" ${state.selected.has(comic.id) ? 'checked' : ''}><button type="button" class="reading-position-toggle ${comic.reading_position ? 'marked' : ''}" data-reading-position="${comic.id}" aria-label="${comic.reading_position ? '清除' : '設為'}上次閱讀位置" title="${comic.reading_position ? '清除上次閱讀位置' : '設為上次看到這裡'}">🔖</button><button type="button" class="reading-marker ${comic.reading_marker ? 'marked' : ''}" data-reading-marker="${comic.id}" aria-label="${comic.reading_marker ? '移除' : '設為'}標記" title="${comic.reading_marker ? '移除標記' : '設為標記'}">📍</button>${comic.cover_name ? `<img loading="lazy" src="${imageUrl(comic.id, comic.cover_name)}">` : ''}</div><div class="card-body"><h3 title="${esc(comic.name)}">${esc(comic.name)}</h3><div class="meta">${comic.author ? `<span class="tag">${esc(comic.author)}</span>` : ''}<span>${fmt(comic.size_bytes)}</span>${comic.reading_marker ? '<span class="marker-label">📍 已標記</span>' : ''}</div><div class="card-path" title="${esc(comic.path)}">${esc(comic.path)}</div><div class="card-tags">${visibleComicTags(comic).slice(0, 6).map(tag => `<span class="card-tag" title="#${esc(tag)}">#${esc(tag)}</span>`).join('')}</div><div class="card-format-badges">${formatBadges(comic)}</div></div></article>`;
}

function renderLibrarySeriesCard(group) {
  const comic = group.representative;
  const title = comic.series_title || comic.group_name || comic.title_guess || comic.name;
  const expected = group.chapterCount || group.downloadedCount;
  const chapterLabel = expected > 0 ? `已下載 ${group.downloadedCount} / ${expected} 話` : `已下載 ${group.downloadedCount} 話`;
  const pageLabel = group.totalPages > 0 ? `${group.totalPages} 頁` : '頁數待掃描';
  const tags = visibleComicTags(comic).filter(tag => !/^合輯$/.test(tag)).slice(0, 5);
  return `<article class="card series-card" data-id="${comic.id}" data-series-key="${esc(group.key)}"><div class="cover"><span class="series-card-label">合輯</span>${comic.cover_name ? `<img loading="lazy" src="${imageUrl(comic.id, comic.cover_name)}">` : ''}</div><div class="card-body"><h3 title="${esc(title)}">${esc(title)}</h3><div class="meta"><span class="tag">${esc(chapterLabel)}</span><span>${fmt(group.totalSize)}</span><span>${pageLabel}</span></div><div class="series-card-note">點入查看已下載的各話${group.chapterCount > group.downloadedCount ? `，共 ${group.chapterCount} 話` : ''}</div><div class="card-path" title="${esc(comic.path)}">${esc(comic.path)}</div><div class="card-tags">${tags.map(tag => `<span class="card-tag" title="#${esc(tag)}">#${esc(tag)}</span>`).join('')}</div><div class="card-format-badges"><span class="card-source-format">${esc(comicSourceName(comic) || 'WN')}</span></div></div></article>`;
}

loadLibrary = async function () {
  const q = new URLSearchParams({
    q: $('#search').value,
    sort: $('#sort').value,
    direction: $('#direction').value,
    author: state.filter.author,
    group: state.filter.group,
    tag: state.filter.tag,
    root: state.filter.root,
    marker: state.filter.marker ? '1' : '0'
  });
  const queryKey = q.toString();
  if (state.libraryQueryKey && state.libraryQueryKey !== queryKey) state.libraryPage = 1;
  state.libraryQueryKey = queryKey;
  const data = await api('/api/comics?' + q);
  state.items = data.items;
  const displayItems = libraryDisplayItems();
  const pageCount = Math.max(1, Math.ceil(displayItems.length / LIBRARY_PAGE_SIZE));
  state.libraryPage = Math.min(Math.max(1, state.libraryPage), pageCount);
  updateReadingPositionButton();
  $('#markerCount').textContent = data.marker_count || 0;
  $('#readingMarkers').classList.toggle('active', state.filter.marker);
  $('#readingMarkers span:first-child').textContent = state.filter.marker ? '← 回到原本位置' : '📍 已標記漫畫';
  $('#summary').textContent = `${displayItems.length} 個項目 · ${data.items.length} 個檔案${state.filter.marker ? ' · 閱讀定位' : state.filter.root ? ' · 指定掃描位置' : ' · 全部位置'}${state.filter.author ? ` · 作者：${state.filter.author}` : ''}${state.filter.group ? ` · 分組：${state.filter.group}` : ''}${state.filter.tag ? ` · 標籤：${state.filter.tag}` : ''}`;
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
  state.filter.marker = false;
  loadLibrary().catch(error => toast(error.message, true));
});

$('#tags').addEventListener('change', event => {
  state.filter.tag = event.target.value;
  state.filter.marker = false;
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
  state.filter.marker = false;
  loadLibrary().catch(error => toast(error.message, true));
}, true);

$('#roots').addEventListener('click', event => {
  const button = event.target.closest('[data-root]');
  if (!button) return;
  state.filter.root = button.dataset.root;
  state.filter.marker = false;
  state.selected.clear();
  loadLibrary().catch(error => toast(error.message, true));
});

$('#clearFilter').onclick = () => {
  if (state.filter.marker && state.markerViewReturn) {
    const saved = state.markerViewReturn;
    state.markerViewReturn = null;
    restoreLibraryView(saved).catch(error => toast(error.message, true));
    return;
  }
  state.filter = { author: '', group: '', tag: '', root: '', marker: false };
  $('#search').value = '';
  state.libraryPage = 1;
  loadLibrary().catch(error => toast(error.message, true));
};

$('#readingMarkers').onclick = () => {
  if (state.filter.marker && state.markerViewReturn) {
    const saved = state.markerViewReturn;
    state.markerViewReturn = null;
    restoreLibraryView(saved).catch(error => toast(error.message, true));
    return;
  }
  state.markerViewReturn = currentLibraryView();
  state.filter = { author: '', group: '', tag: '', root: '', marker: true };
  $('#search').value = '';
  state.libraryQueryKey = '';
  state.libraryPage = 1;
  loadLibrary().catch(error => toast(error.message, true));
};

$('#returnReadingPosition').onclick = () => {
  goToReadingPosition().catch(error => toast(error.message, true));
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
  const displayItems = libraryDisplayItems();
  $('#empty').classList.toggle('hidden', displayItems.length > 0);
  if (state.items.length === 0 && state.filter.marker) {
    $('#empty h2').textContent = '目前沒有閱讀定位';
    $('#empty p').textContent = '在漫畫卡片或作品資訊中按下圖釘，就能把目前看到的這本留下來。';
  } else {
    $('#empty h2').textContent = '書庫目前是空的';
    $('#empty p').textContent = '新增漫畫資料夾後進行掃描，原始檔案不會被移動或改寫。';
  }
  const start = (state.libraryPage - 1) * LIBRARY_PAGE_SIZE;
  const visibleItems = displayItems.slice(start, start + LIBRARY_PAGE_SIZE);
  lib.innerHTML = visibleItems.map(item => item.kind === 'series' ? renderLibrarySeriesCard(item) : renderLibraryComicCard(item.comic)).join('');
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
  const totalPages = Math.ceil(libraryDisplayItems().length / LIBRARY_PAGE_SIZE);
  pagination.classList.toggle('hidden', totalPages <= 1);
  if (totalPages <= 1) {
    pagination.innerHTML = '';
    return;
  }
  const pageButtons = Array.from({ length: totalPages }, (_, index) => index + 1).map(token =>
    `<button class="pagination-page ${token === state.libraryPage ? 'active' : ''}" data-library-page="${token}" aria-label="第 ${token} 頁" ${token === state.libraryPage ? 'aria-current="page"' : ''}>${token}</button>`
  ).join('');
  pagination.innerHTML = `${state.libraryPage > 1 ? '<button data-library-page="previous">上一頁</button>' : ''}${pageButtons}${state.libraryPage < totalPages ? '<button class="pagination-next" data-library-page="next">下一頁</button><button data-library-page="last">尾頁</button>' : ''}`;
}

$('#libraryPagination').onclick = event => {
  const button = event.target.closest('[data-library-page]');
  if (!button) return;
  const totalPages = Math.max(1, Math.ceil(libraryDisplayItems().length / LIBRARY_PAGE_SIZE));
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
  $('#batchBar').classList.toggle('hidden', !state.selectionMode || state.selected.size === 0);
  $('#library').classList.toggle('selection-mode', state.selectionMode);
  $('#selectedCount').textContent = `已選 ${state.selected.size} 本`;
  const toggle = $('#toggleSelection');
  toggle?.classList.toggle('active', state.selectionMode);
  toggle?.setAttribute('aria-pressed', String(state.selectionMode));
  if (toggle) toggle.textContent = state.selectionMode ? '完成多選' : '多選';
}

$('#toggleSelection').onclick = () => {
  state.selectionMode = !state.selectionMode;
  if (!state.selectionMode) state.selected.clear();
  renderCards();
};

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

$('#library').addEventListener('click', event => {
  if (!state.selectionMode || event.target.closest('button,input,a,select')) return;
  const card = event.target.closest('.card');
  if (!card) return;
  if (card.dataset.seriesKey) {
    event.preventDefault();
    event.stopImmediatePropagation();
    toast('合輯請點入後逐話管理，避免只選到其中一話');
    return;
  }
  event.preventDefault();
  event.stopImmediatePropagation();
  const id = Number(card.dataset.id);
  if (state.selected.has(id)) state.selected.delete(id); else state.selected.add(id);
  renderCards();
}, true);

$('#library').addEventListener('click', async event => {
  const positionButton = event.target.closest('[data-reading-position]');
  if (positionButton) {
    event.preventDefault();
    event.stopImmediatePropagation();
    const comicId = Number(positionButton.dataset.readingPosition);
    try {
      if (state.readingPosition?.set && Number(state.readingPosition.comic_id) === comicId) {
        await clearReadingPosition();
        toast('已清除上次閱讀位置');
      } else {
        await setReadingPosition(comicId);
        toast('已記住上次看到這本漫畫的位置');
      }
    } catch (error) {
      toast(error.message, true);
    }
    return;
  }
  const button = event.target.closest('[data-reading-marker]');
  if (!button) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  const comic = state.items.find(item => item.id === Number(button.dataset.readingMarker));
  if (!comic) return;
  try {
    await api(`/api/comics/${comic.id}/reading-marker`, {
      method: 'PUT',
      body: JSON.stringify({ marked: !comic.reading_marker })
    });
    toast(comic.reading_marker ? '已移除閱讀定位' : '已設為閱讀定位');
    await loadLibrary();
  } catch (error) {
    toast(error.message, true);
  }
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
  return groups.map((group, index) =>
    `<section class="duplicate-group"><h3>檔案完全相同 ${index + 1}<span class="similar-score">SHA-256 相同</span></h3>${group.files.map((file, fileIndex) => duplicateFile(file, fileIndex === 0 ? '最新' : fileIndex === group.files.length - 1 ? '最舊' : '')).join('')}${reviewControls(group.files.map(file => file.id), group.verdict)}</section>`
  ).join('');
}

function renderSimilarDuplicates(matches) {
  return matches.map((match, index) =>
    `<section class="duplicate-group"><h3>疑似內容相同 ${index + 1}<span class="similar-score">相似度 ${match.score}%</span></h3>${duplicateFile(match.left, '較新')}${duplicateFile(match.right, '較舊')}${reviewControls([match.left.id, match.right.id], match.verdict)}</section>`
  ).join('');
}

function renderDuplicateResults(groups, similarMatches) {
  const exactMarkup = renderExactDuplicates(groups);
  const similarMarkup = renderSimilarDuplicates(similarMatches);
  $('#duplicateResults').innerHTML = exactMarkup + similarMarkup || '<div class="empty"><h3>沒有找到重複內容</h3><p>目前沒有找到整個檔案相同或圖片內容高度相似的候選。</p></div>';
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
  $('#duplicateStatus').textContent = '正在檢查檔案與漫畫圖片內容…';
  try {
    const result = await api('/api/duplicates/hash', { method: 'POST' });
    const similarMatches = result.similar_matches || [];
    $('#duplicateStatus').textContent = `檢查完成：檔案完全相同 ${result.groups.length} 組，疑似內容相同 ${similarMatches.length} 組。`;
    renderDuplicateResults(result.groups, similarMatches);
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
let webDownloadReconcileTimer = null;
const WEB_DOWNLOAD_BATCH_STORAGE_KEY = 'comicLibrary.webDownloadBatchEntries.v1';

function saveWebDownloadBatchEntries() {
  try {
    const pendingEntries = [...webDownloadBatchEntries.values()].filter(
      item => !item.comic_id && item.duplicate_kind !== 'library'
    );
    localStorage.setItem(WEB_DOWNLOAD_BATCH_STORAGE_KEY, JSON.stringify(pendingEntries));
  } catch { /* 儲存失敗不影響下載 */ }
}

function restoreWebDownloadBatchEntriesFromStorage() {
  try {
    const saved = JSON.parse(localStorage.getItem(WEB_DOWNLOAD_BATCH_STORAGE_KEY) || '[]');
    if (!Array.isArray(saved)) return;
    for (const item of saved) {
      if (!item?.url || item.comic_id || item.duplicate_kind === 'library') continue;
      const id = String(++webDownloadBatchSequence);
      webDownloadBatchEntries.set(id, { ...item, id, status: 'waiting', reason: '' });
    }
  } catch {
    localStorage.removeItem(WEB_DOWNLOAD_BATCH_STORAGE_KEY);
  }
}

restoreWebDownloadBatchEntriesFromStorage();

const pendingBrowserResolutions = new Map();

function is18comicUrl(value) {
  try {
    const url = new URL(value);
    return ['18comic.vip', 'www.18comic.vip'].includes(url.hostname) && /^\/(album|photo)\/\d+/.test(url.pathname);
  } catch {
    return false;
  }
}

function isNhentaiUrl(value) {
  try {
    const url = new URL(value);
    return ['nhentai.net', 'www.nhentai.net'].includes(url.hostname) && /^\/g\/\d+(?:\/\d+)?\/?$/.test(url.pathname);
  } catch {
    return false;
  }
}

function ensureBrowserSourceResolved(url) {
  const source = is18comicUrl(url) ? '18comic' : isNhentaiUrl(url) ? 'nhentai' : '';
  if (!source) return Promise.resolve();
  const sourceId = new URL(url).pathname.match(/\/(?:album|photo|g)\/(\d+)/)?.[1];
  const sourceKey = `${source}:${sourceId}`;
  if (pendingBrowserResolutions.has(sourceKey)) return pendingBrowserResolutions.get(sourceKey);
  const requestId = `comic-source-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const request = (async () => {
    try {
      const cached = await fetch('http://127.0.0.1:8766/api/web-download/preview', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url })
      });
      if (cached.ok) {
        const preview = await cached.json();
        const minimum = source === 'nhentai' ? MIN_NHENTAI_EXTENSION_VERSION : MIN_18COMIC_EXTENSION_VERSION;
        if (hasSupportedExtension(preview.extension_version, minimum)) return preview;
      }
    } catch { /* 交由擴充功能讀取 */ }
    return new Promise((resolve, reject) => {
    const cleanup = () => {
      clearTimeout(ackTimer);
      clearTimeout(resultTimer);
      window.removeEventListener('message', onResult);
    };
    const ackTimer = setTimeout(() => {
      cleanup();
      reject(new Error('漫畫下載助手沒有回應；請在 Edge 擴充功能頁重新載入，接著重新整理本漫畫書庫頁面，再按「修改後重新檢查」'));
    }, 5000);
    const resultTimer = setTimeout(() => {
      cleanup();
      reject(new Error('來源網頁讀取超過 3 分 30 秒；請稍後按「修改後重新檢查」'));
    }, SOURCE_RESOLVE_TIMEOUT_MS);
    function onResult(event) {
      const data = event.data;
      if (event.source !== window || data?.source !== 'comic-download-extension' || data.requestId !== requestId) return;
      if (data.ack) {
        clearTimeout(ackTimer);
        return;
      }
      cleanup();
      if (!data.ok) return reject(new Error(data.error || '無法讀取漫畫來源資料'));
      const minimum = source === 'nhentai' ? MIN_NHENTAI_EXTENSION_VERSION : MIN_18COMIC_EXTENSION_VERSION;
      if (!hasSupportedExtension(data.extension_version, minimum)) {
        return reject(new Error('漫畫下載助手版本過舊；請在 Edge 擴充功能頁重新載入後，再按「修改後重新檢查」'));
      }
      resolve(data);
    }
    window.addEventListener('message', onResult);
    window.postMessage({ source: 'comic-library', type: 'COMIC_SOURCE_RESOLVE_REQUEST', requestId, url }, location.origin);
    });
  })().finally(() => pendingBrowserResolutions.delete(sourceKey));
  pendingBrowserResolutions.set(sourceKey, request);
  return request;
}

function startNhentaiBrowserDelivery(jobId, url) {
  const requestId = `nhentai-delivery-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      window.removeEventListener('message', onResult);
      reject(new Error('漫畫下載助手沒有接到 nhentai 圖片傳送工作'));
    }, 5000);
    function onResult(event) {
      const data = event.data;
      if (event.source !== window || data?.source !== 'comic-download-extension' || data.requestId !== requestId) return;
      if (!data.ack) return;
      clearTimeout(timer);
      window.removeEventListener('message', onResult);
      resolve();
    }
    window.addEventListener('message', onResult);
    window.postMessage({
      source: 'comic-library',
      type: 'COMIC_NHENTAI_DELIVER_REQUEST',
      requestId,
      jobId,
      url
    }, location.origin);
  });
}

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

function showWebDownloadCompletedNotice() {
  webDownloadCompletedNotice = true;
  clearTimeout(webDownloadCompletedNoticeTimer);
  webDownloadCompletedNoticeTimer = setTimeout(() => {
    webDownloadCompletedNotice = false;
    webDownloadCompletedNoticeTimer = null;
    updateWebDownloadIndicator();
  }, 5000);
  updateWebDownloadIndicator();
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

function renderSingleWebDownloadPreview() {
  const data = webDownloadPreviewData;
  if (!data) return;
  const pageNote = data.page_count_adjusted
    ? `（網站標示 ${data.reported_page_count}P，實際原圖清單 ${data.page_count} 頁）`
    : '';
  const tags = Array.isArray(data.selected_tags) ? data.selected_tags : [];
  const tagChips = tags.length
    ? tags.map(tag => `<span class="web-download-tag-chip"><span>${esc(tag)}</span><button type="button" data-remove-web-tag="${esc(tag)}" aria-label="移除標籤 ${esc(tag)}">×</button></span>`).join('')
    : '<small class="muted">目前沒有預設標籤</small>';
  $('#webDownloadPreview').innerHTML = `<strong>${esc(data.title)}</strong><span>${data.page_count} 頁 ${esc(pageNote)} · 將建立 ${esc(data.archive_name)}</span>${data.source_category ? `<span>來源分類：${esc(data.source_category)}</span>` : ''}<div class="web-download-tag-editor"><label>來源標籤（可刪除或新增）</label><div class="web-download-tags">${tagChips}<input id="webDownloadTagInput" type="text" maxlength="80" placeholder="輸入標籤後按 Enter"><button type="button" class="ghost" data-add-web-tag>加入</button></div><small>預設已帶入來源頁標籤，下載前可依需要調整。</small></div>`;
  $('#webDownloadPreview').classList.remove('hidden');
}

function addSingleWebDownloadTag() {
  const input = $('#webDownloadTagInput');
  if (!input || !webDownloadPreviewData) return;
  const name = input.value.trim().replace(/^#/, '');
  if (!name) return;
  if (name.length > 80) return toast('單一標籤不可超過 80 個字元', true);
  const tags = Array.isArray(webDownloadPreviewData.selected_tags)
    ? webDownloadPreviewData.selected_tags : (webDownloadPreviewData.selected_tags = []);
  if (!tags.some(tag => tag.toLowerCase() === name.toLowerCase())) tags.push(name);
  input.value = '';
  renderSingleWebDownloadPreview();
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

function renderWebDownloadBatchItem(entry, inSeries = false) {
  const downloadStatus = entry.download_status || '';
  const activeDownload = ['queued', 'waiting', 'running', 'downloading', 'packing', 'finalizing'].includes(downloadStatus);
  const failedDownload = ['failed', 'cancelled'].includes(downloadStatus);
  const labels = {
    waiting: '等待檢查', checking: '正在檢查名稱與重複', ready: '可以下載',
    starting: '正在排入下載', queued: '等待開始', waiting: '等待開始',
    running: '下載中', downloading: '下載中', packing: '正在建立 ZIP',
    finalizing: '正在加入書庫', paused: '已暫停', error: '檢查失敗',
    failed: '下載失敗', cancelled: '已取消'
  };
  const editable = ['paused', 'error'].includes(entry.status) && !entry.comic_id && !failedDownload;
  const label = entry.duplicate_kind === 'active_job' ? '已在下載佇列'
    : entry.duplicate_kind === 'unindexed_file' ? '檔案待加入書庫'
    : entry.comic_id ? '已在書庫'
    : labels[downloadStatus] || labels[entry.status] || '等待處理';
  const chapterLabel = entry.chapter_number
    ? `第 ${entry.chapter_number} / ${entry.chapter_count || '?'} 話`
    : '';
  const pageLabel = Number(entry.page_count) > 0 ? `${entry.page_count} 頁` : '';
  const archiveLabel = !inSeries && entry.archive_name ? `建立：${entry.archive_name}` : '';
  const detail = [chapterLabel, pageLabel, archiveLabel].filter(Boolean).join(' · ');
  const total = Number(entry.download_total || entry.page_count) || 1;
  const completed = Number(entry.download_completed) || 0;
  const progress = activeDownload || failedDownload
    ? `<div class="web-download-inline-progress"><div><span>${esc(label)}</span><span>${completed} / ${total}</span></div><progress value="${completed}" max="${total}"></progress>${entry.download_message ? `<small>${esc(friendlyWebDownloadMessage(entry.download_message))}</small>` : ''}</div>`
    : `<span>${esc(label)}${entry.reason ? `｜${esc(entry.reason)}` : ''}</span>`;
  const actions = activeDownload
    ? `<div class="web-download-entry-actions"><button class="ghost" data-cancel-series-job="${entry.job_id}">取消下載</button></div>`
    : failedDownload
    ? `<div class="web-download-entry-actions"><button class="ghost" data-retry-series-job="${entry.job_id}">重新下載此話</button><button class="danger" data-delete-series-job="${entry.job_id}" data-entry-id="${entry.id}">移除失敗紀錄</button></div>`
    : entry.status === 'ready'
    ? `<div class="web-download-entry-actions"><button class="primary" data-start-batch-entry="${entry.id}">下載此話</button><button class="ghost" data-delete-batch-entry="${entry.id}">移除</button></div>`
    : entry.comic_id
    ? `<div class="web-download-entry-actions"><a class="ghost" href="/reader.html?comic=${entry.comic_id}" target="_blank" rel="noopener">在書庫閱讀</a></div>`
    : editable
    ? `<div class="web-download-entry-edit"><input value="${esc(entry.url)}" aria-label="修改漫畫網址"></div><div class="web-download-entry-actions"><button class="ghost" data-recheck-batch-entry="${entry.id}">修改後重新檢查</button><button class="danger" data-delete-batch-entry="${entry.id}">刪除</button></div>`
    : inSeries ? '' : `<small class="web-download-source-url">${esc(entry.url)}</small>`;
  return `<div class="web-download-preview-item ${entry.status} ${activeDownload ? 'downloading' : ''}" data-batch-entry="${entry.id}"><strong>${esc(entry.title || label)}</strong>${!inSeries && entry.series_title ? `<small class="web-download-series">合輯：${esc(entry.series_title)}</small>` : ''}${progress}${detail ? `<small class="web-download-chapter-meta">${esc(detail)}</small>` : ''}${actions}</div>`;
}

function renderWebDownloadBatchEntries() {
  if (!isWebDownloadBatchMode()) return;
  const seriesIds = new Set(
    [...webDownloadBatchEntries.values()].map(entry => String(entry.series_id || '')).filter(Boolean)
  );
  for (const seriesId of seriesIds) {
    const chapters = [...webDownloadBatchEntries.values()].filter(
      entry => String(entry.series_id || '') === seriesId
    );
    const hasPendingChapter = chapters.some(entry => !entry.comic_id && entry.duplicate_kind !== 'library');
    if (!hasPendingChapter) {
      for (const chapter of chapters) webDownloadBatchEntries.delete(chapter.id);
    }
  }
  saveWebDownloadBatchEntries();
  const entries = [...webDownloadBatchEntries.values()];
  if (!entries.length) {
    $('#webDownloadPreview').classList.add('hidden');
    $('#webDownloadPreview').innerHTML = '';
    return;
  }
  const seriesGroups = new Map();
  const looseEntries = [];
  for (const entry of entries) {
    if (!entry.series_id) {
      looseEntries.push(entry);
      continue;
    }
    if (!seriesGroups.has(entry.series_id)) seriesGroups.set(entry.series_id, []);
    seriesGroups.get(entry.series_id).push(entry);
  }
  const seriesHtml = [...seriesGroups.entries()].map(([seriesId, chapters]) => {
    chapters.sort((left, right) => (Number(left.chapter_number) || 0) - (Number(right.chapter_number) || 0));
    const readyCount = chapters.filter(entry => entry.status === 'ready').length;
    const title = chapters.find(entry => entry.series_title)?.series_title || `合輯 ${seriesId}`;
    const expectedCount = Math.max(...chapters.map(entry => Number(entry.chapter_count) || 0), chapters.length);
    return `<section class="web-download-series-group" data-series-group="${esc(seriesId)}"><div class="web-download-series-heading"><div><small>合輯目錄</small><strong>${esc(title)}</strong><span>共 ${expectedCount} 話</span></div>${readyCount ? `<button type="button" class="primary" data-start-series="${esc(seriesId)}">下載缺少章節（${readyCount}）</button>` : ''}</div><div class="web-download-preview-list">${chapters.map(entry => renderWebDownloadBatchItem(entry, true)).join('')}</div></section>`;
  }).join('');
  const looseReadyCount = looseEntries.filter(entry => entry.status === 'ready').length;
  const looseHtml = looseEntries.length
    ? `<section class="web-download-loose-group"><div class="web-download-preview-summary"><strong>其他下載項目（${looseEntries.length}）</strong>${looseReadyCount ? `<button type="button" class="primary" data-start-all-batch>下載可下載項目（${looseReadyCount}）</button>` : ''}</div><div class="web-download-preview-list">${looseEntries.map(entry => renderWebDownloadBatchItem(entry, false)).join('')}</div></section>`
    : '';
  $('#webDownloadPreview').innerHTML = `<div class="web-download-series-groups">${seriesHtml}${looseHtml}</div>`;
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
  const serverJobIds = new Set(jobs.map(job => String(job.id)));
  for (const jobId of [...webDownloadJobs.keys()]) {
    if (!serverJobIds.has(String(jobId))) removeWebDownloadJob(jobId);
  }
  for (const job of jobs) {
    const alreadyTracked = webDownloadJobs.has(job.id);
    webDownloadJobs.set(job.id, { ...(webDownloadJobs.get(job.id) || {}), ...job });
    renderWebDownloadJob(job, job);
    if (!alreadyTracked && !['completed', 'failed', 'cancelled'].includes(job.status)) pollWebDownload(job.id);
  }
  updateWebDownloadIndicator();
}

function startWebDownloadReconciliation() {
  if (webDownloadReconcileTimer) return;
  webDownloadReconcileTimer = setInterval(() => {
    restoreActiveWebDownloads().catch(() => { /* 下次同步時再試 */ });
  }, 3000);
}

$('#webDownload').onclick = async () => {
  try {
    await Promise.all([populateWebDownloadRoots(), restoreActiveWebDownloads()]);
    webDownloadCompletedNotice = false;
    webDownloadFailedNotice = false;
    clearTimeout(webDownloadCompletedNoticeTimer);
    webDownloadCompletedNoticeTimer = null;
    updateWebDownloadIndicator();
    if (isWebDownloadBatchMode()) {
      renderWebDownloadBatchEntries();
      drainWebDownloadBatchChecks();
    }
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

function webDownloadUrlsFromFileText(text) {
  const matches = text.match(/https?:\/\/[^\s<>"']+/gi) || [];
  return matches.map(url => url.replace(/[\])}>.,;，。；]+$/g, ''));
}

function addWebDownloadBatchText(text) {
  const urls = [...new Set(webDownloadUrlsFromText(text))];
  if (!urls.length) return { added: 0, skipped: 0 };
  if (!$('#webDownloadRoot').value) return toast('請先選擇儲存位置', true);
  const existingUrls = new Set([
    ...[...webDownloadBatchEntries.values()].map(entry => entry.url),
    ...[...webDownloadJobs.values()].map(job => job.url).filter(Boolean)
  ]);
  let added = 0;
  for (const url of urls) {
    if (existingUrls.has(url)) continue;
    const id = String(++webDownloadBatchSequence);
    webDownloadBatchEntries.set(id, { id, url, title: '', status: 'waiting', reason: '' });
    existingUrls.add(url);
    added += 1;
  }
  $('#webDownloadUrls').value = '';
  renderWebDownloadBatchEntries();
  drainWebDownloadBatchChecks();
  return { added, skipped: urls.length - added };
}

$('#webDownloadFile').addEventListener('change', async event => {
  const input = event.currentTarget;
  const file = input.files?.[0];
  if (!file) return;
  const summary = $('#webDownloadFileSummary');
  if (!$('#webDownloadRoot').value) {
    input.value = '';
    return toast('請先選擇儲存位置', true);
  }
  if (file.size > 5 * 1024 * 1024) {
    input.value = '';
    return toast('文件超過 5 MB，請改用較小的文字文件', true);
  }
  try {
    summary.textContent = `正在讀取 ${file.name}…`;
    const urls = webDownloadUrlsFromFileText(await file.text());
    if (!urls.length) {
      summary.textContent = `${file.name}：沒有找到網址`;
      return toast('文件中沒有找到完整的 http 或 https 網址', true);
    }
    const result = addWebDownloadBatchText(urls.join('\n'));
    summary.textContent = `${file.name}：加入 ${result?.added || 0} 筆${result?.skipped ? `，略過重複 ${result.skipped} 筆` : ''}`;
  } catch (error) {
    summary.textContent = `${file.name}：讀取失敗`;
    toast(`讀取文件失敗：${error.message}`, true);
  } finally {
    input.value = '';
  }
});

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
      body: JSON.stringify({
        url: entry.url,
        root_path: $('#webDownloadRoot').value,
        tags: entry.selected_tags || entry.source_tags || [],
        group_name: entry.series_title || '',
        series_title: entry.series_title || '',
        series_id: entry.series_id || '',
        chapter_id: entry.chapter_id || '',
        chapter_number: Number(entry.chapter_number) || 0,
        chapter_count: Number(entry.chapter_count) || 0,
        delivery_mode: isNhentaiUrl(entry.url) ? 'browser' : 'server'
      })
    });
    if (isNhentaiUrl(entry.url)) await startNhentaiBrowserDelivery(job.id, entry.url);
    entry.job_id = job.id;
    entry.download_status = job.status || 'queued';
    entry.download_completed = Number(job.completed) || 0;
    entry.download_total = Number(job.total || entry.page_count) || 0;
    entry.download_message = job.message || '等待開始';
    webDownloadJobs.set(job.id, entry);
    renderWebDownloadJob(job, entry);
    entry.status = 'queued';
    entry.reason = '已排入背景下載';
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
      const entry = [...webDownloadBatchEntries.values()].find(item => item.status === 'waiting');
      if (!entry) break;
      entry.status = 'checking';
      renderWebDownloadBatchEntries();
      try {
        await ensureBrowserSourceResolved(entry.url);
        const result = await api('/api/web-download/batch-preview', {
          method: 'POST',
          body: JSON.stringify({ urls: [entry.url], root_path: $('#webDownloadRoot').value })
        });
        const items = Array.isArray(result.items) ? result.items : [];
        if (!items.length) throw new Error('背景服務沒有回傳檢查結果');
        if (items.length === 1) {
          Object.assign(entry, items[0]);
          renderWebDownloadBatchEntries();
          continue;
        }
        // A single WNACG directory can expand into any number of chapters.
        // Replace the directory row with the verified chapter rows, then feed
        // them through the same per-archive download path.
        webDownloadBatchEntries.delete(entry.id);
        for (const item of items) {
          const chapterEntry = { ...item, id: String(++webDownloadBatchSequence) };
          webDownloadBatchEntries.set(chapterEntry.id, chapterEntry);
        }
        renderWebDownloadBatchEntries();
      } catch (error) {
        const message = error.message === 'Method Not Allowed'
          ? '背景服務仍是舊版本，請關閉漫畫網頁版後重新啟動'
          : error.message;
        Object.assign(entry, { status: 'error', reason: message });
        renderWebDownloadBatchEntries();
      }
    }
  } finally {
    webDownloadBatchChecking = false;
  }
}

$('#webDownloadPreview').addEventListener('click', event => {
  const startSeriesButton = event.target.closest('[data-start-series]');
  if (startSeriesButton) {
    const seriesId = startSeriesButton.dataset.startSeries;
    const readyEntries = [...webDownloadBatchEntries.values()].filter(
      entry => entry.series_id === seriesId && entry.status === 'ready'
    );
    startSeriesButton.disabled = true;
    Promise.all(readyEntries.map(entry => startCheckedWebDownloadEntry(entry)))
      .finally(() => { if (startSeriesButton.isConnected) startSeriesButton.disabled = false; });
    return;
  }
  const startAllButton = event.target.closest('[data-start-all-batch]');
  if (startAllButton) {
    const readyEntries = [...webDownloadBatchEntries.values()].filter(
      entry => !entry.series_id && entry.status === 'ready'
    );
    startAllButton.disabled = true;
    Promise.all(readyEntries.map(entry => startCheckedWebDownloadEntry(entry)))
      .finally(() => { if (startAllButton.isConnected) startAllButton.disabled = false; });
    return;
  }
  const startButton = event.target.closest('[data-start-batch-entry]');
  if (startButton) {
    const entry = webDownloadBatchEntries.get(startButton.dataset.startBatchEntry);
    if (entry?.status === 'ready') startCheckedWebDownloadEntry(entry);
    return;
  }
  const cancelSeriesButton = event.target.closest('[data-cancel-series-job]');
  if (cancelSeriesButton) {
    const jobId = cancelSeriesButton.dataset.cancelSeriesJob;
    cancelSeriesButton.disabled = true;
    api(`/api/web-download/${jobId}/cancel`, { method: 'POST' })
      .catch(error => toast(error.message, true));
    return;
  }
  const retrySeriesButton = event.target.closest('[data-retry-series-job]');
  if (retrySeriesButton) {
    const jobId = retrySeriesButton.dataset.retrySeriesJob;
    const entry = [...webDownloadBatchEntries.values()].find(item => item.job_id === jobId);
    retrySeriesButton.disabled = true;
    api(`/api/web-download/${jobId}/retry`, { method: 'POST' }).then(job => {
      if (!entry) return;
      Object.assign(entry, {
        status: 'queued', download_status: job.status || 'queued',
        download_completed: 0, download_total: Number(job.total || entry.page_count) || 0,
        download_message: job.message || '等待開始', reason: ''
      });
      webDownloadJobs.set(job.id, { ...entry, ...job });
      renderWebDownloadBatchEntries();
      pollWebDownload(job.id);
    }).catch(error => {
      retrySeriesButton.disabled = false;
      toast(error.message, true);
    });
    return;
  }
  const deleteSeriesButton = event.target.closest('[data-delete-series-job]');
  if (deleteSeriesButton) {
    const jobId = deleteSeriesButton.dataset.deleteSeriesJob;
    api(`/api/web-download/${jobId}`, { method: 'DELETE' }).then(() => {
      webDownloadJobs.delete(jobId);
      webDownloadBatchEntries.delete(deleteSeriesButton.dataset.entryId);
      renderWebDownloadBatchEntries();
      updateWebDownloadIndicator();
    }).catch(error => toast(error.message, true));
    return;
  }
  const removeTagButton = event.target.closest('[data-remove-web-tag]');
  if (removeTagButton) {
    const tag = removeTagButton.dataset.removeWebTag;
    webDownloadPreviewData.selected_tags = (webDownloadPreviewData.selected_tags || []).filter(item => item !== tag);
    renderSingleWebDownloadPreview();
    return;
  }
  if (event.target.closest('[data-add-web-tag]')) {
    addSingleWebDownloadTag();
    return;
  }
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

$('#webDownloadPreview').addEventListener('keydown', event => {
  if (event.key === 'Enter' && event.target.id === 'webDownloadTagInput') {
    event.preventDefault();
    addSingleWebDownloadTag();
  }
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
    await ensureBrowserSourceResolved(url);
    webDownloadPreviewData = await api('/api/web-download/preview', {
      method: 'POST', body: JSON.stringify({ url })
    });
    webDownloadPreviewData.selected_tags = [...(webDownloadPreviewData.source_tags || [])];
    renderSingleWebDownloadPreview();
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

function seriesBatchEntryForJob(job, fallback = {}, create = false) {
  const jobId = String(job.id || fallback.job_id || '');
  let entry = [...webDownloadBatchEntries.values()].find(item => item.job_id === jobId);
  const seriesId = String(job.series_id || fallback.series_id || '');
  const chapterId = String(job.chapter_id || fallback.chapter_id || '');
  if (!entry && seriesId && chapterId) {
    entry = [...webDownloadBatchEntries.values()].find(
      item => String(item.series_id || '') === seriesId && String(item.chapter_id || '') === chapterId
    );
  }
  if (!entry && create && seriesId && chapterId) {
    const id = String(++webDownloadBatchSequence);
    entry = {
      id, url: job.request_url || job.source_url || '', title: job.title || fallback.title || '',
      series_title: job.series_title || job.group_name || fallback.series_title || '',
      series_id: seriesId, chapter_id: chapterId,
      chapter_number: Number(job.chapter_number || fallback.chapter_number) || 0,
      chapter_count: Number(job.chapter_count || fallback.chapter_count) || 0,
      page_count: Number(job.total || fallback.page_count) || 0,
      status: 'queued', reason: ''
    };
    webDownloadBatchEntries.set(id, entry);
  }
  if (entry && jobId) entry.job_id = jobId;
  return entry;
}

function renderWebDownloadJob(job, fallback = {}) {
  const seriesEntry = seriesBatchEntryForJob(job, fallback, true);
  if (seriesEntry) {
    Object.assign(seriesEntry, {
      status: ['failed', 'cancelled'].includes(job.status) ? 'error' : 'queued',
      download_status: job.status || 'queued',
      download_completed: Number(job.completed) || 0,
      download_total: Number(job.total || fallback.page_count || seriesEntry.page_count) || 0,
      download_message: job.message || '等待開始'
    });
    renderWebDownloadBatchEntries();
    updateWebDownloadIndicator();
    return;
  }
  if (job.status === 'completed') {
    $(`[data-web-download-job="${job.id}"]`)?.remove();
    if (!$('#webDownloadJobs').children.length) $('#webDownloadJobs').classList.add('hidden');
    updateWebDownloadIndicator();
    return;
  }
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
  const repairState = job.repair_queue_state || fallback.repair_queue_state || '';
  const repairAction = job.status === 'failed' && !job.archive_created
    ? repairState === 'added' || repairState === 'already_queued'
      ? `<button class="ghost" type="button" disabled>已在待補漫畫</button>`
      : repairState === 'duplicate_library'
      ? `<button class="ghost" type="button" disabled>書庫已有同名漫畫</button>`
      : `<button class="ghost" data-add-repair-web-download="${job.id}">加入待補漫畫</button>`
    : '';
  const action = retryable
    ? `${repairAction}<button class="ghost" data-retry-web-download="${job.id}">重新下載整個項目</button><button class="danger" data-delete-web-download="${job.id}">刪除紀錄</button>`
    : `<button class="ghost" data-cancel-web-download="${job.id}">取消下載</button>`;
  card.classList.toggle('failed', retryable);
  card.classList.remove('completed');
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
    const seriesEntry = seriesBatchEntryForJob(job, fallback, false);
    if (job.status === 'completed') {
      showWebDownloadCompletedNotice();
      toast(`漫畫下載完成：${job.title}`);
      try {
        await loadLibrary();
      } catch (error) {
        toast(`下載已完成，但書庫畫面更新失敗：${error.message}`, true);
      }
      if (seriesEntry) {
        webDownloadBatchEntries.delete(seriesEntry.id);
      }
      removeWebDownloadJob(jobId);
      try { await api(`/api/web-download/${jobId}`, { method: 'DELETE' }); } catch { /* 清除畫面與背景完成紀錄分開處理 */ }
      if (seriesEntry) renderWebDownloadBatchEntries();
      updateWebDownloadIndicator();
    } else {
      webDownloadFailedNotice = true;
      if (seriesEntry) {
        seriesEntry.status = 'error';
        seriesEntry.download_status = job.status;
        seriesEntry.reason = friendlyWebDownloadMessage(job.message || '下載失敗，已取消整批下載');
        renderWebDownloadBatchEntries();
      } else {
        const card = $(`[data-web-download-job="${jobId}"]`);
        card?.classList.add('failed');
      }
      updateWebDownloadIndicator();
      toast(friendlyWebDownloadMessage(job.message || '下載失敗，已取消整批下載'), true);
    }
  } catch (error) {
    try {
      const jobs = await api('/api/web-download');
      if (!jobs.some(job => job.id === jobId)) {
        removeWebDownloadJob(jobId);
        return;
      }
    } catch { /* 保留原本錯誤顯示 */ }
    const seriesEntry = seriesBatchEntryForJob({ id: jobId }, fallback, false);
    if (seriesEntry) {
      seriesEntry.status = 'error';
      seriesEntry.download_status = 'failed';
      seriesEntry.reason = `無法取得下載狀態：${error.message}`;
      renderWebDownloadBatchEntries();
    }
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
      method: 'POST', body: JSON.stringify({
        url: entry.url,
        root_path: rootPath,
        tags: entry.selected_tags || entry.source_tags || [],
        group_name: entry.series_title || '',
        delivery_mode: isNhentaiUrl(entry.url) ? 'browser' : 'server'
      })
    });
    if (isNhentaiUrl(entry.url)) await startNhentaiBrowserDelivery(job.id, entry.url);
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
  removeWebDownloadJob(jobId);
  webDownloadJobs.set(job.id, job);
  renderWebDownloadJob(job, job);
  pollWebDownload(job.id);
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
  const repairButton = event.target.closest('[data-add-repair-web-download]');
  if (repairButton) {
    const jobId = repairButton.dataset.addRepairWebDownload;
    const job = webDownloadJobs.get(jobId);
    if (!job) return toast('找不到這筆下載資料', true);
    repairButton.disabled = true;
    try {
      await repairApi('/api/repair-queue', {
        method: 'POST',
        body: JSON.stringify({
          title: job.title,
          source_url: job.source_url || job.url || '',
          site_key: job.source_category || '',
          origin: 'manual_failed'
        })
      });
      await loadRepairQueue();
      try {
        await api(`/api/web-download/${jobId}`, { method: 'DELETE' });
        removeWebDownloadJob(jobId);
        toast('已移至待補漫畫');
      } catch (deleteError) {
        job.repair_queue_state = 'added';
        job.repair_queue_message = '已手動加入待補漫畫';
        webDownloadJobs.set(jobId, job);
        renderWebDownloadJob(job, job);
        toast(`已加入待補漫畫，但未能清除原下載紀錄：${deleteError.message}`, true);
      }
    } catch (error) {
      repairButton.disabled = false;
      toast(error.message, true);
    }
    return;
  }
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

const repairApi = async (path, options = {}) => {
  const response = await fetch(`http://127.0.0.1:8766${path}`, { headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }, ...options });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || '待補漫畫操作失敗');
  return body;
};
let repairItems = [];
let repairEditing = false;
async function loadRepairQueue() {
  const data = await repairApi('/api/repair-queue');
  repairItems = data.items;
  $('#repairQueue').textContent = `待補漫畫（${data.total}）`;
  $('#repairQueueList').innerHTML = repairItems.length ? repairItems.map(item => repairEditing ? `<div class="repair-item repair-edit"><input type="checkbox" value="${item.id}"><input data-repair-title="${item.id}" value="${esc(item.title)}" aria-label="名稱"><input data-repair-site="${item.id}" value="${esc(item.site_label)}" aria-label="網站"><input data-repair-url="${item.id}" value="${esc(item.source_url)}" placeholder="網址（選填）" aria-label="網址"></div>` : `<div class="repair-item"><span class="repair-site">${esc(item.site_label)}</span><strong title="${esc(item.title)}">${esc(item.title)}</strong>${item.source_url ? `<a href="${esc(item.source_url)}" target="_blank" rel="noopener" title="${esc(item.source_url)}">${esc(item.source_url)}</a>` : ''}</div>`).join('') : '<p class="muted">目前沒有待補漫畫</p>';
  $('#removeRepairQueue').classList.toggle('hidden', !repairEditing);
  $('#saveRepairQueue').classList.toggle('hidden', !repairEditing);
}
$('#repairQueue').onclick = async () => { await loadRepairQueue(); $('#repairQueueDialog').showModal(); };
$('#closeRepairQueue').onclick = () => $('#repairQueueDialog').close();
$('#addRepairQueue').onclick = () => $('#repairQueueForm').classList.toggle('hidden');
$('#editRepairQueue').onclick = async () => { repairEditing = !repairEditing; $('#editRepairQueue').textContent = repairEditing ? '完成編輯' : '編輯'; await loadRepairQueue(); };
$('#repairQueueForm').onsubmit = async event => { event.preventDefault(); try { const site = $('#repairSite').value.trim(); await repairApi('/api/repair-queue', { method: 'POST', body: JSON.stringify({ title: $('#repairTitle').value, site_key: site, site_label: site, source_url: $('#repairUrl').value }) }); event.target.reset(); event.target.classList.add('hidden'); await loadRepairQueue(); } catch (error) { toast(error.message, true); } };
$('#saveRepairQueue').onclick = async () => { try { for (const item of repairItems) { const title = document.querySelector(`[data-repair-title="${item.id}"]`).value; const site = document.querySelector(`[data-repair-site="${item.id}"]`).value.trim(); const url = document.querySelector(`[data-repair-url="${item.id}"]`).value; await repairApi(`/api/repair-queue/${item.id}`, { method: 'POST', body: JSON.stringify({ title, site_key: site, site_label: site, source_url: url }) }); } toast('待補漫畫已儲存'); await loadRepairQueue(); } catch (error) { toast(error.message, true); } };
$('#removeRepairQueue').onclick = async () => { const ids = [...document.querySelectorAll('#repairQueueList input:checked')].map(input => Number(input.value)); if (!ids.length || !confirm('移除所選待補紀錄？')) return; await repairApi('/api/repair-queue', { method: 'DELETE', body: JSON.stringify({ ids }) }); await loadRepairQueue(); };
document.addEventListener('click', async event => { if (!event.target.closest('#addDetailRepair') || !state.comic) return; const comic = state.comic; const site = comicSourceName(comic) === '禁漫' ? '18comic' : comicSourceName(comic) === '紳士' ? 'wnacg' : 'unknown'; const sourceUrl = comic.source_url || ''; if (!confirm(`加入待補漫畫？\n${comic.name}`)) return; try { await repairApi('/api/repair-queue', { method: 'POST', body: JSON.stringify({ title: comic.name, site_key: site, source_url: sourceUrl }) }); await loadRepairQueue(); toast('已加入待補漫畫'); } catch (error) { toast(error.message, true); } });
Promise.all([loadLibrary(), loadRoots(), loadReadingPosition(), restoreActiveWebDownloads(), loadRepairQueue()])
  .then(startWebDownloadReconciliation)
  .catch(error => toast(error.message, true));
