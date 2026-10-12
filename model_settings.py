"""Dashboard controls for documented Ollama chat options."""

import math


FIELDS = [
    {"key": "temperature", "label": "Temperature", "hint": "출력의 다양성 · 0이면 가장 확률 높은 토큰 선택", "min": 0, "max": 2, "step": 0.05},
    {"key": "num_ctx", "label": "컨텍스트 토큰", "hint": "입력과 출력에 쓸 문맥 크기 · 커지면 메모리 사용 증가", "min": 512, "max": 262144, "step": 1, "integer": True},
    {"key": "num_predict", "label": "최대 출력 토큰", "hint": "Thinking과 최종 출력의 생성 한도 · -1은 토큰 한도 없음", "min": -1, "max": 65536, "step": 1, "integer": True},
    {"key": "top_k", "label": "Top K", "hint": "고려할 상위 토큰 수 · 0은 제한 해제", "min": 0, "max": 1000, "step": 1, "integer": True},
    {"key": "top_p", "label": "Top P", "hint": "고려할 토큰의 누적 확률 · 1은 필터 해제", "min": 0, "max": 1, "step": 0.01},
    {"key": "min_p", "label": "Min P", "hint": "최상위 토큰 대비 최소 확률 · 0은 필터 해제", "min": 0, "max": 1, "step": 0.01},
    {"key": "repeat_penalty", "label": "반복 패널티", "hint": "반복 토큰의 가중치 조정 · 1은 패널티 없음", "min": 0, "max": 2, "step": 0.05},
    {"key": "repeat_last_n", "label": "반복 검사 토큰", "hint": "반복을 검사할 범위 · 0은 끄기, -1은 컨텍스트 전체", "min": -1, "max": 262144, "step": 1, "integer": True},
    {"key": "presence_penalty", "label": "등장 패널티", "hint": "이미 등장한 토큰에 적용 · 0은 패널티 없음", "min": -2, "max": 2, "step": 0.1},
    {"key": "frequency_penalty", "label": "빈도 패널티", "hint": "자주 등장한 토큰에 적용 · 0은 패널티 없음", "min": -2, "max": 2, "step": 0.1},
    {"key": "seed", "label": "Seed", "hint": "재현 실험용 난수 시드 · -1은 무작위", "min": -1, "max": 2147483647, "step": 1, "integer": True},
]


def default_model_settings():
    # Preserve the existing app behavior until a user edits the controls.
    return {"think": False, "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 700}}


def settings_schema():
    return {"defaults": default_model_settings(), "fields": FIELDS}


def validate_model_settings(raw):
    if not isinstance(raw, dict) or set(raw) - {"think", "keep_alive", "options"}:
        raise ValueError("모델 세부설정 형식이 올바르지 않습니다.")
    think = raw.get("think", False)
    if think is not None and type(think) is not bool and not (isinstance(think, str) and think in {"low", "medium", "high", "max"}):
        raise ValueError("Thinking은 모델 기본값, 켜기/끄기 또는 지원되는 수준을 선택하세요.")
    options = raw.get("options", {})
    if not isinstance(options, dict):
        raise ValueError("모델 options는 객체여야 합니다.")
    fields = {field["key"]: field for field in FIELDS}
    if set(options) - fields.keys():
        raise ValueError("지원되지 않는 모델 옵션입니다.")
    for key, value in options.items():
        field = fields[key]
        if (type(value) not in {int, float} or not math.isfinite(value)
                or (field.get("integer") and not float(value).is_integer())
                or not field["min"] <= value <= field["max"]
                or (key == "num_predict" and value == 0)):
            raise ValueError(f"{key}: {field['min']}~{field['max']} 범위의 {'정수' if field.get('integer') else '숫자'}를 입력하세요." + (" 0은 허용되지 않습니다." if key == "num_predict" else ""))
    result = {"think": think, "options": {key: int(value) if fields[key].get("integer") else value for key, value in options.items()}}
    if "keep_alive" in raw:
        if type(raw["keep_alive"]) not in {int, str} or raw["keep_alive"] not in {0, -1, "1m", "5m", "10m", "30m", "1h"}:
            raise ValueError("모델 메모리 유지시간을 목록에서 선택하세요.")
        result["keep_alive"] = raw["keep_alive"]
    return result


def model_presets(model):
    if model == "qwen3.5:9b":
        return [{"name": "Qwen3.5 공식 · 일반 작업 / Thinking 끄기",
                 "source": "https://huggingface.co/Qwen/Qwen3.5-9B#best-practices",
                 "settings": {"think": False, "options": {"temperature": 0.7, "top_p": 0.8, "top_k": 20,
                                                           "min_p": 0, "presence_penalty": 1.5, "repeat_penalty": 1.0}}}]
    return []
