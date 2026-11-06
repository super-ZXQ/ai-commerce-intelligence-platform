import { ApiError, apiFetch, redirectToLogin } from './workspace-api.js';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const PAGE_SIZE = 100;
const HISTORY_PAGE_SIZE = 10;
const POLL_INTERVAL = 1000;
const STALE_AFTER_MS = 10 * 60 * 1000;

const CANONICAL_LABELS = {
  order_seq_id: '订单顺序编号',
  order_id: '订单号',
  user_name: '用户名',
  product_id: '商品编号',
  order_amount: '订单金额',
  payment_amount: '付款金额',
  channel_id: '渠道编号',
  platform_type: '平台类型',
  order_time: '下单时间',
  payment_time: '付款时间',
  is_refund: '是否退款',
  discount_amount: '优惠金额',
  payment_duration_sec: '支付耗时(秒)',
};

const STATUS_META = {
  validating: { label: '校验中', tone: 'info' },
  validated: { label: '校验完成', tone: 'ok' },
  confirmed: { label: '已确认', tone: 'info' },
  running: { label: '正在写入', tone: 'warn' },
  completed: { label: '已完成', tone: 'ok' },
  failed: { label: '失败', tone: 'danger' },
  cancelled: { label: '已取消', tone: 'warn' },
};

const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled']);
const TAB_META = {
  valid: { label: '可新增', countKey: 'rows_valid' },
  conflict: { label: '冲突', countKey: 'rows_conflict' },
  invalid: { label: '校验失败', countKey: 'rows_invalid' },
};

const TEMPLATE_HEADER = '订单号,用户名,商品编号,订单金额,付款金额,渠道编号,平台类型,下单时间,付款时间,是否退款,优惠金额,支付耗时_秒';
const TEMPLATE_SAMPLE = 'TEMPLATE-0001,示例用户,P1001,199.00,199.00,c1,APP,2025-01-01 10:00:00,2025-01-01 10:00:05,否,0,5';

const state = {
  me: null,
  file: null,
  detection: null,
  run: null,
  preview: null,
  activeRunId: null,
  tab: 'valid',
  page: 1,
  pollTimer: 0,
  polling: false,
  execStartedAt: 0,
  rowCache: new Map(),
  historyPage: 1,
  historyTotalPages: 0,
  toastTimer: 0,
  confirming: false,
  resuming: false,
};

/* ───────────── 基础工具 ───────────── */

