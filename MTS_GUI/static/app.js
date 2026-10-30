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
const inventoryOpen = new Map();
try {
  const saved = JSON.parse(sessionStorage.getItem('mts-terminal-history') || 'null');
  if (saved?.token === accessToken && Array.isArray(saved.entries)) clientHistory = saved.entries;
} catch {} // Terminal feedback remains available when browser storage is blocked.
let plotSchema, plotValid = false, plotRequest = 0, plotCheckRequest = 0, plotTimer;
let previewBusy = false, previewAutomatic = true, previewRevision = 0, previewManualPending = false;
let plotPresets = [], loadedPresetId = null;
let slurmSchema, slurmReviewedToken = null, slurmPlannedSpec = null;
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
    command += '\nUp to 3 attempts per site; 60 s per attempt.';
    command += data.campaign ? '\nRefresh matching campaign configurations on enabled sites.' : '\n' + sites.map(site => `MTS_Management.cluster.call(load_sites(${JSON.stringify(lastState.workspace + '/MTSManager/sites.toml')})[${JSON.stringify(site)}], 'inventory', timeout=60${action === 'hard_refresh' ? ', hard_refresh=True' : ''})`).join('\n');
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
  $('plot-button').disabled ||= !ready || !plotValid;
  $('plot-preview-update').disabled = !ready || !plotSchema || previewBusy;
  $('plot-preset-update').disabled = !loadedPresetId || !plotSchema;
  for (const id of ['slurm-generate', 'slurm-review']) $(id).disabled = busy || !slurmSchema;
  $('slurm-execute').disabled = busy || !slurmReviewedToken;
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
  const lineage = [], items = [], selection = inventorySelection();
  const displayName = row => row.folder.split('/').at(-1).replace(/\[[0-9a-f]{20,64}\]$/, '');
  const compareNames = (a, b) => displayName(a).localeCompare(displayName(b), undefined, {numeric: true, sensitivity: 'base'});
  rows = [...rows].sort(compareNames);
  const append = tr => {items.push({tr, parents: [...lineage]}); body.append(tr);};
  const visibility = () => items.forEach(({tr, parents}) => {tr.hidden = parents.some(key => !inventoryOpen.get(key));});
  const renderRow = row => {
    const tr = document.createElement('tr'), status = row.status.state;
    tr.className = 'inventory-simulation';
    const percentage = row.progress.fraction === null ? '—' : (100 * row.progress.fraction).toFixed(1) + '%';
    const source = row.local_copy ? 'Local copy' + (row.origin_profile ? ' · ' + siteLabel(row.origin_profile) : '') : row.site ? siteLabel(row.site) + (row.local_paths?.length ? ' · local copy' : '') : '—';
    tr.innerHTML = `<td>${escape(displayName(row))}</td><td>${escape(source)}</td><td><span class="badge ${status === 'finished' ? 'ready' : ''}">${escape(status)}</span>${row.termination ? ' <span class="inventory-termination" title="' + escape(row.termination.stopReason) + '">' + escape(row.termination.stopReason) + '</span>' : ''}</td><td>${percentage}</td><td><div class="table-actions"></div></td>`;
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
    append(tr);
  };
  const section = (label, members, kind, id, render, open = false) => {
    const key = JSON.stringify([selection, ...lineage, id]);
    if (!inventoryOpen.has(key)) inventoryOpen.set(key, kind === 'site' || open);
    const tr = document.createElement('tr'); tr.className = 'inventory-' + kind;
    const th = document.createElement('th'); th.colSpan = 5; th.scope = 'rowgroup';
    const button = document.createElement('button'); button.className = 'inventory-toggle';
    const update = () => {
      button.setAttribute('aria-expanded', String(inventoryOpen.get(key)));
      button.textContent = `${inventoryOpen.get(key) ? '▾' : '▸'} ${label} (${members.length})`;
    };
    button.onclick = () => {inventoryOpen.set(key, !inventoryOpen.get(key)); update(); visibility();};
    update(); th.append(button); tr.append(th); append(tr);
    lineage.push(key); render(members); lineage.pop();
  };
  const category = row => ['running', 'stopping'].includes(row.status.state) ? 'Running' :
    ['prepared', 'queued'].includes(row.status.state) ? 'Queued' :
    ['finished', 'failed', 'stopped'].includes(row.status.state) ? 'Completed' : 'Other records';
  const groups = new Map();
  for (const row of rows) {
    const site = selection.campaign ? row.site || (row.local_copy ? 'Local copies' : 'Missing simulations') : null;
    if (!groups.has(site)) groups.set(site, []);
    groups.get(site).push(row);
  }
  const renderSeeds = (members, open) => {
    const settings = new Map();
    for (const row of members) {
      if (!row.settings_group) {renderRow(row); continue;}
      if (!settings.has(row.settings_group)) settings.set(row.settings_group, []);
      settings.get(row.settings_group).push(row);
    }
    for (const [id, seeds] of settings) {
      seeds.sort(compareNames);
      if (seeds.length === 1) {renderRow(seeds[0]); continue;}
      const first = seeds[0], label = displayName(first).replace(new RegExp(`s${first.seed}$`), '');
      section(label || displayName(first), seeds, 'settings', id, rows => rows.forEach(renderRow), open);
    }
  };
  const renderStates = members => {
    for (const label of ['Running', 'Queued', 'Completed', 'Other records']) {
      const selected = members.filter(row => category(row) === label);
      if (label === 'Other records' && !selected.length) continue;
      section(label, selected, 'category', label, rows => renderSeeds(rows, label === 'Running'), label === 'Running');
    }
  };
  for (const [site, members] of groups) {
    if (site === null) renderStates(members);
    else section(siteLabel(site), members, 'site', site, renderStates);
  }
  visibility();
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
      if (ready) await fillPlotCampaigns(state.campaigns || []);
      if (ready && (firstState || JSON.stringify(state.sites) !== JSON.stringify(previousSites))) await loadSlurmOptions();
    }
    updateActions();
    if (firstState) {
      try {
        localStorage.setItem('mts-token', accessToken);
        sessionStorage.removeItem('mts-token');
      } catch {} // Remember only a token accepted by this running server.
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
        const link = $('plot-open-pdf');
        link.href = '/figures/' + task.result.pdf.split('/').map(encodeURIComponent).join('/') + '?token=' + encodeURIComponent(accessToken);
        link.textContent = 'Open PDF'; link.title = 'Open the last exported PDF'; link.classList.remove('hidden');
        $('task-result').textContent = 'Figure saved: ' + task.result.name;
        if (task.result.warnings?.length) $('plot-warning').textContent = task.result.warnings.join(' ');
      }
      if (task.result?.config_hash) $('task-result').textContent = 'Submitted.';
      if (task.result?.slurm_plan || task.result?.slurm_result) renderSlurmResult(task.result);
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
$('plot-data').onchange = () => loadPlotOptions(false);
$('plot-campaign').onchange = () => loadPlotOptions(true);
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
async function fillPlotCampaigns(campaigns) {
  const select = $('plot-campaign'), previous = select.value;
  select.replaceChildren(...campaigns.map(name => new Option(name, name)));
  if (campaigns.includes(previous)) select.value = previous;
  if (select.value && (!plotSchema || select.value !== previous)) await loadPlotOptions(true);
  if (firstState) await loadPlotPresets();
}
async function loadPlotOptions(resetSource, saved = null) {
  const request = ++plotRequest; ++plotCheckRequest; ++previewRevision; clearTimeout(plotTimer);
  plotSchema = null;
  plotValid = false; updateActions();
  if (resetSource || saved) previewAutomatic = true;
  if (!$('plot-campaign').value) return;
  $('plot-warning').textContent = 'Reading campaign definitions and local data columns…';
  try {
    const query = new URLSearchParams({campaign: $('plot-campaign').value});
    if (!resetSource && $('plot-data').value.trim()) query.set('data', $('plot-data').value.trim());
    const schema = await api('plot_options?' + query);
    if (request !== plotRequest) return;
    plotSchema = schema; $('plot-data').value = schema.data;
    $('data-folders').replaceChildren(...schema.sources.map(source => new Option(`${source.runs} campaign runs`, source.path)));
    $('plot-properties').textContent = `${schema.available} local records of ${schema.expected} planned runs. Varied parameters: ` +
      (Object.keys(schema.parameters).map(key => `${schema.labels[key]} (${schema.parameters[key].length})`).join(', ') || 'none') + '.';
    const seeds = $('plot-seed'), previous = seeds.value;
    seeds.replaceChildren(new Option('Mean of available seeds ± sample SD', 'all'),
      ...schema.seeds.map(seed => new Option('Seed ' + seed, JSON.stringify(seed))));
    if ([...seeds.options].some(option => option.value === previous)) seeds.value = previous;
    $('plot-reduction').replaceChildren(...Object.entries(schema.reductions).map(([key, label]) => new Option(label, key)));
    if (saved) {
      const axes = new Set([...Object.keys(schema.parameters), ...Object.keys(schema.quantities)]);
      if (!axes.has(saved.x) || !axes.has(saved.y) ||
          [saved.color, saved.gradient].some(key => key && !(key in schema.parameters)) ||
          Object.entries(saved.fixed).some(([key, value]) => !schema.parameters[key]?.includes(value)) ||
          (saved.seed !== 'all' && !schema.seeds.includes(saved.seed))) throw new Error('This preset contains selections absent from the current campaign.');
      $('plot-seed').value = saved.seed === 'all' ? 'all' : JSON.stringify(saved.seed);
      $('plot-normalization').value = saved.normalization || 'none';
      $('plot-reduction').value = saved.reduction || 'last';
      $('plot-show-all').checked = Boolean(saved.show_all);
      $('legend-cols').value = saved.ncols || 1; $('legend-loc').value = saved.legend_loc || 'best';
      $('legend-style').value = saved.legend_style || 'compact';
      for (const axis of ['x', 'y']) {
        $('plot-' + axis + 'scale').value = saved[axis + 'scale'] || 'linear';
        $(axis + 'lim').value = saved[axis + 'lim']?.join(', ') || '';
        const input = $('plot-' + axis + 'label');
        input.dataset.edited = String(typeof saved[axis + 'label'] === 'string');
        if (input.dataset.edited === 'true') input.value = saved[axis + 'label'];
      }
    }
    updatePlotControls(saved);
  } catch (error) {
    if (request !== plotRequest) return;
    plotSchema = null; $('plot-warning').textContent = error.message; updateActions();
  }
}
function axisIssue(x, y) {
  const params = plotSchema.parameters, quantities = plotSchema.quantities;
  if (!(x in params) && quantities[x]?.kind !== 'macro') return 'X must be a varied parameter or a saved response column.';
  if (!(y in quantities)) return 'Y must be a measured quantity.';
  if (x === y) return 'X and Y must be different quantities.';
  if (!(x in params) && quantities[y].kind !== 'macro') return 'A timing summary requires a campaign parameter on X.';
  if ([x, y].some(key => key in quantities && !quantities[key].available)) return 'The selected quantity is unavailable in this local collection.';
  return '';
}
function plotChoices(id, choices, selected, issue, order = []) {
  const select = $(id);
  const ranked = choices.map(([key, label]) => ({key, label, reason: issue(key), rank: order.includes(key) ? order.indexOf(key) : order.length}));
  ranked.sort((a, b) => Number(Boolean(a.reason)) - Number(Boolean(b.reason)) || a.rank - b.rank || a.label.localeCompare(b.label, undefined, {numeric: true}));
  select.replaceChildren(...ranked.map(({key, label, reason}) => {
    const option = new Option(label + (reason ? ' (incompatible)' : ''), key);
    option.className = reason ? 'plot-incompatible' : '';
    option.title = reason; return option;
  }));
  if (ranked.some(option => option.key === selected)) select.value = selected;
}
function updatePlotControls(saved = null) {
  if (!plotSchema) return;
  if (typeof saved?.x !== 'string') saved = null;
  const params = plotSchema.parameters, quantities = plotSchema.quantities;
  const axes = [...Object.keys(params).map(key => [key, plotSchema.labels[key]]),
    ...Object.entries(quantities).map(([key, quantity]) => [key, quantity.label])];
  const keys = new Set(axes.map(([key]) => key));
  const x = saved?.x || (keys.has($('plot-x').value) ? $('plot-x').value : 'load');
  const y = saved?.y || (keys.has($('plot-y').value) ? $('plot-y').value : 'avg_sigma12');
  for (const [axis, selected] of [['x', x], ['y', y]]) {
    const common = plotSchema.common_axes[axis];
    const choices = axes.filter(([key]) => $('plot-show-all').checked || common.includes(key) || key === selected);
    plotChoices('plot-' + axis, choices, selected, key => axis === 'x' ? axisIssue(key, y) : axisIssue(x, key), common);
  }
  for (const id of ['plot-color', 'plot-gradient']) {
    const otherKey = id === 'plot-color' ? 'gradient' : 'color';
    const other = saved ? saved[otherKey] || '' : $('plot-' + otherKey).value;
    const selected = saved ? saved[id === 'plot-color' ? 'color' : 'gradient'] || '' : $(id).value;
    plotChoices(id, [['', 'None'], ...Object.keys(params).map(key => [key, plotSchema.labels[key]])], selected,
      key => key && (key === x || key === other) ? 'Choose distinct parameters for X, color and gradient.' : '', ['', ...plotSchema.common_axes.x.filter(key => key in params)]);
  }
  $('plot-reduction-field').classList.toggle('hidden', !(x in params) || quantities[y]?.kind !== 'macro');
  fixedFields(saved?.fixed); legendAliases(saved?.legend_aliases); schedulePlotCheck();
}
function fixedFields(saved = null) {
  const used = [$('plot-x').value, $('plot-color').value, $('plot-gradient').value, 'seed'];
  const previous = Object.fromEntries([...$('fixed-fields').querySelectorAll('[data-property]')].map(el => [el.dataset.property, el.value]));
  for (const [key, value] of Object.entries(saved || {})) previous[key] = JSON.stringify(value);
  $('fixed-fields').replaceChildren();
  for (const [key, values] of Object.entries(plotSchema.parameters)) {
    if (used.includes(key)) continue;
    const field = document.createElement('div'); field.className = 'field';
    const label = document.createElement('label'); label.htmlFor = 'fixed-' + key;
    label.textContent = 'Hold ' + plotSchema.labels[key] + ' fixed';
    const select = document.createElement('select'); select.id = label.htmlFor; select.dataset.property = key;
    select.replaceChildren(...values.map(value => new Option(String(value), JSON.stringify(value))));
    if (values.some(value => JSON.stringify(value) === previous[key])) select.value = previous[key];
    select.onchange = schedulePlotCheck; field.append(label, select); $('fixed-fields').append(field);
  }
}
function plotSelection() {
  return {campaign: $('plot-campaign').value, data: $('plot-data').value,
    x: $('plot-x').value, y: $('plot-y').value, color: $('plot-color').value || null, gradient: $('plot-gradient').value || null,
    seed: $('plot-seed').value === 'all' ? 'all' : JSON.parse($('plot-seed').value), normalization: $('plot-normalization').value,
    reduction: $('plot-reduction').value,
    fixed: Object.fromEntries([...$('fixed-fields').querySelectorAll('[data-property]')].map(el => [el.dataset.property, JSON.parse(el.value)])),
    ncols: +$('legend-cols').value, legend_loc: $('legend-loc').value, xlim: limits('xlim'), ylim: limits('ylim'),
    xscale: $('plot-xscale').value, yscale: $('plot-yscale').value, show_all: $('plot-show-all').checked,
    xlabel: $('plot-xlabel').dataset.edited === 'true' ? $('plot-xlabel').value : null,
    ylabel: $('plot-ylabel').dataset.edited === 'true' ? $('plot-ylabel').value : null,
    legend_style: $('legend-style').value,
    legend_aliases: Object.fromEntries([...$('legend-aliases').querySelectorAll('[data-property]')].map(el => [el.dataset.property, el.value.trim()]))};
}
function legendAliases(saved = null) {
  if (!plotSchema) return;
  const previous = Object.fromEntries([...$('legend-aliases').querySelectorAll('[data-property]')].map(el => [el.dataset.property, el.value]));
  const aliases = {...plotSchema.abbreviations, ...previous, ...(saved || {})};
  $('legend-aliases').replaceChildren();
  for (const key of Object.keys(plotSchema.parameters)) {
    const field = document.createElement('div'); field.className = 'field';
    const label = document.createElement('label'); label.htmlFor = 'legend-alias-' + key; label.textContent = plotSchema.labels[key];
    const input = document.createElement('input'); input.id = label.htmlFor; input.dataset.property = key; input.value = aliases[key];
    input.onchange = schedulePlotCheck; field.append(label, input); $('legend-aliases').append(field);
  }
  $('legend-aliases').classList.toggle('hidden', $('legend-style').value !== 'custom');
}
function syncAxisLabels() {
  if (!plotSchema) return;
  const x = $('plot-x').value, y = $('plot-y').value;
  let ylabel = plotSchema.quantities[y]?.label || plotSchema.labels[y] || y;
  if (x in plotSchema.parameters && plotSchema.quantities[y]?.kind === 'macro') ylabel = plotSchema.reductions[$('plot-reduction').value] + ': ' + ylabel;
  const norm = $('plot-normalization').value;
  if (norm !== 'none') ylabel += ' / ' + (norm === 'L' ? 'L' : '(rows × cols)');
  const defaults = {x: plotSchema.labels[x] || plotSchema.quantities[x]?.label || x, y: ylabel};
  for (const axis of ['x', 'y']) {
    const input = $('plot-' + axis + 'label');
    if (input.dataset.edited !== 'true') input.value = defaults[axis];
  }
}
function schedulePlotCheck() {
  clearTimeout(plotTimer); ++plotCheckRequest; ++previewRevision;
  plotValid = false; updateActions();
  $('plot-preview-image').parentElement.classList.add('is-stale');
  if (!plotSchema) return;
  syncAxisLabels();
  const issue = axisIssue($('plot-x').value, $('plot-y').value);
  if (issue) {$('plot-warning').textContent = issue; return;}
  if (!$('plot-data').value) {$('plot-warning').textContent = 'No local data is available. Download campaign results or choose a local collection.'; return;}
  if (!previewAutomatic) {
    $('plot-preview-status').textContent = 'Automatic preview paused after a render exceeded 5 s. Press Update preview.';
    // Validation remains available without rendering a new image.
    const request = plotCheckRequest;
    plotTimer = setTimeout(async () => {
      try {
        const result = await api('plot_check?' + new URLSearchParams({selection: JSON.stringify(plotSelection())}));
        if (request !== plotCheckRequest) return;
        showPlotValidation(result); plotValid = true;
      } catch (error) {if (request === plotCheckRequest) $('plot-warning').textContent = error.message;}
      updateActions();
    }, 400);
    return;
  }
  $('plot-preview-status').textContent = 'Preview out of date.';
  plotTimer = setTimeout(() => requestPlotPreview(false), 400);
}
function showPlotValidation(result) {
  $('plot-warning').textContent = `${result.mode === 'curve' ? 'Response curves' : 'Scalar comparison'}; ${result.runs} available runs of ${result.expected} selected. ` +
    (result.warnings.join(' ') || 'Equal weight per seed; uncertainty: sample standard deviation.');
}
async function requestPlotPreview(manual) {
  clearTimeout(plotTimer);
  if (!plotSchema || axisIssue($('plot-x').value, $('plot-y').value)) return;
  if (previewBusy) {previewManualPending ||= manual; return;}
  const revision = previewRevision;
  let selection;
  try {selection = plotSelection();} catch (error) {$('plot-warning').textContent = error.message; return;}
  previewBusy = true; updateActions(); $('plot-preview-status').textContent = 'Rendering preview…';
  const slow = setTimeout(() => {
    previewAutomatic = false;
    $('plot-preview-status').textContent = 'Rendering exceeds 5 s. Further previews require Update preview.';
  }, 5000);
  try {
    const result = await api('plot_preview', {...selection, automatic: !manual});
    if (result.manual || result.seconds > 5) previewAutomatic = false;
    if (result.manual || result.busy) {
      previewAutomatic = false;
      $('plot-preview-status').textContent = result.busy ? 'Previous preview is still rendering. Press Update preview when it finishes.' :
        'Automatic preview paused after 5 s. Press Update preview to display the result.';
      return;
    }
    if (revision !== previewRevision) return;
    plotValid = true; showPlotValidation(result);
    const image = $('plot-preview-image');
    image.src = '/previews/' + result.image + '?token=' + encodeURIComponent(accessToken) + '&revision=' + Date.now();
    image.classList.remove('hidden'); image.parentElement.classList.remove('is-stale');
    $('plot-preview-placeholder').classList.add('hidden');
    $('plot-preview-status').textContent = `Preview updated in ${result.seconds.toFixed(2)} s.` +
      (previewAutomatic ? '' : ' Automatic preview paused; use Update preview.');
  } catch (error) {
    if (revision === previewRevision) {plotValid = false; $('plot-warning').textContent = error.message; $('plot-preview-status').textContent = 'Preview unavailable.';}
  } finally {
    clearTimeout(slow); previewBusy = false; updateActions();
    const pending = previewManualPending; previewManualPending = false;
    if (pending || (revision !== previewRevision && previewAutomatic)) requestPlotPreview(pending);
  }
}
async function loadPlotPresets() {
  try {plotPresets = (await api('plot_presets')).presets; renderPlotPresets();}
  catch (error) {$('plot-preset-status').textContent = error.message;}
}
function renderPlotPresets() {
  const list = $('plot-presets-list'); list.replaceChildren();
  for (const preset of plotPresets) {
    const row = document.createElement('div'); row.className = 'plot-preset-row';
    const text = document.createElement('span'); text.textContent = preset.name + ' · ' + preset.selection.campaign;
    const button = document.createElement('button'); button.type = 'button'; button.className = 'small'; button.textContent = 'Load';
    button.onclick = async () => {
      if (![...$('plot-campaign').options].some(option => option.value === preset.selection.campaign)) {
        $('plot-preset-status').textContent = 'The saved campaign is no longer registered.'; return;
      }
      loadedPresetId = preset.id; $('plot-preset-name').value = preset.name;
      $('plot-campaign').value = preset.selection.campaign; $('plot-data').value = preset.selection.data;
      $('plot-preset-status').textContent = 'Loaded: ' + preset.name;
      await loadPlotOptions(false, preset.selection);
    };
    row.append(text, button); list.append(row);
  }
  if (!plotPresets.length) list.textContent = 'No saved plot presets.';
  updateActions();
}
async function savePlotPreset(update) {
  try {
    if (!plotSchema) throw new Error('Select a campaign before saving a preset.');
    const result = await api('plot_presets', {name: $('plot-preset-name').value, selection: plotSelection(), ...(update ? {id: loadedPresetId} : {})});
    plotPresets = result.presets; loadedPresetId = result.saved_id; renderPlotPresets();
    $('plot-preset-status').textContent = update ? 'Preset updated.' : 'New preset saved.';
  } catch (error) {$('plot-preset-status').textContent = error.message;}
}
document.querySelectorAll('.plot-slot').forEach(select => select.onchange = updatePlotControls);
$('plot-show-all').onchange = updatePlotControls;
for (const id of ['plot-seed', 'plot-normalization', 'plot-reduction', 'legend-cols', 'legend-loc', 'plot-xscale', 'plot-yscale', 'xlim', 'ylim']) $(id).onchange = schedulePlotCheck;
for (const axis of ['x', 'y']) $('plot-' + axis + 'label').oninput = event => {event.target.dataset.edited = 'true'; schedulePlotCheck();};
$('plot-labels-default').onclick = () => {for (const axis of ['x', 'y']) $('plot-' + axis + 'label').dataset.edited = 'false'; schedulePlotCheck();};
$('legend-style').onchange = () => {legendAliases(); schedulePlotCheck();};
$('plot-preview-update').onclick = () => requestPlotPreview(true);
$('plot-preset-new').onclick = () => savePlotPreset(false);
$('plot-preset-update').onclick = () => savePlotPreset(true);
function limits(id) {
  if (!$(id).value.trim()) return null;
  const values = $(id).value.split(',').map(Number);
  if (values.length !== 2 || !values.every(Number.isFinite) || values[0] >= values[1]) throw new Error('Axis limits need two increasing numbers separated by a comma.');
  return values;
}
$('plot-form').onsubmit = event => {
  event.preventDefault();
  try {
    if (!plotValid) throw new Error('Choose a compatible selection with available data before exporting.');
    operation('plot', plotSelection());
  } catch (error) {message(error.message);}
};
async function loadSlurmOptions() {
  try {
    const previousSite = $('slurm-site').value, previousAction = $('slurm-action').value;
    slurmSchema = await api('slurm_options');
    $('slurm-site').replaceChildren(new Option('All Slurm sites', '*'), ...slurmSchema.sites.map(name => new Option(siteLabel(name), name)));
    $('slurm-action').replaceChildren(...Object.entries(slurmSchema.actions).map(([key, label]) => new Option(label, key)));
    if (previousSite === '*' || slurmSchema.sites.includes(previousSite)) $('slurm-site').value = previousSite;
    if (previousAction in slurmSchema.actions) $('slurm-action').value = previousAction;
    if (!$('slurm-criteria').children.length) addSlurmCriterion({field: 'L', operator: 'eq', value: 800});
    invalidateSlurm();
    updateActions();
  } catch (error) {$('slurm-status').textContent = error.message;}
}
function invalidateSlurm() {
  slurmReviewedToken = null;
  $('slurm-program').textContent = 'Generate a program from the current criteria.';
  $('slurm-status').textContent = 'Selection changed. Review matching jobs before execution.';
  $('slurm-results').classList.add('hidden'); updateActions();
}
function addSlurmCriterion(saved = null) {
  if (!slurmSchema) return;
  const row = document.createElement('div'); row.className = 'slurm-criterion';
  const field = document.createElement('select'); field.dataset.role = 'field'; field.setAttribute('aria-label', 'Simulation property');
  const keys = Object.keys(slurmSchema.fields).sort((a, b) => (a === 'L' ? -1 : b === 'L' ? 1 : slurmSchema.fields[a].label.localeCompare(slurmSchema.fields[b].label)));
  field.replaceChildren(...keys.map(key => new Option(slurmSchema.fields[key].label, key)));
  field.value = saved?.field || 'L';
  const comparison = document.createElement('select'); comparison.dataset.role = 'operator'; comparison.setAttribute('aria-label', 'Comparison');
  const values = document.createElement('div'); values.className = 'slurm-value';
  const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'small'; remove.textContent = 'Remove';
  remove.onclick = () => {row.remove(); invalidateSlurm();};
  const valueControl = value => {
    const info = slurmSchema.fields[field.value]; values.replaceChildren();
    const numeric = ['int', 'float'].includes(info.type);
    const operators = numeric ? [['eq', 'is'], ['ne', 'is not'], ['lt', '<'], ['le', '≤'], ['gt', '>'], ['ge', '≥']] :
      [['eq', 'is'], ['ne', 'is not'], ...(info.type === 'str' ? [['contains', 'contains']] : [])];
    comparison.replaceChildren(...operators.map(([key, label]) => new Option(label, key)));
    comparison.value = saved?.operator && operators.some(([key]) => key === saved.operator) ? saved.operator : 'eq';
    let input;
    if (info.choices.length) {
      input = document.createElement('select');
      input.replaceChildren(...info.choices.map(v => new Option(({none: 'None', delaunay: 'Delaunay'})[v] || String(v), String(v))), new Option('Custom', '__custom__'));
      const custom = document.createElement('input'); custom.dataset.role = 'custom'; custom.placeholder = 'Custom value'; custom.setAttribute('aria-label', 'Custom property value');
      const known = value !== undefined && info.choices.some(v => String(v) === String(value));
      if (known) input.value = String(value);
      else if (value !== undefined) {input.value = '__custom__'; custom.value = String(value);}
      custom.classList.toggle('hidden', input.value !== '__custom__');
      input.onchange = () => {custom.classList.toggle('hidden', input.value !== '__custom__'); invalidateSlurm();};
      custom.oninput = invalidateSlurm; values.append(input, custom);
    } else {
      input = document.createElement('input'); input.type = numeric ? 'number' : 'text';
      if (numeric) input.step = info.type === 'int' ? '1' : 'any';
      input.value = value === undefined ? '' : String(value); input.oninput = invalidateSlurm; values.append(input);
    }
    input.dataset.role = 'value'; input.setAttribute('aria-label', 'Property value');
  };
  field.onchange = () => {saved = null; valueControl(); invalidateSlurm();};
  comparison.onchange = invalidateSlurm;
  valueControl(saved?.value); row.append(field, comparison, values, remove); $('slurm-criteria').append(row); invalidateSlurm();
}
function slurmSelection() {
  const criteria = [...$('slurm-criteria').children].map(row => {
    const field = row.querySelector('[data-role="field"]').value, type = slurmSchema.fields[field].type;
    let value = row.querySelector('[data-role="value"]').value;
    if (value === '__custom__') value = row.querySelector('[data-role="custom"]').value;
    if (['int', 'float'].includes(type)) {
      if (!value.trim() || !Number.isFinite(+value) || (type === 'int' && !Number.isInteger(+value))) throw new Error('Enter a valid ' + type + ' for ' + slurmSchema.fields[field].label + '.');
      value = +value;
    } else if (type === 'bool') {
      if (!['true', 'false'].includes(value.toLowerCase())) throw new Error('Boolean criteria require true or false.');
      value = value.toLowerCase() === 'true';
    }
    return {field, operator: row.querySelector('[data-role="operator"]').value, value};
  });
  if (!criteria.length) throw new Error('Add at least one criterion.');
  return {site: $('slurm-site').value, action: $('slurm-action').value, match: $('slurm-match').value, criteria};
}
async function generateSlurmProgram() {
  const spec = slurmSelection(), result = await api('slurm_program', spec);
  if (JSON.stringify(spec) !== JSON.stringify(slurmSelection())) throw new Error('Selection changed; generate the current program again.');
  $('slurm-program').textContent = 'Review only:\n' + result.review + '\n\nExecute from a terminal:\n' + result.apply;
  return spec;
}
function renderSlurmResult(result) {
  const lines = []; let count = 0;
  for (const [name, response] of Object.entries(result.sites)) {
    if (response.error) {lines.push(siteLabel(name) + ': ' + response.error); continue;}
    const data = response.data; count += data.jobs.length;
    lines.push(`${siteLabel(name)}: ${data.jobs.length} queued matches; ${data.already.length} already held or ineligible for release; ${data.running.length} active jobs excluded.`);
    if (result.slurm_result) lines.push(`Verified changes: ${data.applied.length}; unverified: ${(data.unverified || []).length}; no longer pending: ${(data.skipped || []).length}.`);
    if (data.command) lines.push(data.command);
    lines.push(...data.jobs.map(job => `${job.job_id}  ${job.name}  ${job.reason}`));
  }
  $('slurm-results').textContent = lines.join('\n'); $('slurm-results').classList.remove('hidden');
  let current = null;
  try {current = JSON.stringify(slurmSelection());} catch {}
  slurmReviewedToken = result.slurm_plan && count && current === slurmPlannedSpec ? result.slurm_plan : null;
  $('slurm-status').textContent = result.slurm_result ? 'Scheduler action completed. Inspect the per-site verification results.' :
    slurmReviewedToken ? `${count} queued jobs reviewed. Run reviewed action applies only to these job IDs; queue state is checked again.` :
    count ? 'Criteria changed; review the current selection again.' : 'No queued jobs match on the available sites.';
  updateActions();
}
$('slurm-add-criterion').onclick = () => addSlurmCriterion();
for (const id of ['slurm-site', 'slurm-match', 'slurm-action']) $(id).onchange = invalidateSlurm;
$('slurm-generate').onclick = () => generateSlurmProgram().catch(error => {$('slurm-status').textContent = error.message;});
$('slurm-review').onclick = async () => {
  try {const spec = await generateSlurmProgram(); slurmPlannedSpec = JSON.stringify(spec); await operation('slurm_plan', spec);}
  catch (error) {$('slurm-status').textContent = error.message;}
};
$('slurm-execute').onclick = () => {if (slurmReviewedToken) {const token = slurmReviewedToken; slurmReviewedToken = null; operation('slurm_execute', {token});}};
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
