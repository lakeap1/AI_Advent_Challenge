"""24 theoretical graphics questions, real replies from the first turn in both modes."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from agent import Agent, SQLiteStore, load_config
from agent.tokens import accounting
from agent.transport import ResponsesTransport


TOPICS = [
    'Чем растровое представление изображения отличается от векторного и где каждое полезно в игровом арте?',
    'Что такое пиксель и разрешение изображения? Почему увеличение разрешения само по себе не восстанавливает потерянные детали?',
    'Чем ортографическая проекция отличается от перспективной? Как это влияет на оценку формы игрового ассета?',
    'Что такое aliasing и почему на диагональных линиях появляются ступеньки? Объясни принцип сглаживания.',
    'Что означают object space, world space и view space? Зачем графическому конвейеру преобразования координат?',
    'Какую роль играют матрицы перемещения, вращения и масштаба? Почему порядок преобразований имеет значение?',
    'Что такое нормаль поверхности? Чем геометрическая нормаль отличается от интерполированной нормали для освещения?',
    'Чем flat shading отличается от smooth shading? Почему сглаженное освещение не меняет силуэт низкополигональной модели?',
    'Что такое UV-развёртка и текстурные координаты? Почему возникает растяжение текстуры?',
    'Что такое texel density и как она связана с размером объекта и распределением UV-островов?',
    'Зачем нужны mipmaps? Объясни компромисс между памятью, детализацией и стабильностью изображения на расстоянии.',
    'Чем nearest, bilinear и trilinear filtering отличаются по принципу? Когда заметны различия?',
    'Для чего применяется anisotropic filtering? Почему поверхность под острым углом требует особой фильтрации?',
    'Чем normal map отличается от bump map и displacement? Что действительно может изменить геометрию и силуэт?',
    'Что означает PBR в контексте материалов? Чем отличаются роли base color, roughness и metallic?',
    'Зачем различать sRGB и линейное цветовое пространство? Почему расчёты освещения требуют внимания к этому различию?',
    'Что такое alpha blending и alpha testing? Как их различия влияют на края, прозрачность и порядок отрисовки?',
    'Для чего нужен depth buffer? Что такое z-fighting и какие общие причины его вызывают?',
    'Что такое back-face culling? Почему исчезновение обратной стороны полигона не обязательно означает удалённую геометрию?',
    'Что такое LOD и какой компромисс он решает? Почему количество треугольников — не единственная мера стоимости рендера?',
    'Что называют draw call и batching? Как материалы и организация объектов могут влиять на число вызовов отрисовки?',
    'Объясни разницу между baked lighting и динамическим освещением. Какие изменения сцены ограничивают использование запечённого света?',
    'Что такое ambient occlusion и чем он отличается от прямых теней? Почему AO не заменяет полноценное освещение?',
    'Чем растеризация отличается от трассировки лучей? Объясни основной принцип и один компромисс каждого подхода.',
]

UPDATES = {
    1: 'Контекст учебного проекта: сундук «Кедр-731», 1500 треугольников, текстура 512×512, бирюзовый акцент. Движок Godot. В готовом ассете прозрачность и логотипы запрещены. Экспорт chest_cedar_731.glb. Ревью 18 сентября; время не назначено. Сохраняй эти решения для следующих вопросов.',
    7: 'Исправление брифа: бюджет теперь 1200 треугольников, утверждённый акцент янтарный. Предложение красного акцента отклонено. Остальное без изменений.',
    13: 'Обновление согласованного имени экспорта: теперь cedar_lod0.glb. Старое имя больше не используем. Другие решения сохраняются.',
    19: 'Новое решение: текстура теперь 1024×1024. Бюджет геометрии не меняется. Время ревью по-прежнему не назначено.',
}

# Expected facts are never appended as answers or supplied to the model.
PROBES = {
    4: ('Исходный размер текстуры', 'Какие размеры текстуры сейчас утверждены? Поля width и height — целые числа.', {'width':512, 'height':512}),
    8: ('Исправленный бюджет', 'Какой бюджет треугольников сейчас утверждён? Поле triangles — целое число.', {'triangles':1200}),
    12: ('Исправленный акцент', 'Какой акцент сейчас утверждён? Поле accent — одно русское прилагательное.', {'accent':'янтарный'}),
    16: ('Новое имя экспорта', 'Какое имя экспортного файла сейчас согласовано? Поле filename — строка.', {'filename':'cedar_lod0.glb'}),
    20: ('Повторное изменение текстуры', 'Какие размеры текстуры сейчас утверждены? Поля width и height — целые числа.', {'width':1024, 'height':1024}),
    24: ('Давние ограничения и неизвестное время', 'Напомни движок и запреты для ассета, дату ревью и назначенное время. Поля engine — строка, transparency и logos — boolean допустимости, day и month — целые числа, time — строка либо null, если время не назначено.', {'engine':'Godot','transparency':False,'logos':False,'day':18,'month':9,'time':None}),
}


def scenario():
    turns=[]
    for number, topic in enumerate(TOPICS, 1):
        prompt=(UPDATES.get(number, '') + '\n' + topic + '\nОбъясни кратко, в 2–3 предложениях. Не повторяй бриф без необходимости.').strip()
        if number in PROBES:
            label, instruction, expected=PROBES[number]
            prompt += '\nТакже проверь память о нашем брифе: ' + instruction
            prompt += '\nОтветь только JSON-объектом с полем explanation (объяснение теории) и перечисленными полями памяти. Не добавляй другие ключи.'
        turns.append({'number':number, 'topic':topic, 'prompt':prompt})
    return turns


def check_reply(text, expected):
    from comparison import fact_check
    try: actual=json.loads(text)
    except (ValueError, TypeError): return False
    if not isinstance(actual,dict) or not isinstance(actual.get('explanation'),str) or not actual['explanation'].strip(): return False
    facts={k:v for k,v in actual.items() if k!='explanation'}
    return fact_check(json.dumps(facts,ensure_ascii=False),expected)


def run(output, *, agent_factory=None):
    from comparison import summarize
    config=load_config()
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    directory=Path(__file__).resolve().parent/'data'/('dialogue-24-'+stamp)
    directory.mkdir(parents=True)
    if agent_factory is None:
        load_dotenv(Path(__file__).with_name('.env'))
        if not os.getenv('OPENAI_API_KEY'): raise RuntimeError('Настройте OPENAI_API_KEY в .env.')
        def agent_factory(mode):
            return Agent(replace(config,context_mode=mode),ResponsesTransport(os.environ['OPENAI_API_KEY']),SQLiteStore(directory/(mode+'.sqlite3')))
    agents={mode:agent_factory(mode) for mode in ('full','compressed')}
    turns=scenario()
    report={'created_at':stamp,'experiment_type':'dialogue_from_empty','model':config.model,'config':asdict(config),
      'planned_user_turns':len(turns), 'scenario':turns, 'modes':{m:{'checks':[], 'turns':[]} for m in agents},
      'method':'Оба чата начинают с пустой истории. Одинаковые 24 вопроса по компьютерной графике, по 48 реплик при успешном завершении. Каждый ответ реально генерируется и сохраняется в истории своего режима; ответы могут различаться. Учтены все вызовы с первого вопроса, включая summary каждые 6 завершённых реплик пользователя/агента. Ничего не подставляется и не удаляется. Порядок режимов чередуется. Шесть проверок памяти включены в вопросы по теории; ожидаемые факты не отправляются как ответы. Один прогон; проверяется память, а не полнота или научная точность объяснений. Кеш не контролируется, USD — наблюдаемая стоимость по сохранённому тарифу.'}
    error=None
    try:
        for mode, agent in agents.items():
            if agent.state()['messages'] or agent.state()['requests']: raise RuntimeError('Эксперимент требует пустой истории: '+mode)
        for turn in turns:
            number=turn['number']
            for mode in (('full','compressed') if number%2 else ('compressed','full')):
                agent=agents[mode]
                before=agent.state()
                result=agent.run(turn['prompt'])
                state=agent.state()
                item=report['modes'][mode]
                item['turns'].append({**turn,'answer':result.text,'status':result.status,'request_id':result.request_id,
                    'history_messages_before':len(before['messages']), 'history_messages_after':len(state['messages']),
                    'summary_revision':state['compression']['revisions'], 'summary_tokens_estimate':state['compression']['summary_tokens_estimate'],
                    'cumulative_tokens':state['token_accounting'], 'cumulative_cost':state['summary']})
                if number in PROBES:
                    label,_,expected=PROBES[number]
                    item['checks'].append({'label':f'Вопрос {number}: {label}','prompt':turn['prompt'],
                        'expected':json.dumps(expected,ensure_ascii=False), 'answer':result.text,
                        'status':result.status,'passed':result.status=='ok' and check_reply(result.text,expected),
                        'history_messages':len(before['messages']),'request_id':result.request_id})
                print(f'{number}/24 {mode}: {result.status}, messages={len(state["messages"])}, summary={state["compression"]["revisions"]}, tokens={state["token_accounting"]["known_total_tokens"]}',flush=True)
                if result.status!='ok': raise RuntimeError('Прогон остановлен: '+mode+' '+result.code)
    except Exception as exc:
        error=exc
        report['error']=str(exc)
    finally:
        for mode, agent in agents.items():
            state=agent.state(); records=state['requests']; item=report['modes'][mode]
            item.update(totals=accounting(records), cost=summarize(records), requests=records,
                summary_totals=accounting([r for r in records if r['metadata'].get('kind')=='summary']),
                passed=sum(c['passed'] for c in item['checks']), final_messages=len(state['messages']),
                final_summary=state['compression']['summary'])
            agent.close()
        full,compressed=(report['modes'][m]['totals'] for m in ('full','compressed'))
        complete=error is None and all(len(m['turns'])==24 and m['final_messages']==48 and m['totals']['complete'] for m in report['modes'].values())
        saving=full['known_total_tokens']-compressed['known_total_tokens']
        percent=100*saving/full['known_total_tokens'] if complete and full['known_total_tokens'] else None
        report.update(complete=complete,saving_tokens=saving if complete else None,saving_percent=percent)
        report['conclusion']=(f'За весь диалог с первого вопроса со сжатием на {abs(saving):,} токенов '+('меньше' if saving>=0 else 'больше')+f' ({abs(percent):.1f}%). Все вызовы summary включены.' if percent is not None else 'Прогон неполный: итоговая экономия не определена.')
        output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
        text=json.dumps(report,ensure_ascii=False,indent=2)
        output.write_text(text,encoding='utf-8');(directory/'report.json').write_text(text,encoding='utf-8')
    if error is not None: raise error
    return report