function element(tag, className = '', text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function setText(node, value) {
  if (node) node.textContent = value == null ? '' : String(value);
}

function formatInteger(value) {
  const number = Number(value);
  return Number.isFinite(number) ? new Intl.NumberFormat('zh-CN').format(number) : '—';
}

function formatBytes(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return '—';
  if (number < 1024) return `${number} B`;
  if (number < 1024 * 1024) return `${(number / 1024).toFixed(1)} KB`;
  return `${(number / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDateTime(value) {
  if (!value) return '—';
  const timestamp = parseNaiveUtc(value);
  if (!Number.isFinite(timestamp)) return String(value);
  return new Date(timestamp).toLocaleString('zh-CN', { hour12: false });
}

function formatDuration(seconds) {
  const number = Number(seconds);
  if (!Number.isFinite(number) || number < 0) return '—';
  if (number < 60) return `${number.toFixed(1)} 秒`;
  const minutes = Math.floor(number / 60);
  return `${minutes} 分 ${Math.round(number % 60)} 秒`;
}

function parseNaiveUtc(value) {
  if (!value) return NaN;
  const text = String(value);
  const withZone = /[zZ]|[+-]\d{2}:\d{2}$/.test(text) ? text : `${text}Z`;
  return new Date(withZone).getTime();
}

function toast(message) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.add('show');
  window.clearTimeout(state.toastTimer);
  state.toastTimer = window.setTimeout(() => node.classList.remove('show'), 3600);
}

function describeError(error) {
  if (error instanceof ApiError) return error.message;
  if (error?.name === 'AbortError') return '请求已取消';
  return '请求失败，请稍后重试';
}

function setBlock(container, className, message) {
  container.replaceChildren(element('div', className, message));
}

function setErrorBlock(container, message, retry) {
  container.replaceChildren();
  const box = element('div', 'state-error');
  box.append(element('span', '', message));
  if (retry) {
    const button = element('button', 'retry', '重试');
    button.type = 'button';
    button.addEventListener('click', () => {
      button.disabled = true;
      button.textContent = '重试中…';
      retry();
    });
    box.append(button);
  }
  container.append(box);
}

function showAlert(message, tone = 'danger') {
  const node = $('#import-alert');
  node.textContent = message;
  node.className = `import-alert${tone === 'ok' ? ' ok' : ''}`;
  node.removeAttribute('hidden');
}

function hideAlert() {
  $('#import-alert').setAttribute('hidden', '');
}

function statusMeta(status) {
  return STATUS_META[status] || { label: status || '未知', tone: '' };
}

function applyStatusPill(node, status) {
  const meta = statusMeta(status);
  node.className = `status-pill ${meta.tone}`.trim();
  node.textContent = meta.label;
  node.removeAttribute('hidden');
}

/* ───────────── 权限与会话 ───────────── */

function showUnauthorized() {
  $('#unauthorized-state').removeAttribute('hidden');
  $('#upload-panel')?.setAttribute('hidden', '');
  $('#preview-panel')?.setAttribute('hidden', '');
  $('#run-panel')?.setAttribute('hidden', '');
  $('#history-panel')?.setAttribute('hidden', '');
  setText($('#import-alert'), '');
  hideAlert();
}

function applyIdentity() {
  const me = state.me;
  if (!me) return;
  setText($('#user-avatar'), (me.username || '?').slice(0, 1).toUpperCase());
  $('#user-avatar')?.setAttribute('title', me.username || '当前用户');
}

function isAdmin() {
  return Boolean(state.me?.scopes?.includes('admin:manage'));
}

/* ───────────── 文件识别 ───────────── */

function fileExtension(name) {
  const lower = String(name || '').toLowerCase();
  if (lower.endsWith('.xlsx')) return 'xlsx';
  if (lower.endsWith('.csv')) return 'csv';
  return 'unknown';
}

async function inspectFile(file) {
  const ext = fileExtension(file.name);
  const buffer = await file.slice(0, 4096).arrayBuffer();
  const bytes = new Uint8Array(buffer);
  const zipMagic = bytes.length >= 2 && bytes[0] === 0x50 && bytes[1] === 0x4b;
  const bom = bytes.length >= 3 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf;

  let encoding = '—';
  if (ext === 'xlsx' || zipMagic) {
    encoding = 'ZIP/XML（Excel 工作簿，无文本编码）';
  } else if (bom) {
    encoding = 'UTF-8（含 BOM）';
  } else {
    try {
      new TextDecoder('utf-8', { fatal: true }).decode(bytes);
      encoding = 'UTF-8';
    } catch {
      encoding = '非 UTF-8（服务端仅接受 UTF-8，可带 BOM）';
    }
  }

  const format = zipMagic ? 'XLSX（ZIP 工作簿）' : ext === 'csv' ? 'CSV（文本）' : '未知';
  const mismatch = (ext === 'csv' && zipMagic) || (ext === 'xlsx' && !zipMagic);
  return { ext, format, encoding, zipMagic, bom, mismatch };
}

function renderDetection(file, detection) {
  const list = $('#detect-list');
  list.replaceChildren();
  const rows = [
    ['文件', file.name],
    ['格式', detection.format],
    ['编码', detection.encoding],
    ['大小', formatBytes(file.size)],
  ];
  for (const [label, value] of rows) {
    const row = element('div');
    row.append(element('dt', '', label), element('dd', '', value));
    list.append(row);
  }
  const nameChip = $('#file-name');
  nameChip.textContent = `${file.name} · ${formatBytes(file.size)}`;
  nameChip.removeAttribute('hidden');
  setText($('#drop-title'), '已选择文件，点击可重新选择');
  if (detection.mismatch) {
    showAlert('文件扩展名与实际文件头不一致，服务端会再次校验；如被拒绝请确认文件格式后重传。');
  } else {
    hideAlert();
  }
}

async function chooseFile(file) {
  state.file = null;
  state.detection = null;
  $('#upload-btn').disabled = true;
  if (!file) {
    setText($('#drop-title'), '拖拽文件到此处，或点击选择');
    $('#file-name').setAttribute('hidden', '');
    const list = $('#detect-list');
    list.replaceChildren();
    const row = element('div');
    row.append(element('dt', '', '文件'), element('dd', '', '尚未选择'));
    list.append(row);
    return;
  }
  const ext = fileExtension(file.name);
  if (ext === 'unknown') {
    showAlert('仅支持 .csv 或 .xlsx 文件，请重新选择。');
    return;
  }
  try {
    const detection = await inspectFile(file);
    state.file = file;
    state.detection = detection;
    renderDetection(file, detection);
    $('#upload-btn').disabled = false;
  } catch {
    showAlert('无法读取文件头，请重新选择文件。');
  }
}

/* ───────────── 上传 ───────────── */

function currentPolicy() {
  const checked = $$('input[name="conflict-policy"]').find((node) => node.checked);
  return checked ? checked.value : 'reject';
}

function setUploadBusy(busy) {
  const button = $('#upload-btn');
  button.disabled = busy || !state.file;
  button.textContent = busy ? '正在上传…' : '开始上传并校验';
  const dropzone = $('#dropzone');
  if (busy) dropzone.setAttribute('aria-disabled', 'true');
  else dropzone.removeAttribute('aria-disabled');
}

async function uploadFile() {
  if (!state.file) return;
  hideAlert();
  setUploadBusy(true);
  const uploadState = $('#upload-state');
  uploadState.removeAttribute('hidden');
  setBlock(uploadState, 'state-block loading', '正在上传，服务端将进行格式校验与逐行解析…');

  const form = new FormData();
  form.append('file', state.file, state.file.name);
  form.append('conflict_policy', currentPolicy());

  try {
    const run = await apiFetch('/api/data-import/upload', { method: 'POST', body: form });
    state.run = run;
    state.activeRunId = run.run_id;
    state.execStartedAt = 0;
    state.rowCache.clear();
    setBlock(uploadState, 'state-block loading', `文件已接收（任务 #${run.run_id}），正在校验…`);
    showPreviewPanel();
    setBlock($('#preview-state'), 'state-block loading', '服务端正在识别列映射并逐行校验，请稍候…');
    $('#preview-content').setAttribute('hidden', '');
    startPolling();
  } catch (error) {
    setErrorBlock(uploadState, `上传失败：${describeError(error)}`, uploadFile);
    if (error?.status === 403) showAlert('当前账号没有数据导入权限，请确认角色配置。');
    else if (error?.status === 422 || error?.status === 413) showAlert(describeError(error));
  } finally {
    setUploadBusy(false);
  }
}

/* ───────────── 轮询 ───────────── */

