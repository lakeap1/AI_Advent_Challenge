"""Структурные условия готовности плана; смысл действий оценивает модель."""
from .memory import text_field


def validate_plan(value, *, required=False):
    if not isinstance(value, list) or len(value) > 6:
        raise ValueError('План должен быть списком не более чем из 6 шагов.')
    if required and not value:
        raise ValueError('Для выполнения нужен план с действиями и критериями проверки.')
    result = []
    for step in value:
        if not isinstance(step, dict) or set(step) != {'action', 'criterion'}:
            raise ValueError('Каждый шаг плана должен содержать действие и критерий проверки.')
        result.append({key: text_field(step[key], 'plan.' + key, 500)
                       for key in ('action', 'criterion')})
    return result


def event_plan(value, event, stage):
    plan = validate_plan(value, required=stage != 'planning')
    if event == 'revise_plan' and plan:
        raise ValueError('При возврате к планированию прежний план нужно очистить.')
    return plan
