const $ = id => document.getElementById(id);
for (const panel of ['start', 'monitor']) $(panel).append($('simulation-help').content.cloneNode(true));
let accessToken = location.hash.slice(1);
try {
  accessToken ||= localStorage.getItem('mts-token') || sessionStorage.getItem('mts-token') || '';
} catch {} // A launcher link still works when browser storage is blocked.
history.replaceState(null, '', location.pathname);
let lastState, lastResult, lastWarningId, firstState = true, poller, inventorySnapshot, inventoryRequest = 0, restarting = false;
let backendHistory = [], clientHistory = [], lastConnectionError = '';
let pendingOperation, mismatchReported = false, hiddenBackendEntries = new Set();
let lastInventoryUpdate = '';
const inventoryOpen = new Map();
let inventorySort = 'date', inventoryAscending = false, inventoryNoticeTimer;
const inventoryDateFormat = new Intl.DateTimeFormat('de-DE', {day: '2-digit', month: '2-digit', year: 'numeric'});
try {
  const saved = JSON.parse(sessionStorage.getItem('mts-terminal-history') || 'null');
  if (saved?.token === accessToken && Array.isArray(saved.entries)) clientHistory = saved.entries;
} catch {} // Terminal feedback remains available when browser storage is blocked.
let plotSchema, plotValid = false, plotRequest = 0, plotCheckRequest = 0, plotTimer;
let previewBusy = false, previewAutomatic = true, previewRevision = 0, previewManualPending = false;
let plotPresets = [], loadedPresetId = null, monitorPlot = null, filePlot = null;
let animationPresets = [], loadedAnimationPresetId = null, recoveringWorkspace = true, draftTimer, initialRefresh = false;
let animationRequest = 0, animationSourceKey = '', animationLatticeKey = '';
let animationHeatmapKind, animationEnergyRange, lastAnimationScan;
const opacityEditor = createOpacityEditor($('animate-opacity-chart'), $('animate-remove-opacity-node'));
let startOptions, campaignBaseSettings, continuationPlan;
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
const actionButtons = ['install-packages','build-simulator','overview-build','run-button','campaign-run','plot-button','animate-preview-button','animate-preview-middle','animate-button','refresh-jobs','remove-missing','archive-campaign','hard-refresh','update-managers','ssh-check','ssh-install','ssh-add','quit','restart'];
function buttonHint(button) {
  if (button.parentElement.classList.contains('action-hint')) return button.parentElement;
  const hint = document.createElement('span');
  hint.className = 'action-hint';
  button.before(hint); hint.append(button);
  return hint;
}
for (const id of [...actionButtons, 'simulation-save']) buttonHint($(id));

const disabledReasons = {
  'run-button': 'Wait for MTS to load the simulation controls.',
  'simulation-save': 'Wait for MTS to load the simulation settings.',
  'simulation-download': 'Save or select a preset first.',
  'campaign-run': 'Select a saved campaign first.',
  'campaign-save': 'Select a base preset first.',
  'plot-button': 'Complete the plot configuration first.',
  'plot-preview-update': 'Select valid plot settings and wait for any active preview to finish.',
  'plot-other-seeds': 'Choose a simulation before selecting other seeds.',
  'plot-preset-new': 'Configure a plot from the Plot page first.',
  'plot-preset-update': 'Load a plot preset and configure a plot first.',
  'animation-preset-update': 'Load an animation preset first.',
  'animate-remove-opacity-node': 'Keep at least one point in the opacity curve.',
  'slurm-generate': 'Load the Slurm controls first.',
  'slurm-review': 'Load the Slurm controls first.',
  'slurm-execute': 'Review the matching jobs first.',
  'update-managers': 'Configure an enabled remote execution site first.',
};
function disabledActionReason(button) {
  return button.parentElement.title || button.dataset.disabledReason ||
    disabledReasons[button.id] || button.title || 'This action is currently unavailable.';
}
function actionLabel(button) {
  return button.getAttribute('aria-label') || button.textContent.trim() || 'Action';
}
function syncDisabledButton(button) {
  if (!button.isConnected) return;
  if (button.matches(':disabled')) {
    const hint = buttonHint(button);
    hint.tabIndex = 0;
    hint.setAttribute('role', 'button');
    hint.setAttribute('aria-disabled', 'true');
    hint.setAttribute('aria-label', actionLabel(button) + '. ' + disabledActionReason(button));
  } else if (button.parentElement.classList.contains('action-hint')) {
    const hint = button.parentElement;
    for (const attribute of ['tabindex', 'role', 'aria-disabled', 'aria-label']) hint.removeAttribute(attribute);
  }
}
function syncButtonsIn(element) {
  if (!(element instanceof Element)) return;
  if (element.matches('button')) syncDisabledButton(element);
  for (const button of element.querySelectorAll('button')) syncDisabledButton(button);
}
new MutationObserver(records => {
  for (const record of records) {
    if (record.type === 'attributes') syncButtonsIn(record.target);
    else for (const element of record.addedNodes) syncButtonsIn(element);
  }
}).observe(document.body, {subtree: true, childList: true, attributes: true, attributeFilter: ['disabled', 'title']});
syncButtonsIn(document.body);

function reportDisabledAction(event) {
  const hint = event.target.closest('.action-hint');
  const button = hint?.querySelector(':scope > button:disabled');
  if (!button) return;
  event.preventDefault();
  event.stopPropagation();
  if (!event.repeat) terminalMessage(actionLabel(button), disabledActionReason(button), 'failed');
}
document.addEventListener('click', reportDisabledAction, true);
document.addEventListener('keydown', event => {
  if (event.key === 'Enter' || event.key === ' ') reportDisabledAction(event);
}, true);

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
  const labels = {plot_monitor: 'Prepare plot data', archive_monitor: 'Archive',
    archive_campaign: 'Archive campaign settings', restore_archive: 'Restore', continuation_plan: 'Load further',
    run: 'Start simulation', resume: 'Resume', remove_missing: 'Remove all missing'};
  const entry = terminalMessage(action === 'inventory' ? 'Refresh: ' + (data.campaign || siteLabel(data.site)) :
    labels[action] || action, command, 'running');
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
  const sources = (inventorySnapshot?.sources || []).filter(source => !inventorySnapshot.campaign ||
    inventorySnapshot.rows.some(row => row.site === source.site));
  const servers = new Map();
  for (const source of sources) {
    const server = lastState.servers.find(server => server.profiles.includes(source.site));
    if (!server) throw new Error(`Unknown inventory server: ${source.site}`);
    const times = servers.get(server.label) || [];
    times.push(source.updated_at);
    servers.set(server.label, times);
  }
  const element = $('inventory-updated');
  element.removeAttribute('title');
  if (!servers.size) {
    $('inventory-updated').textContent = inventorySnapshot?.rows.some(row => row.local_copy) ?
      'Local copies available. Refresh to update remote status.' : 'Refresh to update data overview.';
    return;
  }
  element.replaceChildren();
  for (const [label, times] of servers) {
    const updated = times.includes(null) ? null : Math.min(...times);
    const seconds = updated == null ? null : Math.max(0, Date.now() / 1000 - updated);
    const age = seconds == null ? 'No server snapshot available' :
      seconds < 60 ? 'Less than one minute' :
      seconds < 3600 ? `${Math.floor(seconds / 60)} min ago` :
      seconds < 86400 ? `${Math.floor(seconds / 3600)} h ago` :
      `${Math.floor(seconds / 86400)} d ago`;
    const line = document.createElement('div');
    line.textContent = (servers.size > 1 ? `${label} — last update: ` : 'Last update: ') + age;
    if (updated != null) line.title = new Date(updated * 1000).toLocaleString();
    element.append(line);
  }
}
async function loadInventory(logSelection = true) {
  const selection = inventorySelection(), selected = $('jobs-site').value, request = ++inventoryRequest;
  const entry = logSelection ? terminalMessage('Data source: ' + (selection.campaign || siteLabel(selection.site)),
    'Reading cached inventories, campaign definitions and local copies.', 'running') : null;
  inventorySnapshot = null; inventoryAge();
  $('jobs').innerHTML = '<tr><td colspan="6" class="empty">Loading local inventory…</td></tr>';
  try {
    const snapshot = await api('inventory?' + new URLSearchParams(selection));
    if (request !== inventoryRequest || selected !== $('jobs-site').value) {
      if (entry) finishMessage(entry, 'Selection changed; this result was not displayed.');
      return;
    }
    inventorySnapshot = snapshot;
    $('archive-campaign').classList.toggle('hidden', !snapshot.expected || snapshot.archived !== snapshot.expected);
    $('remove-missing').classList.toggle('hidden', !snapshot.rows.some(row => row.missing && row.site));
    fillMonitorSources(snapshot.campaigns);
    renderInventory(snapshot.rows);
    if (!snapshot.rows.length && snapshot.updated_at == null) $('jobs').innerHTML = '<tr><td colspan="6" class="empty">Refresh to update data overview.</td></tr>';
    const counts = snapshot.expected == null ? `${snapshot.rows.length} records available.` :
      `Expected: ${snapshot.expected}. Found: ${snapshot.found}. Missing from available records: ${snapshot.missing}.` +
      (snapshot.archived ? ` Archived: ${snapshot.archived}.` : '');
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
    $('jobs').innerHTML = '<tr><td colspan="6" class="empty">Local inventory could not be loaded.</td></tr>';
  }
}
function updateActions() {
  if (!lastState) {
    updateRunActions('Connecting to MTS.', 'Connecting to MTS.');
    return;
  }
  const busy = lastState.task.state === 'running' || pendingOperation?.state === 'running', ready = Object.values(lastState.packages).every(Boolean);
  const busyReason = busy ? 'An operation is running. Wait for ' +
    (lastState.task.command || lastState.task.title || pendingOperation?.title || 'it') + ' to finish.' : '';
  for (const button of [...actionButtons.map($), ...document.querySelectorAll('.table-actions button:not(.folder-location), #run-more-menu button, #ssh-list button, #ssh-editor button')]) {
    button.disabled = busy || button.dataset.disabledConnection === 'true' || button.dataset.unavailable === 'true';
    buttonHint(button).title = busyReason || button.dataset.disabledReason || '';
  }
  const installReason = ready ? '' : 'Install the required packages in Setup and tools.';
  for (const id of ['build-simulator', 'overview-build']) {
    $(id).disabled ||= !ready;
    $(id).parentElement.title = busyReason || installReason;
  }
  const siteReason = site => installReason ||
    (!site ? 'Choose an execution site.' :
      site === 'local' && !lastState.local_build ? 'Build MTS2D from the Overview.' : '');
  const campaignSiteReason = siteReason($('campaign-site').value);
  $('campaign-run').disabled ||= Boolean(campaignSiteReason);
  if (!busy && campaignSiteReason) $('campaign-run').parentElement.title = campaignSiteReason;
  const setupReason = !startOptions ? 'Simulation settings are unavailable. Check MTSManager in Setup and tools.' : '';
  customMesh.refresh();
  const triangular = $('run-energy-function').value === 'contiTriangular';
  $('run-triangular-reconnection-warning').hidden = !triangular || $('run-reconnection').value === 'none';
  const vtuRate = $('run-vtu-rate');
  vtuRate.disabled = !$('run-write-vtus').checked;
  const vtuIssue = !vtuRate.disabled && !(vtuRate.valueAsNumber > 0)
    ? 'Enter a positive VTU file rate.' : '';
  const vtuSettingsReason = startOptions && !Object.hasOwn(startOptions.defaults, 'vtuFilesPerUnitLoad')
    ? 'Update MTSManager to use VTU files per unit load.' : '';
  const configurationReason = setupReason || vtuSettingsReason || indentationIssue() || rectangleIssue() || vtuIssue;
  const runSite = $('run-site').value;
  const currentBuildReason = runSite === 'local' && !lastState.local_current_configuration
    ? 'Rebuild MTS2D from the Overview to use the current configuration.' : '';
  const runReason = configurationReason || continuationIssue() || siteReason(runSite) || currentBuildReason ||
    (!continuationPlan && customMesh.issue());
  const conflict = checkRunFolder(runReason);
  $('run-conflict-recheck').hidden = !conflict;
  updateRunActions(continuationPlan ? 'Presets are unavailable while continuing a run.' : configurationReason,
    $('run-button').parentElement.title || runReason || conflict);
  $('campaign-run').disabled ||= !$('campaign-saved').value;
  $('campaign-save').disabled = !campaignBaseSettings;
  $('plot-button').disabled ||= !ready || !plotValid;
  for (const id of ['animate-button', 'animate-preview-button', 'animate-preview-middle']) {
    $(id).disabled ||= !ready || $('animation-settings').hidden;
  }
  $('plot-preview-update').disabled = !ready || !plotSchema || previewBusy;
  $('plot-other-seeds').disabled = busy || !plotSchema;
  $('plot-preset-new').disabled = !plotSchema || isMonitorPlot() || isFilePlot();
  $('plot-preset-update').disabled = !loadedPresetId || $('plot-preset-new').disabled;
  $('animation-preset-update').disabled = !loadedAnimationPresetId;
  for (const id of ['slurm-generate', 'slurm-review']) $(id).disabled = busy || !slurmSchema;
  $('slurm-execute').disabled = busy || !slurmReviewedToken;
  $('refresh-jobs').disabled ||= !ready;
  $('hard-refresh').disabled ||= !ready;
  $('update-managers').disabled ||= !ready || !lastState.sites.some(site => site.remote);
}

