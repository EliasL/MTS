const $ = id => document.getElementById(id);
for (const panel of ['start', 'monitor']) $(panel).append($('simulation-help').content.cloneNode(true));
let accessToken = location.hash.slice(1);
try {
  accessToken ||= localStorage.getItem('mts-token') || sessionStorage.getItem('mts-token') || '';
} catch {} // A launcher link still works when browser storage is blocked.
history.replaceState(null, '', location.pathname);
let lastState, lastResult, firstState = true, poller, inventorySnapshot, inventoryRequest = 0, restarting = false;
let backendHistory = [], clientHistory = [], lastConnectionError = '';
let pendingOperation, mismatchReported = false, hiddenBackendEntries = new Set();
let lastInventoryUpdate = '';
try {
  const saved = JSON.parse(sessionStorage.getItem('mts-terminal-history') || 'null');
  if (saved?.token === accessToken && Array.isArray(saved.entries)) clientHistory = saved.entries;
} catch {} // Terminal feedback remains available when browser storage is blocked.
const names = {size: 'System size L', threads: 'Threads', preconditioner: 'Preconditioner', reconnection: 'Reconnection'};
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const siteLabel = name => String(name).toUpperCase();
function inventorySelection() {
  const value = $('jobs-site').value, split = value.indexOf(':');
  return {[value.slice(0, split)]: value.slice(split + 1)};
}
function fillMonitorSources(campaigns = lastState?.campaigns || inventorySnapshot?.campaigns || []) {
  const select = $('jobs-site'), previous = select.value;
  const servers = document.createElement('optgroup'); servers.label = 'Servers';
  for (const site of lastState.servers || lastState.sites) servers.append(new Option(siteLabel(site.label || site.name), 'site:' + site.name));
  const group = document.createElement('optgroup'); group.label = 'Campaigns';
  for (const name of campaigns) group.append(new Option(name, 'campaign:' + name));
  select.replaceChildren(servers, group);
  if ([...select.options].some(option => option.value === previous)) select.value = previous;
  const hard = $('hard-refresh-site'), previousHard = hard.value;
  hard.replaceChildren(new Option('All servers', '*'), ...(lastState.servers || lastState.sites).map(
    site => new Option(siteLabel(site.label || site.name), site.name)));
  if ([...hard.options].some(option => option.value === previousHard)) hard.value = previousHard;
}
const actionButtons = ['install-packages','build-simulator','run-button','plot-button','refresh-jobs','hard-refresh','update-managers','ssh-check','ssh-install','ssh-add','quit','restart'];
for (const id of actionButtons) {
  const button = $(id), hint = document.createElement('span');
  hint.className = 'action-hint';
  button.before(hint); hint.append(button);
}
function terminalMessage(title, text, state = 'complete') {
  const entry = {id: 'browser-' + crypto.randomUUID(), created_at: Date.now(), title, state,
    logs: [`[${new Date().toLocaleTimeString()}] ${text}`]};
  clientHistory.push(entry); renderTerminal();
  return entry;
}
function finishMessage(entry, text, state = 'complete') {
  entry.state = state;
  entry.logs.push(`[${new Date().toLocaleTimeString()}] ${text}`);
  renderTerminal();
}
function message(text, state = 'failed') {
  if (text) terminalMessage(state === 'failed' ? 'Error' : 'MTS', text, state);
}
async function api(path, data) {
  const response = await fetch('/api/' + path, {method: data === undefined ? 'GET' : 'POST', headers: {'X-MTS-Token': accessToken, 'Content-Type': 'application/json'}, ...(data === undefined ? {} : {body: JSON.stringify(data)})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'The operation failed.');
  return result;
}
function view(name) {
  if (!$(name)?.classList.contains('view')) name = 'overview';
  document.querySelectorAll('.view').forEach(el => el.classList.toggle('active', el.id === name));
  document.querySelectorAll('.sidebar [data-view]').forEach(el => el.classList.toggle('active', el.dataset.view === name));
  try {sessionStorage.setItem('mts-panel', name);} catch {} // Navigation works when storage is blocked.
}
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => view(button.dataset.view)));
try {view(sessionStorage.getItem('mts-panel') || 'overview');} catch {}
async function operation(action, data = {}) {
  let command = `POST /api/${action}`;
  if (['inventory', 'hard_refresh'].includes(action)) {
    const sites = data.campaign ? lastState.sites.map(site => site.name) :
      data.site === '*' ? lastState.sites.map(site => site.name) :
        lastState.servers?.find(server => server.name === data.site)?.profiles || [data.site];
    command += `\nWorkspace.operation('${action}', ${JSON.stringify(data)})`;
    command += data.campaign ? '\nRefresh matching campaign configurations on enabled sites.' : '\n' + sites.map(site => `MTS_Management.cluster.call(load_sites(${JSON.stringify(lastState.workspace + '/MTSManager/sites.toml')})[${JSON.stringify(site)}], 'inventory', timeout=120${action === 'hard_refresh' ? ', hard_refresh=True' : ''})`).join('\n');
  }
  const entry = terminalMessage(action === 'inventory' ? 'Refresh: ' + (data.campaign || siteLabel(data.site)) : action, command, 'running');
  pendingOperation = entry;
  updateActions();
  try {
    const response = await api(action, data);
    entry.task_id = response.task_id;
    entry.legacy = !response.task_id;
    await refresh();
  } catch (error) {finishMessage(entry, error.message, 'failed');}
  finally {updateActions();}
}
function inventoryAge() {
  const updated = inventorySnapshot?.updated_at;
  if (updated == null) {
    $('inventory-updated').textContent = inventorySnapshot?.rows.some(row => row.local_copy) ?
      'Local copies available. Refresh to update remote status.' : 'Refresh to update data overview.';
    $('inventory-updated').removeAttribute('title');
    return;
  }
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - updated));
  const age = seconds < 60 ? `${seconds} s` : seconds < 3600 ? `${Math.floor(seconds / 60)} min` : seconds < 86400 ? `${Math.floor(seconds / 3600)} h` : `${Math.floor(seconds / 86400)} d`;
  $('inventory-updated').textContent = `Last updated ${age} ago` + (inventorySnapshot.sources?.length > 1 ? ' (oldest server snapshot)' : '');
  $('inventory-updated').title = new Date(updated * 1000).toLocaleString();
}
async function loadInventory(logSelection = true) {
  const selection = inventorySelection(), selected = $('jobs-site').value, request = ++inventoryRequest;
  const entry = logSelection ? terminalMessage('Data source: ' + (selection.campaign || siteLabel(selection.site)),
    'Reading cached inventories, campaign definitions and local copies.', 'running') : null;
  inventorySnapshot = null; inventoryAge();
  $('jobs').innerHTML = '<tr><td colspan="5" class="empty">Loading local inventory…</td></tr>';
  try {
    const snapshot = await api('inventory?' + new URLSearchParams(selection));
    if (request !== inventoryRequest || selected !== $('jobs-site').value) {
      if (entry) finishMessage(entry, 'Selection changed; this result was not displayed.');
      return;
    }
    inventorySnapshot = snapshot;
    fillMonitorSources(snapshot.campaigns);
    renderInventory(snapshot.rows);
    if (!snapshot.rows.length && snapshot.updated_at == null) $('jobs').innerHTML = '<tr><td colspan="5" class="empty">Refresh to update data overview.</td></tr>';
    const counts = snapshot.expected == null ? `${snapshot.rows.length} records available.` :
      `Expected: ${snapshot.expected}. Found: ${snapshot.found}. Missing from available records: ${snapshot.missing}.`;
    const unknown = (snapshot.sources || []).filter(source => source.updated_at == null).map(source => source.label);
    $('inventory-coverage').textContent = counts + (selection.campaign ?
      ' Membership uses expected configurations and cached records across servers.' : '') +
      (unknown.length ? ' No remote snapshot: ' + unknown.join(', ') + '.' : '') +
      (selection.campaign && snapshot.uncached_sites?.length ? ' Servers without inventories: ' + snapshot.uncached_sites.join(', ') + '.' : '');
    if (entry) finishMessage(entry, counts + (!snapshot.rows.length ? ' Refresh to update data overview.' : ''));
    inventoryAge();
    updateActions();
  } catch (error) {
    if (entry) finishMessage(entry, error.message, 'failed'); else message(error.message);
    if (request !== inventoryRequest) return;
    $('jobs').innerHTML = '<tr><td colspan="5" class="empty">Local inventory could not be loaded.</td></tr>';
  }
}
function updateActions() {
  if (!lastState) return;
  const busy = lastState.task.state === 'running' || pendingOperation?.state === 'running', ready = Object.values(lastState.packages).every(Boolean);
  for (const button of [...actionButtons.map($), ...document.querySelectorAll('.table-actions button, #ssh-list button, #ssh-editor button')]) {
    if (!button.parentElement.classList.contains('action-hint')) {
      const hint = document.createElement('span'); hint.className = 'action-hint';
      button.before(hint); hint.append(button);
    }
    button.disabled = busy || button.dataset.disabledConnection === 'true' || button.dataset.unavailable === 'true';
    button.parentElement.title = busy ? 'Already running: ' + (lastState.task.command || lastState.task.title) : button.dataset.disabledReason || '';
  }
  $('build-simulator').disabled ||= !ready;
  $('run-button').disabled ||= !ready || ($('run-site').value === 'local' && !lastState.local_build);
  $('plot-button').disabled ||= !ready;
  $('refresh-jobs').disabled ||= !ready;
  $('hard-refresh').disabled ||= !ready;
  $('update-managers').disabled ||= !ready || !lastState.sites.some(site => site.remote);
}
function renderTerminal(history = backendHistory) {
  try {sessionStorage.setItem('mts-terminal-history', JSON.stringify({token: accessToken, entries: clientHistory}));} catch {}
  backendHistory = history;
  history = [...history.filter(entry => !hiddenBackendEntries.has(entry.id) && !clientHistory.some(client => client.task_id === entry.id)),
    ...clientHistory].sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
  const container = $('terminal-history'), ids = new Set(history.map(entry => entry.id));
  let changed = false;
  for (const node of [...container.children]) if (!ids.has(node.dataset.id)) node.remove();
  for (const [index, entry] of history.entries()) {
    let node = [...container.children].find(node => node.dataset.id === entry.id);
    if (!node) {
      for (const previous of container.children) if (previous.dataset.state !== 'running') previous.open = false;
      node = document.createElement('details'); node.dataset.id = entry.id;
      node.innerHTML = '<summary><span></span><button class="remove-message" aria-label="Delete terminal message" title="Delete terminal message">×</button></summary><pre></pre>';
      node.querySelector('button').onclick = async event => {
        event.preventDefault(); event.stopPropagation();
        try {
          if (entry.id.startsWith('browser-')) {
            if (entry.task_id) {await api('terminal', {id: entry.task_id}); hiddenBackendEntries.add(entry.task_id);}
            clientHistory = clientHistory.filter(item => item.id !== entry.id); renderTerminal();
          } else {await api('terminal', {id: entry.id}); await refresh();}
        } catch (error) {message(error.message);}
      };
      node.open = true; container.append(node); changed = true;
    }
    if (container.children[index] !== node) container.insertBefore(node, container.children[index] || null);
    node.dataset.state = entry.state;
    node.querySelector('summary span').textContent = `${entry.title} · ${entry.state}`;
    node.querySelector('summary').title = entry.command || entry.title;
    const output = entry.logs.join('\n') || 'Starting…', pre = node.querySelector('pre');
    if (pre.textContent !== output) {
      pre.textContent = output; pre.scrollTop = pre.scrollHeight; changed = true;
    }
  }
  if (!history.length) container.innerHTML = '<p class="muted">No messages.</p>';
  if (changed) container.scrollTop = container.scrollHeight;
}
function fillSites(sites) {
  document.querySelectorAll('.sites').forEach(select => {
    const previous = select.value;
    select.replaceChildren(...sites.map(site => new Option((siteLabel(site.label || site.name)) + (site.remote ? ' · remote' : ' · this computer'), site.name)));
    if (sites.some(site => site.name === previous)) select.value = previous;
  });
}
function renderInventory(rows) {
  const body = $('jobs'); body.replaceChildren();
  if (!rows.length) {body.innerHTML = '<tr><td colspan="5" class="empty">No managed simulations were found.</td></tr>'; return;}
  for (const row of rows) {
    const tr = document.createElement('tr'), status = row.status.state;
    const percentage = row.progress.fraction === null ? '—' : (100 * row.progress.fraction).toFixed(1) + '%';
    const source = row.local_copy ? 'Local copy' + (row.origin_profile ? ' · ' + siteLabel(row.origin_profile) : '') : row.site ? siteLabel(row.site) + (row.local_paths?.length ? ' · local copy' : '') : '—';
    tr.innerHTML = `<td>${escape(row.folder.split('/').at(-1))}</td><td>${escape(source)}</td><td><span class="badge ${status === 'finished' ? 'ready' : ''}">${escape(status)}</span>${row.termination ? '<br>' + escape(row.termination.stopReason) : ''}</td><td>${percentage}</td><td><div class="table-actions"></div></td>`;
    tr.firstElementChild.title = `${row.folder}\nConfiguration: ${row.config_hash}\nCampaign: ${row.campaign || '—'}`;
    if (row.attempt) {
      const logs = document.createElement('button'); logs.className = 'small'; logs.textContent = 'Logs';
      logs.onclick = () => operation('logs', {site: row.site, attempt: row.attempt});
      tr.querySelector('.table-actions').append(logs);
      if (['running','queued','prepared','stopping'].includes(status)) {
        const stop = document.createElement('button'); stop.className = 'small danger'; stop.textContent = 'Stop';
        stop.onclick = () => {if (confirm('Stop this simulation attempt?')) operation('stop', {site: row.site, attempt: row.attempt});};
        tr.querySelector('.table-actions').append(stop);
      }
    }
    body.append(tr);
  }
}
async function refresh() {
  if (restarting) return;
  try {
    const state = await api('state');
    if (restarting) return;
    lastConnectionError = '';
    const previousSites = lastState?.sites, previousCampaigns = lastState?.campaigns; lastState = state;
    if (state.interface_version !== 3 && !mismatchReported) {
      message('The backend is an older version than this page. Use Quit MTS, then reopen MTS.app. Closing the browser tab does not stop the backend.');
      mismatchReported = true;
    }
    $('workspace-path').textContent = state.workspace;
    $('tools-command').textContent = state.setup_command;
    const ready = Object.values(state.packages).every(Boolean);
    $('ready-badge').textContent = ready ? 'Installed' : 'Installation required';
    $('ready-badge').className = 'badge' + (ready ? ' ready' : '');
    const entries = [...Object.entries(state.packages), ['Local simulator', state.local_build]];
    const list = entries.map(([name, ok]) => `<div class="readiness-row"><span>${escape(name)}</span><span class="badge ${ok ? 'ready' : ''}">${ok ? 'Installed' : 'Not installed'}</span></div>`).join('');
    $('readiness-list').innerHTML = list; $('setup-readiness').innerHTML = list;
    if (firstState || JSON.stringify(state.sites) !== JSON.stringify(previousSites) || JSON.stringify(state.campaigns) !== JSON.stringify(previousCampaigns)) {
      fillSites(state.sites);
      fillMonitorSources(state.campaigns || inventorySnapshot?.campaigns);
      if (ready && state.sites.length && state.interface_version === 3) await loadInventory();
    }
    updateActions();
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
    const task = state.task, terminalHistory = state.history ?? [];
    const entry = clientHistory.find(client => task.id && client.task_id === task.id) ||
      (pendingOperation?.legacy ? pendingOperation : null);
    if (entry) {
      entry.logs = [entry.logs[0], ...task.logs];
      entry.state = task.state;
      entry.command = task.command;
      if (task.state !== 'running') entry.legacy = false;
    }
    renderConnections(state.ssh_connections || []);
    updateActions();
    $('task-badge').textContent = task.state;
    $('task-badge').className = 'badge ' + (task.state === 'complete' ? 'ready' : task.state);
    renderTerminal(terminalHistory);
    $('abort').classList.toggle('hidden', task.state !== 'running');
    $('abort').disabled = !task.cancellable || task.cancel_requested;
    $('abort').title = task.cancellable ? 'Abort this read; keep cached data and running simulations.' :
      'This operation changes files or submits jobs and cannot be safely interrupted.';
    const revision = `${task.id}:${task.inventory_revision}`;
    if (task.inventory_revision && revision !== lastInventoryUpdate) {
      lastInventoryUpdate = revision; await loadInventory(false);
    }
    if (task.state === 'failed') $('task-result').textContent = task.error;
    if (['complete', 'partial', 'aborted'].includes(task.state) && lastResult !== task.id) {
      lastResult = task.id; $('task-result').textContent = task.state === 'aborted' ? 'Operation aborted.' :
        task.state === 'partial' ? 'Some server operations failed; see terminal.' : 'Operation completed.';
      if (task.state === 'aborted') await loadInventory(false);
      if (task.result?.pdf) {
        const link = document.createElement('a'); link.className = 'button primary'; link.target = '_blank'; link.rel = 'noopener';
        link.href = '/figures/' + task.result.pdf.split('/').map(encodeURIComponent).join('/') + '?token=' + encodeURIComponent(accessToken);
        link.textContent = 'Open PDF'; $('plot-result').replaceChildren(link);
        $('task-result').textContent = 'Figure saved: ' + task.result.name;
      }
      if (task.result?.config_hash) $('task-result').textContent = 'Submitted.';
      if (task.result?.inspected_host) showSSHInspection(task.result);
      if (task.result?.saved_profile && sshEditor) await openSSHEditor(sshEditor.host, 'profile', task.result.saved_profile);
      if (task.result?.configured && sshEditor?.type === 'connection') closeSSHEditor();
    }
    if (task.state === 'running' || !terminalHistory.some(entry => entry.id === task.id)) $('task-result').textContent = '';
    inventoryAge();
  } catch (error) {
    if (lastConnectionError !== error.message) message('Connection failed. ' + error.message);
    lastConnectionError = error.message;
  }
}
$('install-packages').onclick = () => operation('install');
$('build-simulator').onclick = () => operation('build');
$('refresh-jobs').onclick = () => operation('inventory', inventorySelection());
$('hard-refresh').onclick = () => operation('hard_refresh', {site: $('hard-refresh-site').value});
$('update-managers').onclick = () => operation('update_managers');
$('jobs-site').onchange = () => loadInventory();
$('abort').onclick = async () => {
  try {await api('abort', {}); $('abort').disabled = true; await refresh();} catch (error) {message(error.message);}
};
async function clearTerminal() {
  try {
    for (const entry of backendHistory) if (entry.state !== 'running') hiddenBackendEntries.add(entry.id);
    clientHistory = clientHistory.filter(entry => entry.state === 'running');
    if (pendingOperation?.state !== 'running') pendingOperation = null;
    renderTerminal(backendHistory.filter(entry => entry.state === 'running'));
    if (lastState?.interface_version === 3) await api('terminal', {});
    await refresh();
  } catch (error) {message(error.message);}
}
$('clear-terminal').onclick = clearTerminal;
$('run-site').onchange = () => {
  terminalMessage('Execution site', 'Selected ' + $('run-site').value + '.'); refresh();
};
$('plot-data').onchange = () => terminalMessage('Plot data source', 'Selected ' + $('plot-data').value + '.');
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
    try {localStorage.removeItem('mts-token'); sessionStorage.removeItem('mts-token'); sessionStorage.removeItem('mts-terminal-history');} catch {}
    document.querySelector('.workspace').innerHTML = '<div class="guide"><h1>MTS is closed.</h1><p>Use the desktop launcher to restart. Simulation jobs continue.</p></div>';}
  catch (error) {message(error.message);}
};
$('restart').onclick = async () => {
  let entry;
  try {
    await api('restart', {});
    restarting = true;
    clearInterval(poller);
    for (const id of actionButtons) $(id).disabled = true;
    entry = terminalMessage('Restart MTS', 'Restarting MTS…', 'running');
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 300));
      try {
        const response = await fetch('/health', {cache: 'no-store', signal: AbortSignal.timeout(1000)});
        const health = await response.json();
        if (response.ok && health.application === 'MTS' && health.workspace === lastState.workspace && !health.restarting) {
          finishMessage(entry, 'Backend ready; reloading page.');
          history.replaceState(null, '', location.pathname + '#' + accessToken);
          location.reload();
          return;
        }
      } catch {} // The listening socket is briefly unavailable during restart.
    }
    throw new Error('Restart timed out. Reopen MTS with the desktop launcher; see .mts/server.log.');
  } catch (error) {
    if (entry) finishMessage(entry, error.message, 'failed');
    else message(error.message);
  }
};
refresh(); poller = setInterval(refresh, 2000);

