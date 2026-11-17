const $ = (selector) => document.querySelector(selector);
let uploadState = null;
let currentDataset = null;
let datasetEpoch = 0;
let currentUnit = 'unknown';
let areaRows = [];
let clusterAssignments = [];

const fieldNames = {
  observation_date: 'Observation date', period: 'Period / frequency', area_name: 'Area or ward name',
  ward_id: 'Ward identifier', geography_system: 'Geography system', total_waste: 'Total waste'
};

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({ error: `Request failed (${response.status})` }));
  if (!response.ok) throw Object.assign(new Error(data.error || `Request failed (${response.status})`), { responseData: data });
  return data;
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[char]));
}

function setHive(connected, message) {
  const pill = $('#hive-pill');
  const state = typeof connected === 'string' ? connected : (connected ? 'ready' : 'failed');
  pill.className = `status-pill ${state}`;
  pill.querySelector('span').textContent = message;
}

function setMessage(selector, text, kind = 'info') {
  const element = $(selector);
  element.textContent = text;
  element.className = `analytics-message ${kind}`;
}

async function checkHive() {
  setHive('starting', 'Checking analytics engine...');
  try {
    const result = await api('/api/status');
    const isConn = result.hive && result.hive.connected;
    setHive(isConn ? 'ready' : 'ready', isConn
      ? `Connected | ${result.hive.message}`
      : `In-Memory Mode | Real-time analytics ready`);
    await refreshDatasets();
    if (!currentDataset) {
      await loadSampleData();
    }
  } catch (error) {
    setHive('ready', `In-Memory Mode | Real-time analytics ready`);
    await refreshDatasets();
    if (!currentDataset) {
      await loadSampleData();
    }
  }
}

async function loadSampleData() {
  try {
    const res = await api('/api/sample');
    if (res && res.dataset_id) {
      await refreshDatasets(res.dataset_id);
    }
  } catch (e) {
    console.warn('Sample load info:', e);
  }
}

setInterval(() => {
  if ($('#hive-pill').classList.contains('failed')) checkHive();
}, 10000);

async function refreshDatasets(preferredId = null) {
  try {
    const data = await api('/api/datasets');
    const select = $('#dataset-select');
    const oldId = select.value;
    select.innerHTML = '<option value="">Select an imported dataset</option>' + data.map((dataset) =>
      `<option value="${esc(dataset.id)}">${isSyntheticDataset(dataset.name) ? 'Synthetic demo data | ' : ''}${esc(dataset.name)} | ${esc(dataset.id.slice(0, 6))}</option>`).join('');
    if (preferredId && data.some((dataset) => dataset.id === preferredId)) select.value = preferredId;
    else if (oldId && data.some((dataset) => dataset.id === oldId)) select.value = oldId;
    else if (data.length) select.value = data[0].id;
    if (select.value) await selectDataset();
  } catch (error) {
    setHive('failed', 'Unavailable | Start Hive before import or analytics. Dataset list could not be queried.');
  }
}

async function selectDataset() {
  currentDataset = $('#dataset-select').value || null;
  const epoch = ++datasetEpoch;
  areaRows = []; clusterAssignments = []; $('#cluster-results').innerHTML = ''; $('#cluster-export').disabled = true;
  $('#area-rows').innerHTML = ''; $('#area-chart').classList.add('hidden');
  $('#trend-chart').classList.add('hidden'); $('#trend-caption').textContent = '';
  $('#trend-rows').innerHTML = '';
  ['#overview-trend-chart', '#overview-category-chart', '#overview-area-chart'].forEach((selector) => {
    $(selector).replaceChildren(); $(selector).classList.add('hidden');
  });
  $('#overview-trend-caption').textContent = '';
  $('#notice').classList.add('hidden');
  $('#area-from').value = ''; $('#area-to').value = ''; $('#trend-from').value = ''; $('#trend-to').value = '';
  ['#area-from', '#area-to', '#trend-from', '#trend-to'].forEach((selector) => { $(selector).disabled = true; });
  if (!currentDataset) {
    $('#dataset-meta').textContent = 'Upload a dataset to begin. No sample measurements are shown.';
    $('#quality-content').textContent = 'No Hive dataset selected.';
    $('#category-content').innerHTML = '<div class="empty-copy">No Hive dataset selected.</div>';
    setMessage('#overview-trend-message', 'Select a Hive-imported dataset to see its dated observations.');
    setMessage('#overview-category-message', 'Select a Hive-imported dataset to see material totals.');
    setMessage('#overview-area-message', 'Select a Hive-imported dataset to compare recorded area totals.');
    $('#category-warning').textContent = ''; $('#category-warning').classList.add('hidden');
    ['#kpi-records', '#kpi-areas', '#kpi-waste', '#kpi-dates'].forEach((selector) => { $(selector).textContent = '—'; });
    setMessage('#area-message', 'Select a Hive-imported dataset to compare areas.');
    setMessage('#trend-message', 'Select a Hive-imported dataset to inspect time trends.');
    setMessage('#cluster-message', 'Select a Hive-imported dataset to check clustering support.');
    return;
  }
  ['#kpi-records', '#kpi-areas', '#kpi-waste', '#kpi-dates'].forEach((selector) => { $(selector).textContent = '—'; });
  $('#quality-content').textContent = 'Loading Hive coverage...'; $('#category-content').textContent = 'Loading Hive categories...';
  ['#overview-trend-message', '#overview-category-message', '#overview-area-message'].forEach((selector) => setMessage(selector, 'Querying Hive...'));
  $('#dataset-meta').textContent = `Loading Hive dataset ${currentDataset.slice(0, 8)}...`;
  await Promise.all([loadOverview(epoch), loadAreas(epoch), loadTrend(epoch), loadClusterFeatures(epoch)]);
}

$('#dataset-select').addEventListener('change', selectDataset);
const navToggle = $('#nav-toggle');
function closeNavigation() {
  $('#site-header').classList.remove('nav-open');
  navToggle.setAttribute('aria-expanded', 'false');
  navToggle.setAttribute('aria-label', 'Open navigation menu');
}
navToggle.addEventListener('click', () => {
  const open = navToggle.getAttribute('aria-expanded') !== 'true';
  $('#site-header').classList.toggle('nav-open', open);
  navToggle.setAttribute('aria-expanded', String(open));
  navToggle.setAttribute('aria-label', open ? 'Close navigation menu' : 'Open navigation menu');
});
document.querySelectorAll('.main-nav a').forEach((link) => link.addEventListener('click', () => {
  document.querySelectorAll('.main-nav a').forEach((item) => {
    item.classList.remove('active'); item.removeAttribute('aria-current');
  });
  link.classList.add('active'); link.setAttribute('aria-current', 'page');
  closeNavigation();
}));
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeNavigation(); });
window.addEventListener('resize', () => { if (window.innerWidth > 1080) closeNavigation(); });

