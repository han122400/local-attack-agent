const {test}=require('node:test');
const assert=require('node:assert/strict');
const settings=require('../static/model-settings-config.js');

test('OpenRouter uses remote labels, model limits and provider sampling defaults',()=>{
  const config=settings.describe('openrouter',{context_length:131072,max_completion_tokens:8192,supported_parameters:['reasoning','temperature']});
  assert.equal(config.title,'OpenRouter 모델 설정');
  assert.equal(config.defaults.context,131072);
  assert.equal(config.defaults.temperature,'');
  assert.equal(config.defaults.think,'null');
  assert.equal(config.outputMax,8192);
  assert.equal(config.thinkVisible,true);
  assert.match(config.contextLabel,/앱/);
});

test('unsupported remote options are hidden and excluded from the request',()=>{
  const info={supported_parameters:['max_tokens'],context_length:32768};
  const config=settings.describe('openrouter',info);
  assert.equal(config.thinkVisible,false);
  assert.equal(config.temperatureVisible,false);
  assert.deepEqual(settings.encode('openrouter',info,{think:'true',temperature:'0.8',context:'32768',output:'2048'}),
    {think:null,options:{num_ctx:32768,num_predict:2048}});
});

test('blank remote temperature preserves provider defaults while explicit values are forwarded',()=>{
  const info={supported_parameters:['temperature','reasoning']};
  const values={think:'null',temperature:'',context:'65536',output:'4096'};
  assert.equal(Object.hasOwn(settings.encode('openrouter',info,values).options,'temperature'),false);
  const explicit=settings.encode('openrouter',info,{...values,think:'true',temperature:'0.7'});
  assert.equal(explicit.options.temperature,0.7);
  assert.equal(explicit.think,true);
});

test('reasoning-effort models expose effort levels and encode the chosen level',()=>{
  const info={supported_parameters:['reasoning','reasoning_effort','temperature'],context_length:200000};
  const config=settings.describe('openrouter',info);
  assert.equal(config.thinkEffort,true);
  assert.equal(settings.encode('openrouter',info,{think:'"low"',temperature:'',context:'8192',output:'4096'}).think,'low');
});

test('effort levels are hidden for models without reasoning_effort and for Ollama',()=>{
  assert.equal(settings.describe('openrouter',{supported_parameters:['reasoning']}).thinkEffort,false);
  assert.equal(settings.describe('ollama',{}).thinkEffort,false);
});

test('Ollama keeps local defaults and its context and generation option semantics',()=>{
  const config=settings.describe('ollama',{});
  assert.equal(config.title,'Ollama 모델 설정');
  assert.deepEqual(config.defaults,{think:'false',temperature:'0',context:8192,output:4096});
  assert.equal(config.thinkVisible,true);
  assert.equal(config.temperatureVisible,true);
  assert.match(config.contextLabel,/num_ctx/);
  assert.deepEqual(settings.encode('ollama',{}, {think:'false',temperature:'0',context:'8192',output:'4096'}),
    {think:false,options:{num_ctx:8192,num_predict:4096,temperature:0}});
});
