import { ApiError, apiFetch, streamAI } from './workspace-api.js';
import { renderBarChart, renderLineChart, renderPieChart } from './workspace-charts.js';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

const VIEW_TITLES = {
  overview: '经营总览',
  assistant: 'AI 数据问答',
  sales: '销售分析',
  rfm: 'RFM 客户分层',
  products: '订单与商品',
  monitor: '运行监控',
};

const ROLE_LABELS = {
  admin: '管理员',
  analyst: '分析员',
  viewer: '查看者',
  ingest: '数据接入',
};

// 与 BI 看板保持一致：八类分群的颜色语义不因工作台重设计而改变。
const SEGMENT_COLORS = {
  重要价值客户: '#1565C0',
  重要发展客户: '#1E88E5',
  重要保持客户: '#1E88E5',
  重要挽留客户: '#FF5722',
  一般价值客户: '#43A047',
  一般发展客户: '#43A047',
  一般保持客户: '#43A047',
  一般挽留客户: '#7E57C2',
};

const ORDER_STATUS_LABELS = {
  ACTIVE: '正常',
  REFUNDED: '已退款',
  CANCELLED: '已取消',
};

const moneyFormatter = new Intl.NumberFormat('zh-CN', {
  minimumFractionDigits: 0,
  maximumFractionDigits: 2,
});
const integerFormatter = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 });

const state = {
  me: null,
  referenceDate: null,
  referencePromise: null,
  overviewLoading: false,
  overviewLoaded: false,
  overviewPeriod: 7,
  salesLoading: false,
  salesLoaded: false,
  rfmLoading: false,
  rfmLoaded: false,
  rfm: { referenceDate: '', effectiveReference: '', segment: null, page: 1, totalPages: 0 },
  productsLoading: false,
  productsLoaded: false,
  orderProducts: [],
  ordersLoading: false,
  ordersLoaded: false,
  orders: { page: 1, totalPages: 0, total: 0, items: new Map() },
  monitorLoading: false,
  monitorLoaded: false,
  ai: {
    ready: null,
    enabled: false,
    reason: '',
    threadId: null,
    busy: false,
    lastQuestion: '',
    controller: null,
    lastResponse: null,
    resultRows: [],
  },
  activeView: 'overview',
  toastTimer: 0,
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

function toNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function formatInteger(value) {
  const number = toNumber(value);
  return number === null ? '—' : integerFormatter.format(number);
}

function formatMoney(value, { compact = false } = {}) {
  const number = toNumber(value);
  if (number === null) return '—';
  if (compact) {
    return `¥ ${new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 }).format(number)}`;
  }
  return `¥ ${moneyFormatter.format(number)}`;
}

function formatMetricMoney(value) {
  const number = toNumber(value);
  if (number === null) return '—';
  return Math.abs(number) >= 1_000_000 ? formatMoney(number, { compact: true }) : formatMoney(number);
}

function formatPercent(value, digits = 1) {
  const number = toNumber(value);
  if (number === null) return '—';
  return `${number.toFixed(digits)}%`;
}

function formatDateTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('zh-CN', { hour12: false });
}

function formatDay(value) {
  if (!value) return '—';
  return String(value).slice(0, 10);
}

function formatDuration(seconds) {
  const number = toNumber(seconds);
  if (number === null) return '—';
  return `${number.toFixed(1)} 秒`;
}

function queryString(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== '' && value !== null && value !== undefined) search.set(key, String(value));
  }
  const result = search.toString();
  return result ? `?${result}` : '';
}

function toast(message) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.add('show');
  window.clearTimeout(state.toastTimer);
  state.toastTimer = window.setTimeout(() => node.classList.remove('show'), 3600);
}

function setLoading(container, message) {
  container.replaceChildren(element('div', 'state-block loading', message));
}

function setEmpty(container, message) {
  container.replaceChildren(element('div', 'state-block', message));
}

