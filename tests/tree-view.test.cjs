const assert = require('node:assert/strict');
const test = require('node:test');
const {layout, riskText} = require('../web/static/tree-view.js');

test('layout connects parents and centers their children in separate leaf slots', () => {
  const result = layout({root_id:'N1', nodes:{
    N1:{id:'N1', child_ids:['N2','N3']},
    N2:{id:'N2', parent_id:'N1', child_ids:['N4']},
    N3:{id:'N3', parent_id:'N1', child_ids:[]},
    N4:{id:'N4', parent_id:'N2', child_ids:[]}
  }});
  assert.deepEqual(result.edges.map(({from,to}) => [from,to]).sort(), [['N1','N2'],['N1','N3'],['N2','N4']]);
  assert.equal(result.positions.N2.x, result.positions.N4.x);
  assert.equal(result.positions.N1.x, (result.positions.N2.x + result.positions.N3.x) / 2);
  assert.ok(result.positions.N3.x > result.positions.N2.x);
  assert.ok(result.positions.N4.y > result.positions.N2.y);
});

test('additional discovery paths are linked without duplicating primary edges', () => {
  const result = layout({root_id:'A', nodes:{
    A:{child_ids:['B','C']}, B:{parent_id:'A', child_ids:[]},
    C:{parent_id:'A', child_ids:[], discovery_paths:[{parent_id:'A', relation:'discovered'}, {parent_id:'B', relation:'enabled_by'}]}
  }});
  assert.equal(result.edges.filter(e => e.from==='A' && e.to==='C').length,1);
  assert.deepEqual(result.edges.find(e => e.from==='B' && e.to==='C'), {from:'B',to:'C',secondary:true,relation:'enabled_by'});
});

test('disconnected and malformed nodes remain bounded and inspectable', () => {
  const result = layout({root_id:'A', nodes:{A:{child_ids:['B']},B:{child_ids:['A','missing']},C:{child_ids:[]}}});
  assert.deepEqual(Object.keys(result.positions).sort(), ['A','B','C']);
  assert.equal(result.edges.length, 1);
  assert.ok(Number.isFinite(result.width) && Number.isFinite(result.height));
});

test('stale risk is labelled previous and never shown as a current score', () => {
  assert.equal(riskText({status:'stale', score:75, reason:'observations_changed'}), '이전 평가 75.00 · 재평가 필요 / 관찰 변경');
  assert.equal(riskText({status:'scored', score:12.25}), '위험도 12.25');
  assert.equal(riskText({status:'not_applicable'}), '위험도 해당 없음');
});

test('rerender keeps the focused node and viewport position', () => {
  const tree = require('../web/static/tree-view.js');
  const previousDocument = global.document;
  class Element {
    constructor() { this.children=[]; this.style={}; this.dataset={}; this.attributes={}; this.className=''; }
    get classList() { return {contains: value => this.className.split(' ').includes(value)}; }
    setAttribute(name,value) { this.attributes[name]=String(value); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    contains(target) { return this === target || this.children.some(child => child.contains(target)); }
    querySelectorAll(selector) {
      return this.children.flatMap(child => [ ...(selector === '.attack-tree-node' && child.classList.contains('attack-tree-node') ? [child] : []), ...child.querySelectorAll(selector) ]);
    }
    addEventListener() {}
    focus(options) { this.focusOptions = options; global.document.activeElement = this; }
  }
  const doc = {activeElement:null, createElement:() => new Element(), createElementNS:() => new Element()};
  global.document = doc;
  try {
    const container = new Element(); container.scrollLeft=42; container.scrollTop=23;
    const data = {root_id:'A', nodes:{A:{id:'A',kind:'root',child_ids:['B']},B:{id:'B',kind:'surface',parent_id:'A',child_ids:[]}}};
    tree.render(container,data,{});
    const first = container.querySelectorAll('.attack-tree-node').find(item => item.dataset.nodeId === 'B');
    first.focus();
    tree.render(container,data,{});
    assert.notEqual(doc.activeElement,first);
    assert.equal(doc.activeElement.dataset.nodeId,'B');
    assert.deepEqual(doc.activeElement.focusOptions,{preventScroll:true});
    assert.equal(container.scrollLeft,42); assert.equal(container.scrollTop,23);
  } finally { global.document = previousDocument; }
});
