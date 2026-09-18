(() => {
  const baseRenderCards = renderCards;
  renderCards = function () {
    baseRenderCards();
    document.querySelectorAll('#library .card[data-id]').forEach(card => {
      const comic = state.items.find(item => item.id === Number(card.dataset.id));
      const meta = card.querySelector('.meta');
      if (!comic || !meta || meta.querySelector('.file-modified-time')) return;
      const value = new Intl.DateTimeFormat('zh-TW', {
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', hour12: false
      }).format(new Date(comic.modified_at * 1000));
      meta.insertAdjacentHTML('beforeend', `<span class="file-modified-time" title="壓縮檔在 Windows 上的最後修改時間">檔案修改：${esc(value)}</span>`);
    });
  };
  if (state.items.length) renderCards();
})();