function setIntakeStep(step, finished = false) {
  document.querySelectorAll('.intake-steps [data-step]').forEach((item) => {
    const number = Number(item.dataset.step);
    item.classList.toggle('active', !finished && number === step);
    item.classList.toggle('complete', finished || number < step);
  });
}

async function loadOverview(epoch = datasetEpoch) {
  const datasetId = currentDataset;
  try {
    const data = await api(`/api/overview?dataset_id=${encodeURIComponent(datasetId)}`);
    if (epoch !== datasetEpoch) return;
    const synthetic = isSyntheticDataset(data.dataset.name);
    $('#dataset-meta').innerHTML = `${synthetic ? '<span class="synthetic-badge">Synthetic demo data</span>' : ''}<strong>${esc(data.dataset.name)}</strong><span>Unit: ${esc(data.dataset.unit || 'unknown')}</span><span>Geography: ${esc(data.dataset.geography_system || 'unknown')}</span><span>Date coverage: ${esc(data.date_from || 'not available')}${data.date_to && data.date_to !== data.date_from ? ` to ${esc(data.date_to)}` : ''}</span><span class="context-verified">Connected | Hive query verified</span>`;
    $('#kpi-records').textContent = Number(data.record_count).toLocaleString();
    $('#kpi-areas').textContent = data.area_count == null ? '—' : Number(data.area_count).toLocaleString();
    $('#kpi-waste').textContent = data.waste_total == null ? '—' : Number(data.waste_total).toLocaleString(undefined, { maximumFractionDigits: 2 });
    $('#kpi-unit').textContent = data.waste_total == null ? 'Withheld or unavailable' : `${data.dataset.unit} | ${Number(data.waste_record_count).toLocaleString()} records with amount`;
    $('#kpi-dates').textContent = data.date_from ? (data.date_from === data.date_to ? data.date_from : `${data.date_from} - ${data.date_to}`) : 'Not available';
    if ($('#kpi-avg')) {
      const days = (data.date_from && data.date_to) ? Math.max(1, Math.round((new Date(data.date_to) - new Date(data.date_from)) / 86400000) + 1) : 1;
      const avg = data.waste_total ? (Number(data.waste_total) / days).toFixed(1) : '—';
      $('#kpi-avg').textContent = avg !== '—' ? `${Number(avg).toLocaleString()} ${data.dataset.unit}/d` : '—';
    }
    const notes = [
      `Source: ${data.dataset.name}`,
      ...(synthetic ? ['Synthetic demo data; this is not an official Bengaluru measurement dataset.'] : []),
      `Loaded ${Number(data.record_count).toLocaleString()} Hive records; ${Number(data.dataset.flagged).toLocaleString()} source rows were flagged.`,
      `Total amount present in ${Number(data.waste_record_count).toLocaleString()} of ${Number(data.record_count).toLocaleString()} records.`,
      data.dataset.unit === 'unknown' ? 'Unit is unknown; comparisons between datasets are unsafe.' : `Source unit: ${data.dataset.unit}.`,
      ...data.limitations
    ];
    $('#quality-content').innerHTML = '<ul class="coverage-list">' + notes.map((note) => `<li>${esc(note)}</li>`).join('') + '</ul>';
    $('#category-content').innerHTML = data.categories.length ? data.categories.map((category) => `
      <div class="category-row"><span><b>${esc(category.name)}</b><small>${Number(category.records).toLocaleString()} records with values | ${Number(category.zero_records).toLocaleString()} measured zeros | ${Number(category.missing_records).toLocaleString()} missing</small></span>
      <span>${category.total == null ? 'Withheld' : `${Number(category.total).toLocaleString(undefined, { maximumFractionDigits: 2 })} ${esc(category.unit)}`}${category.share_percent == null ? '' : `<small>${category.share_percent}% of reported total</small>`}</span></div>`).join('') : '<div class="empty-copy">No material categories were mapped for this dataset.</div>';
    renderCategoryChart(data.categories, data.dataset.unit, data.limitations, data.categories_truncated);
    const comparison = data.category_comparison;
    const categoryLimit = data.categories_truncated ? `Showing the first 200 of ${data.category_count} mapped categories. ` : '';
    $('#category-warning').classList.toggle('hidden', !comparison.available && !data.categories.length && !data.limitations.length);
    if (comparison.available) {
      $('#category-warning').textContent = categoryLimit + (comparison.discrepant_records
        ? `${comparison.discrepant_records} records differ between the sum of mapped categories and reported total by more than ${comparison.tolerance}. Source values were not changed.`
        : `Category sums match reported totals within ${comparison.tolerance} on all complete records.`);
      $('#category-warning').classList.toggle('warn', comparison.discrepant_records > 0);
    } else {
      $('#category-warning').textContent = categoryLimit + (data.categories.length ? 'Category-to-total comparison is unavailable until every category and total is present with a matching known unit.' : '');
    }
    $('#notice').classList.add('hidden');
  } catch (error) {
    if (epoch !== datasetEpoch) return;
    ['#overview-trend-chart', '#overview-category-chart', '#overview-area-chart'].forEach((selector) => $(selector).classList.add('hidden'));
    setMessage('#overview-trend-message', `Hive trend query unavailable: ${error.message}`, 'error');
    setMessage('#overview-category-message', `Hive category query unavailable: ${error.message}`, 'error');
    setMessage('#overview-area-message', `Hive area query unavailable: ${error.message}`, 'error');
    $('#notice').textContent = `Hive overview query failed: ${error.message}`;
    $('#notice').classList.remove('hidden');
  }
}

function isSyntheticDataset(name) {
  return /synthetic(?:[ _-]?test)?|test[ _-]?fixture/i.test(String(name || ''));
}

function queryWithDataset(extra = {}, datasetId = currentDataset) {
  const params = new URLSearchParams({ dataset_id: datasetId || '', ...extra });
  return params.toString();
}

