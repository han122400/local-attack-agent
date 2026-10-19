const $ = id => document.getElementById(id);
const runMatch = location.pathname.match(/^\/runs\/([0-9a-f]{32})$/);
const runId = runMatch ? runMatch[1] : null;
const historyMode = !runId && new URLSearchParams(location.search).get('view') === 'history';
const validRunId = value => /^[0-9a-f]{32}$/.test(String(value || ''));
const dateText = value => value ? new Date(value * 1000).toLocaleString('ko-KR') : '—';
const statusText = value => ({preparing:'준비 중',running:'탐색 중',stopping:'중단 중',finalizing:'정리 중',resetting:'환경 초기화 중',finished:'종료',recovery_required:'복구 필요',completed:'완료',stopped:'중단됨',timed_out:'시간 초과',error:'오류',succeeded:'완료',pending:'대기',failed:'실패',preserved:'보존됨',finalized:'확정됨',collecting:'수집 중'})[value] || value || '확인 중';
const textNode = (tag, value, className='') => { const item=document.createElement(tag); item.textContent=String(value ?? ''); if(className)item.className=className; return item; };
const setText = (id, value) => { $(id).textContent=String(value ?? ''); };
async function jsonRequest(url, options) {
  const response = await fetch(url, {cache:'no-store', ...options});
  let body;
  try { body=await response.json(); } catch { body={}; }
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
  return body;
}

let activeRunId=null, canStart=false, modelReady=false, labReady=false, startPending=false;
let currentRun=null, stream=null, lastSeq=0, readingEvents=false, pendingEvents=new Map();
let timelineEvents=[], consoleRaw='', consoleChannel='', selectedCode=null;
const modelCalls=new Map(), consoleEntries=[];
const runStats={calls:0,executions:0,modelErrors:0,systemErrors:0,unknownFailures:0,succeeded:0,failed:0,timed_out:0,cancelled:0,system_error:0,tokens:0};
let displayQueued=false, latestExecution=null;
let fileSource='runtime', fileItems=[], selectedFile=null, selectedEvidence=null, evidenceItems=[];
const collapsedDirs=new Set(['deps','tmp','browser']);
const goalSuggestions={
  structure:'설정된 대상 웹사이트에서 접근 가능한 페이지와 주요 기능의 구조를 파악해줘. 실제로 관찰한 링크, 화면과 이동 관계를 근거로 기능별 요약과 아직 확인하지 못한 영역을 정리해줘.',
  inputs:'설정된 대상 웹사이트에서 검색, 필터와 입력 요소의 동작을 관찰해줘. 관찰한 화면을 바탕으로 확인할 요소와 비파괴적인 입력값을 스스로 선택하고, 입력 전후의 변화와 근거를 정리해줘. 해당 요소가 없으면 관찰한 범위와 함께 기록해줘.',
  network:'설정된 대상 웹사이트의 화면과 스크립트, 네트워크 요청에서 API와 데이터 흐름을 파악해줘. 실제로 관찰한 경로, 요청 방식, 응답 상태와 데이터 형태를 정리하고, 코드에서 발견한 후보와 실제 호출이 확인된 항목을 구분해줘.',
  inputs_active:'설정된 대상 웹사이트에서 검색, 필터와 입력 요소의 동작을 관찰해줘. 관찰한 화면을 바탕으로 확인할 요소와 파괴적인 입력값을 스스로 선택하고, 입력 전후의 변화를 응답·DOM 근거로 정리해줘. 입력마다 필드를 비우고 테스트하고, 해당 요소가 없으면 관찰한 범위와 함께 기록해줘. 결과는 한국어로 정리해줘.'
};
const goalScope=' 탐색 범위는 설정된 대상 안으로 제한하고, 경로와 탐색 순서는 관찰 결과에 따라 스스로 결정해줘. 계정·데이터를 변경하는 동작은 수행하지 말고, 확인된 사실과 추정을 구분해줘.';

const actionLabel=action=>({read_file:'파일 읽기',write_file:'파일 작성',run_command:'CMD 실행',run_python:'브라우저 코드 실행',finish:'탐색 종료'})[action]||'다음 행동 생성';
const eventLabel=kind=>({run_created:'실험 시작',model_request:'모델 판단 시작',model_response:'모델 응답 완료',command_decoded:'실행 명령 확인',command_normalized:'중복 입력 정리',model_error:'모델 응답 오류',exec_started:'작업 실행',exec_finished:'작업 결과',file_changed:'파일 변경',process_started:'프로세스 시작',process_stopped:'프로세스 종료',runtime_created:'런타임 준비',runtime_destroyed:'런타임 삭제',run_finished_by_model:'모델 종료 판단',finalize_started:'실험 정리 시작',cleanup_step:'정리 진행',evidence_exported:'증거 보존',lab_reset:'실습환경 초기화',initial_lab_reset:'초기 상태 확인',run_finalized:'실험 종료',finalize_failed:'정리 실패',run_error:'실행기 오류'})[kind]||kind;

