const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function dashboard(metrics,j25=null){
  const node=()=>({children:[],textContent:'',hidden:false,append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;}});
  const elements={scorePanel:node(),scoreCards:node(),scorePipelineNote:node()};
  const context={location:{pathname:'/',search:''},URLSearchParams,
    fetch:async()=>({ok:true,json:async()=>({score:0.69,evidence:9,failed:1,repeats:1,compactions:0,cost_usd:0.0054,pipeline_metrics:metrics,j25_metrics:j25})}),
    document:{getElementById:id=>elements[id],createElement:node,addEventListener:()=>{}}};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8'),context);
  return {context,elements};
}

test('score distinguishes execution outputs and unverified claims',async()=>{
  const {context,elements}=dashboard({declared_resources:3,analyzed_resources:1,unreviewed_resources:2,
    claim_counts:{security_effect:1},verified_findings:null});
  await context.loadScore({run_id:'fixture',run_result:'incomplete',findings:[]});
  assert.equal(elements.scoreCards.children[1].children[1].textContent,'실행 출력');
  assert.match(elements.scorePipelineNote.textContent,/분석 기록 1/);
  assert.match(elements.scorePipelineNote.textContent,/보안 영향 주장 1.*미검증/);
});

test('old runs show missing validation metadata instead of zero confirmed findings',async()=>{
  const {context,elements}=dashboard(null);
  await context.loadScore({run_id:'legacy',run_result:'completed',findings:[]});
  assert.match(elements.scorePipelineNote.textContent,/기록 없음/);
});

test('J2.5 score exposes model calls, first comparison and blocked repeats',async()=>{
  const {context,elements}=dashboard(null,{comparisons:2,stable_comparisons:1,pending:3,
    model_calls:4,model_decision_seconds:12.5,first_comparison_step:3,repeat_blocks:1});
  await context.loadScore({run_id:'j25',run_result:'incomplete',findings:[]});
  assert.match(elements.scorePipelineNote.textContent,/모델 호출 4회/);
  assert.match(elements.scorePipelineNote.textContent,/첫 성공 비교 3단계/);
  assert.match(elements.scorePipelineNote.textContent,/반복 차단 1회/);
});

test('J2.5 score distinguishes verified cases from legacy missing records',async()=>{
  const {context,elements}=dashboard(null,{comparisons:2,stable_comparisons:1,pending:3,
    verified_findings:1,case_count:3,verification_rate:null});
  await context.loadScore({run_id:'verified',run_result:'completed',findings:[]});
  assert.match(elements.scorePipelineNote.textContent,/검증된 고유 사례 1건/);
  assert.match(elements.scorePipelineNote.textContent,/비율 측정 불가/);
  const old=dashboard(null,{comparisons:1,stable_comparisons:1,pending:0,verified_findings:null});
  await old.context.loadScore({run_id:'legacy-j25',run_result:'completed',findings:[]});
  assert.match(old.elements.scorePipelineNote.textContent,/검증 기록 없음/);
});