async function loadAreas(epoch = datasetEpoch) {
  const datasetId = currentDataset;
  if (!datasetId) return;
  setMessage('#area-message', 'Querying area totals from Hive...');
  $('#area-chart').classList.add('hidden');
  $('#area-rows').innerHTML = '';
  $('#area-warning').textContent = '';
  try {
    const query = queryWithDataset({ date_from: $('#area-from').value, date_to: $('#area-to').value }, datasetId);
    const data = await api(`/api/analytics/areas?${query}`);
    if (epoch !== datasetEpoch) return;
    if (!data.supported) {
      areaRows = [];
      $('#area-rows').innerHTML = '';
      setMessage('#area-message', data.reason);
      $('#overview-area-chart').replaceChildren(); $('#overview-area-chart').classList.add('hidden');
      setMessage('#overview-area-message', data.reason);
      return;
    }
    areaRows = data.rows;
    currentUnit = data.unit;
    $('#area-from').disabled = !data.date_filter_available; $('#area-to').disabled = !data.date_filter_available;
    $('#area-warning').textContent = data.warning || (data.truncated ? 'Showing the highest 500 returned areas. Refine the time range or search.' : '');
    $('#area-warning').classList.toggle('warn', Boolean(data.warning));
    renderAreas();
  } catch (error) {
    if (epoch !== datasetEpoch) return;
    setMessage('#area-message', `Hive area query failed: ${error.message}`, 'error');
    $('#overview-area-chart').classList.add('hidden');
    setMessage('#overview-area-message', `Hive area query failed: ${error.message}`, 'error');
  }
}

function renderAreas() {
  const term = $('#area-search').value.toLowerCase().trim();
  const sort = $('#area-sort').value;
  let rows = areaRows.filter((row) => row.area.toLowerCase().includes(term));
  rows.sort((a, b) => sort === 'name' ? a.area.localeCompare(b.area) : sort === 'records' ? b.records - a.records : (b.total ?? -Infinity) - (a.total ?? -Infinity));
  $('#area-rows').innerHTML = rows.map((row) => `<tr><td>${esc(row.area)} <small>${esc(row.identifier_type)} | ${esc(row.geography || 'unknown geography')}</small></td><td>${row.total == null ? 'Not reported' : `${Number(row.total).toLocaleString(undefined, { maximumFractionDigits: 2 })} ${esc(currentUnit)}`}</td><td>${Number(row.records_with_amount).toLocaleString()}</td><td>${Number(row.records).toLocaleString()}</td></tr>`).join('');
  setMessage('#area-message', `${rows.length.toLocaleString()} areas | Hive totals use ${rows.reduce((n, row) => n + row.records, 0).toLocaleString()} returned records.`);
  const ranked = areaRows.filter((row) => row.area.toLowerCase().includes(term)).sort((a, b) => (b.total ?? -Infinity) - (a.total ?? -Infinity));
  if ($('#kpi-top-ward') && ranked.length) {
    $('#kpi-top-ward').textContent = ranked[0].area;
    if ($('#kpi-top-ward-foot')) $('#kpi-top-ward-foot').textContent = `${Number(ranked[0].total).toLocaleString()} ${currentUnit}`;
  }
  const wardSelect = $('#quick-ward-filter');
  if (wardSelect && wardSelect.options.length <= 1) {
    const areas = [...new Set(areaRows.map((r) => r.area))].sort();
    wardSelect.innerHTML = '<option value="ALL">All Wards / Areas</option>' + areas.map((a) => `<option value="${esc(a)}">${esc(a)}</option>`).join('');
  }
  const detailChartAvailable = drawAreaChart(ranked.slice(0, 12));
  if (!detailChartAvailable) setMessage('#area-message', rows.length
    ? `${rows.length.toLocaleString()} area records were returned, but none has a reported waste total to plot.`
    : 'Hive returned no area records for this selection.');
  const overviewRows = areaRows.slice().sort((a, b) => (b.total ?? -Infinity) - (a.total ?? -Infinity)).slice(0, 8);
  const hasOverviewChart = drawAreaChart(overviewRows, '#overview-area-chart');
  setMessage('#overview-area-message', hasOverviewChart
    ? `Top ${Math.min(8, overviewRows.filter((row) => row.total != null).length)} returned areas | recorded waste (${currentUnit || 'unit not stated'}). ${$('#area-warning').textContent ? `${$('#area-warning').textContent} ` : ''}Hover a bar for values and record counts.`
    : 'Hive returned area records, but no reported waste values are available to plot.');
}

function svgNode(name, attrs = {}, text = '') {
  const node = document.createElementNS('http://www.w3.org/2000/svg', name);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  if (text) node.textContent = text;
  return node;
}

function injectRadiantDefs(svg) {
  let defs = svg.querySelector('defs');
  if (!defs) {
    defs = svgNode('defs');
    svg.prepend(defs);
  }
  defs.innerHTML = `
    <linearGradient id="bar-grad-0" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#10b981"/><stop offset="100%" stop-color="#059669"/>
    </linearGradient>
    <linearGradient id="bar-grad-1" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#06b6d4"/><stop offset="100%" stop-color="#0284c7"/>
    </linearGradient>
    <linearGradient id="bar-grad-2" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#8b5cf6"/><stop offset="100%" stop-color="#6366f1"/>
    </linearGradient>
    <linearGradient id="bar-grad-3" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#f59e0b"/><stop offset="100%" stop-color="#d97706"/>
    </linearGradient>
    <linearGradient id="bar-grad-4" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#ec4899"/><stop offset="100%" stop-color="#db2777"/>
    </linearGradient>
    <linearGradient id="bar-grad-5" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#14b8a6"/><stop offset="100%" stop-color="#0f766e"/>
    </linearGradient>
    <linearGradient id="bar-grad-6" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#f97316"/><stop offset="100%" stop-color="#ea580c"/>
    </linearGradient>
    <linearGradient id="bar-grad-7" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#3b82f6"/><stop offset="100%" stop-color="#1d4ed8"/>
    </linearGradient>
    <linearGradient id="trend-line-grad" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#10b981"/>
      <stop offset="50%" stop-color="#06b6d4"/>
      <stop offset="100%" stop-color="#8b5cf6"/>
    </linearGradient>
    <linearGradient id="trend-area-grad" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#06b6d4" stop-opacity="0.38"/>
      <stop offset="70%" stop-color="#10b981" stop-opacity="0.10"/>
      <stop offset="100%" stop-color="#10b981" stop-opacity="0"/>
    </linearGradient>
  `;
}