// Decode string fields for display while the JSON is still arriving. These
// drafts never authorize execution; only command_decoded marks validation.
function readModelFields(raw){
  try{const value=JSON.parse(raw);if(value&&typeof value==='object'&&!Array.isArray(value))return value;}catch{}
  const fields={};let i=0;
  const whitespace=()=>{while(/\s/.test(raw[i]||'')&&i<raw.length)i++;};
  function string(){
    if(raw[i++]!=='"')return null;
    let value='';
    while(i<raw.length){
      const char=raw[i++];
      if(char==='"')return {value,complete:true};
      if(char!=='\\'){value+=char;continue;}
      if(i>=raw.length)break;
      const escape=raw[i++], simple={'n':'\n','r':'\r','t':'\t','b':'\b','f':'\f','"':'"','\\':'\\','/':'/'};
      if(escape==='u'){
        const hex=raw.slice(i,i+4);if(!/^[0-9a-f]{4}$/i.test(hex))break;
        value+=String.fromCharCode(parseInt(hex,16));i+=4;
      }else if(Object.hasOwn(simple,escape))value+=simple[escape];else break;
    }
    return {value,complete:false};
  }
  whitespace();if(raw[i++]!=='{')return fields;
  while(i<raw.length){
    whitespace();if(raw[i]===','){i++;whitespace();}
    if(raw[i]!=='"')break;
    const key=string();if(!key?.complete)break;
    whitespace();if(raw[i++]!==':')break;whitespace();
    if(raw[i]!=='"')break;
    const value=string();
    if(['action','plan','reason','path','content','command','code'].includes(key.value))fields[key.value]=value.value;
    if(!value.complete)break;
  }
  return fields;
}

const categoryLabel=value=>({model:'모델',execution:'작업 실행',observation:'관찰',system:'시스템',lifecycle:'세션 관리'})[value]||value;
const outcomeLabel=value=>({succeeded:'성공',failed:'실패',timed_out:'시간 초과',cancelled:'중단',system_error:'시스템 오류로 실행 불가',exhausted:'예산 소진',recorded:'기록',observed:'관찰',received:'수신',started:'시작'})[value]||value;
function classifyEvent(event){
  if(event.event_schema===1)return event;
  // Compatibility for servers still serving old records. Output text is never
  // inspected for error words or HTTP status codes.
  let category='lifecycle',outcome='recorded';
  if(event.kind.startsWith('model_')||event.kind.startsWith('command_'))category='model';
  if(['exec_started','exec_finished','file_changed'].includes(event.kind))category='execution';
  if(['exec_output','observation','http_response'].includes(event.kind)){category='observation';outcome='observed';}
  if(/^(runtime_|process_|browser_|worker_output)/.test(event.kind))category='system';
  if(event.kind==='exec_finished'){
    outcome=event.cancelled?'cancelled':event.failure_source==='system'?'system_error':event.timed_out?'timed_out':event.exit_code===0?'succeeded':'failed';
    if(outcome==='system_error')category='system';
  }
  if(event.kind==='model_error'){
    outcome=event.error_kind==='cancelled'?'cancelled':event.error_kind==='budget'?'exhausted':event.error_kind==='timed_out'?'timed_out':'failed';
    if(event.error_kind==='transport')category='system';
    if(['cancelled','budget'].includes(event.error_kind))category='lifecycle';
  }
  if(['run_error','finalize_failed'].includes(event.kind)){category='system';outcome='failed';}
  return {...event,category,outcome};
}
function executionLabel(result){return result.outcome==='failed'?'작업 실패':outcomeLabel(result.outcome);}
function eventTitle(event){
  if(event.kind==='model_error')return event.category==='system'?'모델 연결 오류':event.outcome==='cancelled'?'모델 생성 중단':event.outcome==='exhausted'?'모델 예산 소진':'모델 응답 실패';
  return {exec_output:'관찰 출력',worker_output:'프로세스 출력',browser_respawn:'브라우저 복구'}[event.kind]||eventLabel(event.kind);
}
function modelErrorLabel(call){
  if(call.done_reason==='length'||call.error_kind==='output_limit')return '출력 한도';
  return {format:'응답 형식 오류',transport:'모델 연결 오류',timed_out:'모델 시간 초과',cancelled:'생성 중단',budget:'호출 예산 소진'}[call.error_kind]||'모델 오류';
}
function callStatus(call){
  if(call.error)return `${call.errorCategory==='system'?'시스템 오류':call.errorOutcome==='cancelled'?'생성 중단':call.errorOutcome==='exhausted'?'예산 소진':modelErrorLabel(call)} · 실행되지 않음`;
  if(call.result)return executionLabel(call.result);
  if(call.executing)return '실행 중';
  if(call.decoded)return call.action==='finish'?'종료 판단':'명령 확인됨';
  return '생성 중 · 실행 전 초안';
}
function getCall(id){
  if(!modelCalls.has(id)){
    const call={id,raw:'',thinking:'',fields:{},outputChannels:new Set()};modelCalls.set(id,call);
    consoleEntries.push({kind:'model',call});
  }
  return modelCalls.get(id);
}
function readableCall(call){
  const fields={...readModelFields(call.raw),...call.fields};
  const lines=[`\n── ${call.id} · ${callStatus(call)} ──`];
  if(fields.action)lines.push(`행동: ${actionLabel(fields.action)}`);
  if(fields.plan)lines.push(`계획: ${fields.plan}`);
  if(fields.reason)lines.push(`이유: ${fields.reason}`);
  if(fields.path)lines.push(`파일: ${fields.path}`);
  if(fields.code)lines.push(`\nPython 코드\n${fields.code}`);
  else if(fields.command)lines.push(`\nCMD 명령\n${fields.command}`);
  else if(fields.content)lines.push(`\n작성할 내용\n${fields.content}`);
  if(call.error){
    if(call.done_reason==='length'||call.error_kind==='output_limit')lines.push(`\n출력 한도 ${call.eval_count||call.limit||'—'}토큰에 도달했습니다. 이 응답의 코드는 실행되지 않았습니다.`);
    else lines.push(`\n${modelErrorLabel(call)}: ${call.error}`);
  }
  if(call.thinking)lines.push(`\nThinking\n${call.thinking}`);
  return lines.join('\n')+'\n';
}
function queueDisplay(){
  if(displayQueued)return;displayQueued=true;
  requestAnimationFrame(()=>{
    displayQueued=false;
    const raw=$('rawOutputToggle')?.checked;
    consoleRaw=consoleEntries.map(entry=>entry.kind==='model'
      ?raw?`\n[${entry.call.id} · 모델 원문]\n${entry.call.raw}\n${entry.call.error?'오류: '+entry.call.error+'\n':''}`:readableCall(entry.call)
      :`\n[${entry.channel}] ${entry.text}`).join('').slice(-200000);
    setText('consoleCount',`${consoleRaw.length.toLocaleString()}자`);
    renderConsole();renderAgentStatus();
  });
}
function renderAgentStatus(){
  setText('callMetric',runStats.calls);setText('execMetric',runStats.executions);
  setText('modelFailureMetric',runStats.modelErrors);setText('systemErrorMetric',runStats.systemErrors);
  setText('workBreakdown',`성공 ${runStats.succeeded} · 실패 ${runStats.failed} · 시간 초과 ${runStats.timed_out} · 중단 ${runStats.cancelled}${runStats.system_error?' · 시스템 문제 '+runStats.system_error:''}`);
  setText('systemErrorNote',currentRun?.log_status==='failed'?'로그 저장 실패 · 오류 집계가 불완전할 수 있습니다.':runStats.unknownFailures?`과거 실패 ${runStats.unknownFailures}건은 원인 미분류`:'실행기 · 연결 · 저장 · 정리');
  setText('tokenMetric',runStats.tokens.toLocaleString());
  if(currentRun?.budgets){setText('callBudget',`최대 ${currentRun.budgets.model_calls}회`);setText('execBudget',`최대 ${currentRun.budgets.tool_calls}회`);}
  const calls=[...modelCalls.values()],latest=calls.at(-1);
  if(latest){
    const fields={...readModelFields(latest.raw),...latest.fields};
    setText('currentPlan',fields.plan||(latest.error?'유효한 계획을 생성하지 못했습니다.':'다음 행동을 생성하고 있습니다.'));setText('currentReason',fields.reason||(latest.error?callStatus(latest):'선택 이유를 기다립니다.'));
    setText('actionStatus',callStatus(latest));$('actionStatus').className=`status-badge ${latest.error?'bad':latest.result?.exit_code===0?'ok':'warn'}`;
  }
  setText('decisionCount',`${runStats.calls}회 · 최근 5회`);
  if(calls.length){
    const parent=$('decisionList'),opened=new Set([...parent.querySelectorAll('details[open]')].map(item=>item.dataset.call));parent.replaceChildren();
    for(const call of calls.slice(-5).reverse()){
      const fields={...readModelFields(call.raw),...call.fields},row=textNode('article','','decision-card');
      const heading=textNode('div','','decision-head');heading.append(textNode('strong',`${call.id} · ${actionLabel(fields.action)}`),textNode('span',callStatus(call),call.error?'error-text':'subtle'));
      row.append(heading,textNode('p',fields.plan||'계획 생성 중','decision-plan'));
      if(fields.reason)row.append(textNode('p',fields.reason,'reason-text'));
      if(call.error)row.append(textNode('p',call.done_reason==='length'?`${call.eval_count||call.limit}토큰 한도로 응답이 잘렸습니다. 실행 0회.`:call.error,'decision-error'));
      const code=fields.code||fields.command||fields.content;
      if(code){const detail=document.createElement('details');detail.dataset.call=call.id;detail.open=opened.has(call.id);detail.append(textNode('summary',`${call.exec_id||'실행 전'} · ${fields.code?'Python 코드':fields.command?'CMD 명령':'파일 내용'} 보기`),textNode('pre',code,'code-preview'));row.append(detail);}
      parent.append(row);
    }
  }
  if(latestExecution){
    const result=latestExecution;
    setText('observationStatus',`${result.exec_id} · ${executionLabel(result)} · ${result.elapsed??'—'}초`);
    const failure=result.exception?.trim().split('\n').at(-1);
    setText('latestObservation',[failure,result.stdout,result.stderr].filter(Boolean).join('\n').slice(0,2400)||`${actionLabel(result.action)} 완료${result.path?'\n'+result.path:''} · 출력 없음`);
    $('observationStatus').classList.toggle('error-text',result.exit_code!==0);
  }
}

