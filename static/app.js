const $ = (id) => document.getElementById(id);
const statusLabels = { idle: '대기 중', running: '실행 중', stopping: '중지 중', stopped: '중지됨', completed: '완료', stalled: '탐색 정체', error: '오류' };
let actionError = '';
let liveConnected = false;
let evidenceSignature = '';
let hypothesesSignature = '';
let experimentsSignature = '';
let eventsSignature = '';
let planSignature = '';
let runState = null;
let modelsReady = false;
let modelsLoading = false;
let startPending = false;

function updateModelControls() {
  const active = ['running', 'stopping'].includes(runState?.status);
  $('modelSelect').disabled = active || startPending || modelsLoading || !modelsReady;
  $('refreshModelsButton').disabled = active || startPending || modelsLoading;
  window.modelSettings.setLocked(active || startPending || modelsLoading || !modelsReady);
  $('startButton').disabled = !runState || active || startPending || modelsLoading || !modelsReady || !$('modelSelect').value || !window.modelSettings.ready;
}

async function loadModels() {
  if (modelsLoading) return;
  const previous = $('modelSelect').value;
  modelsLoading = true;
  $('modelStatus').textContent = '설치된 모델을 확인합니다…';
  $('modelStatus').className = 'model-status';
  updateModelControls();
  try {
    const response = await fetch('/api/models', { cache: 'no-store' });
    if (response.status === 404) throw new Error('서버 재시작 필요 · 실행 창에서 Ctrl+C 후 가상환경의 Python으로 app.py를 다시 실행하세요.');
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    if (!body.settings_schema) throw new Error('세부설정을 적용하려면 대시보드 서버를 재시작하세요.');
    window.modelSettings.configure(body.settings_schema);
    const options = body.models.map(model => {
      const size = Number(model.size);
      const label = size > 0 ? `${model.name} · ${(size / 1024 ** 3).toFixed(1)} GiB` : model.name;
      const option = text('option', label);
      option.value = model.name;
      return option;
    });
    $('modelSelect').replaceChildren(...options);
    modelsReady = options.length > 0;
    if (modelsReady) {
      const names = body.models.map(model => model.name);
      const preferred = ['running', 'stopping'].includes(runState?.status) ? [runState.model, previous] : [previous, runState?.model];
      $('modelSelect').value = [...preferred, body.default_model, names[0]].find(name => names.includes(name));
      $('modelStatus').textContent = `설치된 모델 ${options.length}개 · 비교 전 결과 JSON을 저장하면 사용 모델도 함께 기록됩니다.`;
      await window.modelSettings.load($('modelSelect').value, runState);
    } else {
      const empty = text('option', '설치된 모델 없음');
      empty.value = '';
      $('modelSelect').append(empty);
      $('modelStatus').textContent = 'Ollama에서 모델을 다운로드한 뒤 목록을 새로고침하세요.';
    }
  } catch (error) {
    modelsReady = false;
    const empty = text('option', '모델 목록 확인 실패');
    empty.value = '';
    $('modelSelect').replaceChildren(empty);
    $('modelStatus').textContent = error.message;
    $('modelStatus').className = 'model-status error';
  } finally {
    modelsLoading = false;
    updateModelControls();
  }
}

function text(tag, value, className = '') {
  const node = document.createElement(tag);
  node.textContent = String(value ?? '—');
  if (className) node.className = className;
  return node;
}

function linkToEvidence(id) {
  const link = document.createElement('a');
  link.href = `#evidence-${id}`;
  link.textContent = id;
  link.className = 'evidence-link';
  link.addEventListener('click', () => {
    const group = document.getElementById(`evidence-${id}`)?.closest('details.evidence-group');
    if (group) group.open = true;
  });
  return link;
}

function updateConsole(value, append = false) {
  const output = $('consoleOutput');
  const atBottom = output.scrollTop + output.clientHeight >= output.scrollHeight - 24;
  output.textContent = append ? (output.textContent === '시작하면 여기에 출력됩니다.' ? '' : output.textContent) + value : (value || '시작하면 여기에 출력됩니다.');
  if (atBottom) output.scrollTop = output.scrollHeight;
}

const live = new EventSource('/api/live');
live.addEventListener('snapshot', (event) => { liveConnected = true; updateConsole(JSON.parse(event.data).text); });
live.addEventListener('delta', (event) => { liveConnected = true; updateConsole(JSON.parse(event.data).text, true); });
live.onerror = () => { liveConnected = false; $('streamStatus').textContent = '재연결 중'; $('streamStatus').className = 'count-pill disconnected'; };

