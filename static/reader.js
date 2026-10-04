(() => {
  'use strict';

  const $ = selector => document.querySelector(selector);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
  }[char]));
  const state = { comic: null, images: [], chapters: [], page: 0, thumbsOpen: false, chaptersOpen: false };

  const imageUrl = name => `/api/comics/${state.comic.id}/image?name=${encodeURIComponent(name)}`;
  const thumbnailUrl = name => `/api/comics/${state.comic.id}/thumbnail?name=${encodeURIComponent(name)}&width=160`;

  function showFatal(title, message) {
    $('#readerApp').classList.add('hidden');
    $('#readerMessage').classList.remove('hidden');
    $('#messageTitle').textContent = title;
    $('#messageText').textContent = message;
    document.title = title;
  }

  function showPageNotice(message) {
    const notice = $('#pageNotice');
    notice.textContent = message;
    notice.classList.remove('hidden');
  }

  function hidePageNotice() {
    $('#pageNotice').classList.add('hidden');
  }

  function resetZoom() {
    $('#readerStage').classList.remove('zoomed');
    $('#readerStage').scrollTo({ top: 0, left: 0 });
  }

  function updateButtons() {
    $('#previousPage').disabled = state.page <= 0;
    $('#nextPage').disabled = state.page >= state.images.length - 1;
  }

  function renderThumbs() {
    const strip = $('#thumbStrip');
    if (!state.thumbsOpen) {
      strip.replaceChildren();
      return;
    }
    strip.innerHTML = state.images.map((item, index) => {
      const active = index === state.page ? ' active' : '';
      return `<button type="button" class="thumb${active}" data-page="${index}" aria-label="前往第 ${index + 1} 頁"><img loading="lazy" src="${thumbnailUrl(item.name)}" alt=""><span>${index + 1}</span></button>`;
    }).join('');
    requestAnimationFrame(() => strip.querySelector('.active')?.scrollIntoView({ block: 'nearest', inline: 'center' }));
  }

  function renderChapters() {
    const panel = $('#chapterPanel');
    panel.classList.toggle('hidden', !state.chaptersOpen);
    $('#toggleChapters').setAttribute('aria-expanded', String(state.chaptersOpen));
    if (!state.chaptersOpen) return;
    $('#chapterPanelTitle').textContent = `目錄 · 共 ${state.chapters.length} 話`;
    $('#chapterList').innerHTML = state.chapters.map((chapter, index) => {
      const number = chapter.chapter_number || index + 1;
      const label = chapter.name || chapter.title_guess || `第 ${number} 話`;
      return `<a class="chapter-link${chapter.current ? ' current' : ''}" href="/reader.html?comic=${chapter.id}"><span>第 ${number} 話</span><strong>${esc(label)}</strong><small>${chapter.image_count} 頁${chapter.current ? ' · 當前' : ''}</small></a>`;
    }).join('');
    requestAnimationFrame(() => $('#chapterList .current')?.scrollIntoView({ block: 'center' }));
  }

  function setChaptersOpen(open) {
    state.chaptersOpen = open;
    if (open && state.thumbsOpen) $('#toggleThumbs').click();
    renderChapters();
  }

  function renderPage() {
    if (!state.images.length) return;
    state.page = Math.max(0, Math.min(state.page, state.images.length - 1));
    const item = state.images[state.page];
    const image = $('#pageImage');
    hidePageNotice();
    resetZoom();
    image.alt = `${state.comic.name}，第 ${state.page + 1} 頁`;
    image.src = imageUrl(item.name);
    $('#pageName').textContent = item.name;
    $('#pageCounter').textContent = `${state.page + 1} / ${state.images.length}`;
    updateButtons();
    renderThumbs();
  }

  function movePage(delta) {
    const next = state.page + delta;
    if (next < 0 || next >= state.images.length) return;
    state.page = next;
    renderPage();
  }

  async function loadComic() {
    const rawId = new URLSearchParams(window.location.search).get('comic');
    if (!rawId || !/^\d+$/.test(rawId) || Number(rawId) < 1) {
      showFatal('閱讀連結無效', '這個連結沒有指定可閱讀的漫畫。');
      return;
    }

    try {
      const response = await fetch(`http://127.0.0.1:8766/api/comics/${Number(rawId)}`, {
        headers: { Accept: 'application/json' },
        cache: 'no-store'
      });
      if (!response.ok) {
        let detail = '';
        try { detail = (await response.json()).detail || ''; } catch { /* 使用預設訊息 */ }
        if (response.status === 404) throw new Error('找不到這本漫畫，它可能已被刪除。');
        throw new Error(detail || '漫畫資料目前無法讀取。');
      }

      const comic = await response.json();
      if (comic.status && comic.status !== 'available') {
        throw new Error('漫畫檔案目前不存在或無法使用。');
      }
      if (!Array.isArray(comic.images) || comic.images.length === 0) {
        throw new Error('這本漫畫沒有可閱讀的圖片。');
      }

      state.comic = comic;
      state.images = comic.images;
      state.chapters = Array.isArray(comic.chapters) ? comic.chapters : [];
      state.page = 0;
      $('#readerTitle').textContent = comic.title_guess || comic.name;
      $('#readerFile').textContent = comic.name;
      document.title = `${comic.title_guess || comic.name}｜漫畫閱讀`;
      $('#readerMessage').classList.add('hidden');
      $('#readerApp').classList.remove('hidden');
      $('#toggleChapters').classList.toggle('hidden', state.chapters.length < 2);
      renderPage();
    } catch (error) {
      showFatal('無法開啟漫畫', error.message || '漫畫資料目前無法讀取。');
    }
  }

  $('#previousPage').addEventListener('click', () => movePage(-1));
  $('#nextPage').addEventListener('click', () => movePage(1));
  $('#pageImage').addEventListener('click', () => {
    const stage = $('#readerStage');
    const zoomed = stage.classList.toggle('zoomed');
    if (!zoomed) stage.scrollTo({ top: 0, left: 0 });
  });
  $('#pageImage').addEventListener('error', () => {
    showPageNotice('這一頁無法讀取，漫畫內容可能已經變更。請關閉此分頁後重新開啟。');
  });
  $('#pageImage').addEventListener('load', hidePageNotice);
  $('#toggleThumbs').addEventListener('click', event => {
    if (!state.thumbsOpen && state.chaptersOpen) setChaptersOpen(false);
    state.thumbsOpen = !state.thumbsOpen;
    $('#thumbStrip').classList.toggle('hidden', !state.thumbsOpen);
    event.currentTarget.textContent = state.thumbsOpen ? '關閉縮圖' : '縮圖總覽';
    event.currentTarget.setAttribute('aria-expanded', String(state.thumbsOpen));
    renderThumbs();
  });
  $('#toggleChapters').addEventListener('click', () => setChaptersOpen(!state.chaptersOpen));
  $('#closeChapters').addEventListener('click', () => setChaptersOpen(false));
  $('#thumbStrip').addEventListener('click', event => {
    const button = event.target.closest('[data-page]');
    if (!button) return;
    state.page = Number(button.dataset.page);
    renderPage();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      event.preventDefault();
      movePage(-1);
    } else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      event.preventDefault();
      movePage(1);
    } else if (event.key === 'Escape') {
      if ($('#readerStage').classList.contains('zoomed')) resetZoom();
      else if (state.thumbsOpen) $('#toggleThumbs').click();
      else if (state.chaptersOpen) setChaptersOpen(false);
    }
  });

  loadComic();
})();