function setError(container, message, retry) {
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

function describeError(error) {
  if (error instanceof ApiError) return error.message;
  if (error?.name === 'AbortError') return '请求已取消';
  return '请求失败，请稍后重试';
}

function handleLoadError(error, container, retry) {
  if (error?.status === 401) return;
  if (error?.code === 'RFM_DATA_UNAVAILABLE' || error?.status === 503) {
    setEmpty(container, error.message || '暂无可用订单数据，请先导入经营数据');
    return;
  }
  setError(container, describeError(error), retry);
}

function markDataFresh(prefix = '真实数据') {
  const now = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
  setText($('#data-freshness'), `${prefix} · 更新于 ${now}`);
}

function markDataError() {
  setText($('#data-freshness'), '数据加载失败');
}

function scrollToTop() {
  window.scrollTo({ top: 0, behavior: reducedMotion.matches ? 'auto' : 'smooth' });
}

function rangeFromPeriod(period) {
  if (!state.referenceDate) return { start: '', end: '' };
  const end = new Date(`${state.referenceDate}T00:00:00Z`);
  if (Number.isNaN(end.getTime())) return { start: '', end: '' };
  const start = new Date(end);
  start.setUTCDate(end.getUTCDate() - (period - 1));
  return { start: start.toISOString().slice(0, 10), end: state.referenceDate };
}

function orderStatusMeta(order) {
  const status = String(order.order_status || 'ACTIVE');
  if (status === 'CANCELLED') return { label: ORDER_STATUS_LABELS.CANCELLED, tone: 'error' };
  if (status === 'REFUNDED' || order.is_refunded === '是') return { label: ORDER_STATUS_LABELS.REFUNDED, tone: 'warn' };
  return { label: ORDER_STATUS_LABELS.ACTIVE, tone: 'ok' };
}

/* ───────────── 视图导航 ───────────── */

function canViewMonitor() {
  return Boolean(state.me?.scopes?.includes('admin:manage'));
}

function canUseAi() {
  return Boolean(state.me?.scopes?.includes('ai:query'));
}

function applyIdentity() {
  const me = state.me;
  if (!me) return;
  const role = ROLE_LABELS[me.role] || me.role || me.principal_type || '已登录';
  const avatar = $('#user-avatar');
  setText(avatar, (me.username || '?').slice(0, 1).toUpperCase());
  avatar.title = `${me.username} · ${role}`;
  if (canViewMonitor()) {
    $('#monitor-section')?.removeAttribute('hidden');
    $('#monitor-nav')?.removeAttribute('hidden');
    $('#mobile-monitor')?.removeAttribute('hidden');
  } else {
    $('#monitor-section')?.setAttribute('hidden', '');
    $('#monitor-nav')?.setAttribute('hidden', '');
    $('#mobile-monitor')?.setAttribute('hidden', '');
  }
}

function switchView(view, { push = true } = {}) {
  if (!VIEW_TITLES[view]) view = 'overview';
  if (view === 'monitor' && !canViewMonitor()) {
    toast('当前账号没有运行监控权限');
    view = 'overview';
  }
  state.activeView = view;
  $$('.view').forEach((node) => node.classList.toggle('active', node.id === `view-${view}`));
  $$('.nav-btn, .mobile-nav button').forEach((node) => node.classList.toggle('active', node.dataset.view === view));
  setText($('#crumb-name'), VIEW_TITLES[view]);
  document.title = `${VIEW_TITLES[view]} · 经营工作台`;
  if (push && window.location.hash !== `#${view}`) {
    window.history.pushState({ view }, '', `#${view}`);
  }
  scrollToTop();
  loadView(view);
}

function loadView(view) {
  if (view === 'overview') loadOverview();
  if (view === 'assistant') ensureAssistantReady();
  if (view === 'sales') loadSales();
  if (view === 'rfm') loadRfm();
  if (view === 'products') loadProductsView();
  if (view === 'monitor') loadMonitor();
}

/* ───────────── 共享数据 ───────────── */

function loadReference() {
  if (!state.referencePromise) {
    state.referencePromise = apiFetch('/api/rfm/overview')
      .then((data) => {
        state.referenceDate = data.reference_date || null;
        return data;
      })
      .catch((error) => {
        state.referencePromise = null;
        throw error;
      });
  }
  return state.referencePromise;
}

async function fetchTrend(start, end) {
  return apiFetch(`/api/analytics/sales-trend${queryString({ granularity: 'day', start_date: start, end_date: end })}`);
}

function renderTrendInto(container, rows, { start, end, period, sub, emptyMessage } = {}) {
  if (!Array.isArray(rows) || rows.length === 0) {
    setEmpty(container, emptyMessage || '当前时间范围内暂无订单数据');
    return;
  }
  if (rows.length === 1) {
    const row = rows[0];
    // 单日数据画折线会退化成孤点，还容易被误读成完整趋势；改为明确的单日摘要。
    container.replaceChildren();
    const box = element('div', 'trend-single');
    box.append(
      element('small', '', `${formatDay(row.period)} · 当前范围仅 1 天有订单日期数据`),
      element('strong', '', formatMoney(row.sales)),
      element('span', '', `${formatInteger(row.order_count)} 笔订单`),
    );
    container.append(box);
    if (sub) setText(sub, `${formatDay(row.period)} 单日汇总 · 其余日期没有可用于趋势的订单日期`);
    return;
  }
  renderLineChart(container, rows, { emptyMessage: emptyMessage || '当前时间范围内暂无订单数据' });
  if (sub) {
    setText(sub, start && end
      ? `${formatDay(start)} 至 ${formatDay(end)} · 按日汇总`
      : `最近 ${period || rows.length} 条日粒度记录`);
  }
}

/* ───────────── 经营总览 ───────────── */

async function loadOverview() {
  if (state.overviewLoading) return;
  state.overviewLoading = true;
  const kpiContainer = $('#overview-kpis');
  const trendContainer = $('#overview-trend');
  const productsContainer = $('#overview-products');
  const rfmContainer = $('#overview-rfm');
  const insightsContainer = $('#overview-insights');
  const retry = () => {
    state.overviewLoaded = false;
    loadOverview();
  };

  kpiContainer.querySelectorAll('.state-error').forEach((node) => node.remove());
  setLoading(trendContainer, '正在加载销售趋势…');
  setLoading(productsContainer, '正在加载商品排行…');
  setLoading(rfmContainer, '正在加载客户分群…');
  setLoading(insightsContainer, '正在整理数据入口…');

  const [overviewResult, productsResult, referenceResult] = await Promise.allSettled([
    apiFetch('/api/analytics/sales-overview'),
    apiFetch('/api/analytics/top-products?limit=5'),
    loadReference(),
  ]);

  let overview = null;
  let products = [];
  let rfm = null;
  if (referenceResult.status === 'fulfilled') rfm = referenceResult.value;
  if (overviewResult.status === 'fulfilled') overview = overviewResult.value;
  if (productsResult.status === 'fulfilled') products = productsResult.value;

  if (overview) {
    setText($('#overview-sales'), formatMetricMoney(overview.total_sales));
    $('#overview-sales').title = formatMoney(overview.total_sales);
    setText($('#overview-orders'), formatInteger(overview.total_orders));
    setText($('#overview-users'), `${formatInteger(overview.total_users)} 位客户 · 全部非取消订单`);
    setText($('#overview-aov'), formatMoney(overview.avg_order_value));
    setText($('#overview-refund'), formatPercent(overview.refund_rate));
    setText($('#overview-refund-amount'), `退款金额 ${formatMoney(overview.total_refund_amount)}`);
    ['overview-sales', 'overview-orders', 'overview-aov', 'overview-refund'].forEach((id) => {
      $(`#${id}`).classList.remove('is-error');
    });
  } else {
    ['overview-sales', 'overview-orders', 'overview-aov', 'overview-refund'].forEach((id) => {
      const node = $(`#${id}`);
      node.textContent = '暂不可用';
      node.classList.add('is-error');
    });
    setText($('#overview-users'), '经营指标读取失败');
    setText($('#overview-refund-amount'), '退款金额读取失败');
    const errorBox = element('div', 'state-error');
    errorBox.append(element('span', '', `经营指标读取失败：${describeError(overviewResult.reason)}`));
    const button = element('button', 'retry', '重试');
    button.type = 'button';
    button.addEventListener('click', retry);
    errorBox.append(button);
    kpiContainer.append(errorBox);
  }

  await loadOverviewTrend();
  renderOverviewProducts(products, productsResult.status === 'rejected' ? productsResult.reason : null);
  renderOverviewRfm(rfm, referenceResult.status === 'rejected' ? referenceResult.reason : null);
  renderOverviewInsights({ overview, rfm, products });

  state.overviewLoaded = true;
  state.overviewLoading = false;
  if (overview) markDataFresh();
  else markDataError();
}

async function loadOverviewTrend() {
  const container = $('#overview-trend');
  const range = rangeFromPeriod(state.overviewPeriod);
  setLoading(container, '正在加载销售趋势…');
  try {
    const response = await fetchTrend(range.start, range.end);
    let rows = response?.data || [];
    if (!range.start && rows.length > state.overviewPeriod) {
      rows = rows.slice(-state.overviewPeriod);
    }
    renderTrendInto(container, rows, {
      start: range.start,
      end: range.end,
      period: state.overviewPeriod,
      sub: $('#overview-trend-sub'),
      emptyMessage: '当前口径下暂无日粒度订单数据',
    });
  } catch (error) {
    handleLoadError(error, container, loadOverviewTrend);
  }
}

function renderOverviewProducts(products, error) {
  const container = $('#overview-products');
  if (error) {
    handleLoadError(error, container, loadOverview);
    return;
  }
  if (!Array.isArray(products) || products.length === 0) {
    setEmpty(container, '暂无商品销售数据');
    return;
  }
  container.replaceChildren(tableWrap(buildProductTable(products)));
}

function tableWrap(table) {
  const wrap = element('div', 'table-wrap');
  wrap.append(table);
  return wrap;
}

function buildProductTable(products, { ranking = true, ask = true } = {}) {
  const table = element('table', 'data-table');
  const head = element('thead');
  const headRow = element('tr');
  const columns = ranking ? ['排名', '商品', '销售额', '订单量'] : ['商品', '销售额', '订单量'];
  columns.forEach((label) => headRow.append(element('th', '', label)));
  if (ask) headRow.append(element('th', '', '操作'));
  head.append(headRow);
  table.append(head);

  const body = element('tbody');
  products.forEach((product, index) => {
    const row = element('tr');
    if (ranking) row.append(element('td', 'rank-cell', String(product.rank ?? index + 1)));
    const productCell = element('div', 'product-cell');
    productCell.append(
      element('span', '', product.product_id || '未知商品'),
      element('small', '', `订单 ${formatInteger(product.order_count)} 笔`),
    );
    const productTd = element('td');
    productTd.append(productCell);
    row.append(productTd);
    row.append(element('td', 'num strong', formatMoney(product.total_sales)));
    row.append(element('td', 'num', formatInteger(product.order_count)));
    if (ask) {
      const action = element('td');
      const button = element('button', 'mini-btn', '问 AI');
      button.type = 'button';
      button.addEventListener('click', () => askAboutProduct(product.product_id));
      action.append(button);
      row.append(action);
    }
    body.append(row);
  });
  table.append(body);
  return table;
}

function renderOverviewRfm(rfm, error) {
  const container = $('#overview-rfm');
  container.onclick = null;
  container.style.cursor = '';
  if (error) {
    handleLoadError(error, container, loadOverview);
    return;
  }
  const segments = rfm?.segment_distribution || [];
  if (segments.length === 0) {
    setEmpty(container, '暂无客户分群数据');
    return;
  }
  setText($('#overview-rfm-sub'), `参考日 ${formatDay(rfm.reference_date)} · 共 ${formatInteger(rfm.total_users)} 位客户`);
  const list = element('div', 'platform-list');
  segments.forEach((segment) => {
    const row = element('div', 'platform-row');
    const name = element('div', 'platform-name');
    const dot = element('i', 'platform-dot');
    dot.style.background = SEGMENT_COLORS[segment.segment] || '#7d66ed';
    name.append(dot, element('span', '', segment.segment));
    const track = element('div', 'platform-track');
    const fill = element('span', 'platform-fill');
    fill.style.width = `${Math.min(100, Math.max(2, Number(segment.percentage) || 0))}%`;
    fill.style.background = SEGMENT_COLORS[segment.segment] || '#7d66ed';
    track.append(fill);
    const meta = element('div', 'platform-meta', `${formatInteger(segment.count)} 人 · ${formatPercent(segment.percentage)}`);
    row.append(name, track, meta);
    row.title = `${segment.segment} · ${formatInteger(segment.count)} 位客户 · 占比 ${formatPercent(segment.percentage)}`;
    list.append(row);
  });
  container.replaceChildren(list);
  container.style.cursor = 'pointer';
  container.onclick = () => switchView('rfm');
  container.title = '查看 RFM 客户分层详情';
}

function renderOverviewInsights({ overview, rfm, products }) {
  const container = $('#overview-insights');
  const items = [];

  if (overview) {
    items.push({
      title: '销售规模',
      detail: `非取消订单累计 ${formatMoney(overview.total_sales)}，成交 ${formatInteger(overview.total_orders)} 笔，客单价 ${formatMoney(overview.avg_order_value)}。`,
      ask: '各平台的销售额和客单价有什么差异？',
    });
    items.push({
      title: overview.refund_rate > 5 ? '退款需要关注' : '退款观察',
      detail: `退款订单占比 ${formatPercent(overview.refund_rate)}，退款金额 ${formatMoney(overview.total_refund_amount)}。`,
      ask: '退款订单主要集中在哪些平台和商品？',
    });
  } else {
    items.push({ title: '销售规模', detail: '销售总览接口暂时不可用，已保留重新加载入口。', ask: '' });
  }

  if (rfm) {
    const important = (rfm.segment_distribution || []).find((segment) => segment.segment === '重要价值客户');
    const atRisk = (rfm.segment_distribution || []).find((segment) => segment.segment === '重要挽留客户');
    items.push({
      title: '客户价值',
      detail: `参与分层 ${formatInteger(rfm.total_users)} 位客户；重要价值客户 ${formatInteger(important?.count)} 位${atRisk ? `，重要挽留客户 ${formatInteger(atRisk.count)} 位` : ''}。`,
      ask: '哪些客户值得重点维护？他们有什么消费特征？',
    });
  } else {
    items.push({ title: '客户价值', detail: 'RFM 接口暂时不可用，客户结构未能加载。', ask: '' });
  }

  if (Array.isArray(products) && products.length > 0 && overview) {
    const topSales = products.reduce((sum, product) => sum + (toNumber(product.total_sales) || 0), 0);
    const ratio = toNumber(overview.total_sales) > 0 ? topSales / toNumber(overview.total_sales) * 100 : null;
    items.push({
      title: '商品集中度',
      detail: `TOP ${products.length} 商品贡献 ${formatMoney(topSales)}${ratio === null ? '' : `，占累计销售额 ${formatPercent(ratio)}`}。`,
      ask: '哪些商品贡献了最多销售额？它们的订单量如何？',
    });
  }

  container.replaceChildren();
  const list = element('div', 'insight-list');
  items.slice(0, 4).forEach((item, index) => {
    const row = element('div', 'insight-item');
    row.append(element('div', 'insight-num', String(index + 1)));
    const body = element('div', 'insight-body');
    body.append(element('strong', '', item.title), element('p', '', item.detail));
    if (item.ask) {
      const ask = element('button', 'insight-ask', '继续追问 AI →');
      ask.type = 'button';
      ask.addEventListener('click', () => askQuestion(item.ask));
      body.append(ask);
    }
    row.append(body);
    list.append(row);
  });
  container.append(list);
}

function askAboutProduct(productId) {
  askQuestion(`商品 ${productId} 的销售表现、订单量和退款情况如何？`);
}

async function askQuestion(question) {
  switchView('assistant');
  try {
    await ensureAssistantReady();
  } catch {
    toast('AI 配置读取失败，请稍后重试');
    return;
  }
  if (!state.ai.enabled) {
    const input = $('#chat-input');
    input.value = question;
    toast(state.ai.reason || 'AI 问答当前不可用');
    return;
  }
  $('#chat-input').value = '';
  sendQuestion(question);
}

/* ───────────── 销售分析 ───────────── */

async function loadSales() {
  if (state.salesLoading) return;
  state.salesLoading = true;
  const trendContainer = $('#sales-trend');
  const platformContainer = $('#sales-platforms');
  const productsContainer = $('#sales-products');
  const retry = () => {
    state.salesLoaded = false;
    loadSales();
  };

  setLoading(trendContainer, '正在加载销售趋势…');
  setLoading(platformContainer, '正在加载平台对比…');
  setLoading(productsContainer, '正在加载商品排行…');

  if (!state.salesLoaded) {
    // 销售趋势默认窗口锚定最新订单日期；RFM 接口失败时保留空日期并回退全量趋势。
    await Promise.allSettled([loadReference()]);
    const range = rangeFromPeriod(30);
    if (!$('#sales-start').value) $('#sales-start').value = range.start;
    if (!$('#sales-end').value) $('#sales-end').value = range.end;
  }

  const [overviewOutcome, categoriesOutcome, productsOutcome] = await Promise.allSettled([
    apiFetch('/api/analytics/sales-overview'),
    apiFetch('/api/analytics/category-analysis'),
    apiFetch('/api/analytics/top-products?limit=20'),
  ]);

  if (overviewOutcome.status === 'fulfilled') {
    const overview = overviewOutcome.value;
    setText($('#sales-sales'), formatMetricMoney(overview.total_sales));
    $('#sales-sales').title = formatMoney(overview.total_sales);
    setText($('#sales-orders'), formatInteger(overview.total_orders));
    setText($('#sales-aov'), formatMoney(overview.avg_order_value));
    setText($('#sales-refund'), formatPercent(overview.refund_rate));
  } else {
    ['sales-sales', 'sales-orders', 'sales-aov', 'sales-refund'].forEach((id) => {
      $(`#${id}`).textContent = '暂不可用';
    });
  }

  if (categoriesOutcome.status === 'fulfilled') {
    renderPlatformTable(categoriesOutcome.value?.categories || []);
  } else {
    handleLoadError(categoriesOutcome.reason, platformContainer, retry);
  }

  if (productsOutcome.status === 'fulfilled') {
    renderProductTableInto(productsContainer, productsOutcome.value, retry);
  } else {
    handleLoadError(productsOutcome.reason, productsContainer, retry);
  }

  await loadSalesTrend();
  state.salesLoaded = true;
  state.salesLoading = false;
  if (overviewOutcome.status === 'fulfilled') markDataFresh();
  else markDataError();
}

function renderPlatformTable(categories) {
  const container = $('#sales-platforms');
  if (!categories.length) {
    setEmpty(container, '暂无平台分析数据');
    return;
  }
  const table = element('table', 'data-table');
  const head = element('thead');
  const headRow = element('tr');
  ['平台', '销售额', '占比', '订单量', '退款率'].forEach((label) => headRow.append(element('th', '', label)));
  head.append(headRow);
  table.append(head);
  const body = element('tbody');
  const palette = ['#7d66ed', '#16a394', '#ffa56d', '#f4c62b', '#5b8def', '#e06b8b', '#8b95a5', '#43a047'];
  categories.forEach((item, index) => {
    const row = element('tr');
    const platformCell = element('div', 'platform-name');
    const dot = element('i', 'platform-dot');
    dot.style.background = palette[index % palette.length];
    platformCell.append(dot, element('span', '', item.platform_type || '未知平台'));
    const platformTd = element('td');
    platformTd.append(platformCell);
    row.append(platformTd);
    row.append(element('td', 'num strong', formatMoney(item.total_sales)));
    row.append(element('td', 'num', formatPercent(item.sales_ratio)));
    row.append(element('td', 'num', formatInteger(item.order_count)));
    row.append(element('td', 'num', formatPercent(item.refund_rate)));
    body.append(row);
  });
  table.append(body);
  container.replaceChildren(tableWrap(table));
}

async function loadSalesTrend() {
  const container = $('#sales-trend');
  const note = $('#sales-filter-note');
  const start = $('#sales-start').value;
  const end = $('#sales-end').value;
  if (start && end && start > end) {
    note.textContent = '开始日期不能晚于结束日期';
    note.classList.add('error');
    setEmpty(container, '请修正日期范围后重新查询');
    return;
  }
  note.classList.remove('error');
  setLoading(container, '正在加载销售趋势…');
  try {
    const response = await fetchTrend(start, end);
    renderTrendInto(container, response?.data || [], {
      start,
      end,
      sub: $('#sales-trend-sub'),
      emptyMessage: '当前筛选范围内暂无订单数据',
    });
    note.textContent = start || end
      ? `趋势筛选：${start || '最早'} 至 ${end || '最新'}，指标卡仍为全部非取消订单口径`
      : '未设置日期范围，展示全部日粒度趋势';
  } catch (error) {
    handleLoadError(error, container, loadSalesTrend);
    note.textContent = `趋势加载失败：${describeError(error)}`;
    note.classList.add('error');
  }
}

function renderProductTableInto(container, products, retry) {
  if (!Array.isArray(products) || products.length === 0) {
    setEmpty(container, '暂无商品销售数据');
    return;
  }
  try {
    container.replaceChildren(tableWrap(buildProductTable(products, { ranking: true, ask: true })));
  } catch (error) {
    setError(container, describeError(error), retry);
  }
}

/* ───────────── RFM 客户分层 ───────────── */

function rfmQuery(extra = {}) {
  return queryString({
    reference_date: state.rfm.referenceDate || '',
    n_bins: 5,
    ...extra,
  });
}

async function loadRfm() {
  if (state.rfmLoading) return;
  state.rfmLoading = true;
  const cards = $('#rfm-segments');
  const retry = () => {
    state.rfmLoaded = false;
    loadRfm();
  };
  setLoading(cards, '正在计算 RFM 分群…');
  try {
    const response = await apiFetch(`/api/rfm/segments${rfmQuery()}`);
    state.rfm.summary = response;
    // 过滤条件保持用户输入；实际参考日单独记录，避免把“留空=最新”悄悄改成固定日期。
    state.rfm.effectiveReference = response.reference_date || '';
    setText($('#rfm-total'), formatInteger(response.total_users));
    setText($('#rfm-reference'), `参考日 ${formatDay(response.reference_date)} · 分 ${response.n_bins} 组`);
    setText($('#rfm-recency'), `${formatInteger(response.averages?.recency_days)} 天`);
    setText($('#rfm-frequency'), formatInteger(response.averages?.frequency));
    setText($('#rfm-monetary'), formatMoney(response.averages?.monetary));
    renderRfmCards(response.segments || []);
    state.rfmLoaded = true;
    markDataFresh();
    if (state.rfm.segment) openRfmDetail(state.rfm.segment);
  } catch (error) {
    handleLoadError(error, cards, retry);
    ['rfm-total', 'rfm-recency', 'rfm-frequency', 'rfm-monetary'].forEach((id) => {
      $(`#${id}`).textContent = '暂不可用';
    });
    markDataError();
  } finally {
    state.rfmLoading = false;
  }
}

function renderRfmCards(segments) {
  const container = $('#rfm-segments');
  if (!segments.length) {
    setEmpty(container, '暂无客户分群数据');
    return;
  }
  container.replaceChildren();
  segments.forEach((segment) => {
    const card = element('button', 'rfm-card');
    card.type = 'button';
    card.style.setProperty('--seg-color', SEGMENT_COLORS[segment.segment] || '#7d66ed');
    if (state.rfm.segment === segment.segment) card.classList.add('active');
    const head = element('div', 'rfm-card-head');
    head.append(element('i'), element('span', '', segment.segment));
    const value = element('div', 'rfm-card-value', `${formatInteger(segment.count)} 人`);
    const meta = element('div', 'rfm-card-meta');
    meta.append(element('span', '', formatPercent(segment.percentage)), element('span', '', `客均 ${formatMoney(segment.avg_monetary)}`));
    const bar = element('div', 'rfm-card-bar');
    const fill = element('span');
    fill.style.width = `${Math.min(100, Math.max(2, Number(segment.percentage) || 0))}%`;
    bar.append(fill);
    card.append(head, value, meta, bar);
    card.setAttribute('aria-label', `查看${segment.segment}客户明细`);
    card.addEventListener('click', () => openRfmDetail(segment.segment));
    container.append(card);
  });
}

function openRfmDetail(segment) {
  state.rfm.segment = segment;
  state.rfm.page = 1;
  $$('.rfm-card').forEach((card) => {
    card.classList.toggle('active', card.querySelector('.rfm-card-head span')?.textContent === segment);
  });
  const panel = $('#rfm-detail-panel');
  panel.removeAttribute('hidden');
  setText($('#rfm-detail-title'), `${segment} · 客户明细`);
  loadRfmDetail();
}

async function loadRfmDetail() {
  const container = $('#rfm-detail');
  const segment = state.rfm.segment;
  if (!segment) return;
  setLoading(container, '正在读取客户明细…');
  try {
    const reference = state.rfm.effectiveReference || state.rfm.referenceDate;
    const response = await apiFetch(`/api/rfm/segments/${encodeURIComponent(segment)}${queryString({
      reference_date: reference || '',
      n_bins: 5,
      page: state.rfm.page,
      page_size: 20,
    })}`);
    state.rfm.page = response.page || 1;
    state.rfm.totalPages = response.total_pages || 0;
    setText($('#rfm-detail-sub'), `参考日 ${formatDay(reference)} · 第 ${response.page} / ${Math.max(response.total_pages, 1)} 页 · 共 ${formatInteger(response.total)} 位客户`);
    const users = response.users || [];
    if (users.length === 0) {
      setEmpty(container, '该分群当前没有客户');
      renderPagination($('#rfm-pagination'), response.page, response.total_pages, (page) => {
        state.rfm.page = page;
        loadRfmDetail();
      });
      return;
    }
    const table = element('table', 'data-table');
    const head = element('thead');
    const headRow = element('tr');
    ['客户', 'RFM', 'R', 'F', 'M', '最近消费', '频次', '金额'].forEach((label) => headRow.append(element('th', '', label)));
    head.append(headRow);
    table.append(head);
    const body = element('tbody');
    users.forEach((user) => {
      const row = element('tr');
      row.append(element('td', 'strong', user.user_name || '—'));
      row.append(element('td', 'num', String(user.rfm_score || '—')));
      row.append(element('td', 'num', String(user.r_score ?? '—')));
      row.append(element('td', 'num', String(user.f_score ?? '—')));
      row.append(element('td', 'num', String(user.m_score ?? '—')));
      row.append(element('td', 'num', `${formatInteger(user.recency_days)} 天`));
      row.append(element('td', 'num', formatInteger(user.frequency)));
      row.append(element('td', 'num strong', formatMoney(user.monetary)));
      body.append(row);
    });
    table.append(body);
    container.replaceChildren(table);
    renderPagination($('#rfm-pagination'), response.page, response.total_pages, (page) => {
      state.rfm.page = page;
      loadRfmDetail();
    });
  } catch (error) {
    handleLoadError(error, container, loadRfmDetail);
    $('#rfm-pagination').replaceChildren();
  }
}

function renderPagination(container, page, totalPages, onPage) {
  container.replaceChildren();
  const pages = Math.max(Number(totalPages) || 0, 1);
  container.append(element('span', 'page-info', `第 ${page} / ${pages} 页`));
  const previous = element('button', '', '上一页');
  previous.type = 'button';
  previous.disabled = page <= 1;
  previous.addEventListener('click', () => onPage(page - 1));
  const next = element('button', '', '下一页');
  next.type = 'button';
  next.disabled = page >= pages;
  next.addEventListener('click', () => onPage(page + 1));
  container.append(previous, next);
}

/* ───────────── 订单与商品 ───────────── */

async function loadProductsView() {
  if (state.productsLoading) return;
  state.productsLoading = true;
  if (!state.productsLoaded) {
    const ranking = $('#product-ranking');
    setLoading(ranking, '正在加载商品…');
    try {
      const products = await apiFetch('/api/analytics/top-products?limit=25');
      state.orderProducts = products || [];
      renderProductTableInto(ranking, products, loadProductsView);
      state.productsLoaded = true;
    } catch (error) {
      handleLoadError(error, ranking, loadProductsView);
    }
  }
  if (!state.ordersLoaded) await loadOrders(1);
  state.productsLoading = false;
}

async function loadOrders(page = 1) {
  if (state.ordersLoading) return;
  state.ordersLoading = true;
  const container = $('#orders-table');
  const note = $('#orders-summary');
  const start = $('#order-start').value;
  const end = $('#order-end').value;
  if (start && end && start > end) {
    note.textContent = '开始日期不能晚于结束日期';
    note.classList.add('error');
    setEmpty(container, '请修正日期范围后重新查询');
    state.ordersLoading = false;
    return;
  }
  note.classList.remove('error');
  setLoading(container, '正在读取订单…');
  try {
    const params = queryString({
      start_date: start,
      end_date: end,
      platform_type: $('#order-platform').value.trim(),
      product_id: $('#order-product').value.trim(),
      is_refunded: $('#order-refunded').value,
      page,
      page_size: 20,
      sort_by: 'order_time',
      sort_order: 'desc',
    });
    const response = await apiFetch(`/api/orders/filter${params}`);
    state.orders.page = response.page || page;
    state.orders.totalPages = response.total_pages || 0;
    state.orders.total = response.total || 0;
    state.orders.items = new Map((response.items || []).map((item) => [String(item.id), item]));
    setText(note, `共 ${formatInteger(response.total)} 条订单 · 第 ${response.page} / ${Math.max(response.total_pages, 1)} 页 · 覆盖订单表全部状态（含取消）`);
    renderOrders(response.items || []);
    renderPagination($('#orders-pagination'), response.page, response.total_pages, (target) => loadOrders(target));
    state.ordersLoaded = true;
    markDataFresh();
  } catch (error) {
    handleLoadError(error, container, () => loadOrders(page));
    $('#orders-pagination').replaceChildren();
    setText(note, `订单读取失败：${describeError(error)}`);
    note.classList.add('error');
  } finally {
    state.ordersLoading = false;
  }
}

function renderOrders(orders) {
  const container = $('#orders-table');
  if (!orders.length) {
    setEmpty(container, '当前筛选条件下没有订单');
    return;
  }
  const table = element('table', 'data-table');
  const head = element('thead');
  const headRow = element('tr');
  ['订单号', '客户', '商品', '平台', '支付金额', '下单时间', '状态'].forEach((label) => headRow.append(element('th', '', label)));
  head.append(headRow);
  table.append(head);
  const body = element('tbody');
  orders.forEach((order) => {
    const meta = orderStatusMeta(order);
    const row = element('tr');
    row.tabIndex = 0;
    row.style.cursor = 'pointer';
    row.append(element('td', 'strong', order.order_no || `#${order.id}`));
    row.append(element('td', '', order.user_name || '—'));
    row.append(element('td', 'date', order.product_id || '—'));
    row.append(element('td', '', order.platform_type || '—'));
    row.append(element('td', 'num strong', formatMoney(order.payment_amount)));
    row.append(element('td', 'date', formatDateTime(order.order_time)));
    const statusCell = element('td');
    statusCell.append(element('span', `pill ${meta.tone}`, meta.label));
    row.append(statusCell);
    const open = () => openOrderDialog(order);
    row.addEventListener('click', open);
    row.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        open();
      }
    });
    body.append(row);
  });
  table.append(body);
  container.replaceChildren(table);
}