function elapsed(start, end) {
  if (!start) return '00:00';
  const seconds = Math.max(0, Math.floor((end || Date.now() / 1000) - start));
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

function evidenceCard(record, kind) {
  const card = document.createElement('article');
  card.className = 'evidence-card';
  card.id = `evidence-${record.id}`;
  const head = document.createElement('div'); head.className = 'evidence-head';
  head.append(text('strong', record.id), text('span', kind));
  card.append(head);
  const main = kind === '화면' ? `${record.title || '제목 없음'} · ${record.url}`
    : kind === '행동' ? `${record.label} · ${record.before_screen_id || '시작'} → ${record.after_screen_id || '진행 중'}`
    : kind === '요청' ? `${record.method} ${record.url}`
    : `HTTP ${record.status} · ${record.content_type || '응답 유형 없음'} · ${record.bytes ?? '—'} bytes`;
  card.append(text('div', main, 'evidence-main'));
  if (record.error) card.append(text('div', `응답 수집 실패: ${record.error}`, 'result-error'));
  const relation = kind === '화면' ? `행동 ${record.action_id || '—'} · 요소 ${record.element_ids?.length || 0}개`
    : kind === '행동' ? `요청 ${(record.request_ids || []).join(', ') || '없음'}`
    : kind === '요청' ? `행동 ${record.action_id || '—'} · 쿼리 ${Object.keys(record.query || {}).join(', ') || '없음'}`
    : `요청 ${record.request_id} · 목록 ${record.list_length ?? '—'}개`;
  card.append(text('div', relation, 'evidence-meta'));
  if (kind === '응답' && record.raw) {
    const details = document.createElement('details');
    const summary = text('summary', '응답 원문 미리보기');
    details.append(summary, text('pre', record.raw.slice(0, 1800)));
    card.append(details);
  }
  return card;
}

function renderEvidence(observations) {
  const signature = ['screens', 'elements', 'actions', 'requests', 'responses'].map(key => observations[key].length).join('|')
    + '|' + observations.actions.map(item => item.after_screen_id || '').join(',');
  if (signature === evidenceSignature) return;
  evidenceSignature = signature;
  const parent = $('evidenceList');
  const previousGroups = [...parent.querySelectorAll('details.evidence-group')];
  const openGroups = new Set(previousGroups.filter(item => item.open).map(item => item.dataset.kind));
  const firstRender = previousGroups.length === 0;
  parent.replaceChildren(); parent.className = 'evidence-list';
  const groups = [
    ['screens', '화면'], ['actions', '행동'], ['requests', '요청'], ['responses', '응답']
  ];
  let count = 0;
  for (const [key, label] of groups) {
    const items = observations[key] || [];
    count += items.length;
    if (!items.length) continue;
    const section = document.createElement('details'); section.className = 'evidence-group'; section.dataset.kind = key;
    section.open = openGroups.has(key) || (firstRender && ['screens', 'actions'].includes(key));
    section.append(text('summary', `${label} · ${items.length}`));
    const grid = document.createElement('div'); grid.className = 'evidence-grid';
    for (const item of items) grid.append(evidenceCard(item, label));
    section.append(grid); parent.append(section);
  }
  if (!count) parent.append(text('div', '아직 관찰 기록이 없습니다.', 'empty-history'));
  $('evidenceCount').textContent = `${count} records`;
}

function renderHypotheses(hypotheses) {
  const signature = JSON.stringify(hypotheses);
  if (signature === hypothesesSignature) return;
  hypothesesSignature = signature;
  const parent = $('hypothesisList'); parent.replaceChildren(); parent.className = 'card-list';
  for (const item of hypotheses) {
    const card = document.createElement('article'); card.className = 'result-card';
    const head = document.createElement('div'); head.className = 'result-head';
    head.append(text('strong', item.id), text('span', item.executable ? '실행 가능' : '실행 제외', `badge ${item.executable ? 'yes' : 'no'}`));
    card.append(head, text('p', item.claim));
    const refs = document.createElement('div'); refs.className = 'evidence-refs';
    refs.append(text('span', '근거'));
    for (const id of item.evidence_ids || []) refs.append(linkToEvidence(id));
    card.append(refs);
    const change = item.change || {};
    card.append(text('div', `기준 ${item.baseline?.request_id || '—'} · 변경 ${change.query_key || '—'} → ${change.value || '—'}`, 'result-detail'));
    card.append(text('div', `예상: ${item.expected_observation || '—'}`, 'result-detail'));
    if (item.error) card.append(text('div', item.error, 'result-error'));
    parent.append(card);
  }
  if (!hypotheses.length) parent.append(text('div', '아직 가설이 없습니다.', 'empty-history'));
  $('hypothesisCount').textContent = `${hypotheses.length} hypotheses`;
}

function renderExperiments(experiments) {
  const signature = JSON.stringify(experiments);
  if (signature === experimentsSignature) return;
  experimentsSignature = signature;
  const parent = $('experimentList'); parent.replaceChildren(); parent.className = 'card-list';
  for (const item of experiments) {
    const card = document.createElement('article'); card.className = 'result-card';
    const head = document.createElement('div'); head.className = 'result-head';
    head.append(text('strong', `${item.id} · ${item.hypothesis_id}`), text('span', item.result, `badge ${item.result === '변화 관찰' ? 'yes' : 'no'}`));
    card.append(head);
    if (item.hypothesis_verdict) card.append(text('div', `가설 판정: ${item.hypothesis_verdict} · ${item.note || ''}`, 'result-detail'));
    const refs = document.createElement('div'); refs.className = 'evidence-refs';
    refs.append(text('span', '원래'));
    refs.append(linkToEvidence(item.original_response_id));
    if (item.baseline_response_id) { refs.append(text('span', '기준')); refs.append(linkToEvidence(item.baseline_response_id)); }
    if (item.changed_response_id) { refs.append(text('span', '변경')); refs.append(linkToEvidence(item.changed_response_id)); }
    card.append(refs);
    for (const difference of item.differences?.baseline_vs_changed?.changed_fields || []) {
      card.append(text('div', `${difference.field}: ${JSON.stringify(difference.baseline)} → ${JSON.stringify(difference.changed)}`, 'diff-row'));
    }
    for (const difference of item.differences?.baseline_vs_changed?.json_differences || []) {
      card.append(text('div', `${difference.path}: ${difference.baseline} → ${difference.changed}`, 'diff-row'));
    }
    if (item.error) card.append(text('div', item.error, 'result-error'));
    parent.append(card);
  }
  if (!experiments.length) parent.append(text('div', '아직 실험이 없습니다.', 'empty-history'));
  $('experimentCount').textContent = `${experiments.length} experiments`;
}

function renderEvents(events) {
  const signature = `${events.length}|${events.at(-1)?.message || ''}`;
  if (signature === eventsSignature) return;
  eventsSignature = signature;
  const parent = $('history'); parent.replaceChildren();
  for (const item of [...events].reverse()) {
    const row = document.createElement('div'); row.className = 'history-item';
    row.append(text('div', item.phase, 'history-index'));
    const main = document.createElement('div'); main.className = 'history-main';
    main.append(text('strong', item.message)); row.append(main);
    row.append(text('div', new Date(item.at * 1000).toLocaleTimeString('ko-KR'), 'history-meta'));
    parent.append(row);
  }
  if (!events.length) parent.append(text('div', '아직 실행 기록이 없습니다.', 'empty-history'));
  $('historyCount').textContent = `${events.length} events`;
}

function render(state) {
  runState = state;
  window.modelSettings.syncRun(state);
  renderPlan(state);
  if (!liveConnected) updateConsole(state.console || '');
  $('streamStatus').textContent = state.model_streaming ? '생성 중 ●' : state.status === 'running' ? '연결됨' : '대기';
  $('streamStatus').className = `count-pill ${state.model_streaming ? 'active' : ''}`;
  $('statusText').textContent = statusLabels[state.status] || state.status;
  $('statusDot').className = `status-dot ${state.status}`;
  $('phaseText').textContent = state.phase;
  $('currentPhase').textContent = `${state.phase}\n${state.message}`;
  $('runId').textContent = state.run_id ? state.run_id.slice(0, 8) : '—';
  $('runModel').textContent = state.run_id ? state.model : '—';
  $('message').textContent = actionError || state.message;
  $('stepMetric').replaceChildren(document.createTextNode(`${state.step} `), text('small', `/ ${state.max_steps}`));
  $('progressFill').style.width = `${Math.min(100, state.step / state.max_steps * 100)}%`;
  const observations = state.observations;
  const inputs = observations.elements.filter(item => ['input', 'textarea', 'select'].includes(item.kind)).length;
  $('screenMetric').replaceChildren(document.createTextNode(`${observations.screens.length} `), text('small', `· ${inputs}`));
  $('requestMetric').textContent = state.automatic_requests;
  $('blockedMetric').textContent = `차단 ${state.blocked_requests} · 자동 리소스 포함`;
  $('experimentMetric').replaceChildren(document.createTextNode(`${state.hypotheses.length} `), text('small', `· ${state.experiments.length}`));
  $('elapsed').textContent = elapsed(state.started_at, state.finished_at);
  if (['running', 'stopping'].includes(state.status) && Array.from($('modelSelect').options).some(option => option.value === state.model)) {
    $('modelSelect').value = state.model;
  }
  updateModelControls();
  $('stopButton').disabled = state.status !== 'running';
  $('resetButton').disabled = state.status === 'stopping';
  renderEvidence(observations);
  renderHypotheses(state.hypotheses);
  renderExperiments(state.experiments);
  renderEvents(state.events);
}

function renderPlan(state) {
  const decisions = state.decisions || [];
  const signature = JSON.stringify([state.run_id, state.plan, decisions, state.planner_version]);
  if (signature === planSignature) return;
  planSignature = signature;
  const plan = state.plan?.length ? state.plan : [state.planner_version
    ? '첫 화면을 관찰하면 LLM이 계획을 세웁니다.'
    : '이 기록은 이전 고정 탐색 방식입니다. app.py로 서버를 재시작하면 새 실행에 LLM 계획이 적용됩니다.'];
  $('planList').replaceChildren(...plan.map(step => text('li', step)));
  $('decisionCount').textContent = `${decisions.length} decisions`;
  const cards = decisions.slice(-5).reverse().map(decision => {
    const answer = decision.answer || {};
    const labels = { selected: '실행 중', completed: '완료', rejected: '선택 보류', failed: '실행 실패' };
    const card = document.createElement('article'); card.className = 'result-card';
    const head = document.createElement('div'); head.className = 'result-head';
    head.append(text('strong', `${decision.id} · ${answer.action || '형식 오류'}`),
      text('span', labels[decision.status] || decision.status, `badge ${decision.status === 'completed' ? 'yes' : 'no'}`));
    card.append(head, text('p', answer.reason || decision.error));
    const refs = document.createElement('div'); refs.className = 'evidence-refs';
    if (decision.screen_id) refs.append(linkToEvidence(decision.screen_id));
    if (answer.request_id) refs.append(linkToEvidence(answer.request_id));
    if (answer.element_id) refs.append(text('span', answer.element_id));
    if (answer.value) refs.append(text('span', `입력: ${answer.value}`));
    card.append(refs);
    if (decision.error) card.append(text('div', decision.error, 'result-error'));
    return card;
  });
  $('decisionList').replaceChildren(...cards);
}

let polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  try {
    const response = await fetch('/api/state', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch (error) { $('message').textContent = `대시보드 연결 오류: ${error.message}`; }
  finally { polling = false; }
}

async function action(name) {
  if (name === 'start') {
    if ($('startButton').disabled) return;
  }
  try {
    const options = { method: 'POST' };
    if (name === 'start') {
      const settings = window.modelSettings.read();
      startPending = true;
      updateModelControls();
      options.headers = { 'Content-Type': 'application/json' };
      options.body = JSON.stringify({ target: $('targetInput').value.trim(), model: $('modelSelect').value, model_settings: settings });
    }
    const response = await fetch(`/api/${name}`, options);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${response.status}`);
    }
    actionError = '';
    await poll();
  } catch (error) { actionError = `요청 실패: ${error.message}`; $('message').textContent = actionError; }
  finally { startPending = false; updateModelControls(); }
}

$('refreshModelsButton').addEventListener('click', loadModels);
$('modelSelect').addEventListener('change', async () => {
  const loaded = window.modelSettings.load($('modelSelect').value, runState);
  updateModelControls();
  await loaded;
  updateModelControls();
});
window.addEventListener('model-settings-ready', updateModelControls);
$('startButton').addEventListener('click', () => action('start'));
$('stopButton').addEventListener('click', () => action('stop'));
$('resetButton').addEventListener('click', () => action('reset'));

let labPolling = false;
let labStarting = false;
let labState = null;

function renderLab() {
  $('labStatus').textContent = labStarting ? '시작 요청 중…' : (labState?.message || '상태 확인 중…');
  $('labStatus').className = `lab-status-text ${labState?.status || ''}`;
  $('labStartButton').disabled = labStarting || !labState?.can_start;
  $('labStartButton').textContent = labStarting ? '켜는 중…' : '실험환경 켜기';
}

async function pollLab() {
  if (labPolling || labStarting) return;
  labPolling = true;
  try {
    const response = await fetch('/api/lab', { cache: 'no-store' });
    if (response.status === 404) {
      labState = {
        message: '대시보드 서버 재시작 필요 · 실행 창에서 Ctrl+C 후 가상환경의 Python으로 app.py를 다시 실행하세요.',
        status: 'error', can_start: false,
      };
      return;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    labState = await response.json();
  } catch (error) {
    labState = { message: `실험환경 상태 확인 실패: ${error.message}`, status: 'error', can_start: false };
  } finally { labPolling = false; renderLab(); }
}

async function startLab() {
  if (labStarting || !labState?.can_start) return;
  labStarting = true;
  $('labError').hidden = true;
  renderLab();
  try {
    const response = await fetch('/api/lab/start', { method: 'POST' });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    labState = body;
  } catch (error) {
    $('labError').textContent = error.message;
    $('labError').hidden = false;
  } finally {
    labStarting = false;
    renderLab();
    await pollLab();
  }
}

$('labStartButton').addEventListener('click', startLab);
pollLab();
setInterval(pollLab, 5000);
poll().then(loadModels);
setInterval(poll, 1200);