function updateSidebar(list) {
  activeRunId=validRunId(list.active_run_id) ? list.active_run_id : null;
  const current=$('currentRunLink');
  current.hidden=!activeRunId;
  if(activeRunId) current.href=`/runs/${activeRunId}`;
  for(const [id,active] of [['newRunLink',!runId&&!historyMode],['historyLink',historyMode],['currentRunLink',!!runId&&runId===activeRunId]]) {
    $(id).classList.toggle('active',active);
    if(active)$(id).setAttribute('aria-current','page'); else $(id).removeAttribute('aria-current');
  }
}

function renderRunRows(containerId, runs) {
  const parent=$(containerId); parent.replaceChildren();
  if(!runs.length){ parent.append(textNode('p','저장된 실험이 없습니다.','field-note')); return; }
  for(const item of runs){
    if(!validRunId(item.run_id))continue;
    const row=document.createElement('a'); row.className='run-row'; row.href=`/runs/${item.run_id}`;
    const lead=document.createElement('span');
    lead.append(textNode('strong',item.goal || '목표 기록 없음'),textNode('small',`${item.model || '모델 미기록'} · ${dateText(item.started_at)}`));
    const right=document.createElement('span');
    right.append(textNode('span',`${statusText(item.run_result || item.phase)} · ${statusText(item.cleanup_status)}`,'run-state'),textNode('span',item.run_id,'run-id'));
    row.append(lead,right); parent.append(row);
  }
}