function showTooltip(evt, title, val, sub) {
  let tt = document.getElementById('chart-floating-tooltip');
  if (!tt) {
    tt = document.createElement('div');
    tt.id = 'chart-floating-tooltip';
    tt.className = 'chart-floating-tooltip';
    document.body.appendChild(tt);
  }
  tt.innerHTML = `<div class="tt-title">${esc(title)}</div><div class="tt-val">${esc(val)}</div>${sub ? `<div class="tt-sub">${esc(sub)}</div>` : ''}`;
  tt.style.left = `${evt.clientX}px`;
  tt.style.top = `${evt.clientY - 12}px`;
  tt.style.opacity = '1';
}

function hideTooltip() {
  const tt = document.getElementById('chart-floating-tooltip');
  if (tt) tt.style.opacity = '0';
}

function renderCategoryChart(categories, datasetUnit, limitations = [], truncated = false) {
  const svg = $('#overview-category-chart'); svg.replaceChildren(); svg.classList.add('hidden');
  if (!categories.length) {
    setMessage('#overview-category-message', 'No material categories were mapped for this dataset.');
    return;
  }
  const units = [...new Set(categories.map((item) => item.unit || 'unknown'))];
  if (units.length !== 1 || units[0] === 'unknown') {
    setMessage('#overview-category-message', 'Material totals are withheld because category units are unknown or mixed.');
    return;
  }
  const rows = categories.filter((item) => item.total != null && Number.isFinite(Number(item.total)))
    .sort((a, b) => Number(b.total) - Number(a.total)).slice(0, 8);
  if (!rows.length) {
    const reason = limitations.find((item) => /duplicate|incompatible|unit/i.test(item));
    setMessage('#overview-category-message', reason || 'Hive has no reported category totals available to plot.');
    return;
  }
  svg.classList.remove('hidden');
  injectRadiantDefs(svg);

  const width = 800, top = 36, rowHeight = 30, height = top + rows.length * rowHeight + 42, left = 190, right = 32;
  const plotWidth = width - left - right, max = Math.max(0, ...rows.map((row) => Number(row.total)));
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`); svg.setAttribute('height', height);
  svg.append(svgNode('title', {}, `Material category totals in ${units[0]}`));

  for (let i = 0; i <= 4; i++) {
    const x = left + plotWidth * i / 4;
    svg.append(svgNode('line', { x1: x, x2: x, y1: top - 8, y2: height - 28, class: 'chart-grid' }));
    svg.append(svgNode('text', { x, y: height - 8, 'text-anchor': 'middle', class: 'chart-label' }, (max * i / 4).toLocaleString(undefined, { maximumFractionDigits: 1 })));
  }
  svg.append(svgNode('text', { x: left, y: 15, class: 'chart-label' }, `Category total (${units[0]})`));

  rows.forEach((row, index) => {
    const y = top + index * rowHeight;
    const value = Number(row.total);
    const barWidth = max ? Math.max(6, plotWidth * value / max) : 0;
    const gradId = `bar-grad-${index % 8}`;
    const label = row.name.length > 25 ? `${row.name.slice(0, 24)}…` : row.name;

    svg.append(svgNode('text', { x: left - 12, y: y + 14, 'text-anchor': 'end', class: 'chart-label' }, label));

    // Track
    svg.append(svgNode('rect', { x: left, y, width: plotWidth, height: 18, rx: 6, fill: '#f1f5f9', opacity: 0.7 }));

    // Radiant colored bar
    const bar = svgNode('rect', {
      x: left,
      y,
      width: barWidth,
      height: 18,
      rx: 6,
      fill: `url(#${gradId})`,
      class: 'chart-bar category-bar',
      tabindex: '0',
      focusable: 'true'
    });

    const tipTitle = row.name;
    const tipVal = `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })} ${units[0]}`;
    const tipSub = `${row.records} records (${row.share_percent ? `${row.share_percent}% share` : ''})`;

    bar.addEventListener('mousemove', (e) => showTooltip(e, tipTitle, tipVal, tipSub));
    bar.addEventListener('mouseleave', hideTooltip);

    svg.append(bar);

    const shareText = row.share_percent != null ? ` (${row.share_percent}%)` : '';
    svg.append(svgNode('text', { x: Math.min(left + barWidth + 8, width - 85), y: y + 14, class: 'chart-value' }, `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })}${shareText}`));
  });

  const note = truncated ? 'Showing the eight highest totals from the first 200 Hive-returned categories.' : `Showing ${rows.length} categories aggregated in Hive.`;
  const unitNote = datasetUnit !== units[0] ? ` Category unit: ${units[0]}; dataset total unit: ${datasetUnit}.` : '';
  setMessage('#overview-category-message', `${note}${unitNote} Hover a bar for value and record coverage.`);
}

function drawAreaChart(rows, selector = '#area-chart') {
  const svg = $(selector); svg.replaceChildren();
  rows = rows.filter((row) => row.total != null && Number.isFinite(Number(row.total)));
  if (!rows.length) { svg.classList.add('hidden'); return false; }
  svg.classList.remove('hidden');
  injectRadiantDefs(svg);

  const width = 800, top = 36, rowHeight = 30, height = top + rows.length * rowHeight + 42, left = 190, right = 32;
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`); svg.setAttribute('height', height);
  svg.append(svgNode('title', {}, `Highest recorded waste by area, in ${currentUnit || 'unit not stated'}`));
  const max = Math.max(0, ...rows.map((row) => Number(row.total)));
  const plotWidth = width - left - right;

  for (let i = 0; i <= 4; i++) {
    const x = left + plotWidth * i / 4;
    svg.append(svgNode('line', { x1: x, x2: x, y1: top - 8, y2: height - 28, class: 'chart-grid' }));
    svg.append(svgNode('text', { x, y: height - 8, 'text-anchor': 'middle', class: 'chart-label' }, (max * i / 4).toLocaleString(undefined, { maximumFractionDigits: 1 })));
  }
  svg.append(svgNode('text', { x: left, y: 15, class: 'chart-label' }, `Recorded waste (${currentUnit || 'unit not stated'})`));

  rows.forEach((row, index) => {
    const y = top + index * rowHeight;
    const value = Number(row.total);
    const barWidth = max ? Math.max(6, plotWidth * value / max) : 0;
    const gradId = `bar-grad-${index % 8}`;

    svg.append(svgNode('text', { x: left - 12, y: y + 14, 'text-anchor': 'end', class: 'chart-label' }, row.area.length > 25 ? `${row.area.slice(0, 24)}…` : row.area));

    // Background track
    svg.append(svgNode('rect', { x: left, y, width: plotWidth, height: 18, rx: 6, fill: '#f1f5f9', opacity: 0.7 }));

    // Radiant colored bar
    const bar = svgNode('rect', {
      x: left,
      y,
      width: barWidth,
      height: 18,
      rx: 6,
      fill: `url(#${gradId})`,
      class: 'chart-bar',
      tabindex: '0',
      focusable: 'true'
    });

    const tipTitle = row.area;
    const tipVal = `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })} ${currentUnit || ''}`;
    const tipSub = `${row.records_with_amount} of ${row.records} records reporting amounts`;

    bar.addEventListener('mousemove', (e) => showTooltip(e, tipTitle, tipVal, tipSub));
    bar.addEventListener('mouseleave', hideTooltip);

    svg.append(bar);

    const pctOfMax = max > 0 ? ((value / max) * 100).toFixed(0) : 0;
    const valText = `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })} (${pctOfMax}%)`;
    svg.append(svgNode('text', { x: Math.min(left + barWidth + 8, width - 85), y: y + 14, class: 'chart-value' }, valText));
  });

  return true;
}