function renderConnections(rows) {
  const signature = JSON.stringify(rows);
  if ($('ssh-list').dataset.rows === signature) return;
  $('ssh-list').dataset.rows = signature;
  $('ssh-list').replaceChildren();
  for (const row of rows) {
    const article = document.createElement('div'); article.className = 'ssh-connection';
    const line = document.createElement('div'); line.className = 'ssh-row';
    const status = row.status || 'Unchecked';
    const indicator = status === 'Connected' && !row.profile_warning ? '<span class="ssh-dot" role="img" aria-label="Connected" title="Connected"></span>' : `<span class="ssh-state">${escape(row.profile_warning ? 'Profile setup' : status)}</span>`;
    line.innerHTML = `${indicator}<strong>${escape(siteLabel(row.host))}</strong>${row.disabled && status !== 'Disabled' ? '<span class="ssh-state">Disabled</span>' : ''}<span class="ssh-address" title="${escape(row.address)}">${escape(row.address || row.host)}</span><span class="ssh-profiles" title="Profiles">${escape(row.profiles.map(siteLabel).join(', ') || 'No profile')}</span>`;
    const actions = document.createElement('div'); actions.className = 'ssh-row-actions';
    const button = (label, run) => {const item = document.createElement('button'); item.className = 'small'; item.textContent = label; item.onclick = run; actions.append(item); return item;};
    button('Check connection', () => operation('ssh_check', {host: row.host}));
    button('SSH', () => openSSHEditor(row.host, 'connection')).title = 'Edit SSH connection settings';
    button(row.profiles.length > 1 ? 'Edit profiles' : row.profiles.length ? 'Edit profile' : 'Add profile', () => openSSHEditor(row.host, 'profile'));
    if (row.profiles.length) {
      const deployment = row.deployment?.state || 'unknown';
      const deploy = button('Deploy', () => operation('ssh_deploy', {host: row.host}));
      deploy.classList.add('deployment-' + deployment);
      deploy.dataset.disabledConnection = String(row.disabled);
      deploy.dataset.unavailable = String(status !== 'Connected' || !['missing','mismatch'].includes(deployment));
      const reason = deployment === 'current' ? 'The deployed MTS version matches this installation.' :
        deployment === 'mismatch' ? 'The deployed version differs. Update MTS on this server.' :
        deployment === 'missing' ? 'MTS is not installed. Deploy it to this server.' :
        'Check this connection and its deployed version first.';
      deploy.title = reason; deploy.dataset.disabledReason = reason;
    }
    if (row.repair && !row.disabled) button(row.fingerprint ? 'Accept host key' : 'Retry host key',
      () => operation(row.fingerprint ? 'ssh_accept' : 'ssh_scan', {host: row.host, fingerprint: row.fingerprint}));
    button(row.disabled ? 'Enable' : 'Disable', () => operation('ssh_toggle', {host: row.host, disabled: !row.disabled}));
    line.append(actions); article.append(line);
    if ((row.detail || row.suggestion || row.deployment?.detail) && status !== 'Disabled') {
      const diagnostic = document.createElement('div'); diagnostic.className = 'ssh-diagnostic';
      if (row.detail || row.deployment?.detail) {const pre = document.createElement('pre'); pre.textContent = [row.detail, row.deployment?.detail].filter(Boolean).join('\n'); diagnostic.append(pre);}
      if (row.suggestion) {const p = document.createElement('p'); p.textContent = row.suggestion; diagnostic.append(p);}
      if (row.fingerprint) {const p = document.createElement('p'); p.textContent = 'ED25519: ' + row.fingerprint; diagnostic.append(p);}
      article.append(diagnostic);
    }
    $('ssh-list').append(article);
  }
  if (!rows.length) $('ssh-list').textContent = 'No remote connections configured.';
  const select = $('ssh-key-host'), previous = select.value;
  select.replaceChildren(...rows.filter(row => !row.disabled).map(row => new Option(siteLabel(row.host), row.host)));
  if ([...select.options].some(option => option.value === previous)) select.value = previous;
}
$('ssh-check').onclick = () => operation('ssh_check');
$('ssh-key-form').onsubmit = event => {
  event.preventDefault();
  const password = $('ssh-password').value; $('ssh-password').value = '';
  operation('ssh_install', {host: $('ssh-key-host').value, password});
};
$('ssh-add-form').onsubmit = event => {
  event.preventDefault();
  operation('ssh_add', {name: $('ssh-name').value, hostname: $('ssh-hostname').value,
    user: $('ssh-user').value, port: +$('ssh-port').value, directory: $('ssh-directory').value,
    source: $('ssh-source').value, output_root: $('ssh-output').value, mode: $('ssh-mode').value});
};
$('copy-terminal').onclick = async () => {
  const text = [...$('terminal-history').querySelectorAll('details')].map(node =>
    node.querySelector('summary span').textContent + '\n' + node.querySelector('pre').textContent).join('\n\n');
  try {await navigator.clipboard.writeText(text); $('copy-terminal').textContent = 'Copied';
    setTimeout(() => {$('copy-terminal').textContent = 'Copy all';}, 1500);
  } catch (error) {message('Clipboard access failed: ' + error.message);}
};