function stopPolling() {
  window.clearInterval(state.pollTimer);
  state.pollTimer = 0;
  state.polling = false;
}

function startPolling() {
  stopPolling();
  state.pollTimer = window.setInterval(pollRun, POLL_INTERVAL);
  pollRun();
}

async function pollRun() {
  if (!state.activeRunId || state.polling) return;
  state.polling = true;
  try {
    const run = await apiFetch(`/api/data-import/runs/${state.activeRunId}`);
    state.run = run;
    if (run.status === 'validated') {
      stopPolling();
      await loadPreview();
    } else if (run.status === 'failed' && !run.rows_inserted) {
      stopPolling();
      showValidationFailure(run);
    } else if (run.status === 'validating') {
      if (runIsStale(run)) {
        stopPolling();
        showStalePreview(run);
      } else {
        setText($('#preview-sub'), '服务端正在校验…');
        setText($('#preview-state .state-block'), `已读取 ${run.counts?.rows_read || 0} 行，正在校验…`);
      }
    } else if (run.status === 'confirmed') {
      // 已确认但未启动：停止轮询，展示「继续执行」入口，避免无限轮询。
      stopPolling();
      showExecutionPanel();
      renderExecution(run);
    } else if (run.status === 'running') {
      if (!state.execStartedAt) state.execStartedAt = Date.now();
      showExecutionPanel();
      renderExecution(run);
    } else if (TERMINAL_STATUSES.has(run.status)) {
      stopPolling();
      showExecutionPanel();
      renderExecution(run);
      await loadResult(run);
      loadHistory(state.historyPage);
    }
  } catch (error) {
    if (error?.status === 404) {
      stopPolling();
      showStalePreview({ error_message: '该导入任务已被清理或不存在，请重新上传。' });
      return;
    }
    if (error?.status === 401 || error?.status === 403) {
      stopPolling();
      if (error.status === 403) showUnauthorized();
      return;
    }
    toast(describeError(error));
  } finally {
    state.polling = false;
  }
}

function runIsStale(run) {
  if (!run || !['validating', 'running'].includes(run.status)) return false;
  const heartbeat = parseNaiveUtc(run.heartbeat_at);
  return Number.isFinite(heartbeat) && Date.now() - heartbeat > STALE_AFTER_MS;
}

/* ───────────── 预览 ───────────── */

function showPreviewPanel() {
  $('#preview-panel').removeAttribute('hidden');
}

async function loadPreview() {
  showPreviewPanel();
  $('#preview-content').setAttribute('hidden', '');
  setBlock($('#preview-state'), 'state-block loading', '正在读取校验结果…');
  try {
    const preview = await apiFetch(`/api/data-import/runs/${state.activeRunId}/preview`);
    state.preview = preview;
    state.run = preview.run;
    state.tab = 'valid';
    state.page = 1;
    state.rowCache.clear();
    renderPreview(preview);
  } catch (error) {
    if (error?.status === 404) {
      showStalePreview({ error_message: '该导入任务的校验结果已被清理，请重新上传。' });
      return;
    }
    setErrorBlock($('#preview-state'), `读取校验结果失败：${describeError(error)}`, loadPreview);
  }
}

function showValidationFailure(run) {
  $('#preview-content').setAttribute('hidden', '');
  $('#preview-panel').removeAttribute('hidden');
  setBlock($('#preview-state'), 'state-block', '');
  const box = element('div', 'state-error');
  box.append(
    element('span', '', run.error_message || '校验失败，请检查文件后重新上传。'),
    element('small', '', run.error_code ? `错误码：${run.error_code}` : ''),
  );
  $('#preview-state').replaceChildren(box);
  applyStatusPill($('#preview-status'), run.status);
  setText($('#preview-sub'), run.original_filename || '校验失败');
}

function showStalePreview(run) {
  $('#preview-content').setAttribute('hidden', '');
  $('#preview-panel').removeAttribute('hidden');
  const box = element('div', 'state-warn');
  box.textContent = run.error_message || '校验任务可能已中断或过期，请重新上传文件后再试。';
  $('#preview-state').replaceChildren(box);
  setText($('#preview-sub'), '任务已过期或中断');
  applyStatusPill($('#preview-status'), 'failed');
}

function renderPreview(preview) {
  const run = preview.run;
  setText($('#preview-sub'), `${run.original_filename || '导入文件'} · 校验完成`);
  applyStatusPill($('#preview-status'), run.status);
  $('#preview-state').setAttribute('hidden', '');
  $('#preview-content').removeAttribute('hidden');

  renderSummary(run, preview);
  renderMapping(preview);
  renderCounts(run.counts || {}, preview);

  const invalid = Number(run.counts?.rows_invalid || 0);
  $('#download-failures').toggleAttribute('hidden', invalid <= 0);
  $('#open-confirm').toggleAttribute('hidden', !(run.can_confirm && Number(run.counts?.rows_valid || 0) > 0));
  renderRowTab();
}

function renderSummary(run, preview) {
  const container = $('#file-summary');
  container.replaceChildren();
  const items = [
    ['文件', run.original_filename || '—'],
    ['格式', (run.source_type || '—').toUpperCase()],
    ['大小', formatBytes(run.size_bytes)],
    ['冲突策略', run.conflict_policy === 'skip' ? '跳过冲突行' : '拒绝冲突行并继续'],
    ['校验状态', statusMeta(run.status).label],
    ['失败明细', `${formatInteger(preview.invalid?.length || 0)} 行（预览前 100 行）`],
    ['SHA-256', run.sha256 || '—', true],
    ['任务编号', `#${run.run_id}`],
  ];
  for (const [label, value, mono] of items) {
    const item = element('div', `summary-item${mono ? ' mono' : ''}`);
    item.append(element('small', '', label), element('strong', '', value));
    container.append(item);
  }
}