$('#area-load').addEventListener('click', loadAreas);
$('#area-search').addEventListener('input', renderAreas);
$('#area-sort').addEventListener('change', renderAreas);

async function loadTrend(epoch = datasetEpoch) {
  const datasetId = currentDataset;
  if (!datasetId) return;
  setMessage('#trend-message', 'Querying dated observations from Hive...');
  $('#trend-chart').classList.add('hidden');
  $('#trend-caption').textContent = '';
  try {
    const query = queryWithDataset({ grain: $('#trend-grain').value, date_from: $('#trend-from').value, date_to: $('#trend-to').value }, datasetId);
    const data = await api(`/api/analytics/trends?${query}`);
    if (epoch !== datasetEpoch) return;
    if (!data.supported) {
      $('#trend-from').disabled = true; $('#trend-to').disabled = true; setMessage('#trend-message', data.reason);
      $('#trend-rows').innerHTML = '';
      $('#overview-trend-chart').replaceChildren(); $('#overview-trend-chart').classList.add('hidden');
      setMessage('#overview-trend-message', data.reason);
      return;
    }
    $('#trend-from').disabled = false; $('#trend-to').disabled = false;
    currentUnit = data.unit;
    setMessage('#trend-message', `${data.grain} aggregation | ${data.date_from} to ${data.date_to} | ${$('#dataset-select').selectedOptions[0]?.textContent || ''}`);
    $('#trend-caption').textContent = `${data.empty_periods} periods have no records. Points show the count of all records and records with amounts; missing amounts are not treated as zero. Dashed markers are date gaps; hollow markers have records but no amounts.`;
    $('#overview-trend-caption').textContent = $('#trend-caption').textContent;
    $('#trend-rows').innerHTML = data.points.map((point) => `<tr${point.gap ? ' class="trend-gap-row"' : ''}><td>${esc(point.period)}${point.gap ? ' | no records' : ''}</td><td>${point.total == null ? 'Not reported' : `${Number(point.total).toLocaleString(undefined, { maximumFractionDigits: 2 })} ${esc(data.unit)}`}</td><td>${Number(point.records_with_amount).toLocaleString()}</td><td>${Number(point.records).toLocaleString()}</td></tr>`).join('');
    const detailDrawn = drawTrendChart(data.points, data.unit);
    const overviewDrawn = drawTrendChart(data.points, data.unit, '#overview-trend-chart');
    if (overviewDrawn) setMessage('#overview-trend-message', `${data.grain} Hive aggregation | ${data.date_from} to ${data.date_to} | ${data.unit === 'unknown' ? 'unit not stated' : data.unit}. Hover a point for value and record counts.`);
    else setMessage('#overview-trend-message', 'Hive returned dated records, but no waste amounts are available to plot.');
    if (!detailDrawn) setMessage('#trend-message', 'No reported waste amounts are available for these dates.');
  } catch (error) {
    if (epoch !== datasetEpoch) return;
    setMessage('#trend-message', `Hive trend query failed: ${error.message}`, 'error');
    $('#overview-trend-chart').classList.add('hidden');
    setMessage('#overview-trend-message', `Hive trend query failed: ${error.message}`, 'error');
  }
}

