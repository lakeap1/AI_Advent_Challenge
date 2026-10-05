"""Структурная проверка памяти; не проверяет истинность извлечённых фактов."""
import json


def parse_facts(text, config):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Повторяющийся ключ.')
            result[key] = value
        return result
    data = json.loads(text, object_pairs_hook=unique)
    if not isinstance(data, dict) or len(data) > config.facts_max_keys:
        raise ValueError('Нужен ограниченный словарь.')
    for key, value in data.items():
        if not isinstance(key, str) or not key.strip() or len(key) > 80:
            raise ValueError('Некорректный ключ.')
        if not isinstance(value, str) or not value.strip() or len(value) > config.facts_value_chars:
            raise ValueError('Некорректное значение.')
    for key, value in data.items():
        key.encode("utf-8")
        value.encode("utf-8")
    return data