function openOrderDialog(order) {
  const container = $('#order-detail');
  const meta = orderStatusMeta(order);
  container.replaceChildren();
  const grid = element('div', 'detail-grid');
  const rows = [
    ['订单号', order.order_no, false],
    ['订单 ID', order.id, false],
    ['客户', order.user_name, false],
    ['商品 ID', order.product_id, false],
    ['平台类型', order.platform_type, false],
    ['渠道编号', order.channel_id || '—', false],
    ['订单金额', formatMoney(order.order_amount), false],
    ['支付金额', formatMoney(order.payment_amount), false],
    ['优惠金额', formatMoney(order.discount_amount), false],
    ['支付耗时', formatDuration(order.payment_duration_sec), false],
    ['下单时间', formatDateTime(order.order_time), false],
    ['支付时间', formatDateTime(order.payment_time), true],
    ['订单状态', `${meta.label}（${order.order_status || 'ACTIVE'}）`, true],
    ['退款标记', order.is_refunded === '是' ? '是' : '否', true],
  ];
  rows.forEach(([label, value, wide]) => {
    const row = element('div', `detail-row${wide ? ' detail-wide' : ''}`);
    row.append(element('span', '', label), element('strong', '', value == null || value === '' ? '—' : String(value)));
    grid.append(row);
  });
  container.append(grid);
  const dialog = $('#order-dialog');
  if (!dialog.open) {
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
  }
  $('#close-order-dialog').focus();
}

