/* Documented Ollama options; drafts live in this browser, applied settings live in each run. */
(() => {
  const get = id => document.getElementById(id);
  const storeKey = model => `juice-explorer:model-settings:v1:${model}`;
  let schema = null;
  let currentModel = '';
  let details = null;
  let loading = false;
  let locked = false;
  let serial = 0;
  let syncedRun = '';

  function updateLock() {
    get('modelSettingsFields').disabled = locked || loading || !schema;
    for (const id of ['resetModelSettingsButton', 'modelDefaultsButton', 'officialPresetButton']) {
      get(id).disabled = locked || loading || !schema;
    }
  }

  function status(message, error = false) {
    get('modelSettingsStatus').textContent = message;
    get('modelSettingsStatus').className = `model-status${error ? ' error' : ''}`;
  }

  function setValues(settings) {
    for (const field of schema.fields) get(`option-${field.key}`).value = settings.options?.[field.key] ?? '';
    const think = JSON.stringify(settings.think ?? null);
    get('thinkingSelect').value = [...get('thinkingSelect').options].some(option => option.value === think) ? think : 'null';
    get('keepAliveSelect').value = settings.keep_alive === undefined ? '' : String(settings.keep_alive);
  }

  function read(report = true) {
    if (!schema) throw new Error('모델 세부설정을 불러오는 중입니다.');
    const options = {};
    for (const field of schema.fields) {
      const input = get(`option-${field.key}`);
      input.setCustomValidity(field.key === 'num_predict' && input.value !== '' && Number(input.value) === 0 ? '최대 출력 토큰은 -1 또는 1 이상의 정수여야 합니다.' : '');
      if (!input.checkValidity()) {
        if (report) { get('modelAdvancedSettings').open = true; input.reportValidity(); }
        throw new Error(`${field.label} 값을 확인하세요.`);
      }
      if (input.value !== '') options[field.key] = Number(input.value);
    }
    const result = { think: JSON.parse(get('thinkingSelect').value), options };
    const keep = get('keepAliveSelect').value;
    if (keep !== '') result.keep_alive = ['0', '-1'].includes(keep) ? Number(keep) : keep;
    return result;
  }

  function persist() {
    try { localStorage.setItem(storeKey(currentModel), JSON.stringify(read(false))); } catch (_) { /* Invalid drafts or unavailable storage stay in the form. */ }
  }

  function configure(nextSchema) {
    if (schema || !nextSchema) return;
    schema = nextSchema;
    for (const field of schema.fields) {
      const label = document.createElement('label'); label.htmlFor = `option-${field.key}`;
      label.append(document.createTextNode(field.label));
      const code = document.createElement('code'); code.textContent = field.key;
      const input = document.createElement('input'); input.type = 'number'; input.id = label.htmlFor;
      input.min = field.min; input.max = field.max; input.step = field.step; input.placeholder = '모델/Ollama 기본값';
      const hint = document.createElement('small'); hint.id = `${input.id}-hint`; hint.textContent = field.hint;
      input.setAttribute('aria-describedby', hint.id);
      label.append(code, input, hint); get('modelOptionsGrid').append(label);
    }
    setValues(schema.defaults);
    updateLock();
  }

  async function load(model, state) {
    if (!schema || !model) return;
    const requestId = ++serial;
    currentModel = model; loading = true; details = null;
    get('option-num_ctx').max = 262144;
    get('thinkingSelect').replaceChildren(...[null, false, true].map(value => {
      const option = document.createElement('option'); option.value = JSON.stringify(value);
      option.textContent = value === null ? '모델 기본값' : value ? '켜기' : '끄기'; return option;
    }));
    get('officialPresetButton').hidden = true; get('presetSource').hidden = true;
    status('모델의 지원값과 기본 파라미터를 확인합니다…'); updateLock();
    let draft = schema.defaults;
    try { draft = JSON.parse(localStorage.getItem(storeKey(model))) || draft; } catch (_) { /* Use app defaults. */ }
    if (['running', 'stopping'].includes(state?.status) && state.model === model && state.model_settings) draft = state.model_settings;
    try {
      const response = await fetch(`/api/models/details?model=${encodeURIComponent(model)}`, { cache: 'no-store' });
      const body = await response.json();
      if (requestId !== serial) return;
      if (!response.ok) throw new Error(body.detail || '서버를 재시작하면 모델 세부설정을 사용할 수 있습니다.');
      details = body;
      const values = body.thinking?.values;
      const supported = Array.isArray(values) && values.length ? values : [false, true];
      const select = get('thinkingSelect'); select.replaceChildren();
      for (const value of [null, ...supported]) {
        const option = document.createElement('option'); option.value = JSON.stringify(value);
        option.textContent = value === null ? '모델 기본값' : value === false ? '끄기' : value === true ? '켜기' : value;
        select.append(option);
      }
      get('thinkingHelp').textContent = values ? `모델 기본값: ${JSON.stringify(body.thinking.default)} · Thinking과 최종 출력은 콘솔에서 구분됩니다.` : '지원 메타데이터 없음 · 켜기/끄기 적용 여부는 모델에 따라 다릅니다.';
      get('modelParameters').textContent = body.parameters || '모델에 명시된 파라미터가 없습니다. 나머지는 Ollama 기본값을 사용합니다.';
      const contextField = get('option-num_ctx'); contextField.max = Math.min(262144, body.context_length || 262144);
      for (const field of schema.fields) {
        const match = (body.parameters || '').split('\n').map(line => line.trim().split(/\s+/)).find(parts => parts[0] === field.key);
        get(`option-${field.key}`).placeholder = match ? `모델 기본값: ${match.slice(1).join(' ')}` : '모델/Ollama 기본값';
      }
      const preset = body.presets?.[0];
      if (preset) {
        get('officialPresetButton').hidden = false; get('officialPresetButton').textContent = preset.name;
        get('presetSource').hidden = false; get('presetSource').href = preset.source;
      }
      status(`모델 문맥 한도: ${body.context_length?.toLocaleString('ko-KR') || '미제공'} 토큰 · 입력 범위는 이 앱의 제한입니다.`);
    } catch (error) {
      if (requestId !== serial) return;
      get('modelParameters').textContent = '모델 상세를 읽지 못했습니다.';
      status(error.message, true);
    } finally {
      if (requestId === serial) {
        loading = false; setValues(draft); updateLock();
        window.dispatchEvent(new Event('model-settings-ready'));
      }
    }
  }

  function syncRun(state) {
    get('runSettingsDetails').hidden = !state.run_id || !state.model_settings;
    if (state.model_settings) get('runSettingsValue').textContent = JSON.stringify(state.model_settings, null, 2);
    if (schema && state.run_id !== syncedRun && ['running', 'stopping'].includes(state.status) && state.model_settings) {
      setValues(state.model_settings); syncedRun = state.run_id;
    }
  }

  get('modelSettingsFields').addEventListener('input', persist);
  get('resetModelSettingsButton').addEventListener('click', () => { setValues(schema.defaults); persist(); status('앱 초기값을 적용했습니다. 다음 실행부터 사용합니다.'); });
  get('modelDefaultsButton').addEventListener('click', () => { setValues({ think: null, options: {} }); persist(); status('모델/Ollama 기본값을 사용합니다. 다음 실행부터 적용됩니다.'); });
  get('officialPresetButton').addEventListener('click', () => {
    const preset = details?.presets?.[0]; if (!preset) return;
    let current;
    try { current = read(); } catch (error) { status(error.message, true); return; }
    setValues({ ...current, think: preset.settings.think, options: { ...current.options, ...preset.settings.options } });
    persist(); status('공식 샘플링 값을 적용했습니다. 컨텍스트와 최대 출력 토큰은 직접 조정할 수 있습니다.');
  });
  window.modelSettings = { configure, load, read, syncRun, setLocked(value) { locked = value; updateLock(); }, get ready() { return !!schema && !loading; } };
})();
