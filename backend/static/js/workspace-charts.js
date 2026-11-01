const SVG_NS = 'http://www.w3.org/2000/svg';

function svgElement(tag, attributes = {}, text = '') {
  const element = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
  if (text) element.textContent = text;
  return element;
}

function compactNumber(value) {
  return new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 }).format(Number(value) || 0);
}

const SERIES_COLORS = ['#7d66ed', '#16a394', '#ffa56d', '#f4c62b', '#5b8def', '#e06b8b', '#8b95a5', '#43a047'];

function renderEmpty(container, message) {
  const empty = document.createElement('div');
  empty.className = 'state-block';
  empty.textContent = message;
  container.replaceChildren(empty);
}

export function renderLineChart(container, rows, { ariaLabel = '销售额趋势', emptyMessage = '当前时间范围内暂无订单数据' } = {}) {
  if (!Array.isArray(rows) || rows.length === 0) {
    renderEmpty(container, emptyMessage);
    return;
  }
  container.replaceChildren();

  const width = 720;
  const height = 260;
  const margin = { left: 58, right: 20, top: 20, bottom: 42 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const values = rows.map((row) => Number(row.sales) || 0);
  const maximum = Math.max(...values, 1);
  const roundedMaximum = maximum * 1.12;
  const points = rows.map((row, index) => ({
    x: margin.left + (rows.length === 1 ? plotWidth / 2 : index / (rows.length - 1) * plotWidth),
    y: margin.top + plotHeight - (Number(row.sales) || 0) / roundedMaximum * plotHeight,
    row,
  }));

  const svg = svgElement('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': ariaLabel });
  const defs = svgElement('defs');
  const gradient = svgElement('linearGradient', { id: `chart-shade-${Math.random().toString(36).slice(2)}`, x1: '0', y1: '0', x2: '0', y2: '1' });
  gradient.append(svgElement('stop', { offset: '0%', 'stop-color': '#8e7ff9', 'stop-opacity': '.2' }), svgElement('stop', { offset: '100%', 'stop-color': '#8e7ff9', 'stop-opacity': '0' }));
  defs.append(gradient);
  svg.append(defs);

  for (let index = 0; index <= 4; index += 1) {
    const y = margin.top + index / 4 * plotHeight;
    svg.append(svgElement('line', { x1: margin.left, x2: width - margin.right, y1: y, y2: y, class: 'chart-grid-line' }));
    const value = roundedMaximum * (1 - index / 4);
    svg.append(svgElement('text', { x: margin.left - 10, y: y + 4, 'text-anchor': 'end', class: 'chart-axis-label' }, compactNumber(value)));
  }

  const linePath = points.map((point, index) => `${index ? 'L' : 'M'}${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(' ');
  const areaPath = `${linePath} L${points.at(-1).x.toFixed(2)} ${margin.top + plotHeight} L${points[0].x.toFixed(2)} ${margin.top + plotHeight} Z`;
  svg.append(svgElement('path', { d: areaPath, fill: `url(#${gradient.id})` }));
  svg.append(svgElement('path', { d: linePath, class: 'chart-line' }));

  const labelStep = Math.max(1, Math.ceil(rows.length / 7));
  points.forEach((point, index) => {
    const circle = svgElement('circle', { cx: point.x, cy: point.y, r: rows.length > 45 ? 2.5 : 4, class: 'chart-dot' });
    const orderSuffix = point.row.order_count == null ? '' : ` · 订单 ${point.row.order_count}`;
    circle.append(svgElement('title', {}, `${point.row.period} · 销售额 ${Number(point.row.sales || 0).toFixed(2)}${orderSuffix}`));
    svg.append(circle);
    if (index % labelStep === 0 || index === rows.length - 1) {
      const label = String(point.row.period).slice(5);
      svg.append(svgElement('text', { x: point.x, y: height - 13, 'text-anchor': 'middle', class: 'chart-axis-label' }, label));
    }
  });
  container.append(svg);
}

export function renderBarChart(container, rows, {
  labelKey = 'label',
  valueKey = 'value',
  ariaLabel = '数据对比',
  emptyMessage = '暂无可对比的数据',
} = {}) {
  const visibleRows = Array.isArray(rows) ? rows.slice(0, 12) : [];
  if (visibleRows.length === 0) {
    renderEmpty(container, emptyMessage);
    return;
  }
  container.replaceChildren();

  const width = 720;
  const rowHeight = 30;
  const height = visibleRows.length * rowHeight + 24;
  const labelWidth = Math.min(190, Math.max(110, width * 0.27));
  const valueWidth = 78;
  const barMaxWidth = width - labelWidth - valueWidth - 16;
  const maximum = Math.max(...visibleRows.map((row) => Number(row[valueKey]) || 0), 1);
  const svg = svgElement('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': ariaLabel });

  visibleRows.forEach((row, index) => {
    const y = index * rowHeight + 14;
    const value = Number(row[valueKey]) || 0;
    const barWidth = Math.max(2, value / maximum * barMaxWidth);
    const label = String(row[labelKey] ?? '—').slice(0, 22);
    const bar = svgElement('rect', {
      x: labelWidth,
      y: y - 10,
      width: barWidth,
      height: 15,
      rx: 4,
      class: 'chart-bar',
    });
    // CSS 的 fill 会覆盖 SVG 呈现属性，这里用内联样式保证每根柱子按系列着色。
    bar.style.fill = SERIES_COLORS[index % SERIES_COLORS.length];
    bar.append(svgElement('title', {}, `${row[labelKey]} · ${value.toLocaleString('zh-CN')}`));
    svg.append(bar);
    svg.append(svgElement('text', { x: 0, y: y + 2, class: 'chart-category-label' }, label));
    svg.append(svgElement('text', { x: labelWidth + barWidth + 9, y: y + 2, class: 'chart-value-label' }, compactNumber(value)));
  });
  container.append(svg);
}

export function renderPieChart(container, rows, {
  labelKey = 'label',
  valueKey = 'value',
  ariaLabel = '数据构成',
  emptyMessage = '暂无可展示的构成数据',
} = {}) {
  const visibleRows = (Array.isArray(rows) ? rows : [])
    .map((row) => ({ label: String(row[labelKey] ?? '—'), value: Number(row[valueKey]) || 0 }))
    .filter((row) => row.value > 0)
    .slice(0, 8);
  if (visibleRows.length === 0) {
    renderEmpty(container, emptyMessage);
    return;
  }
  container.replaceChildren();

  const total = visibleRows.reduce((sum, row) => sum + row.value, 0);
  const stops = [];
  let cursor = 0;
  visibleRows.forEach((row, index) => {
    const start = cursor;
    cursor += row.value / total * 100;
    stops.push(`${SERIES_COLORS[index % SERIES_COLORS.length]} ${start.toFixed(2)}% ${cursor.toFixed(2)}%`);
  });

  const layout = document.createElement('div');
  layout.className = 'ai-pie-layout';
  const pie = document.createElement('div');
  pie.className = 'ai-pie';
  pie.setAttribute('role', 'img');
  pie.setAttribute('aria-label', ariaLabel);
  pie.style.background = `conic-gradient(${stops.join(', ')})`;

  const legend = document.createElement('div');
  legend.className = 'ai-pie-legend';
  visibleRows.forEach((row, index) => {
    const item = document.createElement('div');
    const dot = document.createElement('i');
    dot.style.background = SERIES_COLORS[index % SERIES_COLORS.length];
    const label = document.createElement('strong');
    label.textContent = row.label;
    label.style.fontWeight = '600';
    const value = document.createElement('span');
    value.textContent = `${compactNumber(row.value)} · ${(row.value / total * 100).toFixed(1)}%`;
    item.append(dot, label, value);
    legend.append(item);
  });
  layout.append(pie, legend);
  container.append(layout);
}