/* ───────────── 运行监控 ───────────── */

async function loadMonitor() {
  if (!canViewMonitor()) return;
  if (state.monitorLoading) return;
  state.monitorLoading = true;
  const servicesContainer = $('#monitor-services');
  const healthContainer = $('#monitor-health');
  const metricsContainer = $('#monitor-metrics');
  state.monitorServices = null;
  state.monitorHealth = null;
  setLoading(servicesContainer, '正在检查服务…');
  setLoading(healthContainer, '正在检查组件健康…');
  setLoading(metricsContainer, '正在读取请求与缓存指标…');

  // 三项检查互不依赖。外部 BI/AI 探针最长需要 5 秒，
  // 不能把本机数据库健康与请求指标一起拖成 5 秒空白。
  const servicesOutcome = apiFetch('/api/monitor/services-status')
    .then((data) => {
      state.monitorServices = data;
      renderServices(state.monitorServices, state.monitorHealth);
      return data;
    })
    .catch((error) => {
      handleLoadError(error, servicesContainer, loadMonitor);
      throw error;
    });
  const healthOutcome = apiFetch('/api/monitor/health/detailed')
    .then((data) => {
      state.monitorHealth = data;
      renderHealth(data);
      renderServices(state.monitorServices, state.monitorHealth);
      markDataFresh('监控状态');
      return data;
    })
    .catch((error) => {
      handleLoadError(error, healthContainer, loadMonitor);
      throw error;
    });
  const metricsOutcome = apiFetch('/api/monitor/metrics')
    .then((data) => {
      renderMetrics(data);
      return data;
    })
    .catch((error) => {
      handleLoadError(error, metricsContainer, loadMonitor);
      throw error;
    });

  await Promise.allSettled([servicesOutcome, healthOutcome, metricsOutcome]);
  state.monitorLoaded = true;
  state.monitorLoading = false;
}

