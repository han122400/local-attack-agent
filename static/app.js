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
let fileSource='runtime', fileItems=[], selectedFile=null, selectedEvidence=null, evidenceItems=[];
const collapsedDirs=new Set(['deps','tmp','browser']);

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
  $('runStatus').className=`status-badge ${state.phase==='finished'&&state.run_result==='completed'?'ok':state.phase==='recovery_required'?'bad':'warn'}`;
  $('stopButton').hidden=!state.live_available || state.phase==='finalizing' || state.phase==='resetting';
  $('recoverButton').hidden=state.phase!=='recovery_required'&&state.cleanup_status!=='failed'&&state.lab_reset_status!=='failed';
  setText('runError',state.cleanup_error||'');
  const summary=$('resultSummary'); summary.replaceChildren();
  for(const [key,value] of [['실험 결과',state.run_result||'진행 중'],['종료 이유',state.finish_reason||'—'],
    ['로그',state.log_status],['런타임 정리',state.cleanup_status],['Juice Shop 초기화',state.lab_reset_status]]){
    summary.append(textNode('dt',key),textNode('dd',value||'—'));
  }
  setText('settingsValue',JSON.stringify({run_id:state.run_id,target:state.target,goal:state.goal,model:state.model,
    model_settings:state.model_settings,budgets:state.budgets,scenario_source:state.scenario_source,chain_mode:state.chain_mode},null,2));
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
  const detail=event.plan||event.reason||event.action||event.step||event.error||event.message||event.path||'';
  return `${identity}${identity&&detail?' · ':''}${String(detail).slice(0,260)}` || '기록됨';
}

function renderTimeline(){
  const parent=$('timeline');parent.replaceChildren();
  for(const event of timelineEvents){
    const row=document.createElement('button');row.type='button';row.className='timeline-row';
    row.append(textNode('time',new Date(event.at*1000).toLocaleTimeString('ko-KR')),
      textNode('span',event.kind,'kind'),textNode('span',eventDescription(event),'body'));
    if(event.exec_id)row.addEventListener('click',()=>showExecution(event,row));
    parent.append(row);
  }
  setText('eventCount',`${timelineEvents.length}건 · 최근 기록`);
}

function showExecution(event,row){
  for(const item of $('timeline').querySelectorAll('.selected'))item.classList.remove('selected');
  row.classList.add('selected');
  const matching=[...timelineEvents].reverse().find(item=>item.exec_id===event.exec_id&&item.kind==='exec_finished')||event;
  const detail={call_id:matching.call_id,exec_id:matching.exec_id,action:matching.action,
    exit_code:matching.exit_code,elapsed:matching.elapsed,stdout:matching.stdout,
    stderr:matching.stderr,exception:matching.exception,artifact_path:matching.artifact_path};
  setText('executionText',JSON.stringify(detail,null,2));$('executionDetail').hidden=false;
  selectedCode=typeof matching.artifact_path==='string'&&matching.artifact_path.startsWith('artifacts/')
    ?matching.artifact_path.slice('artifacts/'.length):null;
  $('openCodeButton').hidden=!selectedCode;
}

function appendConsole(chunk,channel){
  if(!chunk)return;
  if(channel!==consoleChannel){consoleRaw+=`\n[${channel}] `;consoleChannel=channel;}
  consoleRaw+=chunk;
  if(consoleRaw.length>200000)consoleRaw=consoleRaw.slice(-200000);
  setText('consoleCount',`${consoleRaw.length.toLocaleString()}자 · 화면 보관 범위`);
  renderConsole();
}

function renderConsole(){
  const box=$('consoleOutput'),atBottom=box.scrollTop+box.clientHeight>=box.scrollHeight-28;
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
  else if(consoleRaw) $('newOutputButton').hidden=false;
}

function applyEvent(event){
  if(event.kind==='model_delta')appendConsole(event.text,event.channel==='thinking'?'Ollama thinking':'Ollama');
  else if(event.kind==='exec_output'||event.kind==='worker_output')appendConsole(event.text,event.channel||'output');
  else if(event.kind==='model_request')appendConsole(`\n${event.call_id} · 모델 판단 시작\n`,'system');
  else if(event.kind==='exec_started')appendConsole(`\n${event.exec_id} · ${event.action} · ${event.cwd||''}\n`,'system');
  else if(event.kind==='exec_finished')appendConsole(`\n${event.exec_id} · 종료 코드 ${event.exit_code??'—'}${event.exception?' · '+event.exception.slice(-300):''}\n`,'system');
  else if(event.kind==='finalize_failed'||event.kind==='run_error')appendConsole(`\n${event.kind}: ${event.error||event.message||''}\n`,'system');
  if(!['model_delta','exec_output','worker_output'].includes(event.kind)){
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
  $('openCodeButton').addEventListener('click',()=>{if(selectedCode)selectFile('artifacts',selectedCode);});
  $('consoleSearch').addEventListener('input',renderConsole);
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
