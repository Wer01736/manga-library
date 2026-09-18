/* Direct page navigation for large libraries. */
renderLibraryPagination = function () {
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
  pagination.innerHTML = `${state.libraryPage > 1 ? '<button data-library-page="previous">上一頁</button>' : ''}${pageButtons}<form class="pagination-jump" aria-label="跳至指定頁數"><label>跳至 <input type="number" inputmode="numeric" min="1" max="${totalPages}" value="${state.libraryPage}" aria-label="頁數"> / ${totalPages} 頁</label><button type="submit">前往</button></form>${state.libraryPage < totalPages ? '<button class="pagination-next" data-library-page="next">下一頁</button><button data-library-page="last">尾頁</button>' : ''}`;
};

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

$('#libraryPagination').addEventListener('submit', event => {
  const form = event.target.closest('.pagination-jump');
  if (!form) return;
  event.preventDefault();
  const totalPages = Math.max(1, Math.ceil(state.items.length / LIBRARY_PAGE_SIZE));
  const requested = Number(form.querySelector('input').value);
  if (!Number.isInteger(requested) || requested < 1 || requested > totalPages) {
    toast(`請輸入 1 到 ${totalPages} 的頁數`, true);
    form.querySelector('input').focus();
    return;
  }
  state.libraryPage = requested;
  renderCards();
  document.querySelector('main header')?.scrollIntoView({ block: 'start', behavior: 'smooth' });
});