function renderServices(services, health) {
  const container = $('#monitor-services');
  const labels = { bi_dashboard: 'BI 看板', ai_assistant: 'AI 助手' };
  const cards = [];

  const backendOk = health?.status === 'healthy';
  cards.push({
    name: 'FastAPI 后端',
    detail: health ? `整体状态 ${backendOk ? 'healthy' : 'degraded'}` : '健康接口未返回',
    status: backendOk ? 'ok' : (health ? 'error' : 'disabled'),
  });

  if (services) {
    for (const [key, value] of Object.entries(services)) {
      const ok = value?.status === 'ok';
      cards.push({
        name: labels[key] || key,
        detail: ok
          ? `HTTP ${value.code ?? 200}`
          : (value?.detail || (value?.code ? `HTTP ${value.code}` : '探针失败，服务可能未启动')),
        status: ok ? 'ok' : 'error',
      });
    }
  } else {
    cards.push({ name: '外部服务', detail: '正在检查 BI 与 AI 探针…', status: 'disabled' });
  }

  container.replaceChildren();
  cards.forEach((card) => {
    const box = element('div', 'service-card');
    const text = element('div');
    text.append(element('strong', '', card.name), element('small', '', card.detail));
    const dot = element('i', `status-dot ${card.status}`);
    box.append(text, dot);
    container.append(box);
  });
}

