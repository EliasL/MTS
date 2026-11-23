const $ = id => document.getElementById(id);

export function axisIssue(plotSchema, x, y) {
  const params = plotSchema.parameters, quantities = plotSchema.quantities;
  if (!(x in params) && quantities[x]?.kind !== 'macro') return 'X must be a varied parameter or a saved response column.';
  if (!(y in quantities)) return 'Y must be a measured quantity.';
  if (x === y) return 'X and Y must be different quantities.';
  if (!(x in params) && quantities[y].kind !== 'macro') return 'A timing summary requires a campaign parameter on X.';
  if ([x, y].some(key => key in quantities && !quantities[key].available)) return 'The selected quantity is unavailable in this local collection.';
  return '';
}

export function plotChoices(id, choices, selected, issue, order = []) {
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

export function fixedFields(plotSchema, schedulePlotCheck, saved = null) {
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

export function legendAliases(plotSchema, schedulePlotCheck, saved = null) {
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

export function syncAxisLabels(plotSchema) {
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

export function limits(id) {
  if (!$(id).value.trim()) return null;
  const values = $(id).value.split(',').map(Number);
  if (values.length !== 2 || !values.every(Number.isFinite) || values[0] >= values[1]) throw new Error('Axis limits need two increasing numbers separated by a comma.');
  return values;
}
