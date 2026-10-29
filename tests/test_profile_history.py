"""Exercise browser state without starting a browser or an SSH connection."""

from pathlib import Path
import subprocess
import unittest


class ProfileHistory(unittest.TestCase):
    def test_navigation_restores_panel_after_reload_without_terminal_messages(self):
        source = Path(__file__).resolve().parents[1] / "MTS_GUI/static/app.js"
        subprocess.run(["node", "-", str(source)], input=r'''
const assert = require('node:assert/strict');
const source = require('node:fs').readFileSync(process.argv[2], 'utf8');
const stored = new Map();
global.sessionStorage = {setItem: (key, value) => stored.set(key, value), getItem: key => stored.get(key)};
const panels = ['overview', 'setup', 'monitor'].map(id => {
  const node = {id, active: id === 'overview'};
  node.classList = {contains: name => name === 'view', toggle: (name, value) => node.active = value};
  return node;
});
const buttons = panels.map(panel => ({dataset: {view: panel.id}, classList: {toggle() {}}, addEventListener() {}}));
global.$ = id => panels.find(panel => panel.id === id);
global.document = {querySelectorAll: selector => selector === '.view' ? panels : buttons};
const terminalMessage = () => {throw new Error('Navigation must not log a terminal message');};
const navigation = source.slice(source.indexOf('function view('), source.indexOf('async function operation('));
eval(navigation);
view('setup');
panels.forEach(panel => panel.active = panel.id === 'overview');
eval(navigation); // The same initialization runs after the restart reloads the page.
assert.deepEqual(panels.filter(panel => panel.active).map(panel => panel.id), ['setup']);
''', text=True, check=True, capture_output=True, timeout=10)

    def test_terminal_navigation_and_clear_keep_running_view_and_updates(self):
        source = Path(__file__).resolve().parents[1] / "MTS_GUI/static/app.js"
        subprocess.run(["node", "-", str(source)], input=r'''
const assert = require('node:assert/strict');
const source = require('node:fs').readFileSync(process.argv[2], 'utf8');
const task = {id: 'task', state: 'running', logs: ['first line'], title: 'Update MTS'};
const operation = {id: 'browser-operation', task_id: task.id, state: 'running', logs: [...task.logs], title: task.title};
let backendHistory = [task], clientHistory = [operation], pendingOperation = operation, hiddenBackendEntries = new Set();
const lastState = {interface_version: 3}, accessToken = 'test';
global.sessionStorage = {setItem() {}};
const container = {children: [], append(node) {this.children.push(node);}, scrollHeight: 1};
global.$ = () => container;
global.document = {createElement: () => {
  const content = Object.fromEntries(['button', 'summary span', 'summary', 'pre'].map(key => [key, {}]));
  return {dataset: {}, querySelector: key => content[key], remove() {
    container.children.splice(container.children.indexOf(this), 1);
  }};
}};
const api = async () => {backendHistory = backendHistory.filter(entry => entry.state === 'running');};
const refresh = async () => renderTerminal(backendHistory);
const message = text => {throw new Error(text);};
eval(source.slice(source.indexOf('function renderTerminal('), source.indexOf('function fillSites(')));
eval(source.slice(source.indexOf('async function clearTerminal('), source.indexOf("$('clear-terminal').onclick")));
renderTerminal();
const running = container.children[0];
clientHistory.push({id: 'browser-view', title: 'View', state: 'complete', logs: ['SSH connections'], created_at: 1});
renderTerminal();
assert.equal(running.open, true, 'navigation must not collapse a running log');
(async () => {
  await clearTerminal();
  assert.deepEqual(container.children, [running]);
  assert.equal(running.open, true);
  assert.equal(pendingOperation, operation);
  operation.logs.push('still updating'); renderTerminal();
  assert.match(running.querySelector('pre').textContent, /still updating/);
  task.state = operation.state = 'complete'; await clearTerminal();
  assert.equal(container.children.length, 0);
})().catch(error => {console.error(error); process.exitCode = 1;});
''', text=True, check=True, capture_output=True, timeout=10)

    def test_suggestions_undo_redo_revert_and_branching(self):
        source = Path(__file__).resolve().parents[1] / "MTS_GUI/static/app.js"
        subprocess.run(["node", "-", str(source)], input=r'''
const assert = require('node:assert/strict');
const source = require('node:fs').readFileSync(process.argv[2], 'utf8');
const fields = {directory: {value: '~/saved'}, python: {value: 'python3'}, mode: {value: 'detached'}};
const events = {};
const form = {elements: {namedItem: name => fields[name]}, addEventListener: (name, fn) => events[name] = fn};
global.FormData = class {constructor() {return Object.entries(fields).map(([key, input]) => [key, input.value]);}};
global.document = {createElement: () => ({dataset: {}})};
const buttons = [], actions = {append: button => {
  const index = buttons.indexOf(button); if (index >= 0) buttons.splice(index, 1);
  buttons.push(button);
}};
global.updateActions = () => buttons.forEach(button => button.disabled = button.dataset.unavailable === 'true');
eval(source.slice(source.indexOf('function trackProfileEdits('), source.indexOf('function closeSSHEditor(')));
let slurmVisible = false;
trackProfileEdits(form, actions, true, {textContent: 'Save profile', dataset: {}}, () => slurmVisible = fields.mode.value === 'slurm');
const button = label => buttons.find(item => item.textContent === label);
const click = label => {assert.equal(button(label).disabled, false, label); button(label).onclick();};
assert.equal(button('Undo').hidden, true);
fields.directory.value = '~/manual'; events.input();
form.profileHistory.record();
fields.python.value = '/suggested/python'; form.profileHistory.record();
click('Undo'); assert.equal(fields.python.value, 'python3');
click('Undo'); assert.equal(fields.directory.value, '~/saved');
click('Redo'); assert.equal(fields.directory.value, '~/manual');
click('Redo'); assert.equal(fields.python.value, '/suggested/python');
click('Revert profile'); assert.equal(fields.directory.value, '~/saved');
assert.equal(fields.python.value, 'python3');
click('Undo'); assert.equal(fields.python.value, '/suggested/python');
click('Undo'); fields.directory.value = '~/branch'; events.change();
assert.equal(button('Redo').disabled, true);
click('Revert profile');
assert.equal(fields.directory.value, '~/saved');
assert.equal(button('Revert profile').disabled, true);
fields.mode.value = 'slurm'; events.change();
assert.equal(slurmVisible, true);
click('Undo'); assert.equal(slurmVisible, false);
assert.deepEqual(buttons.slice(-2).map(item => item.textContent), ['Revert profile', 'Save profile']);
''', text=True, check=True, capture_output=True, timeout=10)