function renderHealth(health) {
  const container = $('#monitor-health');
  setText($('#monitor-health-time'), `检查于 ${formatDateTime(health.timestamp)}`);
  const list = element('div', 'metric-list');
  const checks = health.checks || {};
  const labels = { database: '数据库', cache: '缓存', redis: 'Redis' };
  const values = Object.entries(checks);
  if (values.length === 0) {
    setEmpty(container, '健康接口没有返回检查项');
    return;
  }
  values.forEach(([key, check]) => {
    const row = element('div', 'metric-row');
    const label = element('span', '', labels[key] || key);
    const status = String(check?.status || 'unknown');
    const tone = status === 'ok' ? 'ok' : (status === 'disabled' ? '' : 'error');
    const value = element('span', `pill ${tone}`.trim(), status);
    row.append(label, value);
    list.append(row);
  });
  const overall = element('div', 'metric-row');
  overall.append(
    element('span', '', '整体状态'),
    element('strong', health.status === 'healthy' ? '' : 'error', health.status || 'unknown'),
  );
  list.append(overall);
  container.replaceChildren(list);
}

function renderMetrics(metrics) {
  const container = $('#monitor-metrics');
  const list = element('div', 'metric-list');
  const server = metrics.server || {};
  const requests = metrics.requests || {};
  const cache = metrics.cache || {};
  const rows = [
    ['运行时长', `${formatInteger(Math.floor((server.uptime_seconds || 0) / 60))} 分钟`],
    ['请求总数', formatInteger(requests.total)],
    ['成功请求', formatInteger(requests.success)],
    ['失败请求', formatInteger(requests.error)],
    ['错误率', formatPercent(requests.error_rate)],
    ['缓存后端', cache.backend || '—'],
    ['缓存键数量', formatInteger(cache.keys)],
    ['缓存命中', formatInteger(cache.hits)],
    ['正在重建', formatInteger(cache.rebuild_inflight)],
  ];
  rows.forEach(([label, value]) => {
    const row = element('div', 'metric-row');
    row.append(element('span', '', label), element('strong', '', value));
    list.append(row);
  });
  const top = requests.top_endpoints || [];
  if (top.length > 0) {
    list.append(element('div', 'metric-row', '热门接口（当前 worker）'));
    top.slice(0, 4).forEach((item) => {
      const row = element('div', 'metric-row');
      row.append(element('span', '', item.path), element('strong', '', `${formatInteger(item.count)} 次 · ${toNumber(item.avg_ms) ?? 0} ms`));
      list.append(row);
    });
  }
  container.replaceChildren(list);
}

/* ───────────── AI 数据问答 ───────────── */

function ensureAssistantReady() {
  if (!state.ai.ready) {
    state.ai.ready = initAssistant().catch((error) => {
      state.ai.ready = null;
      throw error;
    });
  }
  return state.ai.ready;
}

async function initAssistant() {
  const configContainer = $('#ai-config');
  if (!canUseAi()) {
    state.ai.enabled = false;
    state.ai.reason = '当前账号没有 ai:query 权限';
    configContainer.replaceChildren(element('span', 'status-badge error', '无 AI 查询权限'));
    setAiDisabled(true);
    return;
  }
  if (state.ai.configLoaded) return;
  try {
    const config = await apiFetch('/api/ai/config');
    state.ai.configLoaded = true;
    state.ai.config = config;
    const mode = config.mode === 'managed' ? '平台托管模型' : 'BYOK 个人模型';
    const configured = Boolean(config.configured);
    const databaseOk = Boolean(config.database?.ok);
    const ragOk = Boolean(config.rag?.ok);
    const managedReady = config.mode === 'managed' && configured;
    const byok = config.mode !== 'managed';
    state.ai.enabled = managedReady;
    state.ai.reason = byok
      ? 'BYOK 模式需要在完整 AI 助手中填写个人模型密钥后使用'
      : (!configured ? '平台模型尚未配置，请到完整 AI 助手查看配置说明' : '');
    setAiDisabled(!state.ai.enabled);

    configContainer.replaceChildren();
    configContainer.append(
      element('span', `status-badge ${managedReady ? 'ok' : 'warn'}`, managedReady ? 'AI 已就绪' : 'AI 待配置'),
      element('span', 'status-badge', mode),
      element('span', `status-badge ${databaseOk ? 'ok' : 'error'}`, databaseOk ? '数据库可用' : '数据库不可用'),
      element('span', `status-badge ${ragOk ? 'ok' : 'warn'}`, ragOk ? `RAG ${formatInteger(config.rag?.count)} 段` : 'RAG 不可用'),
    );
    if (config.mode === 'managed' && config.model) {
      configContainer.append(element('span', 'status-badge', `模型 ${config.model}`));
    }
    const limits = config.limits || {};
    configContainer.append(
      element('span', 'status-badge', `每日 ${formatInteger(limits.daily_request_limit)} 次 · 并发 ${formatInteger(limits.max_inflight_per_user)} · 超时 ${formatInteger(limits.query_timeout_seconds)} 秒`),
      element('span', 'config-note', `当前账号 ${config.principal?.name || state.me?.username || ''}`),
    );
    if (!managedReady) {
      const note = element('span', 'config-note');
      note.append(element('span', '', `${state.ai.reason}。`));
      const link = document.createElement('a');
      link.href = '/ai/';
      link.textContent = '前往完整 AI 助手';
      note.append(link);
      configContainer.append(note);
    }
    setText($('#side-ai-status'), managedReady ? '模型与数据状态正常' : '需要完成模型配置');
  } catch (error) {
    configContainer.replaceChildren(element('span', 'status-badge error', `配置读取失败：${describeError(error)}`));
    const retry = element('button', 'mini-btn', '重试配置');
    retry.type = 'button';
    retry.addEventListener('click', () => {
      state.ai.configLoaded = false;
      state.ai.ready = null;
      ensureAssistantReady();
    });
    configContainer.append(retry);
    state.ai.enabled = false;
    state.ai.reason = 'AI 配置读取失败';
    setAiDisabled(true);
    setText($('#side-ai-status'), 'AI 状态读取失败');
  }
}

function setAiDisabled(disabled) {
  $('#chat-input').disabled = disabled;
  $('#chat-form button[data-ai-submit]').disabled = disabled;
}