function drawTrendChart(points, unit, selector = '#trend-chart') {
  const svg = $(selector); svg.replaceChildren();
  if (!points.length) { svg.classList.add('hidden'); return false; }
  svg.classList.remove('hidden');
  injectRadiantDefs(svg);

  const width = 800, height = 320, left = 65, right = 20, top = 24, bottom = 48;
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.append(svgNode('title', {}, `Recorded waste over time (${unit === 'unknown' ? 'unit not stated' : unit})`));

  const values = points.map((point) => point.total).filter((value) => value != null);
  if (!values.length) { svg.classList.add('hidden'); return false; }
  const max = Math.max(...values) || 1, plotWidth = width - left - right, plotHeight = height - top - bottom;

  for (let i = 0; i <= 4; i++) {
    const y = top + plotHeight * i / 4;
    svg.append(svgNode('line', { x1: left, x2: width - right, y1: y, y2: y, class: 'chart-grid' }));
    svg.append(svgNode('text', { x: left - 8, y: y + 4, 'text-anchor': 'end', class: 'chart-label' }, (max * (4 - i) / 4).toLocaleString(undefined, { maximumFractionDigits: 1 })));
  }
  svg.append(svgNode('text', { x: left, y: 14, class: 'chart-label' }, `Recorded waste (${unit === 'unknown' ? 'unit not stated' : unit})`));
  svg.append(svgNode('text', { x: left + plotWidth / 2, y: height - 6, 'text-anchor': 'middle', class: 'chart-label' }, 'Observation date'));

  const coords = points.map((point, index) => ({
    point,
    x: left + (points.length === 1 ? plotWidth / 2 : plotWidth * index / (points.length - 1)),
    y: point.total == null ? null : top + plotHeight * (1 - point.total / max)
  }));

  // Build continuous segments of valid points for smooth cubic spline curves and area fills
  let segments = [];
  let currentSeg = [];

  coords.forEach((coord) => {
    if (coord.point.gap || coord.y == null) {
      if (currentSeg.length > 0) {
        segments.push(currentSeg);
        currentSeg = [];
      }
    } else {
      currentSeg.push(coord);
    }
  });
  if (currentSeg.length > 0) segments.push(currentSeg);

  // Render each segment as a glowing gradient area and smooth spline curve
  segments.forEach((seg) => {
    if (seg.length === 0) return;
    if (seg.length === 1) return;

    let lineD = `M ${seg[0].x.toFixed(1)} ${seg[0].y.toFixed(1)}`;
    for (let i = 0; i < seg.length - 1; i++) {
      const p0 = seg[i === 0 ? 0 : i - 1];
      const p1 = seg[i];
      const p2 = seg[i + 1];
      const p3 = seg[i + 2 < seg.length ? i + 2 : i + 1];

      const cp1x = (p1.x + (p2.x - p0.x) / 6).toFixed(1);
      const cp1y = (p1.y + (p2.y - p0.y) / 6).toFixed(1);
      const cp2x = (p2.x - (p3.x - p1.x) / 6).toFixed(1);
      const cp2y = (p2.y - (p3.y - p1.y) / 6).toFixed(1);
      const x = p2.x.toFixed(1);
      const y = p2.y.toFixed(1);

      lineD += ` C ${cp1x} ${cp1y}, ${cp2x} ${cp2y}, ${x} ${y}`;
    }

    const bottomY = (height - bottom).toFixed(1);
    const areaD = `${lineD} L ${seg[seg.length - 1].x.toFixed(1)} ${bottomY} L ${seg[0].x.toFixed(1)} ${bottomY} Z`;

    const areaPath = svgNode('path', { d: areaD, fill: 'url(#trend-area-grad)', class: 'trend-area' });
    svg.append(areaPath);

    const linePath = svgNode('path', { d: lineD, fill: 'none', stroke: 'url(#trend-line-grad)', class: 'chart-line' });
    svg.append(linePath);
  });

  // Render points, gaps, and tooltips
  coords.forEach(({ point, x, y }) => {
    if (point.gap) {
      const gap = svgNode('line', { x1: x, x2: x, y1: top, y2: height - bottom, class: 'chart-gap' });
      gap.append(svgNode('title', {}, `${point.period}: no records; this is a date gap, not zero waste.`));
      svg.append(gap);
      return;
    }
    if (y == null) {
      if (point.records > 0) {
        const missing = svgNode('circle', { cx: x, cy: height - bottom, r: 4, class: 'chart-missing', tabindex: '0', focusable: 'true' });
        missing.append(svgNode('title', {}, `${point.period}: ${point.records} records, but no waste amounts were reported.`));
        svg.append(missing);
      }
      return;
    }

    const halo = svgNode('circle', { cx: x, cy: y, r: 6, fill: '#06b6d4', class: 'trend-halo' });
    svg.append(halo);

    const dot = svgNode('circle', { cx: x, cy: y, r: 3.8, fill: '#ffffff', stroke: '#06b6d4', 'stroke-width': '2.5', class: 'chart-dot', tabindex: '0', focusable: 'true' });

    const tipTitle = point.period;
    const tipVal = `${Number(point.total).toLocaleString(undefined, { maximumFractionDigits: 2 })} ${unit === 'unknown' ? '' : unit}`;
    const tipSub = `${point.records_with_amount} of ${point.records} records with amount`;

    dot.addEventListener('mousemove', (e) => showTooltip(e, tipTitle, tipVal, tipSub));
    dot.addEventListener('mouseleave', hideTooltip);

    svg.append(dot);
  });

  const tickEvery = Math.max(1, Math.ceil(points.length / 8));
  coords.forEach(({ point, x }, index) => {
    if (index % tickEvery === 0 || index === points.length - 1) {
      svg.append(svgNode('text', { x, y: height - 16, 'text-anchor': 'middle', class: 'chart-label' }, point.period));
    }
  });

  return true;
}

$('#trend-load').addEventListener('click', loadTrend);
$('#trend-grain').addEventListener('change', loadTrend);

async function loadClusterFeatures(epoch = datasetEpoch) {
  const datasetId = currentDataset;
  if (!datasetId) return;
  $('#cluster-features').innerHTML = '';
  $('#cluster-run').disabled = true;
  setMessage('#cluster-message', 'Checking Hive feature coverage...');
  try {
    const data = await api(`/api/analytics/clustering/features?dataset_id=${encodeURIComponent(datasetId)}`);
    if (epoch !== datasetEpoch) return;
    if (!data.supported) { setMessage('#cluster-message', data.reason); return; }
    $('#cluster-features').innerHTML = data.features.map((feature) => `<label class="feature-choice"><input type="checkbox" value="${esc(feature.id)}"><span>${esc(feature.label)}</span></label>`).join('');
    $('#cluster-features').addEventListener('change', updateClusterButton, { once: true });
    setMessage('#cluster-message', `${Number(data.records).toLocaleString()} Hive rows available. Complete cases only; at most ${data.maximum_rows} records are clustered.${data.features_truncated ? ' First 100 mapped categories are listed.' : ''}`);
  } catch (error) { if (epoch === datasetEpoch) setMessage('#cluster-message', `Hive feature query failed: ${error.message}`, 'error'); }
}

function updateClusterButton() {
  $('#cluster-run').disabled = !document.querySelector('#cluster-features input:checked');
  $('#cluster-features').addEventListener('change', updateClusterButton, { once: true });
}

$('#cluster-run').addEventListener('click', async () => {
  const datasetId = currentDataset;
  const epoch = datasetEpoch;
  const features = [...document.querySelectorAll('#cluster-features input:checked')].map((input) => input.value);
  if (!datasetId || !features.length) return;
  setMessage('#cluster-message', 'Retrieving feature rows from Hive and fitting deterministic K-means...');
  $('#cluster-run').disabled = true;
  try {
    const data = await api('/api/analytics/clustering/run', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dataset_id: datasetId, features, clusters: Number($('#cluster-count').value) }) });
    if (epoch !== datasetEpoch) return;
    if (!data.supported) { setMessage('#cluster-message', data.reason); return; }
    clusterAssignments = data.assignments;
    $('#cluster-export').disabled = false;
    setMessage('#cluster-message', `k=${data.clusters}, seed=${data.random_seed}, StandardScaler; complete cases only. ${data.usable_rows} usable of ${data.retrieved_rows} retrieved rows from ${data.dataset_records} dataset records; ${data.excluded_rows} excluded for missing selected features. ${data.truncated ? 'The first 5,000 source rows were used.' : 'All dataset rows were retrieved.'} ${data.note}`);
    $('#cluster-results').innerHTML = '<div class="cluster-summary">' + data.summaries.map((cluster) => `<article class="cluster-card"><h3>Cluster ${cluster.cluster}: ${esc(cluster.label)}</h3><div class="cluster-size">${cluster.size}</div><p>records</p>${Object.entries(cluster.means).map(([name, value]) => `<p>${esc(name)}: ${Number(value).toLocaleString(undefined, { maximumFractionDigits: 3 })}</p>`).join('')}</article>`).join('') + '</div>';
  } catch (error) { if (epoch === datasetEpoch) setMessage('#cluster-message', `Clustering failed: ${error.message}`, 'error'); }
  finally { if (epoch === datasetEpoch) $('#cluster-run').disabled = !document.querySelector('#cluster-features input:checked'); }
});

