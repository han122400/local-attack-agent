/* Provider metadata -> editable controls and the harness's saved settings format. */
(() => {
  function describe(provider, info={}) {
    const remote=provider==='openrouter', supported=info.supported_parameters||[];
    const contextMax=Math.min(262144,info.context_length||262144);
    const outputMax=Math.min(65536,info.max_completion_tokens||65536,info.context_length||65536);
    return {
      title:remote?'OpenRouter 모델 설정':'Ollama 모델 설정',
      help:remote?'선택한 모델이 지원하는 옵션만 표시합니다. 비워 둔 Temperature는 API 제공자의 기본값을 사용합니다.':
        '로컬 Ollama의 추론·샘플링·메모리 설정입니다.',
      thinkVisible:!remote||supported.includes('reasoning'),
      temperatureVisible:!remote||supported.includes('temperature'),
      thinkLabel:remote?'Reasoning · 추론':'Thinking',
      thinkHelp:remote?'기본값은 선택한 모델과 API 제공자의 설정을 따릅니다.':'모델이 지원하는 경우 Thinking을 켜거나 끕니다.',
      contextLabel:remote?'앱 대화 기억 예산 · 토큰':'컨텍스트 토큰 · num_ctx',
      contextHelp:remote?'대화 기억을 압축하는 앱의 토큰 예산입니다. 위에 표시된 모델 문맥 한도 내에서 설정합니다.':
        'Ollama가 사용할 문맥 크기입니다. 값이 커지면 로컬 메모리 사용량이 늘어납니다.',
      outputLabel:remote?'최대 출력 토큰 · max_tokens':'출력 토큰 · num_predict',
      outputHelp:remote?'모델의 출력 한도 내에서 설정합니다. 입력 분량에 따라 실제 출력 상한이 줄어들 수 있습니다.':
        'Thinking과 최종 응답을 합친 생성 토큰 한도입니다.',
      contextMax,outputMax,
      defaults:remote?{think:'null',temperature:'',context:contextMax,output:Math.min(4096,outputMax)}:
        {think:'false',temperature:'0',context:8192,output:4096}
    };
  }

  function encode(provider,info,values) {
    const config=describe(provider,info);
    const think=config.thinkVisible?JSON.parse(values.think):null;
    const options={num_ctx:Number(values.context),num_predict:Number(values.output)};
    if(config.temperatureVisible&&String(values.temperature).trim()!=='')options.temperature=Number(values.temperature);
    return {think,options};
  }

  const api={describe,encode};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  else globalThis.ModelSettingsConfig=api;
})();