function renderMapping(preview) {
  const list = $('#column-mapping');
  list.replaceChildren();
  for (const column of preview.columns || []) {
    const item = element('div', 'mapping-item');
    const label = element('span', '', CANONICAL_LABELS[column.canonical] || column.canonical);
    let status;
    let tone;
    if (!column.source) {
      status = '缺失';
      tone = 'missing';
    } else if (column.source === column.canonical) {
      status = '已匹配';
      tone = 'ok';
    } else {
      status = '别名';
      tone = 'alias';
    }
    const stateNode = element('div');
    stateNode.append(
      element('strong', '', column.source || '未提供'),
      element('span', `map-status ${tone}`, status),
    );
    item.append(label, stateNode);
    if (column.required) item.setAttribute('title', '必需字段');
    list.append(item);
  }

  const unknownLine = $('#unknown-columns');
  unknownLine.replaceChildren();
  const unknown = preview.unknown_columns || [];
  if (unknown.length) {
    unknownLine.append(element('span', '', '未识别列：'));
    for (const name of unknown) unknownLine.append(element('span', 'chip', name));
    unknownLine.removeAttribute('hidden');
  } else {
    unknownLine.setAttribute('hidden', '');
  }

  const missingLine = $('#missing-columns');
  missingLine.replaceChildren();
  const missing = preview.missing_required || [];
  if (missing.length) {
    missingLine.append(element('span', '', '缺失必需列：'));
    for (const name of missing) {
      missingLine.append(element('span', 'chip', CANONICAL_LABELS[name] || name));
    }
    missingLine.removeAttribute('hidden');
  } else {
    missingLine.setAttribute('hidden', '');
  }
}

function renderCounts(counts, preview) {
  const container = $('#preview-counts');
  container.replaceChildren();
  const cards = [
    ['读取行数', counts.rows_read, ''],
    ['可新增', counts.rows_valid, 'valid'],
    ['冲突', counts.rows_conflict, 'conflict'],
    ['校验失败', counts.rows_invalid, 'invalid'],
    ['退款行（预估）', counts.rows_refunded, 'refund'],
  ];
  for (const [label, value, tone] of cards) {
    const card = element('div', `count-card ${tone}`.trim());
    card.append(element('small', '', label), element('strong', '', formatInteger(value)));
    container.append(card);
  }
  // Tab 标题同步计数，避免用户在分页后再回头数一遍。
  for (const button of $$('#row-tabs .tab')) {
    const meta = TAB_META[button.dataset.tab];
    if (meta) button.textContent = `${meta.label} ${formatInteger(counts[meta.countKey] || 0)}`;
  }
  const limit = preview.preview_limit || PAGE_SIZE;
  setText($('#row-table-note'), `每类最多展示前 ${limit} 行，可分页查看；分页只加载当前页。`);
}

function totalForTab(tab) {
  const counts = state.run?.counts || {};
  const meta = TAB_META[tab];
  return Number(counts[meta?.countKey] || 0);
}

function rowsForCurrentPage() {
  if (state.page === 1) return state.preview?.[state.tab] || [];
  return state.rowCache.get(`${state.tab}:${state.page}`) || [];
}

function renderRowTab() {
  for (const button of $$('#row-tabs .tab')) {
    const active = button.dataset.tab === state.tab;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
  }
  renderRows();
}

async function selectTab(tab) {
  if (!TAB_META[tab]) return;
  state.tab = tab;
  state.page = 1;
  renderRowTab();
}

async function loadRowPage(page) {
  const totalPages = Math.max(1, Math.ceil(totalForTab(state.tab) / PAGE_SIZE));
  if (page < 1 || page > totalPages) return;
  if (page === 1) {
    state.page = 1;
    renderRows();
    return;
  }
  const key = `${state.tab}:${page}`;
  if (!state.rowCache.has(key)) {
    setBlock($('#row-table'), 'state-block loading', '正在加载当前页…');
    try {
      const data = await apiFetch(
        `/api/data-import/runs/${state.activeRunId}/rows?status=${state.tab}&page=${page}&page_size=${PAGE_SIZE}`,
      );
      state.rowCache.set(key, data.items || []);
    } catch (error) {
      setErrorBlock($('#row-table'), `加载失败：${describeError(error)}`, () => loadRowPage(page));
      return;
    }
  }
  state.page = page;
  renderRows();
}

function renderRows() {
  const container = $('#row-table');
  const rows = rowsForCurrentPage();
  const total = totalForTab(state.tab);
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const note = $('#row-table-note');
  setText(note, `第 ${state.page} / ${totalPages} 页 · 每页最多 ${PAGE_SIZE} 行 · 共 ${formatInteger(total)} 行`);

  if (!rows.length) {
    setBlock(container, 'state-block', `「${TAB_META[state.tab].label}」暂无记录`);
    renderRowPager(totalPages);
    return;
  }

  const table = element('table', 'row-table');
  const head = element('thead');
  const headRow = element('tr');
  for (const title of rowColumns(state.tab)) headRow.append(element('th', '', title));
  head.append(headRow);
  const body = element('tbody');
  for (const row of rows) body.append(buildRow(state.tab, row));
  table.append(head, body);
  container.replaceChildren(table);
  renderRowPager(totalPages);
}