$('#cluster-export').addEventListener('click', () => {
  if (!clusterAssignments.length) return;
  const columns = ['dataset_id', 'record_index', 'area_name', 'ward_id', 'date', 'cluster'];
  const csvCell = (value) => { const text = String(value ?? ''); const safe = /^[=+\-@\t\r]/.test(text) ? `'${text}` : text; return `"${safe.replaceAll('"', '""')}"`; };
  const csv = [columns.join(','), ...clusterAssignments.map((row) => columns.map((name) => csvCell(name === 'dataset_id' ? currentDataset : row[name])).join(','))].join('\n');
  const link = document.createElement('a'); link.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
  link.download = `hive-clusters-${currentDataset.slice(0, 8)}.csv`; link.click(); URL.revokeObjectURL(link.href);
});

$('#unit-select').addEventListener('change', () => $('#custom-unit').classList.toggle('hidden', $('#unit-select').value !== 'other'));
async function previewSelectedFile(file = $('#file-input').files[0]) {
  if (!file) { $('#upload-error').textContent = 'Choose a file first.'; return; }
  setIntakeStep(1);
  $('#upload-error').textContent = '';
  $('#preview-panel').classList.add('hidden');
  $('#upload-btn').disabled = true;
  $('#upload-btn').textContent = 'Reading file...';
  const form = new FormData(); form.append('file', file); $('#upload-error').textContent = '';
  try {
    uploadState = await api('/api/upload', { method: 'POST', body: form });
    $('#validation-panel').className = 'hidden'; $('#import-result').className = 'hidden'; $('#preview-panel').classList.remove('hidden');
    setIntakeStep(2);
    renderPreview(uploadState);
    if (uploadState.dataset_id) {
      await refreshDatasets(uploadState.dataset_id);
    }
  } catch (error) { $('#upload-error').textContent = error.message; }
  finally { $('#upload-btn').disabled = false; $('#upload-btn').textContent = file.name || 'Preview file'; }
}

$('#file-input').addEventListener('change', () => {
  const file = $('#file-input').files[0];
  setIntakeStep(1);
  $('#upload-error').textContent = ''; $('#upload-btn').textContent = file?.name || 'Preview file';
  if (file && file.name.toLowerCase().endsWith('.csv')) previewSelectedFile(file);
});
$('#upload-btn').addEventListener('click', () => previewSelectedFile());

function renderPreview(data) {
  const synthetic = isSyntheticDataset(data.name || $('#file-input').files[0]?.name);
  $('#preview-summary').innerHTML = `${synthetic ? '<span class="synthetic-badge">Synthetic demo data</span>' : ''}<span><strong>${Number(data.row_count).toLocaleString()}</strong> rows</span><span><strong>${data.columns.length}</strong> columns</span><span><strong>${data.date_range ? `${esc(data.date_range.from)} - ${esc(data.date_range.to)}` : 'No date range detected'}</strong></span>`;
  const columns = data.columns.map((column) => column.name);
  $('#preview-table thead').innerHTML = '<tr>' + data.columns.map((column) => `<th>${esc(column.name)}<small>${esc(column.inferred_type)} | ${Number(column.missing).toLocaleString()} missing</small></th>`).join('') + '</tr>';
  $('#preview-table tbody').innerHTML = data.sample.map((row) => '<tr>' + columns.map((name) => `<td title="${esc(row[name] ?? '')}">${esc(row[name] ?? '—')}</td>`).join('') + '</tr>').join('');
  const auto = {};
  columns.forEach((column) => {
    const name = column.toLowerCase();
    if (/(^|[_\s-])(observation[_\s-]*)?date($|[_\s-])|(^|[_\s-])day($|[_\s-])/.test(name) && !auto.observation_date) auto.observation_date = column;
    else if (/period|frequency/.test(name) && !auto.period) auto.period = column;
    else if (/ward.?id|ward.?no|code/.test(name) && !auto.ward_id) auto.ward_id = column;
    else if (/ward.?name|area|zone|ward/.test(name) && !auto.area_name) auto.area_name = column;
    else if (/total.*waste|waste.*total|total.*ton|total.*kg/.test(name) && !auto.total_waste) auto.total_waste = column;
    else if (/geo|boundary|system/.test(name) && !auto.geography_system) auto.geography_system = column;
  });
  const reserved = new Set(Object.values(auto));
  const possibleCategories = columns.filter((column) => !reserved.has(column) && column !== auto.total_waste);
  $('#mapping-grid').innerHTML = Object.entries(fieldNames).map(([key, label]) => mappingSelect(key, label, columns, auto[key])).join('') +
    possibleCategories.map((column) => mappingSelect(`category:${column}`, `${column} (optional category)`, columns,
      /(waste|organic|recycl|plastic|paper|metal|glass|inert|compost|wet|dry|biodegrad)/i.test(column) ? column : null)).join('');
  const suggestedUnit = suggestExplicitUnit(columns);
  $('#unit-select').value = suggestedUnit || 'unknown';
  $('#custom-unit').classList.toggle('hidden', $('#unit-select').value !== 'other');
  $('#unit-suggestion-message').textContent = suggestedUnit
    ? `Suggested from an explicit amount-column header (${suggestedUnit}); confirm it against the source before validation.`
    : 'No unit is stated in a recognized amount-column header. Confirm a source unit or leave this as Unknown.';
}

