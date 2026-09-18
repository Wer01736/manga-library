(() => {
  const scanButton = document.querySelector('#scan');
  if (!scanButton) return;

  scanButton.insertAdjacentHTML('afterend', `
    <button id="issuesButton" class="issue-nav-button full">
      <span>異常檔案</span><span id="issueCount" class="issue-count">0</span>
    </button>`);

  document.body.insertAdjacentHTML('beforeend', `
    <dialog id="issuesDialog" class="issues-dialog">
      <button class="dialog-close" data-close="issuesDialog">×</button>
      <p class="eyebrow">FILE HEALTH</p>
      <h2>異常檔案檢查</h2>
      <p class="muted">只列出問題，不會自動刪除。完整檢查會實際解壓並解碼每一張圖片，資料量大時需要較長時間。</p>
      <div class="issue-toolbar">
        <select id="issueRoot"><option value="">全部書庫</option></select>
        <button id="refreshIssues" class="ghost">重新整理</button>
        <button id="deepCheck" class="primary">完整檢查圖片</button>
      </div>
      <div id="issueSummary" class="issue-summary"></div>
      <div id="issueList" class="issue-list"></div>
    </dialog>
    <dialog id="issueDeleteDialog" class="issue-delete-dialog">
      <div class="warning-icon">!</div>
      <h2>確認刪除異常檔案？</h2>
      <p>只有按下確認後才會刪除原始檔案，這個動作無法復原。</p>
      <div class="delete-target"><strong id="issueDeleteName"></strong><small id="issueDeletePath"></small></div>
      <div class="dialog-actions"><button id="cancelIssueDelete" class="ghost">取消</button><button id="confirmIssueDelete" class="danger">確認刪除</button></div>
    </dialog>`);

  let issueData = { items: [], total: 0, archive_count: 0, filesystem_count: 0 };
  let pendingIssue = null;

  function issueLabel(item) {
    return item.kind === 'filesystem' ? '檔案系統無法讀取' : '壓縮檔或圖片異常';
  }

  function visibleIssues() {
    const root = document.querySelector('#issueRoot').value;
    if (!root) return issueData.items;
    const prefix = root.replace(/[\\/]+$/, '') + '\\';
    return issueData.items.filter(item => item.path === root || item.path.startsWith(prefix));
  }

  function renderIssues() {
    const items = visibleIssues();
    const filesystemCount = items.filter(item => item.kind === 'filesystem').length;
    const archiveCount = items.length - filesystemCount;
    document.querySelector('#issueSummary').textContent = items.length
      ? `共 ${items.length} 筆：檔案系統 ${filesystemCount} 筆，壓縮檔／圖片 ${archiveCount} 筆`
      : '目前沒有已知異常';
    document.querySelector('#issueList').innerHTML = items.length ? items.map((item, index) => `
      <article class="issue-item">
        <div class="issue-item-head"><span class="issue-kind">${esc(issueLabel(item))}</span><strong>${esc(item.name)}</strong></div>
        <div class="issue-path" title="${esc(item.path)}">${esc(item.path)}</div>
        <div class="issue-error">${esc(item.error)}</div>
        <div class="issue-actions">
          ${item.comic_id ? `<button class="ghost" data-issue-detail="${item.comic_id}">查看紀錄</button>` : ''}
          <button class="danger" data-issue-delete="${index}">刪除檔案</button>
        </div>
      </article>`).join('') : '<div class="issue-empty">✓ 沒有需要處理的異常檔案</div>';
  }

  async function loadIssueRoots() {
    const roots = await api('/api/roots');
    const select = document.querySelector('#issueRoot');
    const current = select.value;
    select.innerHTML = '<option value="">全部書庫</option>' + roots.map(root =>
      `<option value="${esc(root.path)}">${esc(root.path)}</option>`
    ).join('');
    if ([...select.options].some(option => option.value === current)) select.value = current;
  }

  async function loadIssues() {
    issueData = await api('/api/issues');
    const count = document.querySelector('#issueCount');
    count.textContent = issueData.total;
    count.classList.toggle('clear', issueData.total === 0);
    renderIssues();
  }

  document.querySelector('#issuesButton').onclick = async () => {
    try {
      await Promise.all([loadIssueRoots(), loadIssues()]);
      document.querySelector('#issuesDialog').showModal();
    } catch (error) {
      toast(error.message, true);
    }
  };

  document.querySelector('#refreshIssues').onclick = () => loadIssues().catch(error => toast(error.message, true));
  document.querySelector('#issueRoot').onchange = renderIssues;

  document.querySelector('#issueList').onclick = event => {
    const detail = event.target.closest('[data-issue-detail]');
    if (detail) {
      document.querySelector('#issuesDialog').close();
      showDetail(Number(detail.dataset.issueDetail)).catch(error => toast(error.message, true));
      return;
    }
    const remove = event.target.closest('[data-issue-delete]');
    if (!remove) return;
    pendingIssue = visibleIssues()[Number(remove.dataset.issueDelete)];
    document.querySelector('#issueDeleteName').textContent = pendingIssue.name;
    document.querySelector('#issueDeletePath').textContent = pendingIssue.path;
    document.querySelector('#issueDeleteDialog').showModal();
  };

  document.querySelector('#cancelIssueDelete').onclick = () => {
    pendingIssue = null;
    document.querySelector('#issueDeleteDialog').close();
  };

  document.querySelector('#confirmIssueDelete').onclick = async event => {
    if (!pendingIssue) return;
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const url = pendingIssue.comic_id
        ? `/api/comics/${pendingIssue.comic_id}/delete`
        : `/api/issues/${pendingIssue.issue_id}/delete`;
      await api(url, { method: 'POST', body: JSON.stringify({ value: '', confirmation: 'confirm-delete' }) });
      document.querySelector('#issueDeleteDialog').close();
      pendingIssue = null;
      toast('異常檔案已刪除');
      await Promise.all([loadIssues(), loadLibrary(), loadRoots()]);
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  };

  document.querySelector('#deepCheck').onclick = async event => {
    const root = document.querySelector('#issueRoot').value;
    const scope = root || '全部書庫';
    if (!confirm(`即將完整解壓並解碼「${scope}」內的每一張圖片。\n這只會讀取檔案，但可能需要很長時間。要開始嗎？`)) return;
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = '正在逐張檢查…';
    try {
      const result = await api('/api/issues/check', {
        method: 'POST', body: JSON.stringify({ path: root || null })
      });
      toast(`檢查完成：${result.checked} 個檔案，發現 ${result.failed} 個異常`);
      await Promise.all([loadIssues(), loadLibrary(), loadRoots()]);
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = '完整檢查圖片';
    }
  };

  scanButton.onclick = async () => {
    scanButton.disabled = true;
    scanButton.textContent = '掃描中…';
    try {
      const result = await api('/api/scan', { method: 'POST', body: '{}' });
      toast(`掃描完成：找到 ${result.found} 個，異常 ${result.failed} 個，跳過 ${result.skipped} 個`);
      await Promise.all([loadLibrary(), loadIssues()]);
    } catch (error) {
      toast(error.message, true);
    } finally {
      scanButton.disabled = false;
      scanButton.textContent = '↻ 重新掃描書庫';
    }
  };

  loadIssues().catch(() => {});
})();