function rowColumns(tab) {
  if (tab === 'valid') return ['行号', '订单号', '用户名', '订单金额', '付款金额', '下单时间', '退款'];
  if (tab === 'conflict') return ['行号', '订单号', '冲突原因', '错误码'];
  return ['行号', '订单号', '错误码', '失败原因'];
}

function buildRow(tab, row) {
  const tr = element('tr');
  const fields = row.fields || {};
  if (tab === 'valid') {
    const refund = fields.is_refund === '是' ? '是' : '否';
    addCells(tr, [
      row.row_no,
      row.order_id || fields.order_id,
      fields.user_name,
      fields.order_amount,
      fields.payment_amount,
      fields.order_time,
      refund,
    ]);
  } else {
    addCells(tr, [row.row_no, row.order_id || fields.order_id, row.error_message || '—', row.error_code || '—']);
    tr.children[1].className = 'mono';
  }
  return tr;
}

function addCells(row, values) {
  for (const value of values) {
    row.append(element('td', '', value == null || value === '' ? '—' : value));
  }
}

function renderRowPager(totalPages) {
  const container = $('#row-pager');
  container.replaceChildren();
  container.append(element('span', '', `第 ${state.page} / ${totalPages} 页`));
  const previous = element('button', '', '上一页');
  previous.type = 'button';
  previous.disabled = state.page <= 1;
  previous.addEventListener('click', () => loadRowPage(state.page - 1));
  const next = element('button', '', '下一页');
  next.type = 'button';
  next.disabled = state.page >= totalPages;
  next.addEventListener('click', () => loadRowPage(state.page + 1));
  container.append(previous, next);
}

/* ───────────── 二次确认与执行 ───────────── */

function openConfirmDialog() {
  if (!state.run?.can_confirm) return;
  const run = state.run;
  const counts = run.counts || {};
  const policy = run.conflict_policy === 'skip' ? 'skip' : 'reject';
  const conflicts = Number(counts.rows_conflict || 0);
  const invalid = Number(counts.rows_invalid || 0);
  const container = $('#confirm-counts');
  container.replaceChildren();
  const cards = [
    ['写入', counts.rows_valid, 'valid'],
    ['跳过', policy === 'skip' ? conflicts : 0, ''],
    ['拒绝', invalid + (policy === 'reject' ? conflicts : 0), 'invalid'],
    ['退款', counts.rows_refunded, 'refund'],
  ];
  for (const [label, value, tone] of cards) {
    const card = element('div', `count-card ${tone}`.trim());
    card.append(element('small', '', label), element('strong', '', formatInteger(value)));
    container.append(card);
  }
  const dialog = $('#confirm-dialog');
  if (typeof dialog.showModal === 'function') dialog.showModal();
  else dialog.setAttribute('open', '');
}

function closeConfirmDialog() {
  const dialog = $('#confirm-dialog');
  if (typeof dialog.close === 'function') dialog.close();
  else dialog.removeAttribute('open');
}

async function submitConfirm() {
  if (state.confirming || !state.run) return;
  const button = $('#submit-confirm');
  state.confirming = true;
  button.disabled = true;
  button.textContent = '正在确认…';
  try {
    const runId = state.run.run_id;
    const confirmed = await apiFetch(`/api/data-import/runs/${runId}/confirm`, {
      method: 'POST',
      body: { expected_sha256: state.run.sha256 },
    });
    const token = confirmed?.confirm_token;
    if (!token) throw new ApiError('确认令牌未返回，请重新预览后再试', 409, 'IMPORT_CONFIRM_TOKEN_MISSING');
    closeConfirmDialog();
    showExecutionPanel();
    setBlock($('#run-state'), 'state-block loading', '正在抢占执行锁并启动写入…');
    const executed = await apiFetch(`/api/data-import/runs/${runId}/execute`, {
      method: 'POST',
      body: { confirm_token: token },
    });
    state.run = executed;
    state.execStartedAt = Date.now();
    $('#run-content').removeAttribute('hidden');
    $('#run-state').setAttribute('hidden', '');
    renderExecution(executed);
    startPolling();
    toast('已开始导入：写入过程不可撤销，可随时取消剩余分块。');
  } catch (error) {
    closeConfirmDialog();
    const message = error?.status === 409
      ? '已有导入任务正在执行，请等待其结束后重试。'
      : `启动导入失败：${describeError(error)}`;
    showAlert(message);
    $('#run-panel').removeAttribute('hidden');
    $('#run-content').setAttribute('hidden', '');
    $('#run-state').removeAttribute('hidden');
    setErrorBlock($('#run-state'), message, submitConfirm);
  } finally {
    state.confirming = false;
    button.disabled = false;
    button.textContent = '确认并开始导入';
  }
}

function showExecutionPanel() {
  $('#run-panel').removeAttribute('hidden');
}

