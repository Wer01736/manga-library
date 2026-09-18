(() => {
  const openButton = document.querySelector('#hashDuplicates');
  const runButton = document.querySelector('#runExact');
  const dialog = document.querySelector('#duplicatesDialog');
  const actions = dialog?.querySelector('.duplicate-actions');
  if (!openButton || !runButton || !dialog || !actions) return;

  openButton.insertAdjacentHTML('beforeend', '<span id="duplicateNotice" class="duplicate-notice hidden">0</span>');
  actions.insertAdjacentHTML('beforeend', '<button id="backgroundDuplicates" class="ghost hidden">放到背景執行</button>');

  const notice = document.querySelector('#duplicateNotice');
  const backgroundButton = document.querySelector('#backgroundDuplicates');
  let currentJob = null;
  let pollTimer = null;
  let completionAnnouncedFor = '';

  function resultCount(job) {
    return job?.result?.groups?.length || 0;
  }

  function showIndicator(job) {
    openButton.classList.remove('duplicate-running', 'duplicate-ready', 'duplicate-failed');
    notice.classList.add('hidden');
    if (!job || job.status === 'idle') return;
    if (job.status === 'queued' || job.status === 'running') {
      openButton.classList.add('duplicate-running');
      notice.textContent = '…';
      notice.classList.remove('hidden');
      return;
    }
    if (!job.acknowledged && job.status === 'completed') {
      openButton.classList.add('duplicate-ready');
      notice.textContent = String(resultCount(job));
      notice.classList.remove('hidden');
    } else if (!job.acknowledged && job.status === 'failed') {
      openButton.classList.add('duplicate-failed');
      notice.textContent = '!';
      notice.classList.remove('hidden');
    }
  }

  function renderJob(job) {
    currentJob = job;
    showIndicator(job);
    const running = ['queued', 'running'].includes(job.status);
    runButton.disabled = running;
    runButton.textContent = running ? '背景檢查中…' : '重新檢查';
    backgroundButton.classList.toggle('hidden', !running);
    if (running) {
      document.querySelector('#duplicateStatus').textContent = '正在背景比對檔案內容。你可以按「放到背景執行」後繼續使用書庫。';
      return;
    }
    if (job.status === 'completed') {
      const result = job.result || { calculated: 0, groups: [] };
      document.querySelector('#duplicateStatus').textContent = `檢查完成：新計算 ${result.calculated} 個檔案，找到 ${result.groups.length} 組重複內容。`;
      renderExactDuplicates(result.groups);
      return;
    }
    if (job.status === 'failed') {
      document.querySelector('#duplicateStatus').textContent = `檢查失敗：${job.error || job.message}`;
    }
  }

  async function acknowledge(job) {
    if (!job?.id || job.acknowledged) return;
    currentJob = await api(`/api/duplicates/jobs/${job.id}/acknowledge`, { method: 'POST' });
    showIndicator(currentJob);
  }

  async function pollJob(jobId) {
    clearTimeout(pollTimer);
    try {
      const job = await api(`/api/duplicates/jobs/${jobId}`);
      renderJob(job);
      if (['queued', 'running'].includes(job.status)) {
        pollTimer = setTimeout(() => pollJob(jobId), 1000);
        return;
      }
      if (completionAnnouncedFor !== job.id) {
        completionAnnouncedFor = job.id;
        if (job.status === 'completed') toast(`背景重複檢查完成：找到 ${resultCount(job)} 組，請點紅色通知查看`);
        else toast(job.error || '背景重複檢查失敗', true);
      }
      if (dialog.open) await acknowledge(job);
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function startJob() {
    document.querySelector('#duplicateResults').innerHTML = '';
    document.querySelector('#duplicateStatus').textContent = '正在建立背景檢查…';
    const job = await api('/api/duplicates/jobs', { method: 'POST', body: '{}' });
    renderJob(job);
    pollJob(job.id);
  }

  openButton.onclick = async () => {
    try {
      const latest = await api('/api/duplicates/jobs/latest');
      dialog.showModal();
      if (latest.status === 'idle') {
        await startJob();
        return;
      }
      renderJob(latest);
      if (['queued', 'running'].includes(latest.status)) pollJob(latest.id);
      else await acknowledge(latest);
    } catch (error) {
      toast(error.message, true);
    }
  };

  runButton.onclick = () => startJob().catch(error => toast(error.message, true));
  backgroundButton.onclick = () => {
    dialog.close();
    toast('重複檢查會繼續在背景執行，完成後按鈕會變紅通知你');
  };

  api('/api/duplicates/jobs/latest').then(job => {
    currentJob = job;
    showIndicator(job);
    if (['queued', 'running'].includes(job.status)) pollJob(job.id);
  }).catch(() => {});
})();
