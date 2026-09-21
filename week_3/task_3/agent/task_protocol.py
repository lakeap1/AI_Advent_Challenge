"""Машинный контракт ответа и программная таблица событий автомата."""
import json

from .memory import text_field
from .task_state import FIELDS
from .task_plan import event_plan


EVENTS = {
    'planning': {'stay': 'planning', 'plan_ready': 'execution'},
    'execution': {'stay': 'execution', 'result_reported': 'validation', 'revise_plan': 'planning'},
    'validation': {'stay': 'validation', 'revise_work': 'execution', 'finish': 'done'},
    'done': {},
}


def next_stage(state, event):
    if state['paused'] or not isinstance(event, str) or event not in EVENTS[state['stage']]:
        raise ValueError('Событие недопустимо в текущем состоянии.')
    return EVENTS[state['stage']][event]


def parse_task_response(text, state, prompt):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Повторяющееся поле машинного ответа.')
            result[key] = value
        return result
    data = json.loads(text, object_pairs_hook=unique)
    if not isinstance(data, dict) or set(data) != {'answer', 'event', 'evidence', 'plan', *FIELDS}:
        raise ValueError('Неверный набор полей машинного ответа.')
    stage = next_stage(state, data['event'])
    parsed = {key: text_field(data[key], key, limit, empty=key == 'notes')
              for key, limit in FIELDS.items()}
    parsed['answer'] = text_field(data['answer'], 'answer', 16000)
    parsed['event'] = data['event']
    legacy_rework = state['stage'] == 'validation' and data['event'] == 'revise_work' and state['plan'] == []
    parsed['plan'] = event_plan(data['plan'], data['event'], 'planning' if legacy_rework else stage)
    if state['stage'] != 'planning' and data['event'] != 'revise_plan':
        if parsed['plan'] != state['plan'] or parsed['goal'] != state['goal']:
            raise ValueError('Для изменения цели или плана сначала вернитесь к планированию.')
    parsed['evidence'] = text_field(data['evidence'], 'evidence', 1000, empty=data['event'] == 'stay')
    if parsed['evidence'] and parsed['evidence'] not in prompt:
        raise ValueError('Основание события отсутствует в текущем сообщении пользователя.')
    return parsed


def response_contract(state):
    return '''
TASK_RESPONSE_JSON
Ты сам ведёшь задачу. Пользователь не заполняет поля и не переключает этапы.
Верни только JSON-объект с точными ключами:
{"answer":"ответ пользователю", "event":"событие", "evidence":"цитата из текущего сообщения",
 "goal":"цель", "current_step":"текущий шаг", "expected_action":"что ожидаешь от пользователя",
 "notes":"сохранённые решения и наблюдения",
 "plan":[{"action":"действие шага", "criterion":"как проверить результат шага"}]}.
Все значения кроме plan — строки. plan — массив объектов action/criterion.
Никаких дополнительных ключей, stage, paused или ограждений JSON.
Правила оформления профиля и просьбы пользователя относятся ТОЛЬКО к строке answer,
не к машинному JSON-конверту. В answer давай полезную помощь по графике и арту,
а не инструкции по заполнению карточки или нажатию кнопок перехода.
goal до 2000 символов, current_step и expected_action до 500, notes до 6000.
plan содержит от 1 до 6 шагов в порядке выполнения; action и criterion — непустые
строки до 500 символов. В planning при уточнении задачи допустим plan: [].
Перед plan_ready составь весь необходимый план: что делаем, в каком порядке и как
проверяем результат каждого шага. В answer сначала объясни этот план, затем первое
действие. Для простой задачи достаточно одного содержательного шага. Отдельного
подтверждения плана и ручного переключения этапа от пользователя не требуй.
Для учебного вопроса шагами могут быть объяснение понятия, разбор примера и проверка
понимания. Если не хватает существенных данных, останься в planning и уточни их.
В execution/validation/done возвращай сохранённый непустой plan; не стирай его после
выполнения шага или обрезки истории. При необходимости нового плана выбери revise_plan
и верни plan: [], затем составь новый план в planning. Если прежняя задача после
обновления не имеет plan, восстанови его по сохранённому контексту, не выдумывая фактов;
из validation без плана верни revise_work с plan: [], затем из execution верни revise_plan.
Вне planning цель goal и plan должны точно совпадать с сохранёнными. Не переписывай
формулировки, не удаляй неудобные критерии. Для иной цели нужен revise_plan.
Условия завершения этапов:
Планирование: цель понятна, план покрывает её целиком, каждый критерий проверяем.
Выполнение: по КАЖДОМУ шагу получен конкретный результат, соответствующий критерию.
Проверка: все критерии достигнуты, замечания устранены, пользователь подтвердил итог.
Отдельный проверяющий запрос оценивает эти условия; твой event — только предложение.
Читай step_results: passed — уже проверенный результат, pending/failed — открытая работа.
Не считай обещание «проверю» или общую фразу «готово» доказательством всех пунктов.
Помогай пройти оставшиеся шаги. Для учебной цели проверяемым результатом может быть
решённый пример или обоснованное объяснение пользователя; не требуй рендер для теории.
Самостоятельно предложенное тобой решение не доказывает понимание пользователем.
Сохраняй прежние важные решения в notes, дополняй подтверждёнными наблюдениями,
не выдумывай результаты. При изменении цели опирайся на явное пожелание пользователя.
События (этап вычисляет код, допускается одно событие на ответ):
- stay: уточнение, объяснение или ожидание реального результата; можно обновить поля.
- plan_ready: из planning в execution, когда цель понятна и plan содержит действия
  с критериями проверки. Одной рекомендации или заявления «план готов» недостаточно.
  Само предложение проверить материал НЕ результат проверки.
- result_reported: из execution в validation, только когда пользователь сообщил
  о фактически полученном результате; оцени его и уточни, удовлетворяет ли он цели.
- revise_plan: из execution в planning при необходимости пересмотреть цель/план.
- revise_work: из validation в execution, когда результат требует ещё одной попытки.
- finish: из validation в done, только когда пользователь подтвердил достижение цели
  или явно попросил закончить. Не завершай на основании собственной рекомендации.
evidence для каждого перехода — непустая точная цитата из ПОСЛЕДНЕГО сообщения пользователя
(до 1000 символов), которая объясняет событие. Копируй подстроку буквально: не добавляй
внутрь evidence кавычки «», пояснения или многоточие, которых нет в сообщении.
Для stay допустима пустая строка.
Если данных для перехода не хватает, выбери stay и задай содержательный вопрос.
Ты не выполняешь рендер и не управляешь редактором. Внешнее действие выполняет пользователь.
Согласуй answer с событием и новыми полями; не заявляй о завершении при stay.
Доступные сейчас события:
''' + json.dumps(EVENTS[state['stage']], ensure_ascii=False)