let sshEditor;
function sshField(form, name, label, value, type = 'text') {
  const field = document.createElement('div'); field.className = 'field';
  const caption = document.createElement('label'); caption.textContent = label;
  const input = document.createElement(type === 'textarea' ? 'textarea' : 'input');
  input.name = name; input.value = value ?? ''; if (type !== 'textarea') input.type = type;
  caption.append(input); field.append(caption); form.append(field); return input;
}
function trackProfileEdits(form, actions, existing, save, onRestore = () => {}) {
  const snapshot = () => JSON.stringify(Object.fromEntries(new FormData(form)));
  const saved = snapshot(), entries = [saved]; let position = 0;
  const undo = document.createElement('button'), redo = document.createElement('button');
  const revert = document.createElement('button');
  for (const [button, label] of [[undo,'Undo'], [redo,'Redo'], [revert, existing ? 'Revert profile' : 'Reset profile']]) {
    button.type = 'button'; button.textContent = label; actions.append(button);
  }
  actions.append(save);
  function update() {
    undo.hidden = redo.hidden = entries.length < 2;
    undo.dataset.unavailable = String(position === 0);
    redo.dataset.unavailable = String(position === entries.length - 1);
    revert.dataset.unavailable = String(snapshot() === saved);
    updateActions();
  }
  function restore(value) {
    for (const [key, text] of Object.entries(JSON.parse(value))) form.elements.namedItem(key).value = text;
    onRestore();
    update();
  }
  function record() {
    onRestore();
    const value = snapshot();
    if (entries[position] !== value) {
      entries.splice(position + 1); entries.push(value); position++;
    }
    update();
  }
  undo.onclick = () => {if (position > 0) restore(entries[--position]);};
  redo.onclick = () => {if (position < entries.length - 1) restore(entries[++position]);};
  revert.onclick = () => {restore(saved); record();};
  form.addEventListener('input', record); form.addEventListener('change', record);
  form.profileHistory = {record}; update();
}
function closeSSHEditor() {sshEditor = null; $('ssh-editor').replaceChildren(); $('ssh-editor').classList.add('hidden');}
async function openSSHEditor(host, type, selected) {
  try {
    const data = await api('ssh_settings?host=' + encodeURIComponent(host));
    sshEditor = {host, type, data};
    const panel = $('ssh-editor'); panel.classList.remove('hidden'); panel.replaceChildren();
    const top = document.createElement('div'); top.className = 'section-row';
    const heading = document.createElement('h2'); heading.textContent = (type === 'profile' ? 'Simulation profiles: ' : 'SSH settings: ') + siteLabel(host);
    const close = document.createElement('button'); close.className = 'small'; close.textContent = 'Close'; close.onclick = closeSSHEditor;
    top.append(heading, close); panel.append(top);
    if (type === 'profile') buildProfileEditor(panel, data, selected);
    else buildConnectionEditor(panel, data);
    panel.scrollIntoView({block: 'nearest', behavior: 'smooth'}); updateActions();
  } catch (error) {message(error.message);}
}
const profileFields = {directory: 'Working directory', source: 'Simulator source directory', executable: 'Executable',
  output_root: 'Output directory', python: 'Python interpreter', setup: 'Environment setup (shell commands)'};