async function sendQuestion(question) {
  const text = String(question || '').trim();
  if (!text) return;
  if (!state.ai.enabled) {
    toast(state.ai.reason || 'AI 问答当前不可用');
    return;
  }
  if (state.ai.busy) {
    toast('上一个问题仍在分析，请稍候');
    return;
  }

  state.ai.busy = true;
  state.ai.lastQuestion = text;
  $('#regenerate').removeAttribute('hidden');
  updateAiBusy();
  clearEvidence();
  const chat = $('#chat-space');
  chat.append(buildBubble('you', text));
  const bubble = buildBubble('ai pending', '正在分析你的问题…');
  chat.append(bubble);
  scrollChat();

  const controller = new AbortController();
  state.ai.controller = controller;
  let streamed = '';
  try {
    const response = await streamAI({
      query: text,
      threadId: state.ai.threadId,
      signal: controller.signal,
      onEvent: (event) => {
        if (event.type === 'start') {
          state.ai.threadId = event.thread_id || state.ai.threadId;
          setText($('#ai-usage'), `会话 ${String(event.thread_id || '').slice(0, 8)} · 正在分析`);
        }
        if (event.type === 'step') appendStep(event.step);
        if (event.type === 'token') {
          streamed += event.delta || '';
          bubble.textContent = streamed;
          scrollChat();
        }
      },
    });
    state.ai.threadId = response.thread_id || state.ai.threadId;
    bubble.classList.remove('pending');
    bubble.textContent = response.answer || streamed || '本次查询没有生成文字回答。';
    renderAiFinal(response);
    scrollChat();
  } catch (error) {
    bubble.classList.remove('pending');
    bubble.classList.add('error');
    if (controller.signal.aborted) {
      bubble.textContent = '已取消本次查询。';
    } else {
      bubble.textContent = `查询失败：${describeError(error)}`;
      setText($('#ai-usage'), '查询失败');
      if (error instanceof ApiError && error.code === 'AI_MODEL_CONFIG_INVALID') {
        appendConfigHint(bubble);
      }
    }
  } finally {
    state.ai.busy = false;
    state.ai.controller = null;
    updateAiBusy();
    scrollChat();
  }
}

function appendConfigHint(bubble) {
  bubble.append(element('br'));
  const link = document.createElement('a');
  link.href = '/ai/';
  link.textContent = '打开完整 AI 助手检查模型配置';
  bubble.append(link);
}

function updateAiBusy() {
  const busy = state.ai.busy;
  $('#chat-input').disabled = busy || !state.ai.enabled;
  $('#chat-form button[data-ai-submit]').disabled = busy || !state.ai.enabled;
  $('#clear-chat').disabled = false;
}

function buildBubble(role, text) {
  return element('div', `chat-bubble ${role}`, text);
}

function scrollChat() {
  const chat = $('#chat-space');
  chat.scrollTop = chat.scrollHeight;
}

function clearEvidence() {
  $('#ai-steps').replaceChildren(element('div', 'empty-compact', '提问后显示节点动作与耗时'));
  $('#ai-sources').replaceChildren(element('div', 'empty-compact', '暂无来源'));
  $('#ai-sql').replaceChildren();
  $('#ai-analysis').replaceChildren();
  $('#ai-result-panel').setAttribute('hidden', '');
  $('#ai-chart').setAttribute('hidden', '');
  $('#ai-chart').replaceChildren();
  $('#ai-result').replaceChildren();
  $('#download-ai-result').disabled = true;
  state.ai.lastResponse = null;
  state.ai.resultRows = [];
}

function appendStep(step) {
  const list = $('#ai-steps');
  if (list.querySelector('.empty-compact')) list.replaceChildren();
  const item = element('div', 'step-item');
  item.append(element('i', `step-dot${step.status === 'error' ? ' error' : ''}`));
  const body = element('div', 'step-body');
  const title = element('div', 'step-title');
  title.append(element('span', '', step.name || '执行步骤'), element('small', '', `${formatInteger(step.duration_ms)} ms`));
  body.append(title, element('div', 'step-summary', step.summary || ''));
  item.append(body);
  list.append(item);
  list.scrollTop = list.scrollHeight;
  setText($('#ai-usage'), `正在分析 · ${list.children.length} 个公开步骤`);
}

function renderAiFinal(response) {
  state.ai.lastResponse = response;
  state.ai.resultRows = Array.isArray(response.result) ? response.result : [];
  const usage = response.usage || {};
  const stepCount = Array.isArray(response.steps) ? response.steps.length : 0;
  setText(
    $('#ai-usage'),
    `${formatInteger(stepCount)} 个公开步骤 · ${formatInteger(usage.latency_ms)} ms${usage.total_tokens ? ` · ${formatInteger(usage.total_tokens)} tokens` : ''}`,
  );
  renderSources(response.sources || []);
  renderSqlBlock(response);
  renderAnalysisEvidence(response.analysis_evidence || []);
  renderResult(response);
}

function renderSources(sources) {
  const container = $('#ai-sources');
  container.replaceChildren();
  if (!sources.length) {
    container.append(element('div', 'empty-compact', '本次回答没有引用知识库片段'));
    return;
  }
  sources.forEach((source) => {
    const item = element('div', 'source-item');
    const title = element('div', 'source-title');
    title.append(element('span', '', `${source.filename || '未命名来源'}${source.section ? ` · ${source.section}` : ''}`));
    const score = toNumber(source.score);
    if (score !== null) title.append(element('span', 'source-score', score.toFixed(2)));
    item.append(title);
    if (source.snippet) item.append(element('div', 'source-snippet', source.snippet));
    container.append(item);
  });
}

function renderSqlBlock(response) {
  const container = $('#ai-sql');
  container.replaceChildren();
  if (response.sql) {
    const details = element('details', 'details-block');
    details.append(element('summary', '', '生成的只读 SQL'));
    const pre = element('pre', 'details-pre', response.sql);
    details.append(pre);
    container.append(details);
  }
  if (response.sql_error) {
    const details = element('details', 'details-block');
    details.append(element('summary', '', 'SQL 执行错误'));
    details.append(element('pre', 'details-pre', response.sql_error));
    container.append(details);
  }
}

function renderAnalysisEvidence(evidence) {
  const container = $('#ai-analysis');
  container.replaceChildren();
  if (!evidence.length) return;
  const details = element('details', 'details-block');
  details.append(element('summary', '', `多步分析依据（${evidence.length} 项）`));
  evidence.forEach((item) => {
    const box = element('div', 'evidence-item');
    const head = element('div', 'evidence-head');
    const rows = Array.isArray(item.rows) ? item.rows.length : null;
    head.append(
      element('span', '', item.label || item.tool || '分析步骤'),
      element('span', '', rows === null ? '' : `${rows} 行结果`),
    );
    box.append(head);
    if (item.sql) box.append(element('pre', 'details-pre', item.sql));
    details.append(box);
  });
  container.append(details);
}

function renderResult(response) {
  const panel = $('#ai-result-panel');
  const rows = state.ai.resultRows;
  const chart = $('#ai-chart');
  chart.setAttribute('hidden', '');
  chart.replaceChildren();

  if (rows.length === 0) {
    if (response.sql || response.sql_error) {
      panel.removeAttribute('hidden');
      setText($('#ai-result-note'), response.sql_error ? 'SQL 未成功执行，没有结果行' : '查询已执行，但没有返回结果行');
      $('#ai-result').replaceChildren(element('div', 'state-block', '本次查询没有结构化结果'));
      $('#download-ai-result').disabled = true;
    } else {
      panel.setAttribute('hidden', '');
    }
    return;
  }

  panel.removeAttribute('hidden');
  setText($('#ai-result-note'), `共 ${formatInteger(rows.length)} 行结果 · 只读查询`);
  renderResultTable(rows);
  renderAiChart(response.visualization, rows);
  $('#download-ai-result').disabled = false;
}

