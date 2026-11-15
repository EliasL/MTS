class CustomMeshEditor {
  constructor(getConfiguration, loadPreview, onChange) {
    this.getConfiguration = getConfiguration;
    this.loadPreview = loadPreview;
    this.onChange = onChange;
    this.revision = 0;
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
    this.transverseKey = find('mesh-transverse-key');
    this.indenterKey = find('mesh-indenter-key');
    this.resetButton = find('mesh-reset');
    this.downloadButton = find('mesh-download');
    this.retryButton = find('mesh-retry');
    this.section.addEventListener('toggle', () => this.refresh());
    this.retryButton.onclick = () => { this.requestKey = null; this.refresh(); };
    this.resetButton.onclick = () => {
      if (this.pendingTemplate) {
        this.clear();
        this.refresh();
      } else if (this.mesh) {
        this.resetEdits();
      }
      this.update();
    };
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
    return Boolean(this.pendingTemplate || this.templateVtu ||
      this.mesh && (this.removedCount || this.retriangulated.size));
  }

  clear() {
    this.revision++;
    clearTimeout(this.timer);
    this.controller?.abort();
    this.mesh = this.requestKey = this.transform = null;
    this.templateVtu = this.pendingTemplate = null;
    this.error = '';
    this.pending = false;
    this.viewport.hidden = this.legend.hidden = true;
  }

  loadTemplate(vtu) {
    this.pendingTemplate = vtu;
    this.requestKey = null;
    this.section.open = true;
    this.refresh();
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
        const replaced = this.mesh && this.modified && !same && !this.pendingTemplate;
        this.mesh = next;
        if (!same) this.resetEdits();
        if (this.pendingTemplate) {
          this.resetEdits();
          this.applyTemplate(this.pendingTemplate);
          this.templateVtu = this.pendingTemplate;
          this.pendingTemplate = null;
        }
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
    const {rows, cols, boundary, drivenEdge} = settings;
    const indenterSelector = 'FieldData DataArray[Name="indentation_preview"]';
    const indenter = xml.querySelector(indenterSelector) ? Array.from(array(indenterSelector)) : null;
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
    return {points, original, lattice, current, loaded, fixed, count, rows, cols, boundary,
      drivenEdge, periodic, image, loadedImage, imageMoved, stride, pointByIndex, tile, partners,
      firstInTile, indenter};
  }

  resetEdits() {
    this.triangles = this.mesh.original.slice();
    this.removed = new Uint8Array(this.mesh.count);
    this.removedCount = 0;
    this.retriangulated = new Set();
    this.templateVtu = null;
    this.notice = '';
  }

  applyTemplate(vtu) {
    const xml = new DOMParser().parseFromString(vtu, 'application/xml');
    const array = selector => xml.querySelector(selector)?.textContent.trim().split(/\s+/).map(Number);
    const points = array('Points DataArray');
    const cells = array('Cells DataArray[Name="connectivity"]');
    const {lattice, stride, firstInTile, partners} = this.mesh;
    const mismatch = () => new Error('The saved mesh does not match this lattice. Reset edits to use the generated mesh.');
    if (!points?.length || points.length % 3 || !cells?.length || cells.length % 3) throw mismatch();
    const key = (x, y) => `${Math.round(x * 1e8)},${Math.round(y * 1e8)}`;
    const byPosition = new Map();
    for (let i = 0; i < this.mesh.points.length; i += 3) {
      byPosition.set(key(this.mesh.points[i], this.mesh.points[i + 1]), i / 3);
    }
    const pointMap = [];
    for (let i = 0; i < points.length; i += 3) pointMap.push(byPosition.get(key(points[i], points[i + 1])));
    this.removed.fill(1);
    this.removedCount = this.mesh.count;
    for (let i = 0; i < cells.length; i += 3) {
      const face = cells.slice(i, i + 3).map(index => pointMap[index]);
      if (face.some(id => id === undefined) || new Set(face).size !== 3) throw mismatch();
      const tile = Math.min(...face.map(id => lattice[3 * id])) +
        stride * Math.min(...face.map(id => lattice[3 * id + 1]));
      const first = firstInTile.get(tile);
      if (first === undefined || partners[first] < 0) throw mismatch();
      const second = partners[first];
      const [a, b, c, d] = this.tileCorners(tile);
      const usesAD = face.includes(a) && face.includes(d);
      if (!usesAD && !(face.includes(b) && face.includes(c))) throw mismatch();
      if (this.removed[first] && this.removed[second]) this.setTile(first, usesAD);
      const index = [first, second].find(slot =>
        face.every(id => this.triangles.subarray(3 * slot, 3 * slot + 3).includes(id)));
      if (index === undefined || !this.removed[index]) throw mismatch();
      this.removed[index] = 0;
      this.removedCount--;
    }
  }

  issue() {
    if (!this.modified) return '';
    if (this.pending || this.error) return 'Refresh the custom mesh preview for the current settings.';
    if (this.removedCount === this.mesh.count) return 'Restore at least one mesh triangle.';
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
    this.viewport.hidden = this.legend.hidden = !this.mesh || Boolean(this.error);
    this.status.textContent = this.error || (this.pending ? 'Generating mesh preview…' : !ready ? '' :
      [this.notice, `${(this.mesh.count - this.removedCount).toLocaleString()} triangles` +
        (this.modified ? ` · ${this.removedCount.toLocaleString()} removed` : ''), this.issue()].filter(Boolean).join(' · '));
    this.scheduleDraw();
    if (notify) {
      this.revision++;
      this.onChange();
    }
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
    this.templateVtu = null;
    for (const i of selected) this.removed[i] = Number(!restore);
    this.removedCount += (restore ? -1 : 1) * selected.length;
    if (restore) for (const i of selected) this.retriangulate(i);
    this.notice = '';
    this.update();
  }

  tileCorners(key) {
    const {stride, pointByIndex} = this.mesh;
    return [key, key + 1, key + stride, key + stride + 1].map(index => pointByIndex.get(index));
  }

  retriangulate(i) {
    const {partners, tile, points} = this.mesh;
    const partner = partners[i];
    if (partner < 0 || this.removed[partner]) return;
    const key = tile[i];
    const [a, b, c, d] = this.tileCorners(key);
    const distance = (p, q) => (points[3 * p] - points[3 * q]) ** 2 + (points[3 * p + 1] - points[3 * q + 1]) ** 2;
    // The Delaunay diagonal of the lattice parallelogram is the shorter one.
    // A square has two valid diagonals. Use a stable tie break on restored tiles.
    this.setTile(i, distance(a, d) <= distance(b, c) + 1e-10);
  }

  setTile(i, usesAD) {
    const {partners, tile, original} = this.mesh;
    const partner = partners[i], key = tile[i];
    const [a, b, c, d] = this.tileCorners(key);
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
    if (this.templateVtu) return this.templateVtu;
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
    const {current, loaded, fixed, count, image, loadedImage, imageMoved, indenter,
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
    let transverseConstraint = false;
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
      const position = vertex(i);
      ctx.arc(...position, size, 0, 2 * Math.PI);
      ctx.fill();
      const transverse = (fixed[i] & 1 && Math.abs(dx) <= 1e-10) ||
        (fixed[i] & 2 && Math.abs(dy) <= 1e-10);
      if (moving && transverse) {
        transverseConstraint = true;
        ctx.strokeStyle = colors[3];
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
    }
    const spacing = Math.max(1, Math.ceil(driven.length / 8));
    for (let i = 0; i < driven.length; i += spacing) {
      const [id, dx, dy] = driven[i];
      arrow(vertex(id), dx, dy, 16);
    }
    this.transverseKey.hidden = !transverseConstraint;
    this.indenterKey.hidden = !indenter;
    if (indenter) {
      const [x, radius, depth, window] = indenter;
      let surface = -Infinity;
      for (let i = 0; i < active.length; i++) {
        if (active[i] && fixed[i] !== 3 && Math.abs(current[3 * i] - x) <= window) {
          surface = Math.max(surface, current[3 * i + 1]);
        }
      }
      this.indenterKey.querySelector('span').textContent =
        `Final indenter · radius ${radius} · depth ${depth}`;
      if (Number.isFinite(surface)) {
        ctx.strokeStyle = colors[4];
        ctx.lineWidth = 1.6;
        ctx.setLineDash([5, 4]);
        ctx.beginPath();
        ctx.arc(...screen(x, surface + radius - depth), radius * scale, 0, 2 * Math.PI);
        ctx.stroke();
        ctx.setLineDash([]);
        arrow(screen(x, surface + Math.min(0.5 * radius, 18 / scale)), 0, -1, 15);
      }
    }
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
