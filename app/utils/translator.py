from deep_translator import GoogleTranslator
from typing import Any


def translate(obj: Any, target_lang: str) -> Any:
    if isinstance(obj, str):
        try:
            return GoogleTranslator(source='auto', target=target_lang).translate(obj)
        except Exception:
            return obj
    elif isinstance(obj, list):
        return [translate(item, target_lang) for item in obj]
    elif isinstance(obj, dict):
        return {k: translate(v, target_lang) for k, v in obj.items()}
    else:
        return obj