const slurmFields = {time: 'Wall time (D-HH:MM:SS)', partition: 'Partition'};
function buildProfileEditor(panel, data, selected) {
  const toolbar = document.createElement('div'); toolbar.className = 'inline-fields';
  const siteCaption = document.createElement('label'); siteCaption.textContent = 'Site ';
  const sites = document.createElement('select'); sites.setAttribute('aria-label', 'SSH site');
  sites.replaceChildren(...(lastState.ssh_connections || []).map(row => new Option(siteLabel(row.host), row.host)));
  sites.value = data.host; sites.onchange = () => openSSHEditor(sites.value, 'profile'); siteCaption.append(sites); toolbar.append(siteCaption);
  const selector = document.createElement('select'); selector.setAttribute('aria-label', 'Existing simulation profile');
  selector.replaceChildren(...data.profiles.map(profile => new Option(siteLabel(profile.name), profile.name)));
  let editingName = selected ?? data.profiles[0]?.name ?? null;
  selector.value = editingName || ''; selector.hidden = data.profiles.length < 2;
  toolbar.append(selector);
  const create = document.createElement('button'); create.type = 'button'; create.textContent = 'New profile'; toolbar.append(create);
  panel.append(toolbar);
  const form = document.createElement('form'); form.id = 'ssh-profile-editor'; panel.append(form);
  const render = () => {
    form.replaceChildren();
    const profile = data.profiles.find(item => item.name === editingName) || data.default_profile;
    form.dataset.original = editingName || '';
    const title = document.createElement('h3'); title.textContent = editingName ? siteLabel(profile.name) : 'New profile'; form.append(title);
    const grid = document.createElement('div'); grid.className = 'fields'; form.append(grid);
    const name = sshField(grid, 'name', 'Profile name', profile.name); name.required = true; name.readOnly = Boolean(editingName);
    for (const [key, label] of Object.entries(profileFields)) sshField(grid, key, label, profile.settings[key], key === 'setup' ? 'textarea' : 'text');
    const modeField = document.createElement('div'); modeField.className = 'field';
    const modeLabel = document.createElement('label'); modeLabel.textContent = 'Execution mode';
    const mode = document.createElement('select'); mode.name = 'mode';
    mode.replaceChildren(...['detached', 'slurm', 'foreground'].map(value => new Option(value, value))); mode.value = profile.settings.mode;
    modeLabel.append(mode); modeField.append(modeLabel); grid.append(modeField);
    const scheduler = document.createElement('div'); scheduler.className = 'fields'; grid.after(scheduler);
    for (const [key, label] of Object.entries(slurmFields)) sshField(scheduler, 'slurm_' + key, label, profile.settings.slurm?.[key]);
    const partitions = document.createElement('datalist'); partitions.id = 'ssh-partitions'; scheduler.append(partitions);
    form.elements.namedItem('slurm_partition').setAttribute('list', partitions.id);
    const showScheduler = () => scheduler.hidden = mode.value !== 'slurm';
    mode.onchange = showScheduler; showScheduler();
    const advanced = document.createElement('details'), summary = document.createElement('summary'); summary.textContent = 'Other profile settings (JSON)'; advanced.append(summary);
    const extra = Object.fromEntries(Object.entries(profile.settings).filter(([key]) => !(key in profileFields) && key !== 'mode'));
    if (extra.slurm) extra.slurm = Object.fromEntries(Object.entries(extra.slurm).filter(([key]) => !(key in slurmFields)));
    sshField(advanced, 'advanced', 'Manager, scheduler, environment and resource settings', JSON.stringify(extra, null, 2), 'textarea'); form.append(advanced);
    const note = document.createElement('p'); note.className = 'muted'; note.textContent = 'Paths define simulation storage and builds. Manager deployment creates the working directory and installs the shared manager. Simulator sources and builds are configured separately.'; form.append(note);
    const actions = document.createElement('div'); actions.className = 'inline-fields';
    const inspect = document.createElement('button'); inspect.type = 'button'; inspect.textContent = 'Inspect server'; inspect.onclick = () => operation('ssh_inspect', {host: data.host, setup: form.elements.namedItem('setup').value});
    const save = document.createElement('button'); save.textContent = 'Save profile'; save.type = 'submit'; actions.append(inspect, save); form.append(actions);
    trackProfileEdits(form, actions, Boolean(editingName), save, showScheduler);
    const inspection = document.createElement('div'); inspection.id = 'ssh-inspection'; form.append(inspection);
    updateActions();
  };
  selector.onchange = () => {editingName = selector.value; render();};
  create.onclick = () => {editingName = null; selector.selectedIndex = -1; render();}; render();
  form.onsubmit = event => {
    event.preventDefault();
    try {
      const values = Object.fromEntries(new FormData(form)), settings = JSON.parse(values.advanced);
      if (!settings || Array.isArray(settings) || typeof settings !== 'object') throw new Error('Other settings must be a JSON object.');
      for (const key of Object.keys(profileFields)) settings[key] = values[key]; settings.mode = values.mode;
      settings.slurm ||= {};
      if (Array.isArray(settings.slurm) || typeof settings.slurm !== 'object') throw new Error('Slurm settings must be a JSON object.');
      for (const key of Object.keys(slurmFields)) {
        const value = values['slurm_' + key].trim();
        if (value) settings.slurm[key] = value; else delete settings.slurm[key];
      }
      operation('ssh_profile', {host: data.host, name: values.name, original_name: form.dataset.original || null, settings});
    } catch (error) {message(error.message);}
  };
}
function buildConnectionEditor(panel, data) {
  const form = document.createElement('form'), grid = document.createElement('div'); grid.className = 'fields'; form.append(grid);
  for (const [key, label] of Object.entries({hostname: 'Login hostname', user: 'Account name', port: 'Port', proxyjump: 'Proxy jump', proxycommand: 'Proxy command', requesttty: 'Request terminal', stricthostkeychecking: 'Host-key policy', userknownhostsfile: 'Known-hosts files'})) {
    sshField(grid, key, label, data.connection[key], key === 'port' ? 'number' : 'text');
  }
  const identities = data.connection.identityfile;
  sshField(form, 'identityfile', 'Identity files (one path per line)', Array.isArray(identities) ? identities.join('\n') : identities, 'textarea');
  sshField(form, 'extra', 'Additional SSH directives', data.connection.extra, 'textarea');
  const details = document.createElement('details'), summary = document.createElement('summary'); summary.textContent = 'All effective SSH settings';
  const pre = document.createElement('pre'); pre.textContent = Object.entries(data.effective).map(([key, value]) => key + ' ' + (Array.isArray(value) ? value.join(', ') : value)).join('\n'); details.append(summary, pre); form.append(details);
  const save = document.createElement('button'); save.type = 'submit'; save.textContent = 'Save SSH settings'; form.append(save); panel.append(form);
  form.onsubmit = event => {
    event.preventDefault(); const values = Object.fromEntries(new FormData(form));
    values.identityfile = values.identityfile.split('\n').map(value => value.trim()).filter(Boolean);
    operation('ssh_configure', {host: data.host, ...values, port: +values.port});
  };
}
function showSSHInspection(result) {
  if (!sshEditor || sshEditor.host !== result.inspected_host || sshEditor.type !== 'profile') return;
  const panel = $('ssh-inspection'); if (!panel) return;
  panel.replaceChildren();
  $('ssh-partitions').replaceChildren(...(result.information.partitions || []).map(name => new Option(name, name)));
  const executable = $('ssh-profile-editor').elements.namedItem('executable');
  let builds = $('ssh-retained-builds');
  if (builds) builds.remove();
  builds = document.createElement('datalist'); builds.id = 'ssh-retained-builds';
  builds.append(new Option('Working build', 'build/current/MTS2D'));
  for (const build of result.information.retained_builds || []) {
    builds.append(new Option(build.revision.slice(0, 12) + ' · ' + build.built_at.slice(0, 10) + ' · ' + build.sha256.slice(0, 12), build.path));
  }
  executable.setAttribute('list', builds.id); panel.append(builds);
  for (const note of result.notes) {const p = document.createElement('p'); p.textContent = note; panel.append(p);}
  for (const path of result.information.sources) {const p = document.createElement('p'); p.className = 'muted'; p.textContent = 'Source: ' + path; panel.append(p);}
  const apply = document.createElement('button'); apply.type = 'button'; apply.textContent = 'Use suggested profile settings';
  apply.onclick = () => {
    const form = $('ssh-profile-editor'), extra = JSON.parse(form.elements.advanced.value);
    form.profileHistory.record();
    for (const [key, value] of Object.entries(result.suggestions)) {
      const input = form.elements.namedItem(key);
      if (form.dataset.original && ['directory', 'source', 'output_root', 'state_dir', 'executable', 'manager_directory'].includes(key) && (input ? input.value : extra[key])) continue;
      if (input) input.value = value; else extra[key] = value;
    }
    form.elements.advanced.value = JSON.stringify(extra, null, 2);
    form.profileHistory.record();
  };
  panel.append(apply); updateActions();
}
