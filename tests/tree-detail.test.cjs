const test = require('node:test');
const assert = require('node:assert/strict');
const {render, riskText} = require('../web/static/tree-detail.js');
class Element {
  constructor(tag, ownerDocument) { this.tagName = tag; this.ownerDocument = ownerDocument; this.children = []; this.textContent = ''; this.className = ''; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
}
const doc = {createElement(tag) { return new Element(tag, doc); }};
function all(node) { return [node, ...node.children.flatMap(all)]; }
const detail = {run_id: 'a'.repeat(32), node: {id: 'N0002', kind: 'candidate', parent_id: 'N0001',
  locator: {method: 'GET', url: '<img src=x onerror=alert(1)>', input: 'q', auth_context: 'anonymous'},
  hypothesis: {type: 'sqli', statement: '<script>evil()</script>'},
  risk: {score: 25, status: 'stale', reason: 'observations_changed'},
  observations: {query_behavior: 'error', baseline_compared: true, reproduced: false},
  verification_state: 'inconclusive', investigation_state: 'done', exploration_state: 'done',
  discovery_paths: [{parent_id:'N0001', relation:'refines'}], child_ids: [],
  evidence_refs: [{path: 'artifacts/node-evidence/A0001/abc.txt', description:'<svg>proof</svg>'}]},
  attempts: [{attempt_id:'A0001', execution_status:'succeeded', status:'imported', result:{claim:'응답 차이'}, evidence_refs:[]}]};
test('renders node facts and uses text nodes for untrusted content', () => {
  const container = new Element('div', doc); render(container, detail);
  const nodes = all(container), text = nodes.map(item => item.textContent).join('\n');
  assert.match(text, /N0002/); assert.match(text, /이전 평가 · 재평가 필요/);
  assert.match(text, /<script>evil\(\)<\/script>/); assert.match(text, /기준 응답 비교/);
  assert.ok(nodes.every(item => item.tagName !== 'script' && item.tagName !== 'img' && item.tagName !== 'svg'));
  const download = nodes.find(item => item.tagName === 'a');
  assert.equal(download.href, '/api/runs/' + 'a'.repeat(32) + '/evidence-download?path=node-evidence%2FA0001%2Fabc.txt');
});
test('empty and unscored nodes do not fabricate an assessment', () => {
  const container = new Element('div', doc); render(container, null);
  assert.match(all(container).map(item => item.textContent).join(' '), /선택/);
  assert.equal(riskText({status:'unscored', score:null, reason:'parent_limit'}), '평가 불가 · 부모별 평가 한도');
});
