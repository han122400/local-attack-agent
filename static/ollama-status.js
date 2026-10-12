(() => {
  const rows = document.getElementById('ollamaPsRows');
  const status = document.getElementById('ollamaPsStatus');
  const refresh = document.getElementById('refreshOllamaPsButton');
  let loading = false;

  function cell(value, className = '') {
    const element = document.createElement('td');
    element.textContent = String(value ?? '—');
    element.className = className;
    return element;
  }

  function memory(bytes) {
    return typeof bytes === 'number' && Number.isFinite(bytes) && bytes >= 0
      ? `${(bytes / 1024 ** 3).toFixed(2)} GiB` : '—';
  }

  function until(expiresAt) {
    if (!expiresAt) return '—';
    const expires = Date.parse(expiresAt);
    if (!Number.isFinite(expires)) return '—';
    const seconds = Math.ceil((expires - Date.now()) / 1000);
    if (seconds <= 0) return '해제 중';
    if (seconds > 86400 * 365) return '장기 유지';
    if (seconds >= 3600) return `${Math.ceil(seconds / 3600)}시간 후`;
    return seconds >= 60 ? `${Math.ceil(seconds / 60)}분 후` : `${seconds}초 후`;
  }

  function empty(message) {
    const row = document.createElement('tr');
    row.className = 'empty-row';
    const content = cell(message);
    content.colSpan = 7;
    row.append(content);
    rows.replaceChildren(row);
  }

  function render(models) {
    if (!models.length) {
      empty('메모리에 로드된 모델이 없습니다. 탐색에서 모델을 사용하면 여기에 표시됩니다.');
      return;
    }
    rows.replaceChildren(...models.map(model => {
      const row = document.createElement('tr');
      const processor = cell();
      const badge = document.createElement('span');
      badge.className = `processor-badge ${model.processor === '100% GPU' ? 'gpu' : model.processor === '100% CPU' ? 'cpu' : model.processor?.includes('CPU/GPU') ? 'mixed' : 'unknown'}`;
      badge.textContent = model.processor || 'Unknown';
      processor.append(badge);
      const expiry = cell(until(model.expires_at));
      expiry.title = model.expires_at || '';
      row.append(cell(model.name), cell(model.id || '—'), cell(memory(model.size)), processor,
        cell(memory(model.size_vram)), cell(model.context_length), expiry);
      return row;
    }));
  }

  async function poll() {
    if (loading) return;
    loading = true;
    refresh.disabled = true;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 6000);
    try {
      const response = await fetch('/api/ollama/ps', { cache: 'no-store', signal: controller.signal });
      if (response.status === 404) throw new Error('서버 재시작 필요 · 실행 창에서 Ctrl+C 후 run.cmd를 다시 실행하세요.');
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
      if (!Array.isArray(body.models)) throw new Error('실행 모델 목록 형식이 올바르지 않습니다.');
      render(body.models);
      status.className = 'model-status';
      status.textContent = `로드된 모델 ${body.models.length}개 · 3초마다 갱신 · ${new Date(body.checked_at * 1000).toLocaleTimeString('ko-KR')} 확인`;
    } catch (error) {
      empty('실행 상태를 확인할 수 없습니다.');
      status.className = 'model-status error';
      status.textContent = error.name === 'AbortError' ? 'Ollama 실행 상태 조회 시간 초과 · 자동으로 다시 확인합니다.' : error.message;
    } finally {
      clearTimeout(timer);
      loading = false;
      refresh.disabled = false;
    }
  }

  refresh.addEventListener('click', poll);
  poll();
  setInterval(poll, 3000);
})();
