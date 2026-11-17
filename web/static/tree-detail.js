/* Detail view for an attack-surface node. All model-authored content is text. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TreeDetail = api;
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';
  const labels = {
    root:'시작점', surface:'공격 표면', candidate:'검증 후보',
    pending:'대기', discovered:'발견됨', active:'진행 중', investigating:'조사 중', done:'완료', blocked:'차단됨',
    reused:'기존 구조 재사용', untested:'미검증', inconclusive:'미확정', confirmed:'확정', rejected:'기각',
    not_applicable:'해당 없음', scored:'평가 완료', stale:'이전 평가 · 재평가 필요', unscored:'평가 불가',
    observations_changed:'관찰 변경', parent_limit:'부모별 평가 한도', input_limit:'입력 크기 한도',
    invalid_input:'평가 입력 오류', invalid_answer:'평가 응답 오류',
    discovered_relation:'발견', refines:'구체화', enabled_by:'선행조건',
    full:'전체', partial:'일부', none:'없음', unknown:'알 수 없음', self:'본인', cross_user:'다른 사용자',
    public:'공개', private:'비공개', sensitive:'민감', escaped:'이스케이프됨', unescaped:'이스케이프되지 않음',
    unchanged:'변화 없음', error:'오류', controlled_difference:'통제된 차이', anonymous:'익명',
    authenticated:'인증됨', owner:'소유자', non_owner:'비소유자', admin:'관리자',
    auth_bypass:'인증 우회 단서', weak_credentials:'취약한 자격증명 단서', account_enumeration:'계정 열거 단서',
    query_injection:'쿼리 삽입 단서', unescaped_reflection:'이스케이프되지 않은 반사', cross_user_access:'다른 사용자 접근 단서'
  };
  const obsLabels = {
    input_control:'입력 제어', object_scope:'대상 범위', reflection:'입력 반사', data_exposure:'데이터 노출',
    query_behavior:'응답 변화', security_signal:'보안 단서', auth_required:'인증 필요', upload_public:'업로드 공개',
    baseline_compared:'기준 응답 비교', reproduced:'반복 관찰', baseline_status:'기준 응답 상태', response_status:'시험 응답 상태'
  };
  function display(value) {
    if (value === undefined || value === null || value === '') return '알 수 없음';
    if (value === true) return '예';
    if (value === false) return '아니요';
    return labels[value] || String(value);
  }
  function el(doc, tag, className, value) {
    const item = doc.createElement(tag);
    if (className) item.className = className;
    if (value !== undefined) item.textContent = String(value);
    return item;
  }
  function section(doc, title, className) {
    const wrap = el(doc,'section',`node-detail-section ${className || ''}`.trim());
    wrap.append(el(doc,'h4','node-detail-label',title));
    return wrap;
  }
  function fields(doc, rows) {
    const list = el(doc,'dl','node-detail-fields');
    for (const [label, value] of rows) {
      list.append(el(doc,'dt','',label), el(doc,'dd','',display(value)));
    }
    return list;
  }
  function riskText(risk) {
    if (!risk || risk.status === 'not_applicable') return '해당 없음';
    const score = typeof risk.score === 'number' && Number.isFinite(risk.score) ? risk.score.toFixed(2) + ' / 100' : null;
    const state = display(risk.status);
    return [score, state, risk.reason ? display(risk.reason) : null].filter(Boolean).join(' · ');
  }
  function render(container, detail) {
    const doc = container.ownerDocument;
    container.replaceChildren();
    const node = detail && detail.node;
    if (!node) {
      container.append(el(doc,'p','node-detail-empty','노드를 선택하면 조사 내용과 증거가 표시됩니다.'));
      return;
    }
    const heading = el(doc,'div','node-detail-heading');
    const headingText = el(doc,'div','');
    headingText.append(el(doc,'span','node-detail-kicker','SELECTED NODE'), el(doc,'h3','node-detail-title',display(node.id)));
    heading.append(headingText,el(doc,'span','node-detail-kind',display(node.kind)));
    container.append(heading);
    const overview = el(doc,'div','node-detail-overview');
    for (const [name, value] of [['조사 상태',node.investigation_state],['탐색 상태',node.exploration_state],['검증 상태',node.verification_state]]) {
      const card = el(doc,'div','node-detail-metric');
      card.append(el(doc,'span','',name), el(doc,'strong','',display(value)));
      overview.append(card);
    }
    container.append(overview);
    const risk = section(doc,'위험도 조사 우선순위','node-detail-risk');
    risk.append(el(doc,'strong','node-detail-risk-value',riskText(node.risk)));
    risk.append(el(doc,'p','node-detail-note','조사 순서를 위한 평가이며 취약점 확정 결과가 아닙니다.'));
    container.append(risk);
    const locator = node.locator || {};
    const position = section(doc,'대상 정보');
    position.append(fields(doc,[['요청',locator.method],['주소',locator.url],['입력',locator.input || '없음'],['인증 문맥',locator.auth_context],['상위 노드',node.parent_id || '없음']]));
    container.append(position);
    if (node.hypothesis) {
      const hypo = section(doc,'검증 가설');
      hypo.append(el(doc,'p','node-detail-text',`${display(node.hypothesis.type)} · ${node.hypothesis.statement || '가설 설명 없음'}`));
      container.append(hypo);
    }
    const obs = section(doc,'관찰된 정보');
    const obsRows = Object.entries(node.observations || {}).map(([key,value]) => [obsLabels[key] || key,value]);
    obs.append(obsRows.length ? fields(doc,obsRows) : el(doc,'p','node-detail-note','저장된 관찰이 없습니다.'));
    container.append(obs);
    const verification = node.verification;
    if (verification && (verification.claim || verification.next_observation)) {
      const claim = section(doc,'조사 기록');
      if (verification.claim) { claim.append(el(doc,'span','node-detail-subtitle','모델 기록 · 미확인'),el(doc,'p','node-detail-text',verification.claim)); }
      if (verification.next_observation) { claim.append(el(doc,'span','node-detail-subtitle','다음 관찰'),el(doc,'p','node-detail-text',verification.next_observation)); }
      container.append(claim);
    }
    const relations = section(doc,'노드 관계');
    const paths = Array.isArray(node.discovery_paths) ? node.discovery_paths : [];
    const relationRows = paths.map(path => `${display(path.parent_id)} · ${display(path.relation === 'discovered' ? 'discovered_relation' : path.relation)}`);
    const childIds = Array.isArray(node.child_ids) ? node.child_ids : [];
    relations.append(fields(doc,[['발견 경로',relationRows.length ? relationRows.join(' / ') : '없음'],['하위 노드',childIds.length ? childIds.join(', ') : '없음']]));
    container.append(relations);
    const evidence = section(doc,'보존된 증거');
    const refs = (Array.isArray(node.evidence_refs) ? node.evidence_refs : []).filter(ref => ref && typeof ref.path === 'string');
    if (!refs.length) evidence.append(el(doc,'p','node-detail-note','보존된 증거가 없습니다.'));
    else {
      const list = el(doc,'ul','node-detail-evidence');
      for (const ref of refs) {
        const li = el(doc,'li','node-detail-evidence-item');
        li.append(el(doc,'span','node-detail-evidence-title',ref.description || ref.source || '증거 파일'));
        const path = el(doc,'span','node-detail-path',ref.path);
        li.append(path);
        // Only link to the application's own artifact download endpoint.
        if (typeof detail.run_id === 'string' && /^[a-f0-9]{32}$/.test(detail.run_id) && ref.path.startsWith('artifacts/')) {
          const link = el(doc,'a','node-detail-download','다운로드');
          link.href = `/api/runs/${detail.run_id}/evidence-download?path=${encodeURIComponent(ref.path.slice(10))}`;
          li.append(link);
        }
        list.append(li);
      }
      evidence.append(list);
    }
    container.append(evidence);
    const attempts = Array.isArray(detail.attempts) ? detail.attempts : [];
    const history = section(doc,`조사 시도 · ${attempts.length}회`);
    if (!attempts.length) history.append(el(doc,'p','node-detail-note','기록된 시도가 없습니다.'));
    for (const attempt of attempts.slice().reverse()) {
      const item = el(doc,'details','node-detail-attempt');
      item.append(el(doc,'summary','',`${display(attempt.attempt_id)} · ${display(attempt.execution_status || attempt.status)}`));
      if (attempt.result && attempt.result.claim) item.append(el(doc,'p','node-detail-text',attempt.result.claim));
      if (attempt.result && attempt.result.next_observation) item.append(el(doc,'p','node-detail-note',`다음 관찰: ${attempt.result.next_observation}`));
      item.append(fields(doc,[['실행 ID',attempt.exec_id],['결과',attempt.status],['증거 파일',`${(attempt.evidence_refs || []).length}개`]]));
      history.append(item);
    }
    container.append(history);
  }
  return {render, riskText, display};
});
