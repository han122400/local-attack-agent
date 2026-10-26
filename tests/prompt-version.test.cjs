const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function dashboard(fetch){
  const select={value:'',options:[{value:'',textContent:'기본값'}],replaceChildren(...options){this.options=options;}};
  const hint={textContent:'탐색 지침 프롬프트 버전'};
  const context={fetch,location:{pathname:'/',search:''},URLSearchParams,
    document:{getElementById:id=>id==='promptVersionSelect'?select:hint,
      createElement:()=>({}),addEventListener:()=>{}}};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8'),context);
  return {context,select,hint};
}

test('prompt options load both versions and restore the saved choice',async()=>{
  const {context,select}=dashboard(async()=>({ok:true,json:async()=>({versions:['v1','v2'],default:'v2'})}));
  await context.loadPrompts('v1');
  assert.deepEqual(Array.from(select.options,o=>o.value),['v1','v2']);
  assert.equal(select.value,'v1');
});

test('an unavailable saved version falls back to the server default',async()=>{
  const {context,select}=dashboard(async()=>({ok:true,json:async()=>({versions:['v1','v2'],default:'v2'})}));
  await context.loadPrompts('v999');
  assert.equal(select.value,'v2');
});

test('J1 is selectable without changing the v2 default',async()=>{
  const {context,select}=dashboard(async()=>({ok:true,json:async()=>({versions:['J1','v1','v2'],default:'v2'})}));
  await context.loadPrompts('J1');
  assert.deepEqual(Array.from(select.options,o=>o.value),['J1','v1','v2']);
  assert.equal(select.value,'J1');
});

test('a failed prompt request is explained instead of silently showing 기본값',async()=>{
  const {context,hint}=dashboard(async()=>({ok:false,status:404,json:async()=>({detail:'Not Found'})}));
  await context.loadPrompts();
  assert.match(hint.textContent,/불러오지 못/);
});

test('an empty version response does not clear the selector without an explanation',async()=>{
  const {context,select,hint}=dashboard(async()=>({ok:true,json:async()=>({versions:[],default:'v2'})}));
  await context.loadPrompts();
  assert.equal(select.options.length,1);
  assert.match(hint.textContent,/불러오지 못/);
});
