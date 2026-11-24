const preferenceKey = 'mts-inventory-columns';

export function configureInventoryColumns(header, trigger, menu, onChange) {
  const headers = [...header.querySelectorAll('th[data-column]')];
  const byKey = new Map(headers.map(cell => [cell.dataset.column, cell]));
  let order = [...byKey.keys()], hidden = new Set();
  try {
    const saved = JSON.parse(localStorage.getItem(preferenceKey));
    if (Array.isArray(saved?.order)) {
      order = [...new Set([...saved.order.filter(key => byKey.has(key)), ...order])];
    }
    if (Array.isArray(saved?.hidden)) {
      hidden = new Set(saved.hidden.filter(key => byKey.has(key)));
      if (hidden.size === order.length) hidden.clear();
    }
  } catch {} // The same table is available without saved preferences.

  const visible = () => order.filter(key => !hidden.has(key));
  const updateChecks = () => {
    const shown = visible().length;
    for (const input of menu.querySelectorAll('input')) {
      input.checked = !hidden.has(input.value);
      input.disabled = input.checked && shown === 1;
    }
  };
  function apply() {
    for (const key of order) {
      const cell = byKey.get(key);
      cell.hidden = hidden.has(key);
      header.append(cell);
    }
  }
  function save() {
    apply();
    updateChecks();
    try {localStorage.setItem(preferenceKey, JSON.stringify({order, hidden: [...hidden]}));} catch {}
    onChange();
  }

  function open(left, top, fromPointer = false) {
    menu.replaceChildren(...order.map(key => {
      const label = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.value = key;
      input.onchange = () => {
        if (input.checked) hidden.delete(key);
        else hidden.add(key);
        save();
      };
      label.append(input, byKey.get(key).textContent.trim());
      return label;
    }));
    updateChecks();
    const gap = 4;
    const abovePointer = fromPointer && top >= 64;
    const availableHeight = abovePointer ? top : window.innerHeight - (fromPointer ? top : 0);
    menu.style.maxHeight = `${availableHeight - 2 * gap}px`;
    if (!menu.matches(':popover-open')) menu.showPopover({source: trigger});
    const bounds = menu.getBoundingClientRect();
    if (fromPointer) top += abovePointer ? -bounds.height - gap : gap;
    menu.style.left = Math.max(gap, Math.min(left, document.documentElement.clientWidth - bounds.width - gap)) + 'px';
    menu.style.top = Math.max(gap, Math.min(top, window.innerHeight - bounds.height - gap)) + 'px';
    menu.querySelector('input:enabled')?.focus({preventScroll: true});
  }
  trigger.onclick = () => {
    if (menu.matches(':popover-open')) {menu.hidePopover(); return;}
    const bounds = trigger.getBoundingClientRect();
    open(bounds.left, bounds.bottom + 4);
  };
  menu.addEventListener('beforetoggle', event => {
    trigger.setAttribute('aria-expanded', String(event.newState === 'open'));
  });
  header.addEventListener('contextmenu', event => {
    event.preventDefault();
    const {clientX, clientY, buttons} = event;
    const show = () => requestAnimationFrame(() => open(clientX, clientY, true));
    if (!buttons) {show(); return;}
    // Let the opening click finish before the popover can light-dismiss.
    const release = new AbortController();
    window.addEventListener('mouseup', () => {release.abort(); show();}, {capture: true, signal: release.signal});
    window.addEventListener('blur', () => release.abort(), {signal: release.signal});
  });

  let dragged = null, target = null;
  function clearTarget() {
    if (target) delete target.dataset.dropEdge;
    target = null;
  }
  for (const cell of headers) {
    cell.draggable = true;
    cell.addEventListener('dragstart', event => {
      dragged = cell.dataset.column;
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', dragged);
    });
    cell.addEventListener('dragover', event => {
      if (!dragged || dragged === cell.dataset.column) {clearTarget(); return;}
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      if (target !== cell) {clearTarget(); target = cell;}
      const bounds = cell.getBoundingClientRect();
      cell.dataset.dropEdge = event.clientX < bounds.left + bounds.width / 2 ? 'before' : 'after';
    });
    cell.addEventListener('drop', event => {
      if (!dragged || target !== cell) return;
      event.preventDefault();
      const next = order.filter(key => key !== dragged);
      next.splice(next.indexOf(cell.dataset.column) + Number(cell.dataset.dropEdge === 'after'), 0, dragged);
      order = next;
      clearTarget();
      dragged = null;
      save();
    });
    cell.addEventListener('dragend', () => {dragged = null; clearTarget();});
  }
  header.addEventListener('dragleave', event => {
    if (!header.contains(event.relatedTarget)) clearTarget();
  });
  apply();
  return visible;
}
