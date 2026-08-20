document.querySelectorAll('[data-sidebar-toggle]').forEach((button) => {
  button.addEventListener('click', () => document.body.classList.toggle('sidebar-open'));
});

document.querySelectorAll('.flash').forEach((item) => {
  window.setTimeout(() => item.classList.add('fade'), 5000);
});

const scanForm = document.querySelector('[data-scan-form]');
if (scanForm) {
  const button = scanForm.querySelector('[data-scan-button]');
  const progress = document.querySelector('[data-scan-progress]');
  const phase = progress.querySelector('[data-scan-phase]');
  const percent = progress.querySelector('[data-scan-percent]');
  const track = progress.querySelector('[data-scan-track]');
  const bar = progress.querySelector('[data-scan-bar]');
  const message = progress.querySelector('[data-scan-message]');
  const results = progress.querySelector('[data-scan-results]');
  const latestScan = document.querySelector('[data-latest-scan]');
  let pollTimer = null;

  const renderScanState = (state) => {
    const value = Math.max(0, Math.min(Number(state.percent || 0), 100));
    progress.hidden = false;
    phase.textContent = state.phase || '正在巡检';
    percent.textContent = `${value}%`;
    bar.style.width = `${value}%`;
    track.setAttribute('aria-valuenow', String(value));
    message.textContent = state.message || '正在处理频道内容';
    progress.classList.toggle('scan-failed', state.status === 'failed');
    progress.classList.toggle('scan-completed', state.status === 'completed');
    button.disabled = state.status === 'running';
    button.textContent = state.status === 'running' ? '巡检进行中…' : '再次巡检';
    if (state.status === 'completed') {
      results.hidden = false;
      if (state.results_url) results.href = state.results_url;
      if (latestScan && state.finished_at) {
        latestScan.textContent = new Intl.DateTimeFormat('zh-CN', {
          timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
          hour: '2-digit', minute: '2-digit', hour12: false,
        }).format(new Date(state.finished_at)).replaceAll('/', '-');
      }
      window.sessionStorage.removeItem('qqGuardScanStatusUrl');
    }
    if (state.status === 'failed') {
      results.hidden = true;
      window.sessionStorage.removeItem('qqGuardScanStatusUrl');
    }
  };

  const poll = async (statusUrl) => {
    window.clearTimeout(pollTimer);
    try {
      const response = await window.fetch(statusUrl, {
        headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      });
      if (!response.ok) throw new Error('无法读取巡检进度');
      const state = await response.json();
      renderScanState(state);
      if (state.status === 'running') {
        pollTimer = window.setTimeout(() => poll(statusUrl), 1000);
      }
    } catch (error) {
      renderScanState({ status: 'failed', percent: 0, phase: '进度读取失败', message: error.message });
    }
  };

  scanForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    button.disabled = true;
    button.textContent = '启动巡检…';
    results.hidden = true;
    renderScanState({ status: 'running', percent: 2, phase: '准备巡检', message: '正在提交巡检任务' });
    try {
      const response = await window.fetch(scanForm.action, {
        method: 'POST',
        body: new FormData(scanForm),
        headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message || '巡检启动失败');
      window.sessionStorage.setItem('qqGuardScanStatusUrl', payload.status_url);
      poll(payload.status_url);
    } catch (error) {
      renderScanState({ status: 'failed', percent: 0, phase: '无法启动巡检', message: error.message });
    }
  });

  const existingJob = scanForm.dataset.existingJob;
  const savedStatusUrl = window.sessionStorage.getItem('qqGuardScanStatusUrl');
  if (existingJob) {
    poll(`/scan/status/${encodeURIComponent(existingJob)}`);
  } else if (savedStatusUrl) {
    poll(savedStatusUrl);
  }
}

document.querySelectorAll('[data-bulk-form]').forEach((form) => {
  const selectAll = form.querySelector('[data-select-all]');
  const checkboxes = Array.from(form.querySelectorAll('[data-review-checkbox]:not(:disabled)'));
  const count = form.querySelector('[data-selected-count]');
  const submit = form.querySelector('[data-bulk-delete]');
  const moveButton = form.querySelector('[data-bulk-move]');
  const moveTarget = form.querySelector('[data-move-target]');
  const update = () => {
    const selected = checkboxes.filter((checkbox) => checkbox.checked).length;
    count.textContent = String(selected);
    submit.disabled = selected === 0 || selected > 20;
    moveButton.disabled = selected === 0 || selected > 20 || !moveTarget.value;
    selectAll.checked = checkboxes.length > 0 && selected === checkboxes.length;
    selectAll.indeterminate = selected > 0 && selected < checkboxes.length;
  };
  selectAll.addEventListener('change', () => {
    checkboxes.forEach((checkbox) => { checkbox.checked = selectAll.checked; });
    update();
  });
  checkboxes.forEach((checkbox) => checkbox.addEventListener('change', update));
  moveTarget.addEventListener('change', update);
  update();
});

document.querySelectorAll('[data-confirm-action-form]').forEach((form) => {
  form.addEventListener('submit', () => {
    const button = form.querySelector('[data-confirm-submit]');
    button.disabled = true;
    button.textContent = '正在提交，请勿重复点击…';
  });
});
