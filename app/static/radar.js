(() => {
  const publicReadonly = document.body.dataset.publicReadonly === 'true';
  const publicCloud = document.body.dataset.publicCloud === 'true';
  const refreshButton = document.querySelector('#refreshButton');
  const refreshLabel = document.querySelector('#refreshLabel');
  const toast = document.querySelector('#toast');
  const dialog = document.querySelector('#wechatDialog');
  const saveWechatButton = document.querySelector('#saveWechatButton');
  const wechatUrls = document.querySelector('#wechatUrls');
  const wechatMessage = document.querySelector('#wechatMessage');
  const refreshAttempt = document.querySelector('#refreshAttempt');
  const refreshAttemptText = document.querySelector('#refreshAttemptText');
  const wechatLibraryButtonLabel = document.querySelector('#wechatLibraryButtonLabel');
  const wechatPoolPill = document.querySelector('#wechatPoolPill');
  const wechatPoolSummary = document.querySelector('#wechatPoolSummary');
  const wechatDiscoverySummary = document.querySelector('#wechatDiscoverySummary');
  const wechatDiscoveredTotal = document.querySelector('#wechatDiscoveredTotal');
  const wechatDiscoveredAccounts = document.querySelector('#wechatDiscoveredAccounts');
  const wechatDiscoveredWindow = document.querySelector('#wechatDiscoveredWindow');
  const wechatLastDiscovery = document.querySelector('#wechatLastDiscovery');
  const wechatStatTotal = document.querySelector('#wechatStatTotal');
  const wechatStatWindow = document.querySelector('#wechatStatWindow');
  const wechatStatPending = document.querySelector('#wechatStatPending');
  const wechatStatFailed = document.querySelector('#wechatStatFailed');
  const refreshAfterWechatButton = document.querySelector('#refreshAfterWechatButton');
  const wechatImportFile = document.querySelector('#wechatImportFile');
  const previewWechatImportButton = document.querySelector('#previewWechatImportButton');
  const confirmWechatImportButton = document.querySelector('#confirmWechatImportButton');
  const wechatImportPreview = document.querySelector('#wechatImportPreview');
  const wechatImportPreviewSummary = document.querySelector('#wechatImportPreviewSummary');
  const wechatImportIssues = document.querySelector('#wechatImportIssues');
  const wechatPoolRows = document.querySelector('#wechatPoolRows');
  const wechatPoolRange = document.querySelector('#wechatPoolRange');
  const wechatPoolPrev = document.querySelector('#wechatPoolPrev');
  const wechatPoolNext = document.querySelector('#wechatPoolNext');
  let toastTimer;
  let wechatImportFingerprint = '';
  const wechatPoolState = { scope: 'all', limit: 50, offset: 0, total: 0 };
  const wechatStatsState = {
    total: 0,
    inWindow: 0,
    ready: 0,
    pending: 0,
    failed: 0,
    readyInWindow: 0,
    pendingInWindow: 0,
    failedInWindow: 0,
    discoveredTotal: 0,
    discoveredAccounts: 0,
    discoveredInWindow: 0,
    lastDiscoveryAt: '',
  };

  function showToast(message, isError = false) {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.toggle('error', isError);
    toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('show'), 5200);
  }

  function setRefreshing(active) {
    if (!refreshButton) return;
    refreshButton.disabled = publicReadonly || active;
    refreshButton.classList.toggle('is-loading', active);
    refreshLabel.textContent = active ? '正在寻找本周变化…' : '更新近 7 天';
  }

  function showRefreshAttempt(data = {}, isError = false, fallback = '') {
    if (!refreshAttempt || !refreshAttemptText) return;
    const attempt = data.last_attempt || data;
    const message = attempt.message || data.message || fallback || '刷新状态待确认';
    const detail = attempt.error_detail || data.error_detail || '';
    const prefix = attempt.run_state === 'running' ? '当前刷新' : '最近一次刷新';
    refreshAttemptText.textContent = `${prefix}：${message}${detail ? `｜${detail}` : ''}`;
    refreshAttempt.hidden = false;
    refreshAttempt.classList.toggle('state-error', isError);
    refreshAttempt.classList.toggle('state-ok', !isError);
  }

  async function refreshReport() {
    setRefreshing(true);
    showRefreshAttempt({ run_state: 'running', message: '正在检索近 7 天项目变化。' });
    showToast('正在检索、去重并筛选近 7 天的核心变化，请稍候。');
    try {
      const response = await fetch('/api/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.ok) {
        const reason = `${data.message || '更新失败'}${data.error_detail ? `｜${data.error_detail}` : ''}`;
        showRefreshAttempt(data, true, reason);
        const error = new Error(reason);
        error.attemptShown = true;
        throw error;
      }
      showRefreshAttempt(data, false);
      showToast(data.message || `更新完成：本期找到 ${data.candidate_count} 个可看项目。`);
      window.setTimeout(() => window.location.reload(), 650);
    } catch (error) {
      if (!error.attemptShown) {
        showRefreshAttempt({ message: error.message }, true);
      }
      showToast(`更新未完成：${error.message}`, true);
      setRefreshing(false);
    }
  }

  async function pollInitialRefresh() {
    if (document.body.dataset.refreshing !== 'true') return;
    setRefreshing(true);
    const started = Date.now();
    const timer = window.setInterval(async () => {
      try {
        const response = await fetch('/health', { cache: 'no-store' });
        const data = await response.json();
        if (!data.refreshing) {
          window.clearInterval(timer);
          const attempt = data.last_attempt || {};
          if (attempt.ok === false || data.last_error) {
            showRefreshAttempt(attempt, true, data.last_error);
            showToast(`首次检索未完成：${attempt.message || data.last_error}`, true);
            setRefreshing(false);
          } else {
            window.location.reload();
          }
        } else if (Date.now() - started > 90000) {
          window.clearInterval(timer);
          setRefreshing(false);
          showRefreshAttempt(
            { run_state: 'running', message: '检索仍在后台运行，可稍后刷新页面查看。' },
            false,
          );
          showToast('首次检索仍在运行，可稍后刷新页面查看。', true);
        }
      } catch (_) {
        // 本地服务刚启动时可能短暂不可用，下一轮继续检查。
      }
    }, 1800);
  }

  function setWechatMessage(message = '', isError = false) {
    if (!wechatMessage) return;
    wechatMessage.textContent = message;
    wechatMessage.classList.toggle('error', isError);
  }

  function integerFrom(source, keys, fallback = 0) {
    for (const key of keys) {
      const value = Number(source?.[key]);
      if (Number.isFinite(value) && value >= 0) return Math.trunc(value);
    }
    return fallback;
  }

  function extractPoolStats(data = {}) {
    return data.pool_stats || data.stats || {};
  }

  function readableTimestamp(value) {
    const text = String(value || '').trim();
    if (!text) return '';
    const parsed = new Date(text);
    if (Number.isNaN(parsed.getTime())) return text;
    return new Intl.DateTimeFormat('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(parsed);
  }

  function updateWechatStats(data = {}) {
    const stats = extractPoolStats(data);
    const total = integerFrom(stats, ['total', 'pool_total'], wechatStatsState.total);
    const inWindow = integerFrom(stats, ['in_window', 'window'], wechatStatsState.inWindow);
    const ready = integerFrom(stats, ['ready', 'ready_count'], wechatStatsState.ready);
    const pending = integerFrom(stats, ['pending', 'unknown_date'], wechatStatsState.pending);
    const failed = integerFrom(stats, ['failed', 'failed_count'], wechatStatsState.failed);
    const readyInWindow = integerFrom(stats, ['ready_in_window'], wechatStatsState.readyInWindow || Math.min(ready, inWindow));
    const pendingInWindow = integerFrom(stats, ['pending_in_window'], wechatStatsState.pendingInWindow);
    const failedInWindow = integerFrom(stats, ['failed_in_window'], wechatStatsState.failedInWindow);
    const discoveredTotal = integerFrom(stats, ['discovered_total'], wechatStatsState.discoveredTotal);
    const discoveredAccounts = integerFrom(stats, ['discovered_accounts'], wechatStatsState.discoveredAccounts);
    const discoveredInWindow = integerFrom(stats, ['discovered_in_window'], wechatStatsState.discoveredInWindow);
    const lastDiscoveryAt = Object.prototype.hasOwnProperty.call(stats, 'last_discovery_at')
      ? String(stats.last_discovery_at || '').trim()
      : wechatStatsState.lastDiscoveryAt;
    Object.assign(wechatStatsState, {
      total,
      inWindow,
      ready,
      pending,
      failed,
      readyInWindow,
      pendingInWindow,
      failedInWindow,
      discoveredTotal,
      discoveredAccounts,
      discoveredInWindow,
      lastDiscoveryAt,
    });
    if (wechatStatTotal) wechatStatTotal.textContent = String(total);
    if (wechatStatWindow) wechatStatWindow.textContent = String(inWindow);
    if (wechatStatPending) wechatStatPending.textContent = String(pending);
    if (wechatStatFailed) wechatStatFailed.textContent = String(failed);
    if (wechatDiscoveredTotal) wechatDiscoveredTotal.textContent = String(discoveredTotal);
    if (wechatDiscoveredAccounts) wechatDiscoveredAccounts.textContent = String(discoveredAccounts);
    if (wechatDiscoveredWindow) wechatDiscoveredWindow.textContent = String(discoveredInWindow);
    if (wechatLastDiscovery) {
      wechatLastDiscovery.textContent = lastDiscoveryAt ? `最近拓源：${readableTimestamp(lastDiscoveryAt)}` : '尚未进行主动拓源';
    }
    if (wechatDiscoverySummary) {
      wechatDiscoverySummary.textContent = discoveredTotal
        ? `全网发现 ${discoveredTotal} 条 · ${discoveredAccounts} 个公众号 · 本周 ${discoveredInWindow} 条`
        : '全网发现：尚无线索';
    }
    if (wechatLibraryButtonLabel) {
      wechatLibraryButtonLabel.textContent = total ? `公众号文章库 · ${total}` : '公众号文章库';
    }
    wechatPoolPill?.classList.toggle('muted', total === 0);
    if (wechatPoolSummary) {
      if (!total) {
        wechatPoolSummary.textContent = '公众号文章库：尚未加入文章';
      } else {
        const needsAttention = pendingInWindow + failedInWindow || Math.max(0, inWindow - readyInWindow);
        wechatPoolSummary.textContent = `公众号文章：本周 ${inWindow} 篇（${readyInWindow} 已读取 / ${needsAttention} 待处理）`;
      }
    }
    return stats;
  }

  async function readApiResponse(response, fallbackMessage) {
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      const error = new Error(data.message || fallbackMessage);
      error.payload = data;
      throw error;
    }
    return data;
  }

  function activateWechatTab(name) {
    document.querySelectorAll('[data-wechat-tab]').forEach((button) => {
      const active = button.dataset.wechatTab === name;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', active ? 'true' : 'false');
      button.tabIndex = active ? 0 : -1;
    });
    document.querySelectorAll('[data-wechat-panel]').forEach((panel) => {
      panel.hidden = panel.dataset.wechatPanel !== name;
    });
    setWechatMessage('');
    if (name === 'library') loadWechatPool();
  }

  function poolRowValue(row, keys, fallback = '') {
    for (const key of keys) {
      const value = row?.[key];
      if (value !== undefined && value !== null && String(value).trim()) return String(value).trim();
    }
    return fallback;
  }

  function appendCell(rowElement, value, className = '') {
    const cell = document.createElement('td');
    cell.textContent = value;
    if (className) cell.className = className;
    rowElement.appendChild(cell);
    return cell;
  }

  function statusLabel(status) {
    const labels = {
      ready: '已读取',
      pending: '待读取',
      failed: '读取失败',
      invalid: '无法识别',
      discovered: '待取证',
    };
    return labels[status] || status || '状态待确认';
  }

  function sourceKindLabel(kind) {
    const labels = { manual: '手工加入', import: '历史文件', exporter: '历史文件', discovery: '全网发现' };
    return labels[kind] || kind || '来源待确认';
  }

  async function removeWechatArticle(articleId) {
    if (!articleId || !window.confirm('仅将这篇文章移出本地文章库，不影响公众号原文。确定继续吗？')) return;
    try {
      const response = await fetch(`/api/wechat/pool/${encodeURIComponent(articleId)}`, { method: 'DELETE' });
      const data = await readApiResponse(response, '移出文章库失败');
      updateWechatStats(data);
      setWechatMessage(data.message || '已移出文章库。');
      await loadWechatPool();
    } catch (error) {
      setWechatMessage(error.message, true);
    }
  }

  function renderWechatPoolRows(rows, total) {
    if (!wechatPoolRows) return;
    wechatPoolRows.replaceChildren();
    if (!rows.length) {
      const row = document.createElement('tr');
      const cell = appendCell(row, '当前筛选下没有文章。', 'pool-empty');
      cell.colSpan = 6;
      wechatPoolRows.appendChild(row);
    } else {
      rows.forEach((item) => {
        const row = document.createElement('tr');
        const titleCell = document.createElement('td');
        const title = poolRowValue(item, ['title'], '标题待读取');
        const url = poolRowValue(item, ['url', 'source_url']);
        if (/^https:\/\/mp\.weixin\.qq\.com\//i.test(url)) {
          const link = document.createElement('a');
          link.href = url;
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
          link.textContent = title;
          titleCell.appendChild(link);
        } else {
          titleCell.textContent = title;
        }
        row.appendChild(titleCell);
        appendCell(row, poolRowValue(item, ['account_name', 'publisher'], '待确认'));
        appendCell(row, poolRowValue(item, ['published_at', 'publish_time'], '待读取'));
        const sourceKind = poolRowValue(item, ['source_kind', 'source']);
        const sourceCell = appendCell(row, sourceKindLabel(sourceKind));
        if (sourceKind === 'discovery') {
          const provider = poolRowValue(item, ['discovery_provider', 'provider'], '发现渠道待确认');
          const query = poolRowValue(item, ['discovery_query'], '发现词待确认');
          const detail = document.createElement('div');
          detail.className = 'pool-source-detail';
          detail.textContent = `${provider} · ${query}`;
          sourceCell.appendChild(detail);
        }
        const rawStatus = poolRowValue(item, ['status'], 'pending');
        const status = ['ready', 'pending', 'failed', 'invalid', 'discovered'].includes(rawStatus) ? rawStatus : 'pending';
        appendCell(row, statusLabel(rawStatus), `pool-status status-${status}`);
        const actionCell = document.createElement('td');
        if (!publicCloud) {
          const removeButton = document.createElement('button');
          removeButton.type = 'button';
          removeButton.className = 'pool-remove';
          removeButton.textContent = '移出';
          removeButton.addEventListener('click', () => removeWechatArticle(poolRowValue(item, ['article_id', 'id'])));
          actionCell.appendChild(removeButton);
        }
        row.appendChild(actionCell);
        wechatPoolRows.appendChild(row);
      });
    }

    const start = rows.length ? wechatPoolState.offset + 1 : 0;
    const end = wechatPoolState.offset + rows.length;
    if (wechatPoolRange) wechatPoolRange.textContent = total ? `${start}–${end} / ${total}` : '0 篇';
    if (wechatPoolPrev) wechatPoolPrev.disabled = wechatPoolState.offset === 0;
    if (wechatPoolNext) {
      wechatPoolNext.disabled = rows.length < wechatPoolState.limit || (total > 0 && end >= total);
    }
  }

  async function loadWechatPool(options = {}) {
    const summaryOnly = Boolean(options.summaryOnly);
    const params = new URLSearchParams({
      scope: summaryOnly ? 'all' : wechatPoolState.scope,
      limit: String(summaryOnly ? 1 : wechatPoolState.limit),
      offset: String(summaryOnly ? 0 : wechatPoolState.offset),
    });
    try {
      const response = await fetch(`/api/wechat/pool?${params}`, { cache: 'no-store' });
      const data = await readApiResponse(response, '文章库读取失败');
      const stats = updateWechatStats(data);
      if (summaryOnly) return;
      const rows = data.rows || data.items || data.articles || [];
      const fallbackTotal = wechatPoolState.scope === 'all' ? integerFrom(stats, ['total'], rows.length) : rows.length;
      const total = integerFrom(data, ['total', 'filtered_total'], fallbackTotal);
      wechatPoolState.total = total;
      const normalizedRows = Array.isArray(rows) ? rows : [];
      if (!normalizedRows.length && wechatPoolState.offset > 0) {
        wechatPoolState.offset = Math.max(0, wechatPoolState.offset - wechatPoolState.limit);
        await loadWechatPool();
        return;
      }
      renderWechatPoolRows(normalizedRows, total);
    } catch (error) {
      if (summaryOnly) {
        if (wechatPoolSummary) wechatPoolSummary.textContent = '公众号文章库：暂时无法读取';
        return;
      }
      renderWechatPoolRows([], 0);
      setWechatMessage(error.message, true);
    }
  }

  async function discoverWechatSources() {
    const buttons = [...document.querySelectorAll('[data-wechat-discover]')];
    const previousSummary = wechatDiscoverySummary?.textContent || '';
    buttons.forEach((button) => {
      button.disabled = true;
      button.dataset.idleLabel = button.textContent;
      button.textContent = '正在拓源…';
    });
    if (wechatDiscoverySummary) wechatDiscoverySummary.textContent = '全网发现：正在启动拓源任务…';
    if (dialog?.open) setWechatMessage('正在发起主动全网发现；发现结果仍需取得正文和真实发布日期后才能进入评分。');
    try {
      const response = await fetch('/api/wechat/discover', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
      const data = await readApiResponse(response, '主动拓源服务暂不可用');
      updateWechatStats(data);
      const message = data.message || (data.queued ? '拓源任务已启动，发现结果会陆续进入文章库。' : '本轮主动拓源已完成。');
      showToast(message);
      if (dialog?.open) setWechatMessage(`${message} 发现只是线索；未取得正文或真实发布日期不会进入七日评分。`);
      await loadWechatPool({ summaryOnly: true });
      if (data.queued) window.setTimeout(() => loadWechatPool({ summaryOnly: true }), 2500);
    } catch (error) {
      if (wechatDiscoverySummary) {
        wechatDiscoverySummary.textContent = previousSummary || '全网发现：服务暂不可用';
      }
      const message = `${error.message}；仍可粘贴原文或导入历史文件。`;
      showToast(message, true);
      if (dialog?.open) setWechatMessage(message, true);
    } finally {
      buttons.forEach((button) => {
        button.disabled = false;
        button.textContent = button.dataset.idleLabel || '全网拓源一次';
        delete button.dataset.idleLabel;
      });
    }
  }

  async function saveWechatUrls() {
    const urls = (wechatUrls?.value || '')
      .split(/\r?\n/)
      .map((item) => item.trim())
      .filter(Boolean);
    if (!urls.length) {
      setWechatMessage('请先粘贴公众号文章链接。', true);
      return;
    }
    if (urls.length > 500) {
      setWechatMessage('一次最多加入 500 条链接，请分批处理。', true);
      return;
    }
    saveWechatButton.disabled = true;
    setWechatMessage('正在检查并加入文章库…');
    try {
      const response = await fetch('/api/wechat/urls', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ urls }),
      });
      const data = await readApiResponse(response, '加入文章库失败');
      const added = integerFrom(data, ['added_count', 'added']);
      const existing = integerFrom(data, ['existing_count', 'existing']);
      const invalid = integerFrom(data, ['invalid_count', 'invalid']);
      const invalidMessages = (data.results || [])
        .filter((item) => item.status === 'invalid' || item.ok === false)
        .slice(0, 3)
        .map((item) => item.message)
        .filter(Boolean);
      const detail = invalidMessages.length ? `\n${invalidMessages.join('\n')}` : '';
      setWechatMessage(`已处理：新增 ${added}｜已存在 ${existing}｜无法识别 ${invalid}${detail}`, invalid > 0 && added === 0);
      updateWechatStats(data);
      if (!invalid && wechatUrls) wechatUrls.value = '';
      if (added > 0 && refreshAfterWechatButton) refreshAfterWechatButton.hidden = false;
      await loadWechatPool({ summaryOnly: true });
    } catch (error) {
      const payload = error.payload || {};
      if (Array.isArray(payload.results)) {
        const invalid = payload.results.filter((item) => item.status === 'invalid' || item.ok === false);
        const detail = invalid.slice(0, 3).map((item) => item.message).filter(Boolean).join('\n');
        setWechatMessage(`${payload.message || error.message}${detail ? `\n${detail}` : ''}`, true);
      } else {
        setWechatMessage(error.message, true);
      }
    } finally {
      saveWechatButton.disabled = false;
    }
  }

  function resetWechatImportPreview() {
    wechatImportFingerprint = '';
    if (confirmWechatImportButton) confirmWechatImportButton.disabled = true;
    if (wechatImportPreview) wechatImportPreview.hidden = true;
    if (wechatImportPreviewSummary) wechatImportPreviewSummary.textContent = '';
    if (wechatImportIssues) wechatImportIssues.replaceChildren();
  }

  function selectedWechatImportFile() {
    const file = wechatImportFile?.files?.[0];
    if (!file) throw new Error('请先选择 exporter 导出的 JSON 或 CSV 文件。');
    if (!/\.(json|csv)$/i.test(file.name)) throw new Error('只支持 JSON 或 CSV 文件。');
    if (file.size > 10 * 1024 * 1024) throw new Error('文件不能超过 10 MB。');
    return file;
  }

  function previewCounts(data) {
    const preview = data.preview || data;
    return {
      rows: integerFrom(preview, ['row_count', 'rows_total', 'total']),
      added: integerFrom(preview, ['added_count', 'new_count', 'added']),
      existing: integerFrom(preview, ['existing_count', 'existing']),
      invalid: integerFrom(preview, ['invalid_count', 'invalid']),
      start: preview.date_start || preview.start_date || '',
      end: preview.date_end || preview.end_date || '',
    };
  }

  async function previewWechatImport() {
    resetWechatImportPreview();
    previewWechatImportButton.disabled = true;
    setWechatMessage('正在检查历史文件…');
    try {
      const file = selectedWechatImportFile();
      const form = new FormData();
      form.append('file', file);
      const response = await fetch('/api/wechat/import/preview', { method: 'POST', body: form });
      const data = await readApiResponse(response, '历史文件预览失败');
      const counts = previewCounts(data);
      wechatImportFingerprint = data.fingerprint || data.preview?.fingerprint || '';
      if (!wechatImportFingerprint) throw new Error('预览结果缺少文件指纹，请重新选择文件。');
      const dateRange = counts.start || counts.end ? `；日期范围 ${counts.start || '待确认'}～${counts.end || '待确认'}` : '';
      wechatImportPreviewSummary.textContent = `识别 ${counts.rows} 行：新增 ${counts.added}、已存在 ${counts.existing}、需修正 ${counts.invalid}${dateRange}`;
      (data.issues || data.preview?.issues || []).slice(0, 20).forEach((issue) => {
        const item = document.createElement('li');
        item.textContent = issue && typeof issue === 'object' ? issue.message || '存在一条需修正的记录' : String(issue);
        wechatImportIssues.appendChild(item);
      });
      wechatImportPreview.hidden = false;
      confirmWechatImportButton.disabled = false;
      setWechatMessage('预览完成；确认无误后再导入。');
    } catch (error) {
      setWechatMessage(error.message, true);
    } finally {
      previewWechatImportButton.disabled = false;
    }
  }

  async function confirmWechatImport() {
    if (!wechatImportFingerprint) {
      setWechatMessage('请先预览文件。', true);
      return;
    }
    confirmWechatImportButton.disabled = true;
    setWechatMessage('正在导入文章库…');
    try {
      const file = selectedWechatImportFile();
      const form = new FormData();
      form.append('file', file);
      form.append('fingerprint', wechatImportFingerprint);
      const response = await fetch('/api/wechat/import', { method: 'POST', body: form });
      const data = await readApiResponse(response, '历史文件导入失败');
      const added = integerFrom(data, ['added_count', 'added']);
      const existing = integerFrom(data, ['existing_count', 'existing']);
      const invalid = integerFrom(data, ['invalid_count', 'invalid']);
      const inWindow = integerFrom(extractPoolStats(data), ['in_window', 'window']);
      setWechatMessage(`导入完成：新增 ${added}｜已存在 ${existing}｜未导入 ${invalid}。文章库中本周共有 ${inWindow} 篇；可立即更新雷达。`);
      updateWechatStats(data);
      if (added > 0 && refreshAfterWechatButton) refreshAfterWechatButton.hidden = false;
      if (wechatImportFile) wechatImportFile.value = '';
      resetWechatImportPreview();
      await loadWechatPool({ summaryOnly: true });
    } catch (error) {
      setWechatMessage(error.message, true);
      confirmWechatImportButton.disabled = false;
    }
  }

  refreshButton?.addEventListener('click', refreshReport);
  document.querySelectorAll('[data-open-wechat]').forEach((button) => {
    button.addEventListener('click', () => {
      dialog.showModal();
      activateWechatTab(button.dataset.wechatOpenTab || 'urls');
      loadWechatPool({ summaryOnly: true });
    });
  });
  document.querySelectorAll('[data-wechat-discover]').forEach((button) => {
    button.addEventListener('click', discoverWechatSources);
  });
  saveWechatButton?.addEventListener('click', saveWechatUrls);
  document.querySelectorAll('[data-wechat-tab]').forEach((button) => {
    button.addEventListener('click', () => activateWechatTab(button.dataset.wechatTab));
  });
  document.querySelectorAll('[data-wechat-scope]').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelectorAll('[data-wechat-scope]').forEach((item) => item.classList.remove('active'));
      button.classList.add('active');
      wechatPoolState.scope = button.dataset.wechatScope;
      wechatPoolState.offset = 0;
      loadWechatPool();
    });
  });
  wechatPoolPrev?.addEventListener('click', () => {
    wechatPoolState.offset = Math.max(0, wechatPoolState.offset - wechatPoolState.limit);
    loadWechatPool();
  });
  wechatPoolNext?.addEventListener('click', () => {
    wechatPoolState.offset += wechatPoolState.limit;
    loadWechatPool();
  });
  wechatImportFile?.addEventListener('change', () => {
    resetWechatImportPreview();
    setWechatMessage('');
  });
  previewWechatImportButton?.addEventListener('click', previewWechatImport);
  confirmWechatImportButton?.addEventListener('click', confirmWechatImport);
  refreshAfterWechatButton?.addEventListener('click', () => {
    dialog.close();
    refreshReport();
  });
  loadWechatPool({ summaryOnly: true });
  pollInitialRefresh();
})();
