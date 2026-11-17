(function (global) {
  'use strict';

  const CARD_WIDTH = 224, CARD_HEIGHT = 132, GAP_X = 32, GAP_Y = 76, PAD = 28;
  const kindLabels = {root: '시작점', surface: '공격 표면', candidate: '검증 후보'};
  const reasons = {parent_limit: '노드별 평가 한도', call_budget: '호출 한도', cost_budget: '비용 한도',
    time_budget: '시간 한도', scorer_unavailable: '평가 모델 연결 실패', invalid_answer: '평가 응답 오류',
    input_limit: '입력 크기 한도', invalid_input: '평가 입력 오류', observations_changed: '관찰 변경'};
  const verification = {untested: '검증 전', inconclusive: '미확정', confirmed: '확정', rejected: '기각'};

  function riskText(risk) {
    risk = risk || {};
    const value = typeof risk.score === 'number' && Number.isFinite(risk.score) ? risk.score.toFixed(2) : null;
    const reason = reasons[risk.reason] || '';
    if (risk.status === 'stale') return `이전 평가 ${value === null ? '없음' : value} · 재평가 필요${reason ? ' / ' + reason : ''}`;
    if (risk.status === 'scored' && value !== null) return `위험도 ${value}`;
    const status = ({not_applicable: '위험도 해당 없음', pending: '평가 대기', unscored: '평가 불가'})[risk.status] || '미점수';
    return status + (reason ? ' / ' + reason : '');
  }

  // Primary parent edges form a tree.  Leaf slots keep siblings separated;
  // parents sit above the centre of their children. Broken cycles are ignored.
  function layout(data) {
    const nodes = data && data.nodes && typeof data.nodes === 'object' ? data.nodes : {};
    const positions = Object.create(null), edges = [], visited = new Set(), order = [];
    let leaf = 0, maxDepth = 0;
    function visit(id, depth, parent) {
      const node = nodes[id];
      if (!node || visited.has(id)) return null;
      visited.add(id); order.push(id); maxDepth = Math.max(maxDepth, depth);
      const children = [];
      for (const childId of Array.isArray(node.child_ids) ? node.child_ids : []) {
        const x = visit(childId, depth + 1, id);
        if (x !== null) children.push(x);
      }
      const x = children.length ? (children[0] + children[children.length - 1]) / 2 : leaf++ * (CARD_WIDTH + GAP_X);
      positions[id] = {x: PAD + x, y: PAD + depth * (CARD_HEIGHT + GAP_Y), depth, node};
      if (parent) edges.push({from: parent, to: id, secondary: false});
      return x;
    }
    if (data && data.root_id) visit(data.root_id, 0, null);
    // Keep incomplete or disconnected nodes inspectable.
    for (const id of Object.keys(nodes)) if (!visited.has(id)) visit(id, 0, null);
    for (const [id, position] of Object.entries(positions)) {
      for (const path of Array.isArray(position.node.discovery_paths) ? position.node.discovery_paths : []) {
        if (path && positions[path.parent_id] && path.parent_id !== position.node.parent_id && path.parent_id !== id &&
            !edges.some(edge => edge.from === path.parent_id && edge.to === id)) {
          edges.push({from: path.parent_id, to: id, secondary: true, relation: path.relation});
        }
      }
    }
    return {positions, edges, order, width: Math.max(CARD_WIDTH + PAD * 2, leaf * (CARD_WIDTH + GAP_X) - GAP_X + PAD * 2),
      height: (maxDepth + 1) * CARD_HEIGHT + maxDepth * GAP_Y + PAD * 2};
  }

  function label(tag, text, className) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    el.textContent = String(text == null ? '' : text);
    return el;
  }

  function setSelected(container, selectedId) {
    for (const button of container.querySelectorAll('.attack-tree-node')) {
      button.setAttribute('aria-pressed', String(button.dataset.nodeId === selectedId));
    }
  }

  function render(container, data, options) {
    options = options || {};
    const graph = layout(data), oldLeft = container.scrollLeft, oldTop = container.scrollTop;
    const active = document.activeElement;
    const focusedId = active && container.contains(active) && active.classList.contains('attack-tree-node')
      ? active.dataset.nodeId : null;
    container.replaceChildren();
    if (!Object.keys(graph.positions).length) {
      container.append(label('p', '표시할 노드가 없습니다.', 'attack-tree-empty'));
      return;
    }
    const stage = label('div', '', 'attack-tree-stage');
    stage.style.width = `${graph.width}px`; stage.style.height = `${graph.height}px`;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'attack-tree-lines'); svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('width', graph.width); svg.setAttribute('height', graph.height);
    for (const edge of graph.edges) {
      const from = graph.positions[edge.from], to = graph.positions[edge.to];
      if (!from || !to) continue;
      const x1 = from.x + CARD_WIDTH / 2, y1 = from.y + CARD_HEIGHT;
      const x2 = to.x + CARD_WIDTH / 2, y2 = to.y;
      const midpoint = (y1 + y2) / 2;
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', `M ${x1} ${y1} C ${x1} ${midpoint}, ${x2} ${midpoint}, ${x2} ${y2}`);
      path.setAttribute('class', edge.secondary ? 'attack-tree-line secondary' : 'attack-tree-line');
      svg.append(path);
    }
    stage.append(svg);
    for (const id of graph.order) {
      const position = graph.positions[id];
      const node = position.node, risk = node.risk || {}, locator = node.locator || {};
      const button = document.createElement('button');
      button.type = 'button'; button.className = `attack-tree-node kind-${['root', 'surface', 'candidate'].includes(node.kind) ? node.kind : 'other'}`;
      button.dataset.nodeId = id; button.style.left = `${position.x}px`; button.style.top = `${position.y}px`;
      button.setAttribute('aria-pressed', String(id === options.selectedId));
      const heading = label('span', '', 'attack-tree-node-head');
      heading.append(label('span', node.id || id, 'attack-tree-id'), label('span', kindLabels[node.kind] || '노드', 'attack-tree-kind'));
      const url = String(locator.url || '주소 미기록');
      let route = url;
      try { const parsed = new URL(url); route = parsed.pathname + parsed.search || '/'; } catch (_) { /* display as supplied */ }
      const routeLabel = label('span', `${locator.method || '—'} ${route}`, 'attack-tree-route');
      routeLabel.title = url;
      const riskLabel = label('span', riskText(risk), `attack-tree-risk${risk.status === 'stale' ? ' is-stale' : ''}`);
      const state = label('span', verification[node.verification_state] || node.verification_state || '검증 전', 'attack-tree-state');
      const extra = (Array.isArray(node.discovery_paths) ? node.discovery_paths : []).filter(path => path && path.parent_id !== node.parent_id).length;
      if (extra) state.textContent += ` · 추가 관계 ${extra}`;
      button.setAttribute('aria-label', `${node.id || id}, ${kindLabels[node.kind] || '노드'}, ${locator.method || '—'} ${url}, ${riskLabel.textContent}, ${state.textContent}${node.parent_id ? ', 부모 ' + node.parent_id : ''}`);
      button.append(heading, routeLabel, riskLabel, state);
      button.addEventListener('click', () => { setSelected(container, id); if (typeof options.onSelect === 'function') options.onSelect(id); });
      stage.append(button);
    }
    container.append(stage);
    if (focusedId !== null) {
      const focused = Array.from(container.querySelectorAll('.attack-tree-node'))
        .find(button => button.dataset.nodeId === focusedId);
      if (focused) focused.focus({preventScroll: true});
    }
    container.scrollLeft = oldLeft; container.scrollTop = oldTop;
  }
  const api = {render, setSelected, layout, riskText};
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (global) global.AttackTreeView = api;
})(typeof window !== 'undefined' ? window : null);