function updateStartControls() {
  const modeOk=$('scenarioSelect').value==='manual' && $('chainSelect').value==='none';
  const ready=canStart&&modelReady&&labReady&&!startPending&&modeOk;
  $('startButton').disabled=!ready;
  if(!modeOk) setText('startError','자동 시나리오와 캠페인 체인은 준비 중입니다.');
  else if(!startPending) setText('startError','');
}

async function loadRunList() {
  try {
    const list=await jsonRequest('/api/runs');
    canStart=!!list.can_start; updateSidebar(list);
    if(!runId){
      renderRunRows('recentRuns',(list.runs||[]).slice(0,5));
      renderRunRows('historyRuns',list.runs||[]);
      const banner=$('activeRunBanner'); banner.hidden=!activeRunId;
      if(activeRunId) banner.href=`/runs/${activeRunId}`;
      const notice=$('startNotice');
      notice.className=`notice ${canStart?'':'warn'}`;
      notice.textContent=canStart?'새 실험을 시작할 수 있습니다.':`새 실험 대기: ${list.blocked_reason || '진행 중이거나 복구가 필요한 세션이 있습니다.'}`;
      updateStartControls();
    }
  } catch(error){
    canStart=false;
    if(!runId){ $('startNotice').className='notice error'; setText('startNotice',`세션 상태 확인 실패: ${error.message}`); updateStartControls(); }
  }
}

async function loadReadiness() {
  const [models,lab,processor]=await Promise.allSettled([
    jsonRequest('/api/models'),jsonRequest('/api/lab'),jsonRequest('/api/ollama/ps')]);
  const now=new Date().toLocaleTimeString('ko-KR'); setText('sideChecked',`마지막 확인 ${now}`);
  if(models.status==='fulfilled'){
    modelReady=(models.value.models||[]).length>0;
    const select=$('modelSelect'), previous=select.value;
    select.replaceChildren(...(models.value.models||[]).map(model=>{
      const option=textNode('option',`${model.name} · ${(Number(model.size||0)/1024**3).toFixed(1)} GiB`);
      option.value=model.name; return option;
    }));
    const names=(models.value.models||[]).map(item=>item.name);
    select.value=[previous,models.value.default_model,names[0]].find(name=>names.includes(name)) || '';
    select.disabled=!modelReady; setText('modelStatus',modelReady?`설치된 모델 ${names.length}개`:'설치된 모델이 없습니다.');
    setText('ollamaReadiness',modelReady?`연결됨 · 설치된 모델 ${names.length}개`:'연결됨 · 모델 없음');
    setText('sideOllama',modelReady?'연결됨':'모델 없음'); $('sideOllama').className=modelReady?'ok':'bad';
  } else {
    modelReady=false; $('modelSelect').disabled=true;
    setText('modelStatus',models.reason.message); setText('ollamaReadiness',models.reason.message);
    setText('sideOllama','연결 실패'); $('sideOllama').className='bad';
  }
  if(lab.status==='fulfilled'){
    labReady=!!lab.value.ready; setText('labReadiness',lab.value.message);
    setText('sideLab',labReady?'준비됨':statusText(lab.value.status)); $('sideLab').className=labReady?'ok':'bad';
    $('labStartButton').hidden=!lab.value.can_start;
  } else {
    labReady=false; setText('labReadiness',lab.reason.message); setText('sideLab','확인 실패'); $('sideLab').className='bad';
  }
  const processorList=$('processorList'); processorList.replaceChildren();
  if(processor.status==='fulfilled'){
    const items=processor.value.models||[];
    if(!items.length) processorList.append(textNode('p','현재 메모리에 로드된 모델이 없습니다.'));
    for(const item of items) processorList.append(textNode('p',`${item.name} · ${item.processor} · GPU 메모리 ${item.size_vram == null?'알 수 없음':(item.size_vram/1024**3).toFixed(2)+' GiB'}`));
  } else processorList.append(textNode('p',`조회 실패: ${processor.reason.message}`));
  if(!runId)updateStartControls();
}

