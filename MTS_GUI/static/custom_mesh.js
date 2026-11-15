class CustomMeshEditor {
  constructor(getConfiguration, loadPreview, onChange) {
    this.getConfiguration = getConfiguration;
    this.loadPreview = loadPreview;
    this.onChange = onChange;
    const find = id => document.getElementById(id);
    this.section = find('run-custom-mesh');
    this.viewport = find('mesh-viewport');
    this.canvas = find('mesh-canvas');
    this.brush = find('mesh-brush');
    this.radius = find('mesh-radius');
    this.radiusValue = find('mesh-radius-value');
    this.status = find('mesh-status');
    this.legend = find('mesh-legend');
    this.periodicKey = find('mesh-periodic-key');
    this.loadedKey = find('mesh-loaded-key');
    this.resetButton = find('mesh-reset');
    this.downloadButton = find('mesh-download');
    this.retryButton = find('mesh-retry');
    this.section.addEventListener('toggle', () => this.refresh());
    this.retryButton.onclick = () => { this.requestKey = null; this.refresh(); };
    this.resetButton.onclick = () => { this.resetEdits(); this.update(); };
    this.downloadButton.onclick = () => {
      const url = URL.createObjectURL(new Blob([this.exportVtu()], {type: 'application/vnd.vtk.vtu'}));
      const link = document.createElement('a');
      link.href = url;
      link.download = 'initialMesh.vtu';
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    };
    this.radius.oninput = () => {
      this.radiusValue.value = Number(this.radius.value).toFixed(2);
      if (this.cursor) this.showBrush(this.cursor);
    };
    this.canvas.addEventListener('wheel', event => {
      if (!this.transform || !event.deltaY) return;
      event.preventDefault();
      this.radius.value = Number(this.radius.value) * (event.deltaY < 0 ? 1.16 : 1 / 1.16);
      this.radius.oninput();
    }, {passive: false});
    this.canvas.onpointerdown = event => {
      if (event.button !== 0 || !this.transform) return;
      this.lastStroke = null;
      this.canvas.setPointerCapture(event.pointerId);
      this.movePointer(event, true);
    };
    this.canvas.onpointermove = event => this.movePointer(event, event.buttons === 1);
    this.canvas.onpointerleave = this.canvas.onlostpointercapture = () => {
      this.cursor = this.lastStroke = null;
      this.brush.hidden = true;
    };
    new ResizeObserver(() => this.scheduleDraw()).observe(this.canvas);
  }

  get modified() {
    return Boolean(this.mesh && (this.removedCount || this.retriangulated.size));
  }

  clear() {
    clearTimeout(this.timer);
    this.controller?.abort();
    this.mesh = this.requestKey = this.transform = null;
    this.error = '';
    this.pending = false;
    this.viewport.hidden = this.legend.hidden = true;
  }

  refresh() {
    const data = this.getConfiguration();
    if (!data || (!this.section.open && !this.modified)) return;
    const {settings, site, overrides} = data;
    const {rows, cols, boundary, deformation, directions, startLoad, loadIncrement,
      segmentLoad, repeat, energyFunction, drivenEdge, top, bottom, left, right,
      indenterRadius, reconnectionMethod, totalLoad} = settings;
    const key = JSON.stringify([rows, cols, boundary, deformation, directions, startLoad, loadIncrement,
      segmentLoad, repeat, energyFunction, drivenEdge, top, bottom, left, right,
      indenterRadius, reconnectionMethod, totalLoad, site, overrides]);
    if (key === this.requestKey) return;
    this.requestKey = key;
    this.controller?.abort();
    clearTimeout(this.timer);
    const controller = new AbortController();
    this.controller = controller;
    this.pending = true;
    this.error = '';
    this.update(false);
    this.timer = setTimeout(async () => {
      try {
        const text = await this.loadPreview(data, controller.signal);
        if (controller.signal.aborted) return;
        const next = this.parseVtu(text, settings);
        const same = this.mesh && ['points', 'original'].every(field =>
          this.mesh[field].length === next[field].length &&
          this.mesh[field].every((value, index) => next[field][index] === value));
        const replaced = this.modified && !same;
        this.mesh = next;
        if (!same) this.resetEdits();
        this.notice = replaced ? 'Mesh regenerated for the new lattice.' : '';
        this.viewport.hidden = this.legend.hidden = false;
        this.periodicKey.hidden = !next.periodic;
        this.loadedKey.hidden = !next.imageMoved;
      } catch (error) {
        if (controller.signal.aborted) return;
        this.error = error.message;
      }
      this.pending = false;
      this.update();
    }, 260);
  }

  parseVtu(text, settings) {
    const xml = new DOMParser().parseFromString(text, 'application/xml');
    const array = (selector, Type = Float64Array) => {
      const node = xml.querySelector(selector);
      if (!node || node.getAttribute('format') !== 'ascii') throw new Error('Invalid mesh preview VTU.');
      return Type.from(node.textContent.trim().split(/\s+/), Number);
    };
    const field = name => array(`PointData DataArray[Name="${name}"]`);
    const points = array('Points DataArray'), original = array('Cells DataArray[Name="connectivity"]', Int32Array);
    const lattice = field('lattice_index'), current = field('current_position'), loaded = field('loaded_position');
    const fixed = field('fixed_dofs'), count = original.length / 3;
    if (count < 1 || !Number.isInteger(count) || current.length !== points.length ||
        loaded.length !== points.length || lattice.length !== points.length || 3 * fixed.length !== points.length) {
      throw new Error('Invalid mesh preview arrays.');
    }
    const {rows, cols, boundary, deformation, drivenEdge} = settings;
    const periodic = ['periodic', 'periodicAnchored'].includes(boundary);
    const stride = cols + 1;
    const pointByIndex = new Map();
    for (let i = 0; i < fixed.length; i++) pointByIndex.set(lattice[3 * i] + stride * lattice[3 * i + 1], i);
    const tile = new Int32Array(count), partners = new Int32Array(count).fill(-1);
    const firstInTile = new Map();
    for (let i = 0; i < count; i++) {
      const a = 3 * original[3 * i], b = 3 * original[3 * i + 1], c = 3 * original[3 * i + 2];
      const key = Math.min(lattice[a], lattice[b], lattice[c]) +
        stride * Math.min(lattice[a + 1], lattice[b + 1], lattice[c + 1]);
      tile[i] = key;
      if (firstInTile.has(key)) {
        const other = firstInTile.get(key);
        partners[other] = i;
        partners[i] = other;
      } else firstInTile.set(key, i);
    }
    const cell = position => {
      const o = 3 * pointByIndex.get(0), x = 3 * pointByIndex.get(cols), y = 3 * pointByIndex.get(rows * stride);
      return [[position[x] - position[o], position[x + 1] - position[o + 1]],
        [position[y] - position[o], position[y + 1] - position[o + 1]]];
    };
    let image = [], loadedImage = [], imageMoved = false;
    if (periodic) {
      const before = cell(current), after = cell(loaded);
      const movement = before.map(([x, y], i) => Math.hypot(after[i][0] - x, after[i][1] - y));
      const adjacent = movement[1] >= movement[0] ? 1 : 0;
      const outline = ([a, b]) => {
        const i = 3 * pointByIndex.get(0);
        const start = [current[i] + [a, b][adjacent][0], current[i + 1] + [a, b][adjacent][1]];
        return [start, [start[0] + a[0], start[1] + a[1]],
          [start[0] + a[0] + b[0], start[1] + a[1] + b[1]], [start[0] + b[0], start[1] + b[1]]];
      };
      image = outline(before);
      loadedImage = outline(after);
      imageMoved = Math.max(...movement) > 1e-10;
    }
    return {points, original, lattice, current, loaded, fixed, count, rows, cols, boundary, deformation,
      drivenEdge, periodic, image, loadedImage, imageMoved, stride, pointByIndex, tile, partners};
  }

  resetEdits() {
    this.triangles = this.mesh.original.slice();
    this.removed = new Uint8Array(this.mesh.count);
    this.removedCount = 0;
    this.retriangulated = new Set();
    this.notice = '';
  }

  issue() {
    if (!this.modified) return '';
    if (this.pending || this.error) return 'Refresh the custom mesh preview for the current settings.';
    if (this.removedCount === this.mesh.count) return 'Restore at least one mesh triangle.';
    if (this.getConfiguration()?.settings.reconnectionMethod === 'delaunay') {
      return 'Use None or Edge flip reconnection for a custom mesh.';
    }
    const {boundary, drivenEdge, rows, cols, lattice} = this.mesh;
    if (boundary === 'rectangle') {
      const axis = ['left', 'right'].includes(drivenEdge) ? 0 : 1;
      const edge = ['left', 'bottom'].includes(drivenEdge) ? 0 : (axis === 0 ? cols : rows) - 1;
      if (!this.triangles.some((point, i) => !this.removed[Math.floor(i / 3)] && lattice[3 * point + axis] === edge)) {
        return 'Restore material on the driven edge.';
      }
    }
    return '';
  }

  update(notify = true) {
    const ready = this.mesh && !this.pending && !this.error;
    this.resetButton.disabled = !this.modified;
    this.downloadButton.disabled = !ready || this.removedCount === this.mesh?.count;
    this.retryButton.hidden = !this.error;
    this.status.textContent = this.error || (this.pending ? 'Generating mesh preview…' : !ready ? '' :
      [this.notice, `${(this.mesh.count - this.removedCount).toLocaleString()} triangles` +
        (this.modified ? ` · ${this.removedCount.toLocaleString()} removed` : ''), this.issue()].filter(Boolean).join(' · '));
    this.scheduleDraw();
    if (notify) this.onChange();
  }

  movePointer(event, paint) {
    if (!this.transform || this.pending || this.error) return;
    const rect = this.canvas.getBoundingClientRect();
    const cursor = {x: event.clientX - rect.left, y: event.clientY - rect.top, restore: event.shiftKey};
    this.cursor = cursor;
    this.showBrush(cursor);
    if (!paint) { this.lastStroke = null; return; }
    const radius = Number(this.radius.value) * this.transform.scale;
    if (this.lastStroke?.restore === cursor.restore &&
        Math.hypot(cursor.x - this.lastStroke.x, cursor.y - this.lastStroke.y) < radius * 0.4) return;
    this.lastStroke = cursor;
    this.paint(cursor, radius);
  }

  showBrush({x, y, restore}) {
    if (!this.transform) return;
    const radius = Number(this.radius.value) * this.transform.scale;
    this.brush.hidden = false;
    this.brush.classList.toggle('restore', restore);
    Object.assign(this.brush.style, {width: 2 * radius + 'px', height: 2 * radius + 'px',
      transform: `translate(${x - radius}px, ${y - radius}px)`});
  }

  paint({x, y, restore}, radius) {
    const {current, count} = this.mesh;
    const {left, bottom, scale} = this.transform;
    const px = (x - left) / scale, py = (bottom - y) / scale, rr = (radius / scale) ** 2;
    const selected = [];
    for (let i = 0; i < count; i++) {
      if (Boolean(this.removed[i]) !== restore) continue;
      const a = 3 * this.triangles[3 * i], b = 3 * this.triangles[3 * i + 1], c = 3 * this.triangles[3 * i + 2];
      const dx = (current[a] + current[b] + current[c]) / 3 - px;
      const dy = (current[a + 1] + current[b + 1] + current[c + 1]) / 3 - py;
      const side = (p, q) => (current[q] - current[p]) * (py - current[p + 1]) -
        (current[q + 1] - current[p + 1]) * (px - current[p]);
      const ab = side(a, b), bc = side(b, c), ca = side(c, a);
      if (dx * dx + dy * dy <= rr || (ab >= 0 && bc >= 0 && ca >= 0) ||
          (ab <= 0 && bc <= 0 && ca <= 0)) selected.push(i);
    }
    if (!selected.length) return;
    for (const i of selected) this.removed[i] = Number(!restore);
    this.removedCount += (restore ? -1 : 1) * selected.length;
    if (restore) for (const i of selected) this.retriangulate(i);
    this.notice = '';
    this.update();
  }

  retriangulate(i) {
    const {partners, tile, stride, pointByIndex, original, points} = this.mesh;
    const partner = partners[i];
    if (partner < 0 || this.removed[partner]) return;
    const key = tile[i];
    const [a, b, c, d] = [key, key + 1, key + stride, key + stride + 1].map(index => pointByIndex.get(index));
    const distance = (p, q) => (points[3 * p] - points[3 * q]) ** 2 + (points[3 * p + 1] - points[3 * q + 1]) ** 2;
    // The Delaunay diagonal of the lattice parallelogram is the shorter one.
    // A square has two valid diagonals. Use a stable tie break on restored tiles.
    const usesAD = distance(a, d) <= distance(b, c) + 1e-10;
    const first = 3 * Math.min(i, partner), second = 3 * Math.max(i, partner);
    const originalFirst = original.subarray(first, first + 3);
    const originalSecond = original.subarray(second, second + 3);
    const originalUsesAD = [originalFirst, originalSecond].every(cell => cell.includes(a) && cell.includes(d));
    if (usesAD === originalUsesAD) {
      this.triangles.set(originalFirst, first);
      this.triangles.set(originalSecond, second);
      this.retriangulated.delete(key);
    } else {
      this.triangles.set(usesAD ? [a, b, d] : [a, b, c], first);
      this.triangles.set(usesAD ? [a, d, c] : [b, d, c], second);
      this.retriangulated.add(key);
    }
  }

  exportVtu() {
    const remap = new Int32Array(this.mesh.fixed.length).fill(-1);
    const points = [], cells = [];
    for (let i = 0; i < this.mesh.count; i++) {
      if (this.removed[i]) continue;
      const cell = [];
      for (const id of this.triangles.subarray(3 * i, 3 * i + 3)) {
        if (remap[id] < 0) {
          remap[id] = points.length;
          points.push(`${this.mesh.points[3 * id]} ${this.mesh.points[3 * id + 1]} 0`);
        }
        cell.push(remap[id]);
      }
      cells.push(cell.join(' '));
    }
    return `<?xml version="1.0"?>
<VTKFile type="UnstructuredGrid" version="1.0" byte_order="LittleEndian">
<UnstructuredGrid><Piece NumberOfPoints="${points.length}" NumberOfCells="${cells.length}">
<Points><DataArray type="Float64" NumberOfComponents="3" format="ascii">${points.join('\n')}</DataArray></Points>
<Cells>
<DataArray type="Int64" Name="connectivity" format="ascii">${cells.join('\n')}</DataArray>
<DataArray type="Int64" Name="offsets" format="ascii">${cells.map((_, i) => (i + 1) * 3).join(' ')}</DataArray>
<DataArray type="UInt8" Name="types" format="ascii">${'5 '.repeat(cells.length)}</DataArray>
</Cells></Piece></UnstructuredGrid></VTKFile>\n`;
  }

  scheduleDraw() {
    if (!this.frame) this.frame = requestAnimationFrame(() => { this.frame = null; this.draw(); });
  }

  draw() {
    if (!this.mesh || !this.canvas.clientWidth) return;
    const {current, loaded, fixed, count, image, loadedImage, imageMoved, deformation,
      periodic, lattice, cols, rows, stride, pointByIndex} = this.mesh;
    const width = this.canvas.clientWidth, height = this.canvas.clientHeight;
    const ratio = window.devicePixelRatio || 1;
    this.canvas.width = Math.round(width * ratio);
    this.canvas.height = Math.round(height * ratio);
    const ctx = this.canvas.getContext('2d');
    ctx.scale(ratio, ratio);
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    const include = (x, y) => {
      minX = Math.min(minX, x); maxX = Math.max(maxX, x);
      minY = Math.min(minY, y); maxY = Math.max(maxY, y);
    };
    for (let i = 0; i < current.length; i += 3) include(current[i], current[i + 1]);
    for (const [x, y] of [...image, ...loadedImage]) include(x, y);
    const scale = Math.min(Math.max(1, width - 42) / Math.max(1, maxX - minX),
      (height - 58) / Math.max(1, maxY - minY));
    const left = (width - scale * (maxX + minX)) / 2, bottom = (height + scale * (maxY + minY)) / 2;
    this.transform = {scale, left, bottom};
    const screen = (x, y) => [left + x * scale, bottom - y * scale];
    const vertex = i => screen(current[3 * i], current[3 * i + 1]);
    const active = new Uint8Array(fixed.length);
    ctx.beginPath();
    for (let i = 0; i < count; i++) {
      if (this.removed[i]) continue;
      const a = this.triangles[3 * i], b = this.triangles[3 * i + 1], c = this.triangles[3 * i + 2];
      active[a] = active[b] = active[c] = 1;
      ctx.moveTo(...vertex(a)); ctx.lineTo(...vertex(b)); ctx.lineTo(...vertex(c)); ctx.closePath();
    }
    ctx.fillStyle = '#e1e9f0'; ctx.fill();
    ctx.strokeStyle = '#8da1b3'; ctx.lineWidth = scale < 5 ? 0.42 : 0.75; ctx.stroke();
    const arrow = ([x, y], dx, dy, length) => {
      const angle = Math.atan2(-dy, dx);
      const endX = x + length * Math.cos(angle), endY = y + length * Math.sin(angle);
      ctx.strokeStyle = '#772cb3'; ctx.lineWidth = 1.4;
      ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(endX, endY);
      ctx.lineTo(endX - 5 * Math.cos(angle + 0.5), endY - 5 * Math.sin(angle + 0.5));
      ctx.moveTo(endX, endY);
      ctx.lineTo(endX - 5 * Math.cos(angle - 0.5), endY - 5 * Math.sin(angle - 0.5));
      ctx.stroke();
    };
    const colors = ['#13796a', '#a05513', '#1556a5', '#363838', '#772cb3'];
    const driven = [];
    for (let i = 0; i < active.length; i++) {
      if (!active[i]) continue;
      const node = periodic ? pointByIndex.get(lattice[3 * i] % cols + stride * (lattice[3 * i + 1] % rows)) : i;
      const dx = fixed[i] & 1 ? loaded[3 * node] - current[3 * node] : 0;
      const dy = fixed[i] & 2 ? loaded[3 * node + 1] - current[3 * node + 1] : 0;
      const moving = Math.hypot(dx, dy) > 1e-10;
      if (moving) driven.push([i, dx, dy]);
      ctx.fillStyle = colors[moving ? 4 : fixed[i]];
      ctx.beginPath();
      const size = moving ? 3.2 : fixed[i] ? 2.6 : Math.max(0.65, Math.min(1.65, scale / 3));
      ctx.arc(...vertex(i), size, 0, 2 * Math.PI);
      ctx.fill();
    }
    const spacing = Math.max(1, Math.ceil(driven.length / 8));
    for (let i = 0; i < driven.length; i += spacing) {
      const [id, dx, dy] = driven[i];
      arrow(vertex(id), dx, dy, 16);
    }
    if (deformation === 'nanoindentation') arrow(screen((minX + maxX) / 2, maxY + 22 / scale), 0, -1, 18);
    const outline = (polygon, color, dash) => {
      if (!polygon.length) return;
      ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.setLineDash(dash);
      ctx.beginPath(); ctx.moveTo(...screen(...polygon[0]));
      for (const corner of polygon.slice(1)) ctx.lineTo(...screen(...corner));
      ctx.closePath(); ctx.stroke(); ctx.setLineDash([]);
    };
    outline(image, '#536980', []);
    if (imageMoved) {
      outline(loadedImage, '#772cb3', [5, 4]);
      const center = corners => corners[0].map((v, i) => (v + corners[2][i]) / 2);
      const [x, y] = center(image), [nextX, nextY] = center(loadedImage);
      const dx = nextX - x, dy = nextY - y;
      if (Math.hypot(dx, dy) > 1e-10) arrow(screen(x, y), dx, dy, Math.max(13, scale * Math.hypot(dx, dy)));
    }
  }
}