function updateRunActions(saveReason, runReason) {
  for (const [id, reason] of [['simulation-save', saveReason], ['run-button', runReason]]) {
    const button = $(id);
    button.disabled = Boolean(reason);
    button.parentElement.title = reason;
  }
  const status = $('run-action-reasons');
  const text = continuationPlan ? runReason : saveReason && saveReason === runReason ? saveReason :
    [saveReason && 'Save preset — ' + saveReason, runReason && 'Start simulation — ' + runReason]
      .filter(Boolean).join(' ');
  if (status.textContent !== text) status.textContent = text;
  status.hidden = !text;
}
function terminalBracePairs(output) {
  const pairs = new Map(), openings = [];
  let quote = '';
  for (let index = 0; index < output.length; index++) {
    const character = output[index];
    if (quote) {
      if (character === '\\') index++;
      else if (character === quote || character === '\n') quote = '';
    } else if (character === '{') {
      openings.push(index);
    } else if (openings.length) {
      if (character === '}') pairs.set(openings.pop(), index);
      else if (character === '"' || character === "'") quote = character;
    }
  }
  return pairs;
}

function renderTerminalOutput(pre, output) {
  const pairs = terminalBracePairs(output);
  if (!pairs.size) {pre.textContent = output; return;}
  const expanded = new Set([...pre.querySelectorAll('.terminal-brace[aria-expanded="true"]')].map(button => button.dataset.offset));
  const fragment = document.createDocumentFragment(), sections = [];
  let parent = fragment, start = 0;
  for (const {index} of output.matchAll(/[{}]/g)) {
    if (pairs.has(index)) {
      parent.append(output.slice(start, index));
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'terminal-brace'; button.textContent = '{';
      button.dataset.offset = index;
      button.setAttribute('aria-label', 'Terminal brace section');
      button.setAttribute('aria-expanded', expanded.has(button.dataset.offset));
      button.onclick = () => button.setAttribute('aria-expanded', button.getAttribute('aria-expanded') === 'false');
      const content = document.createElement('span'); content.className = 'terminal-brace-content';
      parent.append(button, content);
      sections.push({end: pairs.get(index), parent});
      parent = content;
    } else if (sections.at(-1)?.end === index) {
      parent.append(output.slice(start, index));
      parent = sections.pop().parent;
      const closing = document.createElement('span'); closing.textContent = '}';
      parent.append(closing);
    } else {
      continue;
    }
    start = index + 1;
  }
  fragment.append(output.slice(start));
  pre.replaceChildren(fragment);
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
      node.innerHTML = `<summary><span></span>
        <button type="button" class="copy-message" aria-label="Copy terminal message" title="Copy terminal message">⧉</button>
        <button type="button" class="remove-message" aria-label="Delete terminal message" title="Delete terminal message">×</button>
        </summary><pre></pre>`;
      const copyButton = node.querySelector('.copy-message');
      let copyFeedbackTimer;
      copyButton.onclick = async event => {
        event.preventDefault(); event.stopPropagation();
        try {
          await navigator.clipboard.writeText(node.querySelector('pre').textContent);
          copyButton.textContent = '✓';
          copyButton.title = 'Copied';
          clearTimeout(copyFeedbackTimer);
          copyFeedbackTimer = setTimeout(() => {
            copyButton.textContent = '⧉';
            copyButton.title = 'Copy terminal message';
          }, 1500);
        } catch (error) {message('Clipboard access failed: ' + error.message);}
      };
      node.querySelector('.remove-message').onclick = async event => {
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
    node.dataset.warning = Boolean(entry.warning);
    node.querySelector('summary span').textContent = `${entry.title} · ${entry.state}`;
    node.querySelector('summary').title = entry.command || entry.title;
    const output = entry.logs.join('\n') || 'Starting…', pre = node.querySelector('pre');
    if (pre.textContent !== output) {
      renderTerminalOutput(pre, output); pre.scrollTop = pre.scrollHeight; changed = true;
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
async function openFolderLocation(row) {
  const actions = [];
  if (row.ssh_command) {
    actions.push((async () => {
      await navigator.clipboard.writeText(row.ssh_command);
      $('inventory-notice').textContent = 'SSH command copied to clipboard.';
      clearTimeout(inventoryNoticeTimer);
      inventoryNoticeTimer = setTimeout(() => {$('inventory-notice').textContent = '';}, 3000);
    })());
  }
  const copy = row.local_paths?.[0];
  if (row.local_copy || copy || !row.ssh_command) {
    actions.push(api('open_run_folder', {folder: copy || row.folder,
      site: copy || row.local_copy ? null : row.site, config_hash: row.config_hash}));
  }
  for (const result of await Promise.allSettled(actions)) {
    if (result.status === 'rejected') message(result.reason.message);
  }
}
function selectedRuns(rows) {
  return {scope: inventorySelection(), runs: rows.map(({site, folder, config_hash, manifest_sha256}) =>
    ({site, folder, config_hash, manifest_sha256}))};
}
function runParent(folder) {
  return folder.slice(0, folder.lastIndexOf('/'));
}
function archiveRuns(rows) {
  const selection = selectedRuns(rows);
  try {
    if (localStorage.getItem('mts-archive-skip-warning') === 'true') {
      operation('archive_monitor', selection);
      return;
    }
  } catch {} // The warning remains available when browser storage is blocked.
  const dialog = $('archive-warning'), checkbox = $('archive-skip-warning');
  const destinations = new Set(rows.map(row => {
    const root = runParent(row.folder);
    return `${row.site ? siteLabel(row.site) : 'Local copy'} · ${root}-archive`;
  }));
  $('archive-destinations').textContent = `${rows.length} ${rows.length === 1 ? 'run' : 'runs'}\n${[...destinations].join('\n')}` +
    (rows.some(row => row.local_paths?.length) ? '\nDownloaded copies remain in their current locations.' : '');
  checkbox.checked = false;
  dialog.returnValue = '';
  dialog.onclose = () => {
    if (dialog.returnValue !== 'archive') return;
    if (checkbox.checked) {
      try {localStorage.setItem('mts-archive-skip-warning', 'true');} catch {}
    }
    operation('archive_monitor', selection);
  };
  dialog.showModal();
}
async function loadArchiveLog() {
  const status = $('archive-log-status'), body = $('archive-log-rows');
  status.textContent = 'Reading archive records…';
  try {
    const entries = await api('archive_history');
    body.replaceChildren();
    for (const entry of entries) {
      const row = body.insertRow();
      const label = entry.campaign || entry.output_folder.split('/').at(-1).replace(/\[[0-9a-f]{20}\]$/, '');
      const source = entry.site ? siteLabel(entry.site) : entry.kind === 'campaign' ? 'Campaign settings' : 'Local copy';
      for (const value of [label, source, new Date(entry.timestamp).toLocaleString(), entry.status]) row.insertCell().textContent = value;
      row.cells[0].title = `${entry.output_folder}\n${entry.archive_folder}`;
      const actions = row.insertCell();
      actions.className = 'table-actions';
      if (entry.status === 'Archived') {
        const button = document.createElement('button');
        button.className = 'small'; button.textContent = 'Restore';
        button.setAttribute('aria-label', 'Restore ' + label);
        button.onclick = () => operation('restore_archive', {id: entry.event_id});
        actions.append(button);
      }
    }
    status.textContent = entries.length ? '' : 'No archive actions recorded.';
    updateActions();
  } catch (error) {status.textContent = error.message;}
}
$('archive-log').ontoggle = () => {if ($('archive-log').open) loadArchiveLog();};
const runMoreMenu = $('run-more-menu');
let runMoreAnchor;
runMoreMenu.addEventListener('beforetoggle', event => {
  if (event.newState === 'closed') {
    runMoreAnchor?.setAttribute('aria-expanded', 'false');
    runMoreAnchor = null;
  }
});
function openRunMenu(anchor, rows) {
  if (runMoreMenu.matches(':popover-open')) {
    const sameAnchor = runMoreAnchor === anchor;
    runMoreMenu.hidePopover();
    if (sameAnchor) return;
  }
  const resume = $('run-more-resume'), further = $('run-more-further'), archive = $('run-more-archive');
  const template = $('run-more-template');
  template.dataset.unavailable = String(rows.length !== 1);
  template.dataset.disabledReason = rows.length !== 1 ? 'Select one simulation.' : '';
  template.onclick = async () => {
    runMoreMenu.hidePopover();
    try {
      const result = await api('run_template', selectedRuns(rows));
      if (!startOptions) await loadStartOptions();
      setContinuation(null);
      $('simulation-preset').value = '';
      $('simulation-preset-name').value = '';
      $('simulation-download').disabled = true;
      $('run-site').value = [...$('run-site').options].some(option => option.value === result.site)
        ? result.site : 'local';
      $('run-label').value = result.label;
      view('start');
      showPreset(result.settings);
      if (result.initial_mesh) customMesh.loadTemplate(result.initial_mesh);
      $('run-form').scrollIntoView({block: 'start'});
      queueWorkspaceDraft();
    } catch (error) { message(error.message); }
  };
  const stoppable = ['finished', 'failed', 'stopped'];
  const furtherUnavailable = rows.length !== 1 || !rows[0].site || !stoppable.includes(rows[0].status.state);
  const archiveUnavailable = rows.some(row => ![...stoppable, 'local copy'].includes(row.status.state));
  const canResume = rows.length === 1 && rows[0].site && rows[0].status.state === 'failed';
  resume.classList.toggle('hidden', !canResume);
  resume.onclick = () => {runMoreMenu.hidePopover(); operation('resume', selectedRuns(rows));};
  further.dataset.unavailable = String(furtherUnavailable);
  further.dataset.disabledReason = furtherUnavailable ? 'Select one stopped run on its execution site.' : '';
  archive.dataset.unavailable = String(archiveUnavailable);
  archive.dataset.disabledReason = archiveUnavailable ? 'Archive is available after all selected runs have stopped.' : '';
  further.onclick = () => {runMoreMenu.hidePopover(); operation('continuation_plan', selectedRuns(rows));};
  archive.onclick = () => {runMoreMenu.hidePopover(); archiveRuns(rows);};
  updateActions();
  runMoreAnchor = anchor;
  anchor.setAttribute('aria-expanded', 'true');
  runMoreMenu.showPopover({source: anchor});
  const trigger = anchor.getBoundingClientRect(), menu = runMoreMenu.getBoundingClientRect();
  const left = Math.max(4, Math.min(trigger.left, document.documentElement.clientWidth - menu.width - 4));
  const below = trigger.bottom + 4;
  const top = below + menu.height <= window.innerHeight - 4 ? below : Math.max(4, trigger.top - menu.height - 4);
  runMoreMenu.style.left = left + 'px';
  runMoreMenu.style.top = top + 'px';
  if (canResume && !resume.disabled) resume.focus({preventScroll: true});
  else if (!template.disabled) template.focus({preventScroll: true});
  else if (!further.disabled) further.focus({preventScroll: true});
  else if (!archive.disabled) archive.focus({preventScroll: true});
}
function appendRunActions(container, rows) {
  const plot = document.createElement('button');
  plot.className = 'small'; plot.textContent = 'Plot';
  plot.onclick = () => operation('plot_monitor', selectedRuns(rows));
  const more = document.createElement('button');
  more.className = 'small'; more.type = 'button'; more.textContent = 'More';
  more.setAttribute('aria-controls', 'run-more-menu');
  more.setAttribute('aria-expanded', 'false');
  more.onclick = () => openRunMenu(more, rows);
  container.append(plot, more);
}
for (const button of document.querySelectorAll('.inventory-sort')) {
  button.onclick = () => {
    const key = button.dataset.sort;
    inventoryAscending = inventorySort === key ? !inventoryAscending : key !== 'date';
    inventorySort = key;
    document.querySelector('#jobs-card th[aria-sort]')?.removeAttribute('aria-sort');
    button.closest('th').setAttribute('aria-sort', inventoryAscending ? 'ascending' : 'descending');
    if (inventorySnapshot) {renderInventory(inventorySnapshot.rows); updateActions();}
  };
}
function renderInventory(rows) {
  if (runMoreMenu.matches(':popover-open')) runMoreMenu.hidePopover();
  const body = $('jobs'); body.replaceChildren();
  if (!rows.length) {body.innerHTML = '<tr><td colspan="6" class="empty">No managed simulations were found.</td></tr>'; return;}
  const lineage = [], items = [], selection = inventorySelection();
  const displayName = row => row.folder.split('/').at(-1).replace(/\[[0-9a-f]{20,64}\]$/, '');
  const sourceLabel = row => row.local_copy ? 'Local copy' + (row.origin_profile ? ' · ' + siteLabel(row.origin_profile) : '') :
    row.site ? siteLabel(row.site) + (row.local_paths?.length ? ' · local copy' : '') : null;
  const timestamp = row => Date.parse(row.run_date || row.status.started_at || '');
  const compareText = new Intl.Collator(undefined, {numeric: true, sensitivity: 'base'}).compare;
  const value = {simulation: displayName, state: row => row.status.state, load: row => row.progress.fraction,
    date: row => Number.isNaN(timestamp(row)) ? null : timestamp(row), source: sourceLabel}[inventorySort];
  rows = [...rows].sort((a, b) => {
    const left = value(a), right = value(b);
    const order = left !== null && right !== null ?
      (typeof left === 'number' ? left - right : compareText(left, right)) : 0;
    return Number(left === null) - Number(right === null) || (inventoryAscending ? order : -order) ||
      compareText(displayName(a), displayName(b));
  });
  const append = tr => {items.push({tr, parents: [...lineage]}); body.append(tr);};
  const visibility = () => items.forEach(({tr, parents}) => {tr.hidden = parents.some(key => !inventoryOpen.get(key));});
  const renderRow = row => {
    const tr = document.createElement('tr'), status = row.status.state;
    const stateLabel = status === 'failed' ?
      (row.status.failure_reason ? 'Error: ' + row.status.failure_reason : 'Error') :
      status === 'missing' ? 'Missing' : status;
    tr.className = 'inventory-simulation';
    const percentage = row.progress.fraction === null ? '—' : (100 * row.progress.fraction).toFixed(1) + '%';
    const source = sourceLabel(row) || '—';
    const time = timestamp(row), date = Number.isNaN(time) ? '—' : inventoryDateFormat.format(time);
    tr.innerHTML = `<td>${escape(displayName(row))}</td><td><div class="table-actions"></div></td><td><span class="badge ${status === 'finished' ? 'ready' : status === 'failed' ? 'failed' : ''}">${escape(stateLabel)}</span>${row.termination ? ' <span class="inventory-termination" title="' + escape(row.termination.stopReason) + '">' + escape(row.termination.stopReason) + '</span>' : ''}</td><td class="inventory-load">${percentage}</td><td class="inventory-date">${date}</td><td class="inventory-source">${escape(source)}</td>`;
    tr.firstElementChild.title = `${row.folder}\nConfiguration: ${row.config_hash}\nCampaign: ${row.campaign || '—'}\nState: ${stateLabel}\nLoad: ${percentage}\nDate: ${date}\nSource: ${source}`;
    if (row.attempt) {
      const logs = document.createElement('button'); logs.className = 'small'; logs.textContent = 'Log';
      logs.onclick = () => operation('logs', {site: row.site, attempt: row.attempt});
      tr.querySelector('.table-actions').append(logs);
      if (['running','queued','prepared','stopping'].includes(status)) {
        const stop = document.createElement('button'); stop.className = 'small danger'; stop.textContent = 'Stop';
        stop.onclick = () => {if (confirm('Stop this simulation attempt?')) operation('stop', {site: row.site, attempt: row.attempt});};
        tr.querySelector('.table-actions').append(stop);
      }
    }
    if (!row.missing) {
      const folder = document.createElement('button'); folder.className = 'small folder-location';
      folder.textContent = 'Open';
      folder.onclick = () => openFolderLocation(row);
      const actions = tr.querySelector('.table-actions');
      actions.append(folder); appendRunActions(actions, [row]);
    }
    append(tr);
  };
  const section = (label, members, kind, id, render, open = false) => {
    const key = JSON.stringify([selection, ...lineage, id]);
    if (!inventoryOpen.has(key)) inventoryOpen.set(key, kind === 'site' || open);
    const tr = document.createElement('tr'); tr.className = 'inventory-' + kind;
    const th = document.createElement('th'); th.colSpan = 6; th.scope = 'rowgroup';
    const button = document.createElement('button'); button.className = 'inventory-toggle';
    const update = () => {
      button.setAttribute('aria-expanded', String(inventoryOpen.get(key)));
      button.textContent = `${inventoryOpen.get(key) ? '▾' : '▸'} ${label} (${members.length})`;
    };
    button.onclick = () => {inventoryOpen.set(key, !inventoryOpen.get(key)); update(); visibility();};
    update(); th.append(button); tr.append(th);
    if (kind === 'settings' && members.some(row => !row.missing)) {
      th.colSpan = 1;
      const actions = document.createElement('td'), buttons = document.createElement('div');
      buttons.className = 'table-actions';
      appendRunActions(buttons, members.filter(row => !row.missing));
      actions.colSpan = 5; actions.append(buttons); tr.append(actions);
    }
    append(tr);
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
      const executable = row.executable_sha256 || row.build?.executable_sha256;
      const key = row.settings_group ? JSON.stringify([row.site, runParent(row.folder), row.settings_group, executable]) : row;
      if (!settings.has(key)) settings.set(key, []);
      settings.get(key).push(row);
    }
    for (const [id, seeds] of settings) {
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
  if (restarting || initialRefresh) return;
  if (firstState) initialRefresh = true;
  try {
    const state = await api('state');
    if (restarting) return;
    lastConnectionError = '';
    const previousSites = lastState?.sites, previousCampaigns = lastState?.campaigns; lastState = state;
    if (state.interface_version !== 4 && !mismatchReported) {
      message('The backend is an older version than this page. Use Quit MTS, then reopen MTS.app. Closing the browser tab does not stop the backend.');
      mismatchReported = true;
    }
    $('workspace-path').textContent = state.workspace;
    $('tools-command').textContent = state.setup_command;
    const ready = Object.values(state.packages).every(Boolean);
    $('ready-badge').textContent = ready ? 'Installed' : 'Installation required';
    $('ready-badge').className = 'badge' + (ready ? ' ready' : '');
    const buildStatus = state.local_build_status || (state.local_build ? 'installed' : 'missing');
    const [buildLabel, buildStyle] = ({installed: ['Installed', 'ready'], outdated: ['Outdated', 'outdated'],
      missing: ['Not installed', '']})[buildStatus];
    const packageRows = Object.entries(state.packages).map(([name, ok]) =>
      `<div class="readiness-row"><span>${escape(name)}</span><span class="badge ${ok ? 'ready' : ''}">${ok ? 'Installed' : 'Not installed'}</span></div>`).join('');
    $('readiness-list').innerHTML = packageRows;
    $('setup-readiness').innerHTML = packageRows +
      `<div class="readiness-row"><span>Local simulator</span><span class="badge ${buildStyle}"${buildStatus === 'outdated' ?
        ' title="Local simulations can use the installed build when it supports the requested settings. Rebuild Release to use the current source."' : ''}>${buildLabel}</span></div>`;
    $('overview-build-status').textContent = buildLabel;
    $('overview-build-status').className = 'badge ' + buildStyle;
    $('overview-build').hidden = buildStatus === 'installed';
    $('overview-build').textContent = buildStatus === 'missing' ? 'Build Release' : 'Rebuild Release';
    if (state.interface_version === 4 && state.packages.MTSManager && !startOptions) await loadStartOptions();
    if (firstState || JSON.stringify(state.sites) !== JSON.stringify(previousSites) || JSON.stringify(state.campaigns) !== JSON.stringify(previousCampaigns)) {
      if (!firstState && startOptions && JSON.stringify(state.campaigns) !== JSON.stringify(previousCampaigns)) await loadStartOptions();
      fillSites(state.sites);
      fillMonitorSources(state.campaigns || inventorySnapshot?.campaigns);
      if (ready && state.sites.length && state.interface_version >= 3) await loadInventory(!firstState);
      if (ready) await fillPlotSources(state.campaigns || []);
      if (ready && (firstState || JSON.stringify(state.sites) !== JSON.stringify(previousSites))) await loadSlurmOptions();
    }
    updateActions();
    if (firstState) {
      try {
        localStorage.setItem('mts-token', accessToken);
        sessionStorage.removeItem('mts-token');
      } catch {} // Remember only a token accepted by this running server.
      if (!ready) view('setup');
      if (ready) {
        await loadAnimationPresets();
        await recoverWorkspace();
      }
      recoveringWorkspace = false;
      firstState = false;
      if ($('plot-preview-image').classList.contains('hidden')) schedulePlotCheck();
    }
    const task = state.task, terminalHistory = state.history ?? [];
    const scan = task.animation_scan;
    if (scan?.energy_range && task.id !== lastAnimationScan && task.id !== lastResult &&
        JSON.stringify(scan.animation_selection) === JSON.stringify(animationSelection())) {
      lastAnimationScan = task.id;
      animationEnergyRange = scan.energy_range;
      updateOpacityLimits();
      queueWorkspaceDraft();
    }
    const entry = clientHistory.find(client => task.id && client.task_id === task.id) ||
      (pendingOperation?.legacy ? pendingOperation : null);
    if (entry) {
      entry.logs = [entry.logs[0], ...task.logs];
      entry.state = task.state;
      entry.command = task.command;
      entry.warning = task.warning;
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
    if (task.state === 'failed') {
      $('task-result').textContent = task.warning ? 'Action required. See the terminal.' : task.error;
      if (task.warning && task.id !== lastWarningId) {
        lastWarningId = task.id;
        $('operation-warning-message').textContent = task.error;
        const dialog = $('operation-warning');
        if (!dialog.open) dialog.showModal();
      }
    }
    if (['complete', 'partial', 'aborted'].includes(task.state) && lastResult !== task.id) {
      lastResult = task.id; $('task-result').textContent = task.state === 'aborted' ? 'Operation aborted.' :
        task.state === 'partial' ? 'Some server operations failed; see terminal.' : 'Operation completed.';
      if (task.state === 'aborted') await loadInventory(false);
      if (task.result?.missing_removed != null) {
        await loadInventory(false);
        if (task.state === 'complete') $('task-result').textContent = `Removed ${task.result.missing_removed} missing run records.`;
      }
      if (task.result?.archived || task.result?.restored || task.result?.campaign_archived) {
        if ($('archive-log').open) await loadArchiveLog();
        if (task.result.restored) $('task-result').textContent = 'Restored to the original location.';
        if (task.result.campaign_notes?.length && task.state === 'complete') {
          $('task-result').textContent = 'Simulations archived. Campaign settings retained. See the terminal.';
        }
      }
      if (task.result?.animation_preview &&
          JSON.stringify(task.result.animation_selection) === JSON.stringify(animationSelection())) {
        $('animate-preview-image').src = '/previews/' + encodeURIComponent(task.result.animation_preview) +
          '?token=' + encodeURIComponent(accessToken);
        $('animate-preview-output').hidden = false;
        $('animate-output').hidden = true;
      }
      if (task.result?.video) {
        const url = '/videos/' + task.result.video.split('/').map(encodeURIComponent).join('/') + '?token=' + encodeURIComponent(accessToken);
        if (JSON.stringify(task.result.animation_selection) === JSON.stringify(animationSelection())) {
          $('animate-video').src = $('animate-download').href = url;
          $('animate-output').hidden = false;
          $('animate-preview-output').hidden = true;
        }
        $('task-result').textContent = `Animation saved (${task.result.seconds.toFixed(1)} s).`;
      }
      if (task.result?.pdf) {
        const link = $('plot-open-pdf');
        link.href = '/figures/' + task.result.pdf.split('/').map(encodeURIComponent).join('/') + '?token=' + encodeURIComponent(accessToken);
        link.textContent = 'Open PDF'; link.title = 'Open the last exported PDF'; link.classList.remove('hidden');
        $('task-result').textContent = 'Figure saved: ' + task.result.name;
        if (task.result.warnings?.length) $('plot-warning').textContent = task.result.warnings.join(' ');
      }
      if (task.result?.plot_selection) {
        const previous = task.result.previous_selection;
        const follow = !previous || $('plot-campaign').value === previous;
        monitorPlot = task.result;
        await fillPlotSources(state.campaigns || [], follow ? monitorPlot : null);
        if (!previous) view('plot');
      }
      if (task.result?.config_hash) {
        $('task-result').textContent = 'Submitted.';
        runFolderKey = null;
      }
      if (task.result?.continuation_plan) await showContinuation(task.result.continuation_plan);
      if (task.result?.slurm_plan || task.result?.slurm_result) renderSlurmResult(task.result);
      if (task.result?.inspected_host) showSSHInspection(task.result);
      if (task.result?.saved_profile && sshEditor) await openSSHEditor(sshEditor.host, 'profile', task.result.saved_profile);
      if (task.result?.configured && sshEditor?.type === 'connection') closeSSHEditor();
      queueWorkspaceDraft();
    }
    if (task.state === 'running' || !terminalHistory.some(entry => entry.id === task.id)) $('task-result').textContent = '';
  } catch (error) {
    if (lastConnectionError !== error.message) message('Connection failed. ' + error.message);
    lastConnectionError = error.message;
  } finally {initialRefresh = false;}
}
$('install-packages').onclick = () => operation('install');
$('overview-build').onclick = $('build-simulator').onclick = () => operation('build');
$('refresh-jobs').onclick = () => operation('inventory', inventorySelection());
$('remove-missing').onclick = () => operation('remove_missing', inventorySelection());
$('hard-refresh').onclick = () => operation('hard_refresh', {site: $('hard-refresh-site').value});
$('update-managers').onclick = () => operation('update_managers');
$('jobs-site').onchange = () => {
  $('archive-campaign').classList.add('hidden');
  $('remove-missing').classList.add('hidden');
  loadInventory();
};
$('archive-campaign').onclick = () => operation('archive_campaign', inventorySelection());
$('abort').onclick = async () => {
  try {await api('abort', {}); $('abort').disabled = true; await refresh();} catch (error) {message(error.message);}
};
async function clearTerminal() {
  try {
    for (const entry of backendHistory) if (entry.state !== 'running') hiddenBackendEntries.add(entry.id);
    clientHistory = clientHistory.filter(entry => entry.state === 'running');
    if (pendingOperation?.state !== 'running') pendingOperation = null;
    renderTerminal(backendHistory.filter(entry => entry.state === 'running'));
    if (lastState?.interface_version >= 3) await api('terminal', {});
    await refresh();
  } catch (error) {message(error.message);}
}
$('clear-terminal').onclick = clearTerminal;
$('run-site').onchange = () => {
  terminalMessage('Execution site', 'Selected ' + $('run-site').value + '.'); refresh();
};
$('campaign-site').onchange = $('campaign-saved').onchange = updateActions;
$('plot-data').onchange = () => loadPlotOptions(false);
$('plot-campaign').onchange = () => loadPlotOptions(true);

const customMesh = new CustomMeshEditor(
  () => startOptions && !continuationPlan && $('start').classList.contains('active')
    ? {...runConfiguration(), site: $('run-site').value} : null,
  async (data, signal) => {
    const response = await fetch('/api/mesh_preview', {method: 'POST', signal,
      headers: {'X-MTS-Token': accessToken, 'Content-Type': 'application/json'}, body: JSON.stringify(data)});
    if (!response.ok) throw new Error((await response.json()).error || 'Could not generate the mesh preview.');
    return response.text();
  },
  updateActions,
);
let runFolderKey, runFolderConflict = '', runFolderTimer;
function checkRunFolder(runReason) {
  if (!startOptions || continuationPlan || !$('start').classList.contains('active') ||
      runReason || !/^([A-Za-z0-9])[A-Za-z0-9_.-]{0,119}$/.test($('run-label').value)) {
    if (runFolderKey) {
      clearTimeout(runFolderTimer);
      runFolderKey = null;
      runFolderConflict = '';
    }
    return '';
  }
  const data = {...runConfiguration(), label: $('run-label').value, site: $('run-site').value};
  const edited = customMesh.modified;
  const key = JSON.stringify([data, edited ? customMesh.revision : 0]);
  if (key === runFolderKey) return runFolderConflict;
  clearTimeout(runFolderTimer);
  runFolderKey = key;
  runFolderConflict = '';
  runFolderTimer = setTimeout(async () => {
    try {
      if (edited) {
        const mesh = new TextEncoder().encode(customMesh.exportVtu());
        const digest = await crypto.subtle.digest('SHA-256', mesh);
        data.initial_mesh_sha256 = Array.from(new Uint8Array(digest), byte =>
          byte.toString(16).padStart(2, '0')).join('');
      }
      if (runFolderKey !== key) return;
      const result = await api('run_conflict', data);
      if (runFolderKey !== key) return;
      runFolderConflict = result.conflict;
      updateActions();
    } catch {
      // The execution host checks again during run preparation.
    }
  }, 500);
  return '';
}
$('run-conflict-recheck').onclick = () => {runFolderKey = null; updateActions();};
for (const event of ['input', 'change']) $('run-form').addEventListener(event, ({target}) => {
  if (target.id.startsWith('run-')) updateActions();
});

const runFields = {
  deformation: 'deformation', loading: 'loading', rows: 'rows', cols: 'cols', nrThreads: 'threads',
  seed: 'seed', boundary: 'boundary', reconnectionMethod: 'reconnection', totalLoad: 'load',
  loadIncrement: 'increment', startLoad: 'start-load', directions: 'directions',
  segmentLoad: 'segment', repeat: 'repeat',
  writeMeshVTUs: 'write-vtus', vtuFilesPerUnitLoad: 'vtu-rate', logDuringMinimization: 'log-minimization',
  minimizer: 'minimizer', energyFunction: 'energy-function', initialGuessNoise: 'initial-noise',
  indenterRadius: 'indenter-radius', indentationContact: 'indentation-contact',
};
const rectangleEdges = ['top', 'bottom', 'left', 'right'];
const edgeChoices = [
  ['free', 'Free'], ['fixed_x', 'Fixed in x'], ['fixed_y', 'Fixed in y'], ['fixed', 'Fixed in x and y'],
];
const hasRunField = key => Object.hasOwn(runFields, key) || key === 'drivenEdge' || rectangleEdges.includes(key);
for (const edge of rectangleEdges) {
  const initial = edge === 'bottom' ? 'fixed' : 'free';
  $('run-edge-' + edge).replaceChildren(...edgeChoices.map(([value, label]) =>
    new Option(label, value, value === initial, value === initial)));
}
function axialExperiment() {
  return ['compression', 'tension'].includes($('run-deformation').value);
}
function indentationExperiment() {
  return $('run-deformation').value === 'nanoindentation';
}
function indentationIssue() {
  if (!indentationExperiment()) return '';
  if (!Object.hasOwn(startOptions.defaults, 'indenterRadius')) return 'Update MTSManager to use nanoindentation.';
  if ($('run-indenter-radius').valueAsNumber <= 0) return 'Enter a positive indenter radius.';
  if ($('run-increment').valueAsNumber <= 0) return 'Enter a positive depth increment.';
  const spacing = $('run-energy-function').value === 'contiTriangular' ? Math.sqrt(3) / 2 : 1;
  const height = ($('run-rows').valueAsNumber - 1) * spacing;
  if ($('run-start-load').valueAsNumber + runTotalLoad() >= height) {
    return 'The final indentation depth must be less than the sample height.';
  }
  return '';
}
function boundaryDisplacement() {
  return axialExperiment() && $('run-axial-method').value === 'displacement';
}
function verticalDrivenEdge() {
  return ['top', 'bottom'].includes($('run-driven-edge').value);
}
function runTotalLoad() {
  return $('run-load').valueAsNumber + (continuationPlan?.original_total_load ?? 0);
}
function continuationIssue() {
  if (!continuationPlan) return '';
  const end = $('run-start-load').valueAsNumber + runTotalLoad();
  if (continuationPlan.checkpoint && end <= continuationPlan.checkpoint.load) {
    return 'Set a final load above the checkpoint load.';
  }
  if (axialExperiment() && !boundaryDisplacement() && $('run-deformation').value === 'compression' && end >= 1) {
    return 'Final compression strain must be less than 1.';
  }
  return '';
}
function rectangleIssue() {
  if (!boundaryDisplacement()) return '';
  if (!Object.hasOwn(startOptions.defaults, 'drivenEdge')) return 'Update MTSManager to use boundary displacement.';
  const vertical = verticalDrivenEdge();
  const driven = $('run-driven-edge').value;
  if (!['fixed', vertical ? 'fixed_y' : 'fixed_x'].includes($('run-edge-' + driven).value)) {
    return 'Choose a constraint that includes the driven axis.';
  }
  const triangular = $('run-energy-function').value === 'contiTriangular';
  if (triangular && !vertical) return 'Choose the top or bottom driven edge for a triangular sample.';
  for (const edge of vertical ? ['left', 'right'] : ['top', 'bottom']) {
    if (['fixed', vertical ? 'fixed_y' : 'fixed_x'].includes($('run-edge-' + edge).value)) {
      return 'The ' + edge + ' edge fixes the driven component at a shared corner.';
    }
  }
  if ($('run-increment').valueAsNumber <= 0) return 'Enter a positive displacement increment.';
  const end = $('run-start-load').valueAsNumber + runTotalLoad();
  const span = ($(vertical ? 'run-rows' : 'run-cols').valueAsNumber - 1) * (triangular ? Math.sqrt(3) / 2 : 1);
  if ($('run-deformation').value === 'compression' && end >= span) {
    return 'Final compression displacement must be less than ' + Number(span.toPrecision(8)) + ' mesh length units.';
  }
  return '';
}
function setDirectionChoices(value) {
  $('run-directions').value = value;
  if ($('run-deformation').value === 'sequentialSlip') return;
  const choices = [['horizontal', 'Horizontal (x)'], ['vertical', 'Vertical (y)']];
  if (!axialExperiment()) {
    choices.push(['-horizontal', 'Reverse horizontal'], ['-vertical', 'Reverse vertical'], ['custom', 'Custom sequence']);
  }
  const select = $('run-direction-choice');
  select.replaceChildren(...choices.map(([key, label]) => new Option(label, key)));
  const match = value.replace(/\s/g, '').replace(/^\+/, '');
  select.value = choices.some(([key]) => key === match) ? match : 'custom';
}
function updateSlipTotal() {
  if ($('run-deformation').value !== 'sequentialSlip') return;
  const entries = $('run-directions').value.split(',').filter(entry => entry.trim()).length;
  $('run-load').value = entries - (continuationPlan?.original_total_load ?? 0);
}
function renderExperiment() {
  const deformation = $('run-deformation').value;
  const slip = deformation === 'sequentialSlip', axial = axialExperiment();
  const indentation = indentationExperiment();
  const midpoint = deformation === 'directDisplacement';
  const displacement = boundaryDisplacement();
  const editSequence = slip || (!displacement && !indentation && $('run-direction-choice').value === 'custom');
  const schedule = !slip && !axial && !indentation;
  $('run-boundary-field').hidden = slip || displacement || indentation;
  $('run-axial-method-field').hidden = !axial;
  $('run-edge-settings').hidden = !displacement;
  $('run-direction-choice-field').hidden = slip || displacement || indentation;
  for (const id of ['indenter-radius', 'indentation-contact']) {
    $('run-' + id + '-field').hidden = !indentation;
    $('run-' + id).disabled = !indentation || Boolean(continuationPlan);
  }
  $('run-directions-field').hidden = $('run-directions').disabled = !editSequence;
  $('run-custom-directions-label').textContent = slip ? 'Slip sequence' : 'Custom sequence';
  $('run-midpoint-option').hidden = $('run-midpoint-help').hidden = !midpoint;
  $('run-start-load-field').hidden = slip;
  $('run-start-load').min = axial || indentation ? 0 : '';
  $('run-loading').closest('.field').hidden = indentation;
  for (const option of $('run-boundary').options) {
    option.disabled = axial && !['periodic', 'fixed'].includes(option.value);
  }
  for (const id of ['run-segment-field', 'run-repeat-field']) $(id).hidden = !schedule;
  $('run-load').readOnly = slip;
  const unit = indentation ? 'depth' : displacement ? 'displacement' : axial ? 'strain' : 'load';
  const titleUnit = unit[0].toUpperCase() + unit.slice(1);
  $('run-load-label').textContent = continuationPlan ? 'Extra ' + unit :
    slip ? 'Total load' : 'Additional ' + unit;
  $('run-increment-label').textContent = titleUnit + ' increment';
  $('run-start-load-label').textContent = 'Initial ' + unit;
  $('run-load-explanation').textContent = continuationPlan ?
    'Amount added to the source run’s configured maximum.' : slip ? 'Each entry contributes one full slip.' :
    indentation ? 'Depth is measured downward from the relaxed top surface in mesh length units.' :
    axial && !displacement ? 'Use positive engineering strain. The transformation prescribes stretch along the loading axis and keeps the transverse span fixed.' :
    'Amount applied after initialization. Add it to the initial value to get the final value.';
  $('run-directions-label').textContent = axial ? 'Loading axis' : 'Direction';
  $('run-slip-help').classList.toggle('hidden', !slip);
  const axis = verticalDrivenEdge() ? 'y' : 'x';
  const transverse = axis === 'y' ? 'x' : 'y';
  const driven = $('run-driven-edge').value;
  const normal = 'fixed_' + axis;
  for (const edge of rectangleEdges) {
    const select = $('run-edge-' + edge);
    const active = displacement && edge === driven;
    for (const option of select.options) {
      option.disabled = active && ![normal, 'fixed'].includes(option.value);
      option.textContent = active && option.value === normal ? 'Drive ' + axis.toUpperCase() :
        active && option.value === 'fixed' ? 'Drive ' + axis.toUpperCase() + ', fix ' + transverse.toUpperCase() :
        edgeChoices.find(([key]) => key === option.value)[1];
    }
    if (active && ![normal, 'fixed'].includes(select.value)) {
      select.value = select.value === 'fixed_' + transverse ? 'fixed' : normal;
    }
  }
  const motion = deformation === 'compression' ? 'toward' : 'away from';
  $('run-edge-description').textContent = 'Displacement is prescribed in ' + axis + ' on the driven edge, ' +
    motion + ' the opposite edge. Choose whether to fix ' + transverse + '. Adjacent constraints also apply at corners. ' +
    'Values are in mesh length units.';
  updateSlipTotal();
  updateEndLoad();
  updateActions();
}
function updateEndLoad() {
  const initial = $('run-start-load').valueAsNumber, additional = runTotalLoad();
  const end = initial + additional, axial = axialExperiment() && !boundaryDisplacement();
  const compression = $('run-deformation').value === 'compression';
  const value = axial ? 1 + (compression ? -end : end) : end;
  $('run-load').setCustomValidity(axial && compression && end >= 1 ?
    'Final compression strain must be less than 1.' : '');
  $('run-end-load').hidden = !Number.isFinite(end) || (!continuationPlan && (boundaryDisplacement() ||
    (!axial && !indentationExperiment() && (!initial || $('run-deformation').value === 'sequentialSlip'))));
  $('run-end-load').textContent = continuationPlan ? 'New maximum ' + Number(end.toPrecision(12)) :
    (indentationExperiment() ? 'Final depth ' : axial ? 'Final axial stretch ' : 'Final load ') + Number(value.toPrecision(12));
}
$('run-start-load').oninput = $('run-load').oninput = () => {updateEndLoad(); updateActions();};
for (const id of ['increment', 'rows', 'cols', 'indenter-radius', 'vtu-rate']) $('run-' + id).oninput = updateActions;
$('run-write-vtus').onchange = updateActions;
$('run-energy-function').onchange = $('run-reconnection').onchange = updateActions;
$('run-axial-method').onchange = $('run-driven-edge').onchange = renderExperiment;
for (const edge of rectangleEdges) $('run-edge-' + edge).onchange = updateActions;
$('run-directions').oninput = () => {
  if ($('run-deformation').value !== 'sequentialSlip' && $('run-directions').value.includes(',')) $('run-advanced').open = true;
  renderExperiment();
};
$('run-direction-choice').onchange = () => {
  const choice = $('run-direction-choice').value;
  if (choice !== 'custom') $('run-directions').value = choice;
  renderExperiment();
};
$('run-deformation').onchange = () => {
  setDirectionChoices($('run-deformation').value === 'sequentialSlip' ? '1,2,2' :
    axialExperiment() ? 'vertical' : 'horizontal');
  if (axialExperiment() && !['periodic', 'fixed'].includes($('run-boundary').value)) $('run-boundary').value = 'fixed';
  if ($('run-deformation').value === 'directDisplacement' && $('run-boundary').value === 'periodic') {
    $('run-boundary').value = 'anchored';
  }
  $('simulation-preset').value = '';
  $('simulation-download').disabled = true;
  renderExperiment();
};
function runConfiguration() {
  const settings = {};
  for (const [key, id] of Object.entries(runFields)) {
    if (['indenterRadius', 'indentationContact'].includes(key) && !indentationExperiment()) continue;
    const input = $('run-' + id);
    if (key === 'vtuFilesPerUnitLoad' && input.disabled && Number.isNaN(input.valueAsNumber)) continue;
    settings[key] = input.type === 'checkbox' ? input.checked :
      input.type === 'number' ? input.valueAsNumber : input.value;
  }
  for (const key of continuationPlan?.fixed_settings ?? []) {
    if (!hasRunField(key)) settings[key] = continuationPlan.settings[key];
  }
  settings.totalLoad = runTotalLoad();
  if (indentationExperiment()) {
    Object.assign(settings, {boundary: 'fixedBottom', loading: 'relaxation', directions: 'vertical',
      segmentLoad: 0, repeat: false});
  } else if (settings.deformation === 'sequentialSlip') {
    Object.assign(settings, {boundary: 'anchored', startLoad: 0, segmentLoad: 1, repeat: false});
  } else if (axialExperiment()) {
    Object.assign(settings, {segmentLoad: 0, repeat: false});
    if (boundaryDisplacement()) {
      Object.assign(settings, {boundary: 'rectangle', drivenEdge: $('run-driven-edge').value,
        directions: startOptions.defaults.directions});
      for (const edge of rectangleEdges) settings[edge] = $('run-edge-' + edge).value;
    }
  }
  return {settings, overrides: $('run-extra').value};
}
function showPreset(settings) {
  customMesh.clear();
  const rectangle = settings.boundary === 'rectangle';
  $('run-axial-method').value = rectangle ? 'displacement' : 'affine';
  for (const [key, id] of Object.entries(runFields)) {
    const input = $('run-' + id);
    if (input.type === 'checkbox') input.checked = settings[key];
    else if (key === 'boundary' && (rectangle || settings.boundary === 'fixedBottom')) input.value = 'fixed';
    else if (key === 'indenterRadius') input.value = settings[key] ?? input.defaultValue;
    else if (key === 'indentationContact') input.value = settings[key] ?? 'penalty';
    else input.value = settings[key];
  }
  if (rectangle) {
    $('run-driven-edge').value = settings.drivenEdge;
    for (const edge of rectangleEdges) $('run-edge-' + edge).value = settings[edge];
  }
  $('run-extra').value = Object.entries(settings)
    .filter(([key, value]) => !hasRunField(key) && !continuationPlan?.fixed_settings.includes(key) &&
      value !== startOptions.defaults[key])
    .map(([key, value]) => key + ' = ' + value).join('\n');
  setDirectionChoices(settings.directions);
  $('run-advanced').open = settings.loading !== 'relaxation' ||
    (settings.deformation !== 'sequentialSlip' && (settings.repeat || settings.directions.includes(',')));
  renderExperiment();
}
async function loadStartOptions() {
  startOptions = await api('start_options');
  updateExtraSettingsHelp();
  for (const [id, names, placeholder] of [
    ['simulation-preset', startOptions.presets, 'New configuration'],
    ['campaign-base', startOptions.presets, 'Choose a saved preset'],
    ['campaign-saved', startOptions.campaigns, 'Choose a saved campaign'],
  ]) {
    const select = $(id), previous = select.value;
    select.replaceChildren(new Option(placeholder, ''), ...names.map(name =>
      new Option(id !== 'campaign-saved' && name === 'Example-simple-shear' ? 'Example simple shear' : name, name)));
    if (names.includes(previous)) select.value = previous;
  }
  if (!$('campaign-axes').children.length) addCampaignAxis();
  updateActions();
}
function updateExtraSettingsHelp() {
  if (!startOptions) return;
  $('run-extra-help').textContent = Object.keys(startOptions.defaults)
    .filter(key => key !== 'initialMeshSha256' && !hasRunField(key) &&
      !continuationPlan?.fixed_settings.includes(key)).sort().join(', ');
}
function setContinuation(plan) {
  const previouslyFixed = continuationPlan?.fixed_settings ?? [];
  const fixed = plan?.fixed_settings ?? [];
  continuationPlan = plan;
  $('run-form').classList.toggle('is-continuation', Boolean(plan));
  updateExtraSettingsHelp();
  for (const key of new Set([...previouslyFixed, ...fixed])) {
    if (runFields[key]) $('run-' + runFields[key]).disabled = fixed.includes(key);
  }
  for (const id of ['site', 'axial-method', 'driven-edge', ...rectangleEdges.map(edge => 'edge-' + edge)]) {
    $('run-' + id).disabled = Boolean(plan);
  }
  $('run-continuation').hidden = !plan;
  if (plan) {
    const checkpoint = plan.checkpoint ? `checkpoint step ${plan.checkpoint.step} at load ${Number(plan.checkpoint.load.toPrecision(12))}` :
      'Continue from the latest saved checkpoint';
    const originalEnd = plan.settings.startLoad + plan.original_total_load;
    $('run-continuation-source').textContent =
      `${plan.source_label} · ${siteLabel(plan.site)} · ${checkpoint}` +
      ` · previous maximum ${Number(originalEnd.toPrecision(12))}`;
  }
}
async function showContinuation(plan) {
  if (!startOptions) await loadStartOptions();
  setContinuation(plan);
  $('simulation-preset').value = '';
  $('simulation-preset-name').value = '';
  $('simulation-download').disabled = true;
  $('run-site').value = plan.site;
  $('run-label').value = plan.label;
  showPreset({...plan.settings, totalLoad: 1});
  view('start');
  $('run-form').scrollIntoView({block: 'start'});
}
$('simulation-new').onclick = () => {
  const site = $('run-site').value;
  setContinuation(null);
  customMesh.clear();
  $('run-form').reset();
  $('run-advanced').open = false;
  $('run-site').value = site;
  $('simulation-download').disabled = true;
  setDirectionChoices('horizontal');
  renderExperiment();
};
$('run-clear-continuation').onclick = $('simulation-new').onclick;
$('simulation-preset').onchange = async () => {
  const name = $('simulation-preset').value;
  $('simulation-download').disabled = true;
  if (!name) return $('simulation-new').click();
  try {
    const {settings} = await api('simulation_preset', {action: 'load', name});
    if ($('simulation-preset').value !== name) return;
    setContinuation(null);
    $('run-label').value = name;
    $('simulation-preset-name').value = name;
    showPreset(settings);
    $('simulation-download').disabled = false;
  } catch (error) {message(error.message);}
};
async function importSimulation(file) {
  if (continuationPlan) throw new Error('Cancel continue loading before importing a configuration.');
  if (!startOptions) await loadStartOptions();
  const {settings, label} = await api('simulation_preset', {action: 'import', text: await file.text()});
  setContinuation(null);
  $('simulation-preset').value = '';
  $('simulation-download').disabled = true;
  $('run-label').value = label;
  $('simulation-preset-name').value = label || file.name.replace(/\.conf$/i, '');
  showPreset(settings);
  view('start');
  $('run-form').scrollIntoView({block: 'start'});
  queueWorkspaceDraft();
}
$('simulation-upload').onchange = async event => {
  const [file] = event.target.files;
  try {
    if (file) await importSimulation(file);
  } catch (error) {message(error.message);}
  finally {event.target.value = '';}
};
$('simulation-save').onclick = async () => {
  if (continuationPlan || $('simulation-save').disabled || !$('run-form').reportValidity()) return;
  const name = $('simulation-preset-name').value.trim();
  try {
    await api('simulation_preset', {action: 'save', name, ...runConfiguration()});
    await loadStartOptions();
    $('simulation-preset').value = name;
    $('simulation-download').disabled = false;
    terminalMessage('Simulation preset', 'Saved ' + name + '.conf');
  } catch (error) {message(error.message);}
};
$('simulation-download').onclick = async () => {
  const name = $('simulation-preset').value;
  try {
    const {text} = await api('simulation_preset', {action: 'load', name});
    const url = URL.createObjectURL(new Blob([text], {type: 'text/plain'}));
    const link = document.createElement('a');
    link.href = url;
    link.download = name + '.conf';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {message(error.message);}
};
$('run-form').onsubmit = event => {
  event.preventDefault();
  if (!$('run-button').disabled) {
    const data = {...runConfiguration(), label: $('run-label').value, site: $('run-site').value};
    if (continuationPlan) data.continuation = {
      selection: continuationPlan.selection, checkpoint: continuationPlan.checkpoint,
      checkpoint_catalog: continuationPlan.checkpoint_catalog,
    };
    if (!continuationPlan && customMesh.modified) {
      const issue = customMesh.issue();
      if (issue) return message(issue);
      data.initial_mesh = customMesh.exportVtu();
    }
    operation('run', data);
  }
};

const commonSweepParameters = {
  L: 'Square size L', rows: 'Rows', cols: 'Columns', seed: 'Random seed',
  startLoad: 'Initial load', totalLoad: 'Additional load', loadIncrement: 'Load increment',
  reconnectionMethod: 'Reconnection', initialGuessNoise: 'Initial noise', epsR: 'Force tolerance', nrThreads: 'Threads',
};
function fillCampaignParameters(select, value = select.value) {
  const defaults = startOptions.defaults;
  const slip = campaignBaseSettings?.deformation === 'sequentialSlip';
  const keys = $('campaign-show-all').checked ?
    ['L', ...Object.keys(defaults).filter(key => key !== 'initialMeshSha256').sort()] : slip ?
      ['directions', ...Object.keys(commonSweepParameters).filter(key => key !== 'startLoad' && key !== 'totalLoad')] :
      Object.keys(commonSweepParameters);
  if (value && !keys.includes(value)) keys.push(value);
  select.replaceChildren(...keys.map(key => new Option(key === 'directions' && slip ? 'Slip sequence' : commonSweepParameters[key] || key, key)));
  select.value = value;
}
function updateCampaignParameters() {
  for (const select of $('campaign-axes').querySelectorAll('[data-axis="parameter"]')) fillCampaignParameters(select);
}
function updateCampaignVtuWarning() {
  const checkbox = $('campaign-vtu');
  $('campaign-vtu-field').hidden = checkbox.disabled = !campaignBaseSettings?.writeMeshVTUs;
  const sweep = [...$('campaign-axes').querySelectorAll('[data-axis="parameter"]')]
    .find(select => select.value === 'writeMeshVTUs');
  const writesVTUs = sweep ?
    sweep.closest('fieldset').querySelector('[data-axis="values"]').value.split('\n').some(value => value.trim() === 'true') :
    campaignBaseSettings?.writeMeshVTUs;
  $('campaign-vtu-warning').hidden = !campaignBaseSettings || !writesVTUs ||
    (!checkbox.disabled && checkbox.checked);
}
$('campaign-show-all').onchange = updateCampaignParameters;
$('campaign-vtu').onchange = $('campaign-axes').oninput = $('campaign-axes').onchange = updateCampaignVtuWarning;
function addCampaignAxis() {
  const row = $('campaign-axis').content.firstElementChild.cloneNode(true);
  const parameter = row.querySelector('[data-axis="parameter"]'), mode = row.querySelector('[data-axis="mode"]');
  const defaults = startOptions.defaults;
  fillCampaignParameters(parameter, 'seed');
  function updateMode() {
    const numeric = parameter.value === 'L' || typeof defaults[parameter.value] === 'number';
    for (const option of mode.options) option.disabled = !numeric && option.value !== 'custom';
    if (!numeric) mode.value = 'custom';
    const custom = mode.value === 'custom';
    row.querySelectorAll('[data-range]').forEach(field => {
      field.hidden = custom;
      field.querySelector('input').disabled = custom;
    });
    row.querySelector('[data-custom]').hidden = !custom;
    row.querySelector('[data-axis-hint]').textContent = numeric ? 'Separate numbers with commas or spaces.' :
      typeof defaults[parameter.value] === 'boolean' ? 'Enter true or false, one per line.' : 'Enter one value per line.';
  }
  parameter.onchange = mode.onchange = updateMode;
  row.querySelector('[data-remove]').onclick = () => {row.remove(); updateCampaignVtuWarning();};
  updateMode();
  $('campaign-axes').append(row);
  return row;
}
$('campaign-base').onchange = async () => {
  campaignBaseSettings = null;
  $('campaign-vtu').checked = false;
  updateCampaignVtuWarning();
  updateActions();
  const name = $('campaign-base').value;
  try {
    const preset = name && await api('simulation_preset', {action: 'load', name});
    if ($('campaign-base').value !== name) return;
    campaignBaseSettings = preset?.settings;
    updateCampaignParameters();
    updateCampaignVtuWarning();
    updateActions();
  } catch (error) {message(error.message);}
};
$('campaign-add-axis').onclick = () => {if (startOptions) addCampaignAxis();};
$('campaign-form').onsubmit = async event => {
  event.preventDefault();
  const axes = campaignAxes();
  try {
    const disableVTUs = !$('campaign-vtu').disabled && $('campaign-vtu').checked;
    const result = await api('save_campaign', {name: $('campaign-name').value.trim(),
      preset: $('campaign-base').value, axes, writeMeshVTUs: disableVTUs ? false : null});
    await loadStartOptions();
    $('campaign-saved').value = result.name;
    updateActions();
    terminalMessage('Campaign', 'Saved ' + result.name + ' with ' + result.runs + ' runs.');
    await refresh();
  } catch (error) {message(error.message);}
};
$('campaign-run').onclick = () => operation('run_campaign', {name: $('campaign-saved').value, site: $('campaign-site').value});

function animationSelection() {
  const seed = isFilePlot() ? 'all' : $('plot-seed').value;
  return {campaign: $('plot-campaign').value, data: $('plot-data').value.trim(),
    seed: !seed || seed === 'all' ? 'all' : JSON.parse(seed)};
}
function showVisualization(tab) {
  for (const name of ['plot', 'animate']) {
    const active = name === tab;
    $(name + '-tab').setAttribute('aria-selected', String(active));
    $(name + '-tab').tabIndex = active ? 0 : -1;
    $(name + '-panel').hidden = !active;
  }
  const seed = $('plot-seed').options[0];
  if (seed) seed.textContent = tab === 'animate' ? 'All seeds' : 'Mean of available seeds ± sample SD';
  $('animation-settings').disabled = tab !== 'animate';
  if (tab === 'animate') {
    clearTimeout(plotTimer);
    return loadAnimationOptions();
  } else if (!plotValid) schedulePlotCheck();
}
for (const tab of ['plot', 'animate']) {
  const button = $(tab + '-tab');
  button.onclick = () => showVisualization(tab);
  button.onkeydown = event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 'plot' : event.key === 'End' ? 'animate' : tab === 'plot' ? 'animate' : 'plot';
    showVisualization(next);
    $(next + '-tab').focus();
  };
}
async function loadAnimationOptions() {
  if ($('animate-panel').hidden) return;
  const request = ++animationRequest, selection = animationSelection();
  const settings = $('animation-settings'), status = $('animate-status');
  const key = JSON.stringify(selection);
  if (key !== animationSourceKey) {
    $('animate-video').pause();
    $('animate-output').hidden = true;
    $('animate-preview-output').hidden = true;
    animationSourceKey = key;
    animationEnergyRange = null;
  }
  settings.hidden = true; status.textContent = '';
  updateActions();
  try {
    const options = await api('animation_options?' + new URLSearchParams(selection));
    if (request !== animationRequest || !options.boundary) return;
    const periodic = ['periodic', 'periodicAnchored'].includes(options.boundary);
    if (key !== animationLatticeKey) {
      $('animate-defect-lattice').value = options.square_model ? 'square' : 'triangular';
      $('animate-tiling').value = options.boundary === 'periodic' ? 'tile' : 'cell';
      animationHeatmapKind = null;
      animationLatticeKey = key;
    }
    $('animate-tiling').querySelector('[value="tile"]').disabled = !periodic;
    if (!periodic && $('animate-tiling').value === 'tile') $('animate-tiling').value = 'cell';
    const method = $('animate-defect-method');
    $('animate-defect-method-field').hidden = !options.delaunay_dislocations;
    method.querySelector('[value="delaunay"]').disabled = !options.delaunay_dislocations;
    if (!options.delaunay_dislocations) method.value = 'native';
    $('animate-kind').querySelector('[value="shear"]').disabled = !options.square_model;
    if (!options.square_model && $('animate-kind').value === 'shear') $('animate-kind').value = 'energy';
    settings.hidden = false;
    updateAnimationControls();
    updateActions();
  } catch (error) {
    if (request !== animationRequest) return;
    status.textContent = error.message;
    if (recoveringWorkspace) message('Could not restore the animation selection. ' + error.message);
  }
}
function updateOpacityLimits() {
  const energy = $('animate-kind').value === 'energy';
  const limits = ['animate-energy-min', 'animate-limit'].map((id, index) => {
    const input = $(id), scanned = energy ? animationEnergyRange?.[index] : undefined;
    input.placeholder = Number.isFinite(scanned) ? String(Number(scanned.toPrecision(6))) : 'From data';
    return input.value ? input.valueAsNumber : scanned;
  });
  opacityEditor.setLimits(...limits);
}
function updateAnimationControls() {
  const kind = $('animate-kind').value;
  const energy = kind === 'energy', stress = kind === 'stress';
  const coloured = energy || stress || ['shear', 'matrix'].includes(kind);
  const conditional = energy && $('animate-conditional-opacity').checked;
  if (coloured && animationHeatmapKind !== kind) {
    $('animate-heatmap').value = energy ? 'inferno' : 'coolwarm';
    animationHeatmapKind = kind;
  }
  $('animate-matrix-field').hidden = !['matrix', 'disk'].includes(kind);
  $('animate-stress-component-field').hidden = !stress;
  $('animate-stress-limit-field').hidden = $('animate-stress-limit').disabled = !stress;
  $('animate-limit-field').hidden = $('animate-limit').disabled = stress;
  $('animate-tiling-field').hidden = kind === 'disk';
  $('animate-colouring').hidden = $('animate-colouring').disabled = !coloured;
  $('animate-energy-min-field').hidden = $('animate-energy-min').disabled = !energy;
  $('animate-limit-label').textContent = energy ? 'Upper energy limit' : 'Colour limit';
  $('animate-limit').min = energy ? '' : '0';
  $('animate-conditional-field').hidden = !energy;
  $('animate-opacity-editor').hidden = !conditional;
  $('animate-opacity-field').hidden = $('animate-opacity').disabled = conditional;
  $('animate-mesh-overlays').hidden = $('animate-defects-field').hidden = kind === 'disk';
  $('animate-defect-controls').hidden = kind === 'disk' || !$('animate-dislocations').checked;
  $('animate-defect-tolerance').disabled = $('animate-defect-tolerance-field').hidden =
    $('animate-defect-controls').hidden || $('animate-defect-method').value !== 'delaunay';
  $('animate-defect-scale').disabled = $('animate-defect-thickness').disabled = $('animate-defect-controls').hidden;
  $('animate-title-size').disabled = $('animate-title-field').hidden = !$('animate-show-title').checked;
  $('animate-node-controls').hidden = kind === 'disk' || !$('animate-show-nodes').checked;
  $('animate-node-size').disabled = $('animate-node-controls').hidden;
  $('animate-border-field').hidden = kind === 'disk';
  $('animate-show-legend-field').hidden = !coloured && $('animate-node-controls').hidden;
  $('animate-legend-size').disabled = $('animate-legend-field').hidden =
    $('animate-show-legend-field').hidden || !$('animate-show-legend').checked;
  $('animate-rate-field').hidden = $('animate-rate').disabled = $('animate-playback').value !== 'load';
  updateOpacityLimits();
}
$('animate-kind').onchange = () => {
  if ($('animate-kind').value === 'none' && !$('animate-show-nodes').checked) $('animate-show-lines').checked = true;
  updateAnimationControls();
};
for (const id of ['animate-show-nodes', 'animate-playback', 'animate-dislocations', 'animate-defect-method', 'animate-show-title',
  'animate-show-legend', 'animate-conditional-opacity']) {
  $(id).onchange = updateAnimationControls;
}
for (const id of ['animate-energy-min', 'animate-limit']) $(id).oninput = updateOpacityLimits;
for (const id of ['animate-rate', 'animate-minimum', 'animate-fps', 'animate-playback']) {
  $(id).oninput = () => {
    animationEnergyRange = null;
    updateOpacityLimits();
  };
}
function runAnimation(action, previewFrame) {
  const fields = ['animate-fps', 'animate-rate', 'animate-minimum', 'animate-workers', 'animate-node-size',
    'animate-legend-size', 'animate-title-size', 'animate-defect-scale', 'animate-defect-thickness', 'animate-defect-tolerance',
    'animate-limit', 'animate-stress-limit', 'animate-energy-min', 'animate-opacity'];
  if (fields.some(id => !$(id).reportValidity())) return;
  const number = id => $(id).valueAsNumber;
  const kind = $('animate-kind').value;
  const limit = kind === 'stress' ? 'animate-stress-limit' : 'animate-limit';
  operation(action, {...animationSelection(), preview_frame: previewFrame,
    kind, matrix: $('animate-matrix').value, stress_component: $('animate-stress-component').value,
    tiling: $('animate-tiling').value, playback: $('animate-playback').value, fps: number('animate-fps'),
    seconds_per_load: $('animate-rate').disabled ? 15 : number('animate-rate'), minimum_seconds: number('animate-minimum'),
    limit: $('animate-colouring').hidden || !$(limit).value ? null : number(limit),
    energy_min: $('animate-energy-min').disabled || !$('animate-energy-min').value ? null : number('animate-energy-min'),
    opacity: $('animate-colouring').hidden || $('animate-opacity').disabled ? 100 : number('animate-opacity'),
    opacity_curve: $('animate-opacity-editor').hidden ? null : opacityEditor.points,
    dpi: Number($('animate-dpi').value), heatmap: $('animate-heatmap').value,
    show_title: $('animate-show-title').checked, title_size: $('animate-title-size').disabled ? 8 : number('animate-title-size'),
    show_border: $('animate-show-border').checked, show_legend: $('animate-show-legend').checked,
    dislocations: !$('animate-defect-controls').hidden, dislocation_lattice: $('animate-defect-lattice').value,
    dislocation_method: $('animate-defect-method').value,
    dislocation_bond_tolerance: $('animate-defect-tolerance').disabled ? undefined : number('animate-defect-tolerance'),
    dislocation_scale: $('animate-defect-scale').disabled ? 100 : number('animate-defect-scale'),
    dislocation_thickness: $('animate-defect-thickness').disabled ? 100 : number('animate-defect-thickness'),
    workers: $('animate-workers').value ? number('animate-workers') : null,
    show_nodes: $('animate-show-nodes').checked, show_mesh_lines: $('animate-show-lines').checked,
    node_size: $('animate-node-size').disabled ? 100 : number('animate-node-size'),
    legend_size: $('animate-legend-size').disabled ? 100 : number('animate-legend-size'),
    node_colours: Object.fromEntries(['Fixed', 'Free', 'Displaced'].map(label =>
      [label, $('animate-node-' + label.toLowerCase()).value]))});
}
$('animate-preview-button').onclick = () => runAnimation('animate_preview', 'first');
$('animate-preview-middle').onclick = () => runAnimation('animate_preview', 'middle');
$('animate-button').onclick = () => runAnimation('animate');

function isMonitorPlot() {
  return monitorPlot?.plot_selection === $('plot-campaign').value;
}
function isFilePlot() {
  return filePlot?.plot_selection === $('plot-campaign').value;
}
async function selectDataFile(selection) {
  filePlot = selection;
  view('plot');
  await fillPlotSources(lastState?.campaigns || [], selection);
  await showVisualization(selection.plot_selection === '@csv' ? 'plot' : 'animate');
  $('plot').scrollIntoView({block: 'start'});
  queueWorkspaceDraft();
}
function askForDataPath(name = '', paths = []) {
  $('data-file-help').textContent = name ?
    `Choose or paste the full path to ${name}. The browser supplies the file contents and name.` :
    'Paste the full path to a CSV, PVD or VTU file.';
  $('data-file-matches').replaceChildren(...paths.map(path => new Option(path, path)));
  $('data-file-path').value = paths.length === 1 ? paths[0] : '';
  $('data-file-status').textContent = '';
  if (!$('data-file-dialog').open) $('data-file-dialog').showModal();
}
$('open-data-path').onclick = () => askForDataPath();
$('data-file-cancel').onclick = () => $('data-file-dialog').close();
$('data-file-form').onsubmit = async event => {
  event.preventDefault();
  try {
    const selection = await api('open_data_file', {path: $('data-file-path').value.trim()});
    await selectDataFile(selection);
    $('data-file-dialog').close();
  } catch (error) {$('data-file-status').textContent = error.message;}
};
let fileDragDepth = 0, openingDrop = false;
const hasDraggedFiles = event => event.dataTransfer?.types.includes('Files');
document.addEventListener('dragenter', event => {
  if (!hasDraggedFiles(event)) return;
  event.preventDefault();
  ++fileDragDepth;
  $('file-drop').hidden = false;
});
document.addEventListener('dragover', event => {
  if (!hasDraggedFiles(event)) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = 'copy';
});
document.addEventListener('dragleave', () => {
  if (fileDragDepth && !--fileDragDepth) $('file-drop').hidden = true;
});
document.addEventListener('drop', async event => {
  if (!hasDraggedFiles(event)) return;
  event.preventDefault();
  fileDragDepth = 0;
  $('file-drop').hidden = true;
  if (openingDrop) return;
  openingDrop = true;
  try {
    const files = event.dataTransfer.files;
    if (files.length !== 1) throw new Error('Drop one configuration or data file at a time.');
    const file = files[0];
    if (/\.conf$/i.test(file.name)) {
      await importSimulation(file);
    } else if (/\.(csv|pvd|vtu)$/i.test(file.name)) {
      let sha256;
      if (crypto.subtle && file.size <= 64 * 1024 ** 2) {
        let bytes;
        try {bytes = await file.arrayBuffer();}
        catch {askForDataPath(file.name); return;}
        const digest = await crypto.subtle.digest('SHA-256', bytes);
        sha256 = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
      }
      const selection = await api('open_data_file', {name: file.name, size: file.size, sha256});
      if (selection.paths) askForDataPath(file.name, selection.paths);
      else await selectDataFile(selection);
    } else throw new Error('Drop a .conf, .csv, .pvd or .vtu file.');
  } catch (error) {message(error.message);}
  finally {openingDrop = false;}
});
$('plot-other-seeds').onchange = event => {
  const include = event.target.checked;
  event.target.checked = monitorPlot.include_seeds;
  operation('plot_monitor', {selection: monitorPlot.plot_selection, include_seeds: include});
};
async function fillPlotSources(campaigns, runSelection = null) {
  const select = $('plot-campaign'), previous = select.value;
  const selected = runSelection?.plot_selection || previous;
  select.replaceChildren(...campaigns.map(name => new Option(name, name)));
  if (filePlot) select.prepend(new Option(filePlot.plot_name, filePlot.plot_selection));
  if (monitorPlot) select.prepend(new Option(monitorPlot.plot_name, monitorPlot.plot_selection));
  if ([...select.options].some(option => option.value === selected)) select.value = selected;
  else if (campaigns.length) select.value = campaigns[0];
  if (select.value && (!plotSchema || select.value !== previous || runSelection)) {
    await loadPlotOptions(true, null, Boolean(runSelection?.previous_selection));
  }
  if (firstState) await loadPlotPresets();
}
async function loadPlotOptions(resetSource, saved = null, keepAxes = false) {
  const request = ++plotRequest; ++plotCheckRequest; ++previewRevision; ++animationRequest; clearTimeout(plotTimer);
  plotSchema = null;
  plotValid = false; updateActions();
  if (resetSource || saved) previewAutomatic = true;
  const monitor = isMonitorPlot(), file = isFilePlot(), csv = file && filePlot.plot_selection === '@csv';
  if (file) $('plot-data').value = filePlot.path;
  else if (resetSource) $('plot-data').value = '';
  $('plot-data').readOnly = monitor || file;
  $('plot-other-seeds-field').classList.toggle('hidden', !monitor || !monitorPlot.has_other_seeds);
  $('plot-other-seeds').checked = monitor && monitorPlot.include_seeds;
  $('plot-seed-field').classList.toggle('hidden', monitor || file);
  $('plot-normalization-field').classList.toggle('hidden', csv);
  $('plot-normalization').disabled = csv;
  if (csv) $('plot-normalization').value = 'none';
  $('animation-settings').hidden = true;
  if (!$('plot-campaign').value) {loadAnimationOptions(); return;}
  $('plot-warning').textContent = 'Reading local data columns…';
  try {
    const query = new URLSearchParams({campaign: $('plot-campaign').value});
    if (!resetSource && $('plot-data').value.trim()) query.set('data', $('plot-data').value.trim());
    const schema = await api('plot_options?' + query);
    if (request !== plotRequest) return;
    plotSchema = schema; $('plot-data').value = schema.data;
    $('data-folders').replaceChildren(...schema.sources.map(source => new Option(`${source.runs} campaign runs`, source.path)));
    $('plot-properties').textContent = csv ? 'Numeric columns from the selected CSV file.' : monitor || file ?
      `${schema.available} of ${schema.expected} selected runs have macro data.` :
      `${schema.available} local records of ${schema.expected} planned runs. Varied parameters: ` +
      (Object.keys(schema.parameters).map(key => `${schema.labels[key]} (${schema.parameters[key].length})`).join(', ') || 'none') + '.';
    const seeds = $('plot-seed'), previous = seeds.value;
    seeds.replaceChildren(new Option($('animate-panel').hidden ? 'Mean of available seeds ± sample SD' : 'All seeds', 'all'),
      ...schema.seeds.map(seed => new Option('Seed ' + seed, JSON.stringify(seed))));
    if (!monitor && [...seeds.options].some(option => option.value === previous)) seeds.value = previous;
    $('plot-reduction').replaceChildren(...Object.entries(schema.reductions).map(([key, label]) => new Option(label, key)));
    if (saved) {
      const axes = new Set([...Object.keys(schema.parameters), ...Object.keys(schema.quantities)]);
      if (!axes.has(saved.x) || !axes.has(saved.y) ||
          [saved.color, saved.gradient].some(key => key && !(key in schema.parameters)) ||
          Object.entries(saved.fixed).some(([key, value]) => !schema.parameters[key]?.includes(value)) ||
          (saved.seed !== 'all' && !schema.seeds.includes(saved.seed))) throw new Error('This preset contains selections absent from the current campaign.');
      $('plot-seed').value = saved.seed === 'all' ? 'all' : JSON.stringify(saved.seed);
      $('plot-normalization').value = csv ? 'none' : saved.normalization || 'none';
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
    const monitorY = schema.quantities.avg_sigma12?.available ? 'avg_sigma12' :
      Object.keys(schema.quantities).find(key => key !== 'load' && schema.quantities[key].kind === 'macro' && schema.quantities[key].available);
    const csvX = schema.common_axes.x[0];
    const defaults = csv ? {x: csvX, y: schema.common_axes.y.find(key => key !== csvX)} :
      {x: 'load', y: monitorY || 'avg_sigma12'};
    updatePlotControls((monitor || file) && !keepAxes ? defaults : saved);
  } catch (error) {
    if (request !== plotRequest) return;
    plotSchema = null; $('plot-warning').textContent = error.message; updateActions();
    if (recoveringWorkspace && saved) throw error;
  }
  if (request === plotRequest) loadAnimationOptions();
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
function plotSelection(draft = false) {
  return {campaign: $('plot-campaign').value, data: $('plot-data').value,
    x: $('plot-x').value, y: $('plot-y').value, color: $('plot-color').value || null, gradient: $('plot-gradient').value || null,
    seed: $('plot-seed').value === 'all' ? 'all' : JSON.parse($('plot-seed').value), normalization: $('plot-normalization').value,
    reduction: $('plot-reduction').value,
    fixed: Object.fromEntries([...$('fixed-fields').querySelectorAll('[data-property]')].map(el => [el.dataset.property, JSON.parse(el.value)])),
    ncols: +$('legend-cols').value, legend_loc: $('legend-loc').value,
    xlim: draft ? null : limits('xlim'), ylim: draft ? null : limits('ylim'),
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
  if (!plotSchema || $('plot-panel').hidden) return;
  if (isMonitorPlot() && !plotSchema.available) {
    $('plot-warning').textContent = 'No macro data is available for this selection. Select the runs again after output is available.';
    return;
  }
  syncAxisLabels();
  const issue = axisIssue($('plot-x').value, $('plot-y').value);
  if (issue) {$('plot-warning').textContent = issue; return;}
  if (!$('plot-data').value) {$('plot-warning').textContent = 'No local data is available. Download campaign results or choose a local collection.'; return;}
  if (recoveringWorkspace) return;
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
  if (isFilePlot() && filePlot.plot_selection === '@csv') {
    $('plot-warning').textContent = result.warnings.join(' ') || 'Plotting numeric values in CSV row order.';
    return;
  }
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
    queueWorkspaceDraft();
  } catch (error) {
    if (revision === previewRevision) {plotValid = false; $('plot-warning').textContent = error.message; $('plot-preview-status').textContent = 'Preview unavailable.';}
  } finally {
    clearTimeout(slow); previewBusy = false; updateActions();
    const pending = previewManualPending; previewManualPending = false;
    if (pending || (revision !== previewRevision && previewAutomatic)) requestPlotPreview(pending);
  }
}
function formFields(scope) {
  return [...document.querySelectorAll(scope)].flatMap(root =>
    [...root.querySelectorAll('input[id], select[id], textarea[id]')]).filter(input =>
    !['file', 'password', 'button', 'submit'].includes(input.type));
}
function captureFields(scope) {
  return Object.fromEntries(formFields(scope).map(input => [input.id,
    input.type === 'checkbox' ? input.checked : input.value]));
}
function restoreFields(scope, fields) {
  const skipped = [];
  for (const input of formFields(scope)) {
    if (!Object.hasOwn(fields, input.id)) continue;
    const value = fields[input.id];
    if (input.type === 'checkbox' && typeof value === 'boolean') input.checked = value;
    else if (typeof value === 'string' && (input.tagName !== 'SELECT' ||
             [...input.options].some(option => option.value === value && !option.disabled))) input.value = value;
    else skipped.push(input.id);
  }
  return skipped;
}
const animationFields = '#animation-settings .animation-groups';
function renderAnimationPresets() {
  const list = $('animation-presets-list');
  list.replaceChildren();
  for (const preset of animationPresets) {
    const row = document.createElement('div'), label = document.createElement('span'), button = document.createElement('button');
    row.className = 'plot-preset-row'; label.textContent = preset.name;
    button.type = 'button'; button.className = 'small'; button.textContent = 'Load';
    button.onclick = () => {
      const status = $('animation-preset-status');
      try {
        opacityEditor.restore(preset.selection.curve);
        const skipped = restoreFields(animationFields, preset.selection.fields);
        animationHeatmapKind = $('animate-kind').value;
        animationEnergyRange = null;
        updateAnimationControls();
        loadedAnimationPresetId = preset.id;
        $('animation-preset-name').value = preset.name;
        status.textContent = skipped.length ? 'Preset loaded with settings adjusted for the selected simulation.' : 'Loaded ' + preset.name + '.';
        updateActions();
        queueWorkspaceDraft();
      } catch (error) {status.textContent = error.message;}
    };
    row.append(label, button); list.append(row);
  }
  if (!animationPresets.length) list.textContent = 'No saved animation presets.';
}
async function loadAnimationPresets() {
  try {
    animationPresets = (await api('animation_presets')).presets;
    renderAnimationPresets();
  } catch (error) {$('animation-preset-status').textContent = error.message;}
}
async function saveAnimationPreset(update) {
  const status = $('animation-preset-status');
  try {
    const result = await api('animation_presets', {name: $('animation-preset-name').value,
      selection: {fields: captureFields(animationFields), curve: opacityEditor.points},
      ...(update ? {id: loadedAnimationPresetId} : {})});
    animationPresets = result.presets;
    loadedAnimationPresetId = result.saved_id;
    renderAnimationPresets();
    updateActions();
    status.textContent = update ? 'Preset updated.' : 'New preset saved.';
    queueWorkspaceDraft();
  } catch (error) {status.textContent = error.message;}
}
$('animation-preset-new').onclick = () => saveAnimationPreset(false);
$('animation-preset-update').onclick = () => saveAnimationPreset(true);

const draftKey = 'mts-workspace-draft';
const draftForms = '#run-form, #campaign-form, #plot-form, #jobs-card, #advanced-tools';
const draftPanels = '#start, #plot, #setup, #monitor';
function campaignAxes() {
  return [...$('campaign-axes').children].map(row => Object.fromEntries(
    [...row.querySelectorAll('[data-axis]')].map(input => [input.dataset.axis, input.value])));
}
function mediaPath(id, property) {
  const element = $(id), url = element.getAttribute(property);
  return url ? new URL(url, location.href).pathname : null;
}
function saveWorkspaceDraft() {
  clearTimeout(draftTimer);
  if (recoveringWorkspace || !lastState || !Object.values(lastState.packages).every(Boolean) || !$('plot-form')) return;
  try {
    const plot = plotSchema && $('plot-x').value && $('plot-y').value ? plotSelection(true) : null;
    sessionStorage.setItem(draftKey, JSON.stringify({
      version: 1, workspace: lastState.workspace, view: document.querySelector('.view.active')?.id,
      tab: $('animate-panel').hidden ? 'plot' : 'animate', fields: captureFields(draftForms),
      details: [...document.querySelectorAll('details[id]')].filter(node => node.closest(draftPanels) && node.open).map(node => node.id),
      monitorPlot, filePlot, plot, curve: opacityEditor.points, campaignAxes: campaignAxes(), continuationPlan,
      animationEnergyRange, animationSourceKey, animationLatticeKey, lastAnimationScan,
      inventory: {sort: inventorySort, ascending: inventoryAscending, open: [...inventoryOpen]},
      presets: {plot: loadedPresetId, animation: loadedAnimationPresetId},
      media: {plot: $('plot-preview-image').classList.contains('hidden') ? null : mediaPath('plot-preview-image', 'src'),
        stale: $('plot-preview-image').parentElement.classList.contains('is-stale'),
        animation: $('animate-preview-output').hidden ? null : mediaPath('animate-preview-image', 'src'),
        video: $('animate-output').hidden ? null : mediaPath('animate-video', 'src'),
        videoTime: $('animate-video').currentTime, pdf: mediaPath('plot-open-pdf', 'href')},
      previewAutomatic, appliedTask: lastResult, slurmCriteria: slurmCriteria(), scroll: window.scrollY,
    }));
  } catch {} // A browser may deny access to session storage.
}
function queueWorkspaceDraft() {
  clearTimeout(draftTimer);
  if (!recoveringWorkspace) draftTimer = setTimeout(saveWorkspaceDraft, 200);
}
for (const event of ['input', 'change', 'click', 'toggle', 'pointerup']) document.addEventListener(event, queueWorkspaceDraft, true);
window.addEventListener('pagehide', saveWorkspaceDraft);

function restoreMedia(saved) {
  const items = [
    ['plot-preview-image', saved.plot, '/previews/', 'src'],
    ['animate-preview-image', saved.animation, '/previews/', 'src'],
    ['animate-video', saved.video, '/videos/', 'src'],
    ['animate-download', saved.video, '/videos/', 'href'],
    ['plot-open-pdf', saved.pdf, '/figures/', 'href'],
  ];
  for (const [id, path, prefix, attribute] of items) {
    if (!path) continue;
    if (typeof path !== 'string' || !path.startsWith(prefix) || path.includes('..')) throw new Error('Invalid saved media path.');
    const element = $(id);
    if (attribute === 'src') element.addEventListener('error', () => {
      if (mediaPath(id, attribute) !== path) return;
      message('A saved preview or video is no longer available.');
      if (id === 'plot-preview-image') {
        element.classList.add('hidden');
        $('plot-preview-placeholder').classList.remove('hidden');
        $('plot-preview-status').textContent = 'Preview unavailable.';
      } else {
        $(id === 'animate-video' ? 'animate-output' : 'animate-preview-output').hidden = true;
      }
    }, {once: true});
    element[attribute] = path + '?token=' + encodeURIComponent(accessToken);
  }
  if (saved.plot) {
    $('plot-preview-image').classList.remove('hidden');
    $('plot-preview-placeholder').classList.add('hidden');
    $('plot-preview-image').parentElement.classList.toggle('is-stale', Boolean(saved.stale));
    $('plot-preview-status').textContent = saved.stale ? 'Preview out of date.' : '';
  }
  $('animate-preview-output').hidden = !saved.animation;
  $('animate-output').hidden = !saved.video;
  if (saved.pdf) $('plot-open-pdf').classList.remove('hidden');
  if (saved.video && Number.isFinite(saved.videoTime)) {
    const video = $('animate-video');
    const seek = () => {if (Number.isFinite(video.duration)) video.currentTime = Math.min(saved.videoTime, video.duration);};
    if (video.readyState) seek(); else video.addEventListener('loadedmetadata', seek, {once: true});
  }
}

async function recoverWorkspace() {
  let draft;
  try {
    const saved = sessionStorage.getItem(draftKey);
    if (!saved) return;
    draft = JSON.parse(saved);
    if (draft.version !== 1 || draft.workspace !== lastState.workspace || !draft.fields || !draft.inventory) {
      throw new Error('Saved workspace settings use an incompatible format.');
    }
  } catch (error) {message('Workspace recovery failed. ' + error.message); return;}
  const attempt = async (label, recover) => {
    try {await recover();}
    catch (error) {message(`Could not restore ${label}. ${error.message}`);}
  };
  await attempt('the view and inventory', async () => {
    view(draft.view);
    lastResult = draft.appliedTask;
    const source = draft.fields['jobs-site'];
    if ([...$('jobs-site').options].some(option => option.value === source)) {
      $('jobs-site').value = source;
    } else if (source) message('The saved Monitor source is no longer available.');
    const sort = [...document.querySelectorAll('.inventory-sort')].find(button => button.dataset.sort === draft.inventory.sort);
    if (sort) {
      inventorySort = draft.inventory.sort;
      inventoryAscending = Boolean(draft.inventory.ascending);
      document.querySelector('#jobs-card th[aria-sort]')?.removeAttribute('aria-sort');
      sort.closest('th').setAttribute('aria-sort', inventoryAscending ? 'ascending' : 'descending');
    }
    for (const [key, open] of draft.inventory.open) inventoryOpen.set(key, Boolean(open));
    if ($('jobs-site').value) await loadInventory(false);
  });
  await attempt('simulation and campaign settings', async () => {
    setContinuation(draft.continuationPlan ?? null);
    restoreFields('#run-form, #campaign-form', draft.fields);
    setDirectionChoices($('run-directions').value);
    const skipped = restoreFields('#run-form', draft.fields);
    if (continuationPlan) {
      $('run-extra').value = $('run-extra').value.split('\n').filter(line =>
        !continuationPlan.fixed_settings.includes(line.split('=', 1)[0].trim())).join('\n');
    }
    renderExperiment();
    if ($('campaign-base').value) await $('campaign-base').onchange();
    skipped.push(...restoreFields('#campaign-form', draft.fields));
    $('campaign-axes').replaceChildren();
    for (const axis of draft.campaignAxes) {
      const row = addCampaignAxis();
      for (const input of row.querySelectorAll('[data-axis]')) {
        if (input.dataset.axis === 'parameter') fillCampaignParameters(input, axis.parameter);
        else if (Object.hasOwn(axis, input.dataset.axis)) input.value = axis[input.dataset.axis];
      }
      row.querySelector('[data-axis="parameter"]').onchange();
    }
    updateCampaignVtuWarning();
    if (skipped.length) throw new Error('Some options are no longer available.');
  });
  await attempt('setup settings', async () => {
    const skipped = restoreFields('#advanced-tools', draft.fields);
    if (slurmSchema && draft.slurmCriteria) {
      $('slurm-criteria').replaceChildren();
      for (const criterion of draft.slurmCriteria) {
        if (!Object.hasOwn(slurmSchema.fields, criterion.field)) {
          skipped.push(criterion.field);
          continue;
        }
        addSlurmCriterion(criterion);
      }
    }
    if (skipped.length) throw new Error('Some options are no longer available.');
  });
  await attempt('plot settings', async () => {
    const name = draft.fields['plot-campaign'];
    if (draft.filePlot) {
      filePlot = draft.filePlot;
      $('plot-campaign').prepend(new Option(filePlot.plot_name, filePlot.plot_selection));
    }
    if (draft.monitorPlot) {
      const source = draft.monitorPlot.plot_selection;
      await attempt('the Monitor plot selection', async () => {
        await api('plot_options?' + new URLSearchParams({campaign: source}));
        monitorPlot = draft.monitorPlot;
        $('plot-campaign').prepend(new Option(monitorPlot.plot_name, source));
      });
      if (!monitorPlot && name === source) return;
    }
    if (!name) return;
    if (![...$('plot-campaign').options].some(option => option.value === name)) {
      throw new Error('The selected campaign or simulation is unavailable.');
    }
    $('plot-campaign').value = name;
    $('plot-data').value = draft.fields['plot-data'] || '';
    await loadPlotOptions(false, draft.plot, true);
    if (!plotSchema) throw new Error($('plot-warning').textContent);
    const skipped = restoreFields('#plot-panel', draft.fields);
    if ($('plot-data').value && plotSchema && !axisIssue($('plot-x').value, $('plot-y').value)) {
      let spec;
      try {spec = plotSelection();} catch {} // Keep an unfinished axis limit available for editing.
      if (spec) {
        const result = await api('plot_check?' + new URLSearchParams({selection: JSON.stringify(spec)}));
        showPlotValidation(result);
        plotValid = true;
      }
    }
    if (skipped.length) throw new Error('Some options are no longer available.');
  });
  await attempt('animation settings', async () => {
    const skipped = restoreFields(animationFields, draft.fields);
    if (!Object.hasOwn(draft.fields, 'animate-stress-limit') && !$('animate-energy-min').value) {
      $('animate-energy-min').value = $('animate-energy-min').defaultValue;
    }
    opacityEditor.restore(draft.curve);
    animationEnergyRange = draft.animationEnergyRange;
    lastAnimationScan = draft.lastAnimationScan;
    animationSourceKey = draft.animationSourceKey || '';
    animationLatticeKey = draft.animationLatticeKey || '';
    animationHeatmapKind = $('animate-kind').value;
    updateAnimationControls();
    await showVisualization(draft.tab === 'animate' ? 'animate' : 'plot');
    if (skipped.length) throw new Error('Some options are no longer available.');
  });
  await attempt('workspace details', async () => {
    for (const id of draft.details) if ($(id)?.tagName === 'DETAILS') $(id).open = true;
    loadedPresetId = plotPresets.some(preset => preset.id === draft.presets?.plot) ? draft.presets.plot : null;
    loadedAnimationPresetId = animationPresets.some(preset => preset.id === draft.presets?.animation) ? draft.presets.animation : null;
    restoreFields('.plot-presets', draft.fields);
    previewAutomatic = draft.previewAutomatic !== false;
    if (draft.media) {
      const media = {...draft.media};
      if (draft.animationSourceKey !== JSON.stringify(animationSelection())) media.animation = media.video = null;
      if (!plotSchema || $('plot-campaign').value !== draft.fields['plot-campaign']) media.stale = true;
      restoreMedia(media);
    }
    if (Number.isFinite(draft.scroll)) requestAnimationFrame(() => window.scrollTo(0, draft.scroll));
  });
  updateActions();
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
$('plot-seed').onchange = () => {schedulePlotCheck(); loadAnimationOptions();};
for (const id of ['plot-normalization', 'plot-reduction', 'legend-cols', 'legend-loc', 'plot-xscale', 'plot-yscale', 'xlim', 'ylim']) $(id).onchange = schedulePlotCheck;
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
function slurmCriteria() {
  return [...$('slurm-criteria').children].map(row => {
    const field = row.querySelector('[data-role="field"]').value;
    let value = row.querySelector('[data-role="value"]').value;
    if (value === '__custom__') value = row.querySelector('[data-role="custom"]').value;
    return {field, operator: row.querySelector('[data-role="operator"]').value, value};
  });
}
function slurmSelection() {
  const criteria = slurmCriteria().map(({field, operator, value}) => {
    const type = slurmSchema.fields[field].type;
    if (['int', 'float'].includes(type)) {
      if (!value.trim() || !Number.isFinite(+value) || (type === 'int' && !Number.isInteger(+value))) throw new Error('Enter a valid ' + type + ' for ' + slurmSchema.fields[field].label + '.');
      value = +value;
    } else if (type === 'bool') {
      if (!['true', 'false'].includes(value.toLowerCase())) throw new Error('Boolean criteria require true or false.');
      value = value.toLowerCase() === 'true';
    }
    return {field, operator, value};
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
  saveWorkspaceDraft();
  try {await api('quit', {}); clearInterval(poller);
    try {localStorage.removeItem('mts-token'); sessionStorage.removeItem('mts-token'); sessionStorage.removeItem('mts-terminal-history');} catch {}
    document.querySelector('.workspace').innerHTML = '<div class="guide"><h1>MTS is closed.</h1><p>Use the desktop launcher to restart. Simulation jobs continue.</p></div>';}
  catch (error) {message(error.message);}
};
$('restart').onclick = async () => {
  let entry;
  try {
    saveWorkspaceDraft();
    await api('restart', {});
    restarting = true;
    clearInterval(poller);
    for (const id of actionButtons) {
      $(id).disabled = true;
      $(id).parentElement.title = 'Wait for MTS to restart.';
    }
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
setInterval(inventoryAge, 60000);

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