function renderExecution(run) {
  const content = $('#run-content');
  content.removeAttribute('hidden');
  $('#run-state').setAttribute('hidden', '');
  applyStatusPill($('#run-status'), run.status);

  const percent = Math.max(0, Math.min(100, Number(run.progress_percent) || 0));
  const bar = $('#run-progressbar');
  bar.setAttribute('aria-valuenow', String(Math.round(percent)));
  const fill = $('#run-progress');
  fill.style.width = `${percent}%`;
  setText($('#run-percent'), `${percent.toFixed(1)}%`);

  const elapsed = elapsedSeconds(run);
  setText($('#run-elapsed'), elapsed === null ? '已用时 —' : `已用时 ${formatDuration(elapsed)}`);

  renderRunCounts(run.counts || {});

  const message = $('#run-message');
  message.className = 'run-message';
  if (run.status === 'cancelled') {
    message.classList.add('warn');
    setText(message, '已取消：已提交的分块保留在订单表中，未处理的行没有写入。需要补充数据请重新上传。');
  } else if (run.status === 'failed') {
    message.classList.add('danger');
    setText(message, run.error_message || '执行失败，请查看失败明细后重试。');
  } else if (run.cancel_requested) {
    message.classList.add('warn');
    setText(message, '已请求取消：当前分块提交后会停止后续写入，已提交部分不会回滚。');
  } else if (run.status === 'running') {
    setText(message, '正在按分块事务写入，进度会随每次提交更新。');
  } else if (run.status === 'completed') {
    setText(message, '导入完成，并已执行一次只读一致性校验。');
  } else if (run.status === 'confirmed') {
    setText(message, '已确认但尚未开始写入，可点击「继续执行」重新获取确认令牌并启动。');
  } else {
    setText(message, statusMeta(run.status).label);
  }

  const resume = $('#resume-run');
  resume.toggleAttribute('hidden', run.status !== 'confirmed');
  resume.disabled = false;
  resume.textContent = '继续执行';
  const cancel = $('#cancel-run');
  const canCancel = run.status === 'running' && !run.cancel_requested;
  cancel.toggleAttribute('hidden', !canCancel);
  cancel.disabled = false;
  cancel.textContent = '取消剩余分块';
  $('#refresh-result').toggleAttribute('hidden', !TERMINAL_STATUSES.has(run.status));
  const actions = $('.run-actions');
  if (actions) {
    actions.hidden = !canCancel && !TERMINAL_STATUSES.has(run.status) && run.status !== 'confirmed';
  }
}

function renderRunCounts(counts) {
  const container = $('#run-metrics');
  container.replaceChildren();
  const cards = [
    ['已插入', counts.rows_inserted, 'valid'],
    ['已跳过', counts.rows_skipped, ''],
    ['已拒绝', counts.rows_rejected, 'invalid'],
    ['退款行', counts.rows_refunded, 'refund'],
  ];
  for (const [label, value, tone] of cards) {
    const card = element('div', `count-card ${tone}`.trim());
    card.append(element('small', '', label), element('strong', '', formatInteger(value)));
    container.append(card);
  }
}

function elapsedSeconds(run) {
  if (run.finished_at && run.started_at) {
    const start = parseNaiveUtc(run.started_at);
    const end = parseNaiveUtc(run.finished_at);
    if (Number.isFinite(start) && Number.isFinite(end) && end >= start) return (end - start) / 1000;
  }
  if (run.status === 'running') {
    if (state.execStartedAt) return (Date.now() - state.execStartedAt) / 1000;
    const start = parseNaiveUtc(run.started_at);
    if (Number.isFinite(start)) return Math.max(0, (Date.now() - start) / 1000);
  }
  return null;
}

async function loadResult(run) {
  const container = $('#run-result');
  container.removeAttribute('hidden');
  container.replaceChildren(element('div', 'state-block loading', '正在读取执行结果…'));
  try {
    const result = await apiFetch(`/api/data-import/runs/${run.run_id}/result`);
    const failed = result.failed || [];
    container.replaceChildren();
    const line = element('div', 'result-line');
    line.append(
      element('strong', '', `任务 #${run.run_id} · ${statusMeta(result.run.status).label}`),
      element('span', '', `失败 ${formatInteger(failed.length)} 行`),
      element('span', '', `跳过 ${formatInteger(result.skipped || 0)} 行`),
    );
    container.append(line);
    if (failed.length) {
      const note = element('p', 'table-note', '以下为执行阶段失败的前 100 行，完整明细请下载 CSV。');
      const table = buildFailureTable(failed);
      container.append(note, table);
      const download = element('button', 'outline-button', '下载失败明细 CSV');
      download.type = 'button';
      download.addEventListener('click', () => downloadFailures(run.run_id));
      container.append(download);
    } else if (result.run.status === 'completed') {
      container.append(element('p', 'table-note', '没有执行阶段失败行。失败明细可通过上方预览区的导出按钮获取。'));
    }
  } catch (error) {
    setErrorBlock(container, `读取结果失败：${describeError(error)}`, () => loadResult(run));
  }
}

function buildFailureTable(rows) {
  const table = element('table', 'row-table');
  const head = element('thead');
  const headRow = element('tr');
  for (const title of ['行号', '订单号', '错误码', '失败原因']) headRow.append(element('th', '', title));
  head.append(headRow);
  const body = element('tbody');
  for (const row of rows) {
    const tr = element('tr');
    addCells(tr, [row.row_no, row.order_id, row.error_code, row.error_message || '—']);
    body.append(tr);
  }
  table.append(head, body);
  return table;
}

async function cancelRun() {
  if (!state.activeRunId || state.run?.status !== 'running') return;
  const button = $('#cancel-run');
  button.disabled = true;
  button.textContent = '正在请求取消…';
  try {
    const run = await apiFetch(`/api/data-import/runs/${state.activeRunId}/cancel`, { method: 'POST' });
    state.run = run;
    renderExecution(run);
    if (run.status === 'completed' || run.status === 'failed') {
      toast('任务已结束，未执行取消。');
    } else if (run.status === 'cancelled') {
      toast('已取消：尚未开始执行，未写入任何订单。');
    } else {
      toast('已请求取消：已提交分块保留，剩余分块不再写入。');
    }
    if (!TERMINAL_STATUSES.has(run.status)) startPolling();
    else {
      await loadResult(run);
      loadHistory(state.historyPage);
    }
  } catch (error) {
    toast(`取消失败：${describeError(error)}`);
    button.disabled = false;
    button.textContent = '取消剩余分块';
  }
}