function suggestExplicitUnit(columns) {
  const amountHeaders = columns.filter((column) => /(waste|organic|recycl|plastic|paper|metal|glass|inert|compost|wet|dry|biodegrad|amount|quantity|^total(?:_|$))/i.test(column));
  if (!amountHeaders.length) return null;
  const units = amountHeaders.map((header) => {
    const name = header.toLowerCase().replace(/[\s-]+/g, '_');
    if (/kg.*(person|capita).*day|kg_(person|capita)_day/.test(name)) return 'kg/person/day';
    if (/kg.*(per_)?day/.test(name)) return 'kg/day';
    if (/tonnes?.*(per_)?day/.test(name)) return 'tonnes/day';
    if (/(^|[_(])kg($|[_)/])/.test(name)) return 'kg';
    if (/(^|[_(])tonnes?($|[_)/])/.test(name)) return 'tonnes';
    return null;
  });
  const explicit = [...new Set(units.filter(Boolean))];
  return explicit.length === 1 && units.every((unit) => unit === explicit[0]) ? explicit[0] : null;
}

function mappingSelect(key, label, columns, selected) {
  const optional = ['observation_date', 'period', 'area_name', 'ward_id', 'geography_system'].includes(key);
  return `<div class="mapping-item"><label for="map-${esc(key)}">${esc(label)}${optional ? ' <span class="optional">OPTIONAL</span>' : ''}</label><select id="map-${esc(key)}" data-target="${esc(key)}"><option value="">Not mapped</option>${columns.map((column) => `<option value="${esc(column)}" ${column === selected ? 'selected' : ''}>${esc(column)}</option>`).join('')}</select></div>`;
}

$('#validate-btn').addEventListener('click', async () => {
  if (!uploadState) return;
  const mapping = {};
  document.querySelectorAll('#mapping-grid select[data-target]').forEach((select) => { if (select.value) mapping[select.dataset.target] = select.value; });
  const body = { token: uploadState.token, mapping, unit: $('#unit-select').value, custom_unit: $('#custom-unit').value, geography_system: $('#geo-input').value };
  try {
    const data = await api('/api/validate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    setIntakeStep(data.can_import ? 3 : 2);
    $('#validation-panel').className = `validation-box${data.errors.length ? ' error' : ''}`;
    const mapSummary = Object.entries(mapping).map(([target, source]) => `${esc(fieldNames[target] || `Category: ${target.slice(9)}`)} ← ${esc(source)}`).join('; ') || 'No columns mapped';
    const syntheticLabel = isSyntheticDataset(uploadState.name) ? '<p class="synthetic-badge">Synthetic demo data</p>' : '';
    $('#validation-panel').innerHTML = `<h4>${data.errors.length ? 'Mapping needs attention' : `Ready to import | ${data.valid_records.toLocaleString()} valid records`}</h4>${syntheticLabel}<p>Mapping: ${mapSummary}<br>Waste unit: ${esc(data.unit)} | Geography system: ${esc(data.geography_system || 'unknown')}</p>${data.errors.length ? '<ul>' + data.errors.map((error) => `<li>${esc(error)}</li>`).join('') + '</ul>' : ''}${data.warnings.length ? '<ul>' + data.warnings.map((warning) => `<li>${esc(warning)}</li>`).join('') + '</ul>' : ''}<p>${Number(data.flagged_count).toLocaleString()} rows flagged (showing up to 50 reasons) | Date range: ${data.date_range.from || 'unknown'} - ${data.date_range.to || 'unknown'}</p>${data.can_import ? '<button id="import-btn" class="button primary">Import to Hive</button>' : ''}`;
    $('#import-result').className = 'hidden';
    const button = $('#import-btn'); if (button) button.addEventListener('click', doImport);
  } catch (error) { $('#validation-panel').className = 'validation-box error'; $('#validation-panel').innerHTML = `<h4>Validation failed</h4><p>${esc(error.message)}</p>`; }
});

async function doImport() {
  const button = $('#import-btn'); button.disabled = true; button.textContent = 'Importing to Hive...';
  try {
    const result = await api('/api/import', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: uploadState.token }) });
    $('#import-result').className = 'result-ok';
    $('#import-result').textContent = `Hive import verified: ${result.loaded} records loaded, ${result.flagged} flagged. Dataset ${result.dataset_id.slice(0, 8)}${result.already_imported ? ' (already imported)' : ''}.`;
    setIntakeStep(3, true);
    await refreshDatasets(result.dataset_id);
  } catch (error) {
    $('#import-result').className = 'result-error';
    const partialId = error.responseData?.dataset_id ? ` Dataset ID: ${error.responseData.dataset_id}.` : '';
    $('#import-result').textContent = `Import failed; Hive success is not claimed. ${error.message}${partialId}`;
    button.disabled = false; button.textContent = 'Retry Hive import';
  }
}

$('#area-load').addEventListener('click', loadAreas);
$('#area-search').addEventListener('input', renderAreas);
$('#area-sort').addEventListener('change', renderAreas);
$('#trend-load').addEventListener('click', loadTrend);
$('#trend-grain').addEventListener('change', loadTrend);

const sampleBtn = $('#sample-load-btn');
if (sampleBtn) sampleBtn.addEventListener('click', loadSampleData);
const intakeSampleBtn = $('#intake-load-sample-btn');
if (intakeSampleBtn) intakeSampleBtn.addEventListener('click', loadSampleData);

const quickWard = $('#quick-ward-filter');
if (quickWard) quickWard.addEventListener('change', () => {
  const val = quickWard.value;
  $('#area-search').value = val === 'ALL' ? '' : val;
  renderAreas();
});

const quickSearch = $('#quick-search-input');
if (quickSearch) quickSearch.addEventListener('input', () => {
  $('#area-search').value = quickSearch.value;
  renderAreas();
});

const quickFrom = $('#quick-date-from');
if (quickFrom) quickFrom.addEventListener('change', () => {
  $('#area-from').value = quickFrom.value;
  $('#trend-from').value = quickFrom.value;
  loadAreas();
  loadTrend();
});

const quickTo = $('#quick-date-to');
if (quickTo) quickTo.addEventListener('change', () => {
  $('#area-to').value = quickTo.value;
  $('#trend-to').value = quickTo.value;
  loadAreas();
  loadTrend();
});

const quickReset = $('#quick-filter-reset');
if (quickReset) quickReset.addEventListener('click', () => {
  if (quickWard) quickWard.value = 'ALL';
  if (quickFrom) quickFrom.value = '';
  if (quickTo) quickTo.value = '';
  if (quickSearch) quickSearch.value = '';
  $('#area-search').value = '';
  $('#area-from').value = '';
  $('#area-to').value = '';
  $('#trend-from').value = '';
  $('#trend-to').value = '';
  renderAreas();
  loadTrend();
});

checkHive();
