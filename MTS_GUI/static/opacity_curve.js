function createOpacityEditor(svg, remove) {
  const points = [[0, 0], [1, 1]];
  const left = 43, right = 286, top = 12, bottom = 106;
  let selected = points[0], dragging = false;
  const xPixel = x => left + x * (right - left);
  const yPixel = y => bottom - y * (bottom - top);
  const clamp = (value, lo, hi) => Math.min(Math.max(value, lo), hi);
  const format = value => Number(value.toPrecision(4)).toString();

  function redraw() {
    const coords = [[0, points[0][1]], ...points, [1, points.at(-1)[1]]];
    svg.querySelector('polyline').setAttribute('points', coords.map(([x, y]) => xPixel(x) + ',' + yPixel(y)).join(' '));
    svg.querySelector('g').replaceChildren(...points.map(([x, y], index) => {
      const node = document.createElementNS(svg.namespaceURI, 'circle');
      node.setAttribute('cx', xPixel(x));
      node.setAttribute('cy', yPixel(y));
      node.setAttribute('r', 5);
      node.classList.toggle('selected', points[index] === selected);
      node.dataset.index = index;
      return node;
    }));
    remove.disabled = points.length === 1;
  }

  function move(x, y) {
    const index = points.indexOf(selected);
    selected[0] = clamp(x, index ? points[index - 1][0] + .0001 : 0,
      index + 1 < points.length ? points[index + 1][0] - .0001 : 1);
    selected[1] = clamp(y, 0, 1);
    redraw();
  }

  function position(event) {
    const {x, y} = new DOMPoint(event.clientX, event.clientY).matrixTransform(svg.getScreenCTM().inverse());
    return [(x - left) / (right - left), (bottom - y) / (bottom - top)];
  }

  remove.onclick = () => {
    if (points.length === 1) return;
    const index = points.indexOf(selected);
    points.splice(index, 1);
    selected = points[Math.min(index, points.length - 1)];
    redraw();
  };
  svg.onpointerdown = event => {
    if (event.button !== 0) return;
    const index = event.target.dataset.index;
    if (index !== undefined) {
      selected = points[Number(index)];
    } else {
      const [x, y] = position(event);
      if (x < 0 || x > 1 || y < 0 || y > 1 || points.some(point => Math.abs(point[0] - x) < .0001)) return;
      selected = [x, y];
      points.push(selected);
      points.sort((a, b) => a[0] - b[0]);
    }
    svg.focus();
    svg.setPointerCapture(event.pointerId);
    dragging = true;
    redraw();
  };
  svg.onpointermove = event => {if (dragging) move(...position(event));};
  svg.onlostpointercapture = () => {dragging = false;};
  svg.onkeydown = event => {
    if (['Delete', 'Backspace'].includes(event.key)) remove.click();
    else if (event.key === 'PageUp' || event.key === 'PageDown') {
      selected = points[clamp(points.indexOf(selected) + (event.key === 'PageUp' ? -1 : 1), 0, points.length - 1)];
      redraw();
    } else if (event.key.startsWith('Arrow')) {
      const dx = {ArrowLeft: -.01, ArrowRight: .01}[event.key] || 0;
      const dy = {ArrowDown: -.01, ArrowUp: .01}[event.key] || 0;
      move(selected[0] + dx, selected[1] + dy);
    } else return;
    event.preventDefault();
  };
  redraw();
  return {
    points,
    restore(values) {
      if (!Array.isArray(values) || !values.length || values.some((point, index) =>
        !Array.isArray(point) || point.length !== 2 ||
        point.some(value => !Number.isFinite(value) || value < 0 || value > 1) ||
        (index && point[0] <= values[index - 1][0]))) throw new Error('Invalid saved opacity curve.');
      points.splice(0, points.length, ...values.map(point => [...point]));
      selected = points[0];
      redraw();
    },
    setLimits(lower, upper) {
      svg.querySelector('[data-limit="lower"]').textContent = lower == null ? 'Lower limit' : format(lower);
      svg.querySelector('[data-limit="upper"]').textContent = upper == null ? 'Upper limit' : format(upper);
    },
  };
}