async function resumeRun() {
  if (!state.activeRunId || state.run?.status !== 'confirmed' || state.resuming) return;
  const button = $('#resume-run');
  state.resuming = true;
  button.disabled = true;
  button.textContent = '正在启动…';
  try {
    // 刷新页面后确认令牌可能已丢失：重新确认会轮换出新令牌。
    const runId = state.activeRunId;
    const confirmed = await apiFetch(`/api/data-import/runs/${runId}/confirm`, {
      method: 'POST',
      body: { expected_sha256: state.run.sha256 },
    });
    const token = confirmed?.confirm_token;
    if (!token) throw new ApiError('确认令牌未返回，请重新上传文件', 409, 'IMPORT_CONFIRM_TOKEN_MISSING');
    const executed = await apiFetch(`/api/data-import/runs/${runId}/execute`, {
      method: 'POST',
      body: { confirm_token: token },
    });
    state.run = executed;
    state.execStartedAt = Date.now();
    renderExecution(executed);
    startPolling();
    toast('已继续执行导入。');
  } catch (error) {
    const message = error?.status === 409
      ? '已有导入任务正在执行，请等待其结束后重试。'
      : `继续执行失败：${describeError(error)}`;
    showAlert(message);
    button.disabled = false;
    button.textContent = '继续执行';
  } finally {
    state.resuming = false;
  }
}

/* ───────────── 失败明细下载 ───────────── */

async function downloadFailures(runId) {
  try {
    const response = await fetch(`/api/data-import/runs/${runId}/failures.csv`, {
      credentials: 'same-origin',
      headers: { Accept: 'text/csv' },
    });
    if (response.status === 401) {
      redirectToLogin();
      return;
    }
    if (!response.ok) {
      let message = '失败明细下载失败，请稍后重试';
      try {
        const payload = await response.json();
        message = payload?.detail?.message || message;
      } catch {
        /* 非 JSON 错误响应保留默认文案 */
      }
      toast(message);
      return;
    }
    const blob = await response.blob();
    const disposition = response.headers.get('content-disposition') || '';
    const match = /filename="?([^";]+)"?/.exec(disposition);
    downloadBlob(blob, match?.[1] || `import-${runId}-failures.csv`);
    if (response.headers.get('x-result-truncated') === 'true') {
      toast('失败明细超过导出上限，CSV 已截断。');
    }
  } catch {
    toast('网络连接失败，失败明细暂未下载。');
  }
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1500);
}

function downloadTemplate() {
  const content = `\uFEFF${TEMPLATE_HEADER}\n${TEMPLATE_SAMPLE}\n`;
  downloadBlob(new Blob([content], { type: 'text/csv;charset=utf-8' }), '订单导入模板.csv');
}

/* ───────────── 导入历史 ───────────── */

async function loadHistory(page = 1) {
  const target = Math.max(1, Number(page) || 1);
  const container = $('#history-table');
  const stateBox = $('#history-state');
  stateBox.removeAttribute('hidden');
  setBlock(stateBox, 'state-block loading', '正在加载导入历史…');
  container.setAttribute('hidden', '');
  try {
    const data = await apiFetch(
      `/api/data-import/runs?page=${target}&page_size=${HISTORY_PAGE_SIZE}`,
    );
    state.historyPage = data.page || target;
    state.historyTotalPages = data.total_pages || 0;
    setText(
      $('#history-sub'),
      isAdmin()
        ? `管理员视图 · 共 ${formatInteger(data.total)} 条记录，可查看全部账号的导入`
        : `仅展示当前账号 · 共 ${formatInteger(data.total)} 条记录`,
    );
    if (!data.items?.length) {
      setBlock(stateBox, 'state-block', '还没有导入记录。上传第一份文件后，这里会显示校验与执行结果。');
      $('#history-pager').replaceChildren();
      return;
    }
    stateBox.setAttribute('hidden', '');
    container.removeAttribute('hidden');
    renderHistory(data.items);
    renderHistoryPager();
  } catch (error) {
    if (error?.status === 403) {
      showUnauthorized();
      return;
    }
    setErrorBlock(stateBox, `加载导入历史失败：${describeError(error)}`, () => loadHistory(target));
  }
}

function renderHistory(items) {
  const container = $('#history-table');
  const table = element('table', 'history-table');
  const head = element('thead');
  const headRow = element('tr');
  const columns = ['任务', '文件', '状态', '读取 / 写入', '冲突策略', '时间'];
  // 操作人列只在管理员视图出现，普通账号始终只能看到自己的任务。
  if (isAdmin()) columns.push('操作人');
  columns.push('操作');
  for (const title of columns) headRow.append(element('th', '', title));
  head.append(headRow);

  const body = element('tbody');
  for (const run of items) {
    const tr = element('tr');
    addCells(tr, [
      `#${run.run_id}`,
      run.original_filename || '—',
      statusMeta(run.status).label,
      `${formatInteger(run.counts?.rows_read)} / ${formatInteger(run.counts?.rows_inserted)}`,
      run.conflict_policy === 'skip' ? '跳过冲突' : '拒绝冲突',
      formatDateTime(run.finished_at || run.heartbeat_at || run.started_at),
    ]);
    tr.children[1].className = 'file-cell';
    const statusCell = element('td');
    statusCell.append(element('span', `status-pill ${statusMeta(run.status).tone}`.trim(), statusMeta(run.status).label));
    tr.children[2].replaceWith(statusCell);
    if (isAdmin()) tr.append(element('td', '', run.operator || '—'));
    const actionCell = element('td');
    const open = element('button', '', '查看');
    open.type = 'button';
    open.addEventListener('click', () => openRun(run.run_id));
    actionCell.append(open);
    tr.append(actionCell);
    body.append(tr);
  }
  table.append(head, body);
  container.replaceChildren(table);
}