function renderResultTable(rows) {
  const container = $('#ai-result');
  const columns = Object.keys(rows[0] || {});
  const table = element('table', 'data-table');
  const head = element('thead');
  const headRow = element('tr');
  columns.forEach((column) => headRow.append(element('th', '', column)));
  head.append(headRow);
  table.append(head);
  const body = element('tbody');
  rows.slice(0, 200).forEach((row) => {
    const tr = element('tr');
    columns.forEach((column) => tr.append(element('td', '', row[column] == null ? '—' : String(row[column]))));
    body.append(tr);
  });
  table.append(body);
  container.replaceChildren(table);
  if (rows.length > 200) {
    container.append(element('div', 'empty-compact', `仅展示前 200 行，共 ${rows.length} 行`));
  }
}

function renderAiChart(visualization, rows) {
  const chart = $('#ai-chart');
  if (!visualization || !Array.isArray(visualization.data) || visualization.data.length === 0) return;
  const xField = visualization.x_field || Object.keys(visualization.data[0])[0];
  const yField = visualization.y_field || Object.keys(visualization.data[0])[1];
  if (!xField || !yField) return;
  const dataset = visualization.data.map((row) => ({ label: row[xField], value: row[yField] }));
  chart.removeAttribute('hidden');
  if (visualization.chart_type === 'line') {
    const series = visualization.data.map((row) => ({
      period: String(row[xField] ?? ''),
      sales: Number(row[yField]) || 0,
    }));
    renderLineChart(chart, series, { ariaLabel: `${xField} 与 ${yField} 趋势` });
  } else if (visualization.chart_type === 'pie') {
    renderPieChart(chart, dataset, { ariaLabel: `${xField} 构成` });
  } else {
    renderBarChart(chart, dataset, { ariaLabel: `${xField} 与 ${yField} 对比` });
  }
  // 表格和图表都展示时，避免用户误以为图表数据与表格不一致。
  if (rows.length > 0) {
    chart.title = `图表基于查询结果的 ${dataset.length} 个分组`;
  }
}

function downloadAiCsv() {
  const rows = state.ai.resultRows;
  if (!rows.length) {
    toast('当前没有可下载的查询结果');
    return;
  }
  const columns = Object.keys(rows[0] || {});
  const escape = (value) => {
    let text = value == null ? '' : String(value);
    // 仅对文本做公式注入防护，避免把负号开头的合法数值当成恶意公式。
    if (typeof value === 'string' && /^[=+@-]/.test(text) && !/^-?\d+(\.\d+)?$/.test(text)) {
      text = `'${text}`;
    }
    return `"${text.replace(/"/g, '""')}"`;
  };
  const lines = [columns.map(escape).join(',')];
  rows.forEach((row) => lines.push(columns.map((column) => escape(row[column])).join(',')));
  const blob = new Blob([`\uFEFF${lines.join('\r\n')}`], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `ai-query-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.csv`;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function clearChat() {
  if (state.ai.controller) state.ai.controller.abort();
  state.ai.threadId = null;
  state.ai.lastQuestion = '';
  state.ai.resultRows = [];
  state.ai.lastResponse = null;
  state.ai.busy = false;
  updateAiBusy();
  const chat = $('#chat-space');
  chat.replaceChildren(buildBubble('ai', '新的会话已经开始。你可以继续问销售趋势、商品排名、退款变化或客户分层。'));
  clearEvidence();
  $('#regenerate').setAttribute('hidden', '');
  setText($('#ai-usage'), '尚未开始查询');
  toast('已开始新的 AI 会话');
}

async function regenerate() {
  if (state.ai.busy || !state.ai.lastQuestion) return;
  const question = state.ai.lastQuestion;
  try {
    if (state.ai.threadId) {
      await apiFetch('/api/ai/pop-last-turn', {
        method: 'POST',
        body: { thread_id: state.ai.threadId, question },
      });
    }
    const chat = $('#chat-space');
    const bubbles = $$('.chat-bubble', chat);
    let removedAnswer = false;
    for (let index = bubbles.length - 1; index >= 0; index -= 1) {
      const bubble = bubbles[index];
      if (bubble.classList.contains('you')) {
        bubble.remove();
        break;
      }
      if (!removedAnswer && bubble.classList.contains('ai')) {
        bubble.remove();
        removedAnswer = true;
      }
    }
    clearEvidence();
    await sendQuestion(question);
  } catch (error) {
    toast(`重新生成失败：${describeError(error)}`);
  }
}

/* ───────────── 事件绑定与启动 ───────────── */

function bindGlobalEvents() {
  $('#logout').addEventListener('click', async () => {
    try {
      await apiFetch('/api/auth/logout', { method: 'POST' });
    } catch {
      // 退出失败也必须清除本地浏览状态，服务端 Cookie 由 /logout 兜底清除。
    }
    window.location.assign('/login');
  });

  $('#home-ask').addEventListener('submit', (event) => {
    event.preventDefault();
    const question = $('#home-query').value.trim();
    if (!question) {
      toast('请输入要向 AI 提出的经营问题');
      return;
    }
    askQuestion(question);
  });

  $$('.quick button').forEach((button) => {
    button.addEventListener('click', () => {
      $('#home-query').value = button.dataset.q || '';
      $('#home-ask').requestSubmit();
    });
  });

  $$('[data-view]').forEach((button) => {
    button.addEventListener('click', () => switchView(button.dataset.view));
  });

  $$('[data-link]').forEach((button) => {
    button.addEventListener('click', () => switchView(button.dataset.link));
  });

  $$('.period-group button').forEach((button) => {
    button.addEventListener('click', () => {
      state.overviewPeriod = Number(button.dataset.period) || 7;
      $$('.period-group button').forEach((item) => item.classList.toggle('active', item === button));
      loadOverviewTrend();
    });
  });

  $('#sales-filter').addEventListener('submit', (event) => {
    event.preventDefault();
    loadSalesTrend();
  });

  $('#rfm-filter').addEventListener('submit', (event) => {
    event.preventDefault();
    state.rfm.referenceDate = $('#rfm-date').value;
    state.rfm.segment = null;
    $('#rfm-detail-panel').setAttribute('hidden', '');
    state.rfmLoaded = false;
    loadRfm();
  });

  $('#orders-filter').addEventListener('submit', (event) => {
    event.preventDefault();
    loadOrders(1);
  });

  $('#refresh-monitor').addEventListener('click', () => {
    state.monitorLoaded = false;
    loadMonitor();
  });

  $('#chat-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const input = $('#chat-input');
    const question = input.value.trim();
    if (!question) return;
    input.value = '';
    sendQuestion(question);
  });

  $('#clear-chat').addEventListener('click', clearChat);
  $('#regenerate').addEventListener('click', regenerate);
  $('#download-ai-result').addEventListener('click', downloadAiCsv);

  $('#close-order-dialog').addEventListener('click', () => $('#order-dialog').close());
  $('#order-dialog').addEventListener('click', (event) => {
    if (event.target === $('#order-dialog')) $('#order-dialog').close();
  });

  window.addEventListener('popstate', () => {
    const view = window.location.hash.replace('#', '');
    switchView(VIEW_TITLES[view] ? view : 'overview', { push: false });
  });
}

async function init() {
  bindGlobalEvents();
  try {
    state.me = await apiFetch('/api/auth/me');
  } catch (error) {
    if (error?.status === 401) return;
    setText($('#data-freshness'), '会话信息读取失败');
    toast(describeError(error));
  }
  applyIdentity();
  const initial = window.location.hash.replace('#', '');
  switchView(VIEW_TITLES[initial] ? initial : 'overview', { push: false });
}

document.addEventListener('DOMContentLoaded', init);