async function startRun(event) {
  event.preventDefault();
  if(startPending)return;
  if($('scenarioSelect').value!=='manual'||$('chainSelect').value!=='none'){
    setText('startError','이 모드는 준비 중입니다.'); return;
  }
  const goal=$('goalInput').value.trim();
  if(!goal){setText('startError','탐색 목표를 입력하세요.'); $('goalInput').focus(); return;}
  const thinkValue=$('thinkSelect').value;
  const body={target:$('targetInput').value,model:$('modelSelect').value,goal,
    scenario_source:'manual',chain_mode:'none',
    model_settings:{think:thinkValue==='null'?null:thinkValue==='true',options:{
      temperature:Number($('temperatureInput').value),num_ctx:Number($('contextInput').value),num_predict:Number($('predictInput').value)}},
    budgets:{seconds:Number($('secondsInput').value),model_calls:Number($('modelCallsInput').value),
      tool_calls:Number($('toolCallsInput').value),command_seconds:Number($('commandSecondsInput').value),finalize_seconds:120}};
  startPending=true; updateStartControls(); setText('startButton','시작 요청 중');
  try{
    const started=await jsonRequest('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!validRunId(started.run_id))throw new Error('시작 응답의 run_id가 올바르지 않습니다.');
    location.assign(`/runs/${started.run_id}`);
  }catch(error){
    try{
      const latest=await jsonRequest('/api/runs');
      if(validRunId(latest.active_run_id)){location.assign(`/runs/${latest.active_run_id}`);return;}
    }catch{}
    setText('startError',error.message);
    startPending=false; setText('startButton','실험 시작'); updateStartControls();
  }
}

function initMain(){
  $('mainView').hidden=false; $('runView').hidden=true;
  $('setupSection').hidden=historyMode; $('historySection').hidden=!historyMode;
  if(historyMode){setText('mainHeading','실험 기록');setText('mainLead','저장된 세션의 결과와 실행 로그를 확인합니다.');}
  $('startForm').addEventListener('submit',startRun);
  for(const button of $('goalSuggestions').querySelectorAll('button[data-goal]'))button.addEventListener('click',()=>{
    const input=$('goalInput');input.value=goalSuggestions[button.dataset.goal]+goalScope;
    input.dispatchEvent(new Event('input',{bubbles:true}));input.focus();input.setSelectionRange(input.value.length,input.value.length);
  });
  for(const id of ['scenarioSelect','chainSelect'])$(id).addEventListener('change',updateStartControls);
  $('refreshModelsButton').addEventListener('click',loadReadiness);
  $('refreshReadinessButton').addEventListener('click',loadReadiness);
  $('labStartButton').addEventListener('click',async()=>{
    try{await jsonRequest('/api/lab/start',{method:'POST'});await loadReadiness();}
    catch(error){setText('labReadiness',error.message);}
  });
  loadRunList(); setInterval(loadRunList,3000);
}

function showTab(name){
  for(const tab of ['observe','evidence','settings']){
    const active=tab===name; $(`${tab}Panel`).hidden=!active;
    $(`${tab}Tab`).setAttribute('aria-selected',String(active));
  }
  if(name==='evidence')loadEvidenceFiles();
}

function renderRunHeader(state){
  currentRun=state; setText('runGoal',state.goal||'목표 기록 없음'); setText('runModel',state.model||'—'); setText('runId',runId);
  const seconds=Math.max(0,Math.floor((state.finished_at||Date.now()/1000)-(state.started_at||Date.now()/1000)));
  setText('runElapsed',`${Math.floor(seconds/60)}분 ${seconds%60}초`);
  const phase=statusText(state.phase), result=state.run_result?statusText(state.run_result):'탐색 중';
  setText('runStatus',`${result} · ${phase} · 정리 ${statusText(state.cleanup_status)} · 초기화 ${statusText(state.lab_reset_status)}`);
  $('runStatus').className=`status-badge ${state.phase==='finished'&&state.run_result==='completed'?'ok':state.phase==='recovery_required'||state.run_result==='error'?'bad':'warn'}`;
  $('stopButton').hidden=!state.live_available || state.phase==='finalizing' || state.phase==='resetting';
  $('recoverButton').hidden=state.phase!=='recovery_required'&&state.cleanup_status!=='failed'&&state.lab_reset_status!=='failed';
  setText('runError',state.cleanup_error||(state.run_result==='error'?state.finish_reason:'')||'');
  const summary=$('resultSummary'); summary.replaceChildren();
  for(const [key,value] of [['실험 결과',state.run_result||'진행 중'],['종료 이유',state.finish_reason||'—'],
    ['로그',state.log_status],['런타임 정리',state.cleanup_status],['Juice Shop 초기화',state.lab_reset_status]]){
    summary.append(textNode('dt',key),textNode('dd',value||'—'));
  }
  setText('settingsValue',JSON.stringify({run_id:state.run_id,target:state.target,goal:state.goal,model:state.model,
    model_settings:state.model_settings,budgets:state.budgets,scenario_source:state.scenario_source,chain_mode:state.chain_mode},null,2));
  renderAgentStatus();
  $('exportLink').href=`/api/runs/${runId}/export`;
  if(!state.live_available&&stream){stream.close();stream=null;setText('connectionStatus','저장 기록');refreshEvents();}
  if(!state.live_available&&!stream)setText('connectionStatus','저장 기록');
  if(state.live_available&&!stream)connectLive();
  if(!state.runtime_exists&&fileSource==='runtime'){
    fileSource='artifacts'; selectedFile=null; loadFiles();
  }
}

async function loadRunState(){
  try{const state=await jsonRequest(`/api/runs/${runId}`);renderRunHeader(state);}
  catch(error){setText('runError',`실험 상태 조회 실패: ${error.message}`);}
}

function queueEvent(event){
  if(event.run_id!==runId||!Number.isInteger(event.seq)||event.seq<=lastSeq)return;
  pendingEvents.set(event.seq,event);
  while(pendingEvents.has(lastSeq+1)){
    const next=pendingEvents.get(lastSeq+1);pendingEvents.delete(lastSeq+1);
    lastSeq++;applyEvent(next);
  }
  if(pendingEvents.size>1000){pendingEvents.clear();refreshEvents();}
}

async function refreshEvents(){
  if(readingEvents)return;
  readingEvents=true;
  try{
    for(let page=0;page<100;page++){
      const body=await jsonRequest(`/api/runs/${runId}/events?after_seq=${lastSeq}&limit=500`);
      for(const event of body.events||[])queueEvent(event);
      if(!body.has_more||!(body.events||[]).length)break;
    }
  }catch(error){setText('connectionStatus',`기록 조회 실패: ${error.message}`);}
  finally{readingEvents=false;}
}

function connectLive(){
  if(stream)return;
  setText('connectionStatus','연결 중');
  stream=new EventSource(`/api/runs/${runId}/live`);
  stream.onopen=()=>{setText('connectionStatus','실시간 연결됨');refreshEvents();};
  stream.addEventListener('update',message=>{
    try{const event=JSON.parse(message.data);const expected=lastSeq+1;queueEvent(event);if(event.seq>expected)refreshEvents();}
    catch{refreshEvents();}
  });
  stream.addEventListener('stream_end',()=>{
    stream.close();stream=null;setText('connectionStatus','저장 기록');refreshEvents();loadRunState();
  });
  stream.addEventListener('error',()=>{setText('connectionStatus','연결 끊김 · 재연결 중');refreshEvents();});
}

function eventDescription(event){
  const identity=[event.call_id,event.exec_id].filter(Boolean).join(' · ');
  let detail=event.plan||event.reason||event.message||event.error||event.path||event.step||(event.action?actionLabel(event.action):event.status?statusText(event.status):'');
  if(event.kind==='model_response')detail=`${event.eval_count??'—'}토큰 · ${event.elapsed??'—'}초 · ${event.done_reason==='length'?'출력 한도에서 잘림':'응답 수신'}`;
  if(event.kind==='exec_finished')detail=`${executionLabel(event)} · ${actionLabel(event.action)} · 종료 코드 ${event.exit_code??'—'} · ${event.elapsed??'—'}초${event.exception?' · '+event.exception.split('\n').filter(Boolean).at(-1):''}`;
  if(['exec_output','worker_output'].includes(event.kind))detail=event.text||'';
  if(event.kind==='command_normalized')detail=`중복 필드 정리: ${(event.fields||[]).join(', ')}`;
  return `${identity}${identity&&detail?' · ':''}${String(detail).slice(0,260)}` || '기록됨';
}

function renderTimeline(){
  const parent=$('timeline');parent.replaceChildren();
  const selected=$('eventCategoryFilter').value;
  const visible=timelineEvents.filter(event=>selected==='all'||event.category===selected);
  for(const event of visible){
    const row=document.createElement('button');row.type='button';row.className='timeline-row';
    row.append(textNode('time',new Date(event.at*1000).toLocaleTimeString('ko-KR')),
      textNode('span',eventTitle(event),'kind'),textNode('span',eventDescription(event),'body'));
    row.dataset.kind=event.kind;row.dataset.category=event.category;row.dataset.outcome=event.outcome;
    row.title=`${categoryLabel(event.category)} · ${outcomeLabel(event.outcome)}`;
    row.addEventListener('click',()=>showExecution(event,row));
    parent.append(row);
  }
  setText('eventCount',`${visible.length}건 · 최근 ${timelineEvents.length}건 중`);
}

function showExecution(event,row){
  for(const item of $('timeline').querySelectorAll('.selected'))item.classList.remove('selected');
  row.classList.add('selected');
  const matching=event.exec_id&&event.category==='execution'?[...timelineEvents].reverse().find(item=>item.exec_id===event.exec_id&&item.kind==='exec_finished')||event:event;
  const detail=[`${matching.call_id||'—'} · ${matching.exec_id||'—'} · ${matching.action?actionLabel(matching.action):eventTitle(matching)}`];
  if(matching.kind==='exec_finished')detail.push(`종료 코드: ${matching.exit_code??'없음'} · 소요 시간: ${matching.elapsed??'—'}초`);
  detail.push(`분류: ${categoryLabel(matching.category)} · 결과: ${outcomeLabel(matching.outcome)}`);
  if(matching.component)detail.push(`발생 위치: ${matching.component}`);
  if(matching.cause_code)detail.push(`원인 코드: ${matching.cause_code}`);
  if(matching.failure_source)detail.push(`실패 원인 구분: ${matching.failure_source}`);
  if(matching.exception_type)detail.push(`예외 종류: ${matching.exception_type}`);
  detail.push(`재시도: ${matching.retryable===true?'수정·복구 후 가능 (자동 재시도 아님)':matching.retryable===false?'자동 재시도 대상 아님':'미지정'}`);
  if(matching.message||matching.error||matching.text)detail.push(`\n원문 메시지\n${matching.message||matching.error||matching.text}`);
  if(matching.path||matching.cwd)detail.push(`작업 경로: ${matching.path||matching.cwd}`);
  if(matching.stdout)detail.push(`\n표준 출력\n${matching.stdout}`);
  if(matching.stderr)detail.push(`\n오류 출력\n${matching.stderr}`);
  if(matching.exception)detail.push(`\n예외\n${matching.exception}`);
  if(matching.output_truncated)detail.push('\n출력 일부가 잘렸습니다. 전체 저장 기록을 확인하세요.');
  setText('executionText',detail.join('\n'));$('executionDetail').hidden=false;
  selectedCode=typeof matching.artifact_path==='string'&&matching.artifact_path.startsWith('artifacts/')
    ?matching.artifact_path.slice('artifacts/'.length):null;
  $('openCodeButton').hidden=!selectedCode;
}

function appendConsole(chunk,channel){
  if(!chunk)return;
  const last=consoleEntries.at(-1);
  if(last?.kind==='text'&&last.channel===channel)last.text=(last.text+chunk).slice(-200000);
  else consoleEntries.push({kind:'text',channel,text:chunk.slice(-200000)});
  if(consoleEntries.length>500)consoleEntries.shift();
  queueDisplay();
}

function renderConsole(){
  const box=$('consoleOutput'),previousTop=box.scrollTop,atBottom=box.scrollTop+box.clientHeight>=box.scrollHeight-28;
  const search=$('consoleSearch').value;
  box.replaceChildren();
  if(!search)box.textContent=consoleRaw;
  else{
    const lower=consoleRaw.toLocaleLowerCase(),needle=search.toLocaleLowerCase();let cursor=0,found=0;
    while(cursor<consoleRaw.length&&found<2000){
      const pos=lower.indexOf(needle,cursor);
      if(pos<0)break;
      box.append(document.createTextNode(consoleRaw.slice(cursor,pos)));
      box.append(textNode('mark',consoleRaw.slice(pos,pos+search.length)));
      cursor=pos+search.length;found++;
    }
    box.append(document.createTextNode(consoleRaw.slice(cursor)));
  }
  if($('autoScrollToggle').checked&&atBottom)box.scrollTop=box.scrollHeight;
  else if(consoleRaw){box.scrollTop=previousTop;$('newOutputButton').hidden=false;}
}

function applyEvent(event){
  event=classifyEvent(event);
  if(event.category==='system'&&['failed','system_error'].includes(event.outcome)){runStats.systemErrors++;queueDisplay();}
  if(event.kind==='model_request'){
    const call=getCall(event.call_id);call.limit=event.settings?.options?.num_predict;runStats.calls++;queueDisplay();
  }else if(event.kind==='model_delta'){
    const call=getCall(event.call_id);
    if(event.channel==='thinking')call.thinking=(call.thinking+event.text).slice(-50000);
    else call.raw=(call.raw+event.text).slice(0,200000);
    queueDisplay();
  }else if(event.kind==='model_response'){
    const call=getCall(event.call_id);call.raw=String(event.output||'').slice(0,200000);
    call.done_reason=event.done_reason;call.eval_count=event.eval_count;runStats.tokens+=event.eval_count||0;queueDisplay();
  }else if(event.kind==='command_decoded'){
    const call=getCall(event.call_id);call.decoded=true;call.action=event.action;
    call.fields={action:event.action,plan:event.plan,reason:event.reason};queueDisplay();
  }else if(event.kind==='model_error'){
    const call=getCall(event.call_id);call.error=event.message;call.error_kind=event.error_kind;call.errorCategory=event.category;call.errorOutcome=event.outcome;
    if(event.category==='model'&&['failed','timed_out'].includes(event.outcome))runStats.modelErrors++;
    queueDisplay();
  }else if(event.kind==='command_normalized')appendConsole(`${event.call_id} · 동일한 중복 필드를 정리했습니다: ${(event.fields||[]).join(', ')}\n`,'응답 정리');
  else if(event.kind==='exec_output'||event.kind==='worker_output'){
    if(event.call_id)getCall(event.call_id).outputChannels.add(event.channel);
    appendConsole(event.text,event.channel||'output');
  }
  else if(event.kind==='exec_started'){
    const call=getCall(event.call_id);call.executing=true;call.exec_id=event.exec_id;runStats.executions++;
    appendConsole(`${event.exec_id} · ${actionLabel(event.action)}\n작업 경로: ${event.cwd||'—'}\n`,'실행');
  }else if(event.kind==='exec_finished'){
    const call=getCall(event.call_id);call.result=event;latestExecution=event;
    if(event.outcome==='failed'&&(!event.failure_source||event.failure_source==='unknown'))runStats.unknownFailures++;
    if(['succeeded','failed','timed_out','cancelled','system_error'].includes(event.outcome))runStats[event.outcome]++;
    for(const channel of ['stdout','stderr'])if(event[channel]&&!call.outputChannels.has(channel))appendConsole(event[channel],channel);
    appendConsole(`${event.exec_id} · ${executionLabel(event)} · 종료 코드 ${event.exit_code??'—'} · ${event.elapsed??'—'}초${event.exception?'\n'+event.exception:''}${event.output_truncated?'\n출력 일부 잘림':''}\n`,'실행 결과');
  }
  else if(event.kind==='finalize_failed'||event.kind==='run_error')appendConsole(`\n${event.kind}: ${event.error||event.message||''}\n`,'system');
  if(event.kind!=='model_delta'){
    timelineEvents.push(event);if(timelineEvents.length>350)timelineEvents.shift();renderTimeline();
  }
  if(event.kind==='file_changed'&&fileSource==='runtime')loadFiles();
  if(['cleanup_step','lab_reset','run_finalized','finalize_failed'].includes(event.kind))loadRunState();
}

function hiddenByCollapse(path){
  const parts=path.split('/');
  for(let i=1;i<parts.length;i++)if(collapsedDirs.has(parts.slice(0,i).join('/')))return true;
  return false;
}

function renderFileList(parentId,items,source,evidence=false){
  const parent=$(parentId);parent.replaceChildren();
  if(!items.length){parent.append(textNode('p','표시할 파일이 없습니다.','field-note'));return;}
  for(const item of items){
    if(hiddenByCollapse(item.path))continue;
    const depth=item.path.split('/').length-1;
    const button=document.createElement('button');button.type='button';button.className='file-item';
    button.style.paddingLeft=`${8+depth*16}px`;
    const label=item.path.split('/').at(-1);
    if(item.kind==='directory'){
      button.classList.add('folder');button.textContent=`${collapsedDirs.has(item.path)?'▸':'▾'} ${label}`;
      button.setAttribute('aria-expanded',String(!collapsedDirs.has(item.path)));
      button.addEventListener('click',()=>{if(collapsedDirs.has(item.path))collapsedDirs.delete(item.path);else collapsedDirs.add(item.path);renderFileList(parentId,items,source,evidence);});
    }else{
      button.textContent=`${item.kind==='link'?'↗':'·'} ${label} · ${item.kind==='file'?item.size+' B':item.kind}`;
      if(item.kind==='link')button.disabled=true;
      else button.addEventListener('click',()=>selectFile(source,item.path,evidence,item));
      if((evidence?selectedEvidence:selectedFile)?.path===item.path)button.classList.add('selected');
    }
    parent.append(button);
  }
}

async function selectFile(source,path,evidence=false,metadata=null){
  const target=evidence?'evidencePreview':'filePreview',meta=evidence?'evidenceMeta':'fileMeta';
  setText(meta,`${source} · ${path} · 읽는 중`);
  try{
    const data=await jsonRequest(`/api/runs/${runId}/file?source=${encodeURIComponent(source)}&path=${encodeURIComponent(path)}`);
    if(evidence)selectedEvidence={path,source,size:data.size,modified_at:data.modified_at};
    else selectedFile={path,source,size:data.size,modified_at:data.modified_at};
    setText(target,data.kind==='binary'?'바이너리 파일 · 텍스트 미리보기 없음':data.content||'');
    setText(meta,`${source} · ${path} · ${data.size} B · ${dateText(data.modified_at)}${data.truncated?' · 미리보기 잘림':''}`);
    renderFileList(evidence?'evidenceFiles':'fileList',evidence?evidenceItems:fileItems,source,evidence);
  }catch(error){setText(meta,`${source} · ${path} · ${error.message}`);setText(target,'파일을 읽을 수 없습니다.');}
}

async function loadFiles(){
  if(!runId)return;
  if(fileSource==='artifacts'&&currentRun?.runtime_exists)return;
  try{
    const response=await jsonRequest(`/api/runs/${runId}/files?source=${fileSource}`);
    if(!response.available&&fileSource==='runtime'){
      fileSource='artifacts';selectedFile=null;setText('fileHeading','보존된 코드·증거');return loadFiles();
    }
    fileItems=response.files||[];setText('fileHeading',fileSource==='runtime'?'런타임 파일':'보존된 코드·증거');
    setText('fileCount',`${fileItems.filter(item=>item.kind==='file').length}개${response.truncated?' · 목록 일부':''}`);
    renderFileList('fileList',fileItems,fileSource);
    if(selectedFile){
      const current=fileItems.find(item=>item.path===selectedFile.path&&item.kind==='file');
      if(!current){setText('fileMeta',`${selectedFile.path} · 삭제됨 또는 미보존`);setText('filePreview','');selectedFile=null;}
      else if(current.size!==selectedFile.size||current.modified_at!==selectedFile.modified_at)selectFile(fileSource,current.path,false,current);
    }
  }catch(error){setText('fileMeta',`파일 목록 조회 실패: ${error.message}`);}
}

async function loadEvidenceFiles(){
  if(!runId)return;
  try{
    const response=await jsonRequest(`/api/runs/${runId}/files?source=artifacts`);
    evidenceItems=response.files||[];renderFileList('evidenceFiles',evidenceItems,'artifacts',true);
  }catch(error){setText('evidenceMeta',`증거 목록 조회 실패: ${error.message}`);}
}

function initRun(){
  $('mainView').hidden=true;$('runView').hidden=false;
  $('eventCategoryFilter').addEventListener('change',renderTimeline);
  for(const name of ['observe','evidence','settings'])$(`${name}Tab`).addEventListener('click',()=>showTab(name));
  $('stopButton').addEventListener('click',async()=>{
    $('stopButton').disabled=true;
    try{await jsonRequest(`/api/runs/${runId}/stop`,{method:'POST'});await loadRunState();}
    catch(error){setText('runError',error.message);}finally{$('stopButton').disabled=false;}
  });
  $('recoverButton').addEventListener('click',async()=>{
    $('recoverButton').disabled=true;
    try{await jsonRequest(`/api/runs/${runId}/reset`,{method:'POST'});await loadRunState();}
    catch(error){setText('runError',error.message);}finally{$('recoverButton').disabled=false;}
  });
  $('openCodeButton').addEventListener('click',()=>{if(selectedCode){selectFile('artifacts',selectedCode);$('filePreview').scrollIntoView({block:'center'});}});
  $('consoleSearch').addEventListener('input',renderConsole);
  $('rawOutputToggle').addEventListener('change',()=>{setText('outputModeNote',$('rawOutputToggle').checked?'모델의 JSON 원문입니다. 실행 출력은 그대로 표시합니다.':'계획·이유·코드를 풀어 표시합니다. 생성 중인 코드는 실행 전 초안입니다.');queueDisplay();});
  $('wrapToggle').addEventListener('change',()=> $('consoleOutput').classList.toggle('wrap',$('wrapToggle').checked));
  $('autoScrollToggle').addEventListener('change',()=>{if($('autoScrollToggle').checked){$('newOutputButton').hidden=true;$('consoleOutput').scrollTop=$('consoleOutput').scrollHeight;}});
  $('consoleOutput').addEventListener('scroll',()=>{
    const box=$('consoleOutput');
    if(box.scrollTop+box.clientHeight<box.scrollHeight-45){$('autoScrollToggle').checked=false;}
    else if(box.scrollHeight>0){$('autoScrollToggle').checked=true;$('newOutputButton').hidden=true;}
  });
  $('newOutputButton').addEventListener('click',()=>{$('autoScrollToggle').checked=true;$('newOutputButton').hidden=true;$('consoleOutput').scrollTop=$('consoleOutput').scrollHeight;});
  loadRunList();loadRunState();refreshEvents();loadFiles();
  setInterval(loadRunState,2500);
  setInterval(()=>{if(currentRun?.runtime_exists&&fileSource==='runtime')loadFiles();},1700);
  setInterval(loadRunList,5000);
  setInterval(()=>{if(!stream)refreshEvents();},4000);
  window.addEventListener('beforeunload',()=>{if(stream)stream.close();});
}

document.addEventListener('DOMContentLoaded',()=>{
  $('menuToggle').addEventListener('click',()=>{
    const open=$('sidebar').classList.toggle('open');$('menuToggle').setAttribute('aria-expanded',String(open));
  });
  if(runId)initRun();else initMain();
  loadReadiness();setInterval(loadReadiness,15000);
});