function renderHistoryPager() {
  const container = $('#history-pager');
  container.replaceChildren();
  if (state.historyTotalPages <= 1) return;
  container.append(element('span', '', `第 ${state.historyPage} / ${state.historyTotalPages} 页`));
  const previous = element('button', '', '上一页');
  previous.type = 'button';
  previous.disabled = state.historyPage <= 1;
  previous.addEventListener('click', () => loadHistory(state.historyPage - 1));
  const next = element('button', '', '下一页');
  next.type = 'button';
  next.disabled = state.historyPage >= state.historyTotalPages;
  next.addEventListener('click', () => loadHistory(state.historyPage + 1));
  container.append(previous, next);
}

async function openRun(runId) {
  hideAlert();
  state.activeRunId = Number(runId);
  state.execStartedAt = 0;
  state.rowCache.clear();
  try {
    const run = await apiFetch(`/api/data-import/runs/${state.activeRunId}`);
    state.run = run;
    if (run.status === 'validated') {
      showPreviewPanel();
      await loadPreview();
      $('#preview-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
    } else if (run.status === 'validating') {
      showPreviewPanel();
      if (runIsStale(run)) {
        showStalePreview(run);
      } else {
        setBlock($('#preview-state'), 'state-block loading', '该任务仍在服务端校验中…');
        $('#preview-content').setAttribute('hidden', '');
        startPolling();
      }
      $('#preview-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
    } else if (run.status === 'failed' && !run.counts?.rows_inserted) {
      showPreviewPanel();
      showValidationFailure(run);
      $('#preview-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
    } else if (run.status === 'confirmed' || run.status === 'running') {
      showExecutionPanel();
      renderExecution(run);
      if (run.status === 'running') startPolling();
      $('#run-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
    } else {
      showExecutionPanel();
      renderExecution(run);
      await loadResult(run);
      $('#run-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  } catch (error) {
    if (error?.status === 404) {
      showAlert('该导入任务已被清理或不存在，请刷新历史后重试。');
      return;
    }
    showAlert(`读取任务失败：${describeError(error)}`);
  }
}

/* ───────────── 事件绑定 ───────────── */

function bindEvents() {
  $('#logout').addEventListener('click', async () => {
    try {
      await apiFetch('/api/auth/logout', { method: 'POST' });
    } catch {
      // 退出失败也要回登录页，由 /logout 兜底清理服务端 Cookie。
    }
    window.location.assign('/login');
  });

  const dropzone = $('#dropzone');
  const input = $('#file-input');
  dropzone.addEventListener('click', () => input.click());
  dropzone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      input.click();
    }
  });
  input.addEventListener('change', () => chooseFile(input.files?.[0] || null));

  for (const type of ['dragenter', 'dragover']) {
    dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      dropzone.classList.add('dragover');
    });
  }
  for (const type of ['dragleave', 'drop']) {
    dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      dropzone.classList.remove('dragover');
    });
  }
  dropzone.addEventListener('drop', (event) => {
    const file = event.dataTransfer?.files?.[0] || null;
    chooseFile(file);
  });

  $('#upload-btn').addEventListener('click', uploadFile);
  $('#download-template').addEventListener('click', downloadTemplate);
  $('#download-failures').addEventListener('click', () => {
    if (state.activeRunId) downloadFailures(state.activeRunId);
  });

  for (const button of $$('#row-tabs .tab')) {
    button.addEventListener('click', () => selectTab(button.dataset.tab));
  }

  $('#open-confirm').addEventListener('click', openConfirmDialog);
  $('#close-confirm').addEventListener('click', closeConfirmDialog);
  $('#cancel-confirm').addEventListener('click', closeConfirmDialog);
  $('#submit-confirm').addEventListener('click', submitConfirm);
  $('#confirm-dialog').addEventListener('click', (event) => {
    if (event.target === $('#confirm-dialog')) closeConfirmDialog();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && $('#confirm-dialog').open) closeConfirmDialog();
  });

  $('#cancel-run').addEventListener('click', cancelRun);
  $('#resume-run').addEventListener('click', resumeRun);
  $('#refresh-result').addEventListener('click', () => {
    if (!state.run) return;
    renderExecution(state.run);
    loadResult(state.run);
    loadHistory(state.historyPage);
  });
  $('#refresh-history').addEventListener('click', () => loadHistory(state.historyPage));

  window.addEventListener('beforeunload', stopPolling);
}

async function init() {
  bindEvents();
  try {
    state.me = await apiFetch('/api/auth/me');
  } catch (error) {
    if (error?.status === 401) return;
    if (error?.status === 403) {
      showUnauthorized();
      return;
    }
    showAlert(`会话信息读取失败：${describeError(error)}`);
    return;
  }
  applyIdentity();
  if (!state.me?.scopes?.includes('data:import')) {
    showUnauthorized();
    return;
  }
  const runParam = new URLSearchParams(window.location.search).get('run');
  if (runParam && Number(runParam) > 0) {
    openRun(Number(runParam));
  }
  loadHistory(1);
}

document.addEventListener('DOMContentLoaded', init);
