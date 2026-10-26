const $ = id => document.getElementById(id);
let accessToken = location.hash.slice(1);
try {
  accessToken ||= localStorage.getItem('mts-token') || sessionStorage.getItem('mts-token') || '';
} catch {} // A launcher link still works when browser storage is blocked.
history.replaceState(null, '', location.pathname);
let lastState, lastResult, firstState = true, currentAction = '', poller;
const names = {size: 'System size L', threads: 'Threads', preconditioner: 'Preconditioner', reconnection: 'Reconnection'};
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function message(text) {$('message').textContent = text; $('message').classList.toggle('hidden', !text);}
async function api(path, data) {
  const response = await fetch('/api/' + path, {method: data === undefined ? 'GET' : 'POST', headers: {'X-MTS-Token': accessToken, 'Content-Type': 'application/json'}, ...(data === undefined ? {} : {body: JSON.stringify(data)})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'The operation failed.');
  return result;
}
function view(name) {
  document.querySelectorAll('.view').forEach(el => el.classList.toggle('active', el.id === name));
  document.querySelectorAll('.sidebar [data-view]').forEach(el => el.classList.toggle('active', el.dataset.view === name));
  message('');
}
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => view(button.dataset.view)));
$('go-jobs').addEventListener('click', () => $('jobs-card').scrollIntoView({behavior: 'smooth'}));
async function operation(action, data = {}) {
  try {
    message(''); currentAction = action; lastResult = null;
    await api(action, data); await refresh();
  } catch (error) {message(error.message);}
}
function fillSites(sites) {
  document.querySelectorAll('.sites').forEach(select => {
    const previous = select.value;
    select.replaceChildren(...sites.map(site => new Option(site.name + (site.remote ? ' · remote' : ' · this computer'), site.name)));
    if (sites.some(site => site.name === previous)) select.value = previous;
  });
}
function renderInventory(rows) {
  const body = $('jobs'); body.replaceChildren();
  if (!rows.length) {body.innerHTML = '<tr><td colspan="4" class="empty">No managed simulations were found at this site.</td></tr>'; return;}
  for (const row of rows) {
    const tr = document.createElement('tr'), status = row.status.state;
    const percentage = row.progress.fraction === null ? '—' : (100 * row.progress.fraction).toFixed(1) + '%';
    tr.innerHTML = `<td>${escape(row.folder.split('/').at(-1))}</td><td><span class="badge ${status === 'finished' ? 'ready' : ''}">${escape(status)}</span>${row.termination ? '<br>' + escape(row.termination.stopReason) : ''}</td><td>${percentage}</td><td><div class="table-actions"></div></td>`;
    if (row.attempt) {
      const logs = document.createElement('button'); logs.className = 'small'; logs.textContent = 'Logs';
      logs.onclick = () => operation('logs', {site: $('jobs-site').value, attempt: row.attempt});
      tr.querySelector('.table-actions').append(logs);
      if (['running','queued','prepared','stopping'].includes(status)) {
        const stop = document.createElement('button'); stop.className = 'small danger'; stop.textContent = 'Stop';
        stop.onclick = () => {if (confirm('Stop this simulation attempt?')) operation('stop', {site: $('jobs-site').value, attempt: row.attempt});};
        tr.querySelector('.table-actions').append(stop);
      }
    }
    body.append(tr);
  }
}
async function refresh() {
  try {
    const state = await api('state'); const previousSites = lastState?.sites; lastState = state;
    $('workspace-path').textContent = state.workspace;
    $('tools-command').textContent = state.setup_command;
    const ready = Object.values(state.packages).every(Boolean), busy = state.task.state === 'running';
    $('ready-badge').textContent = ready ? 'Installed' : 'Installation required';
    $('ready-badge').className = 'badge' + (ready ? ' ready' : '');
    const entries = [...Object.entries(state.packages), ['Local simulator', state.local_build]];
    const list = entries.map(([name, ok]) => `<div class="readiness-row"><span>${escape(name)}</span><span class="badge ${ok ? 'ready' : ''}">${ok ? 'Installed' : 'Not installed'}</span></div>`).join('');
    $('readiness-list').innerHTML = list; $('setup-readiness').innerHTML = list;
    for (const id of ['install-packages','build-simulator','run-button','plot-button','refresh-jobs']) $(id).disabled = busy;
    $('build-simulator').disabled ||= !ready;
    $('run-button').disabled ||= !ready || ($('run-site').value === 'local' && !state.local_build);
    $('plot-button').disabled ||= !ready;
    $('refresh-jobs').disabled ||= !ready;
    $('quit').disabled = busy;
    if (firstState || JSON.stringify(state.sites) !== JSON.stringify(previousSites)) fillSites(state.sites);
    if (firstState) {
      try {
        localStorage.setItem('mts-token', accessToken);
        sessionStorage.removeItem('mts-token');
      } catch {} // Remember only a token accepted by this running server.
      $('plot-data').value = state.data_folders[0] || state.workspace + '/MTSManager/output';
      $('data-folders').replaceChildren(...state.data_folders.map(path => new Option(path, path)));
      if (!ready) view('setup');
      firstState = false;
    }
    const task = state.task;
    $('task-badge').textContent = task.state;
    $('task-badge').className = 'badge ' + (task.state === 'complete' ? 'ready' : task.state);
    $('task-title').textContent = task.title || 'Log';
    $('task-log').textContent = task.logs.join('\n') || 'No output.';
    if (task.state === 'failed') $('task-result').textContent = task.error;
    if (task.state === 'complete' && lastResult !== JSON.stringify(task)) {
      lastResult = JSON.stringify(task); $('task-result').textContent = 'Operation completed.';
      if (Array.isArray(task.result)) renderInventory(task.result);
      if (task.result?.pdf) {
        const link = document.createElement('a'); link.className = 'button primary'; link.target = '_blank'; link.rel = 'noopener';
        link.href = '/figures/' + task.result.pdf.split('/').map(encodeURIComponent).join('/') + '?token=' + encodeURIComponent(accessToken);
        link.textContent = 'Open PDF'; $('plot-result').replaceChildren(link);
        $('task-result').textContent = 'Figure saved: ' + task.result.name;
      }
      if (task.result?.config_hash) $('task-result').textContent = 'Submitted.';
    }
    message('');
  } catch (error) {message('Connection failed. ' + error.message);}
}
$('install-packages').onclick = () => operation('install');
$('build-simulator').onclick = () => operation('build');
$('refresh-jobs').onclick = () => operation('inventory', {site: $('jobs-site').value});
$('run-site').onchange = () => refresh();
$('run-form').onsubmit = event => {
  event.preventDefault();
  try {
    const settings = {rows: +$('run-size').value, cols: +$('run-size').value, seed: +$('run-seed').value,
      nrThreads: +$('run-threads').value, boundary: $('run-boundary').value, totalLoad: +$('run-load').value,
      loadIncrement: +$('run-increment').value, reconnectionMethod: $('run-reconnection').value,
      initialGuessNoise: 0, writeMeshVTUs: false, writeDumps: false, nrVTUFrames: 0, ...JSON.parse($('run-extra').value)};
    operation('run', {settings, label: $('run-label').value, site: $('run-site').value});
  } catch (error) {message('Check the advanced parameter JSON: ' + error.message);}
};
function fixedFields() {
  const used = [$('plot-x').value, $('plot-color').value, $('plot-gradient').value];
  $('fixed-fields').replaceChildren();
  for (const [key, label] of Object.entries(names)) {
    if (used.includes(key)) continue;
    const field = document.createElement('div'); field.className = 'field';
    field.innerHTML = `<label for="fixed-${key}">Hold ${label.toLowerCase()} fixed</label>`;
    let input;
    if (['size', 'threads'].includes(key)) {input = document.createElement('input'); input.type = 'number'; input.min = 1; input.value = key === 'size' ? 400 : 16;}
    else {input = document.createElement('select'); const values = key === 'preconditioner' ? ['exactTangent','none'] : ['none','edgeFlip']; input.replaceChildren(...values.map(v => new Option(v, v)));}
    input.id = 'fixed-' + key; input.dataset.property = key; field.append(input); $('fixed-fields').append(field);
  }
}
document.querySelectorAll('.slot').forEach(select => select.onchange = fixedFields); fixedFields();
function limits(id) {
  if (!$(id).value.trim()) return null;
  const values = $(id).value.split(',').map(Number);
  if (values.length !== 2 || !values.every(Number.isFinite) || values[0] >= values[1]) throw new Error('Axis limits need two increasing numbers separated by a comma.');
  return values;
}
$('plot-form').onsubmit = event => {
  event.preventDefault();
  try {
    const fixed = Object.fromEntries([...$('fixed-fields').querySelectorAll('[data-property]')].map(el => [el.dataset.property, el.type === 'number' ? +el.value : el.value]));
    operation('plot', {data: $('plot-data').value, y: $('plot-y').value, x: $('plot-x').value,
      color: $('plot-color').value, gradient: $('plot-gradient').value || null, fixed,
      ncols: +$('legend-cols').value, legend_loc: $('legend-loc').value, xlim: limits('xlim'), ylim: limits('ylim')});
  } catch (error) {message(error.message);}
};
$('quit').onclick = async () => {
  try {await api('quit', {}); clearInterval(poller);
    try {localStorage.removeItem('mts-token'); sessionStorage.removeItem('mts-token');} catch {}
    document.querySelector('.workspace').innerHTML = '<div class="guide"><h1>MTS is closed.</h1><p>Use the desktop launcher to restart. Simulation jobs continue.</p></div>';}
  catch (error) {message(error.message);}
};
refresh(); poller = setInterval(refresh, 2000);
