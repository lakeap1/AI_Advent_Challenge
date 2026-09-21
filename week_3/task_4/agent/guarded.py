"""Цикл с проверкой инвариантов до записи памяти и публикации ответа."""
from dataclasses import replace
import json

from .invariants import parse_checks, conflict_text
from .task_protocol import parse_task_response
from .task_validation import needs_validation, gate
from .task_state import StageNotReady
from .storage import StorageError


def check(agent, phase, text, snapshot, parent_id, context):
    from .core import AgentResult
    ip = dict(name=agent._config.input_policy, status='accepted')
    op = dict(name='completed_text_and_invariant_checks', status='not_checked')
    meta = {**agent._metadata(), 'kind': f'invariant_{phase}', 'parent_request_id': parent_id,
            'invariants': snapshot, 'context': {'task_state': context['task_state'],
                                              'sent_messages': len(context['messages'])}}
    data = json.dumps(dict(phase=phase, rules=snapshot['rules'], context=context, text=text), ensure_ascii=False)
    valid = agent._input_policy(data, agent._config)
    if isinstance(valid, AgentResult):
        result = replace(valid, input_policy={**ip, 'status': 'rejected'}, output_policy=op)
        rid = agent._store.begin(agent._record(result, meta))
        return replace(result, request_id=rid)
    pending = AgentResult('error', 'Проверяются правила задачи.', usage_status='unavailable', input_policy=ip, output_policy=op)
    record = agent._record(pending, meta); record['status'] = 'pending'
    rid = agent._store.begin(record)
    payload = {**agent._payload([dict(role='user', content=data)]),
               'instructions': agent._config.invariant_check_instructions,
               'max_output_tokens': agent._config.invariant_check_output_tokens}
    result = replace(agent._invoke(payload, meta, ip, op), request_id=rid)
    agent._store.pending_result(rid, agent._record(result, meta))
    if result.status == 'ok':
        try:
            checks = parse_checks(result.text, snapshot)
            meta['invariant_check'] = dict(phase=phase, checks=checks)
            if any(item['violated'] for item in checks):
                result = replace(result, status='rejected', text=conflict_text(snapshot, checks, phase),
                                 code=f'invariant_{phase}_conflict', output_policy={**op, 'status': 'rejected'})
            else:
                result = replace(result, text='Конфликтов по результату модельной проверки не найдено.')
        except (ValueError, TypeError, RecursionError):
            result = replace(result, status='rejected', text='Проверка правил вернула неполный или неверный результат. Ответ не принят.',
                             code='invalid_invariant_check', output_policy={**op, 'status': 'rejected'})
    agent._store.finish(rid, agent._record(result, meta))
    return result


def run_guarded(agent, text, use_working, use_long_term):
    from .core import AgentResult
    snapshot = agent._invariants()
    has_rules = bool(snapshot and snapshot['rules'])
    ip = dict(name=agent._config.input_policy, status='accepted')
    op = dict(name='completed_text_task_schema_and_invariants' if has_rules else 'completed_text_and_task_schema', status='not_checked')
    history = agent._store.state()['messages']
    context_messages = agent._context_messages(history + [dict(role='user', content=text)], use_working, use_long_term)
    profile = agent._profile()
    context = dict(task_state=agent._task_state(), task_id=agent._task_id,
                   dialogue_id=agent._dialogue_id, invariants=snapshot,
                   profile_id=getattr(agent, 'profile_id', None), profile_refs=profile['refs'] if profile else [],
                   sent_message_ids=[m['id'] for m in agent._selected(history)],
                   sent_messages=len(context_messages), facts_revision=agent._store.memory()['revisions'],
                   selection=dict(working=use_working, long_term=use_long_term),
                   memory_refs=[dict(id=e['id'], revision=e['revision']) for entries in agent._layers(use_working, use_long_term).values() for e in entries])
    meta = {**agent._metadata(), 'kind': 'answer', 'mode': agent._config.context_mode,
            'branch_id': agent._store.memory()['active_branch'], 'context': context,
            'token_metrics': agent.preview(text, use_working=use_working, use_long_term=use_long_term)['token_metrics']}
    # Parent is not a provider call until input checking passes.
    pending = AgentResult('error', 'Проверяется запрос.', input_policy=ip, output_policy=op)
    record = agent._record(pending, meta); record['status'] = 'pending'
    rid = agent._store.begin(record, text)
    guard_context = dict(task_state=context['task_state'], messages=context_messages)
    if has_rules:
        incoming = check(agent, 'input', text, snapshot, rid, guard_context)
        meta['invariant_input'] = dict(request_id=incoming.request_id, status=incoming.status, code=incoming.code)
        if incoming.status != 'ok':
            result = AgentResult(incoming.status, incoming.text, incoming.code, request_id=rid,
                                 input_policy=dict(name='invariant_input', status='rejected'), output_policy=op)
            agent._store.finish(rid, agent._record(result, meta))
            return result
    # Persist pending usage before the network call, for interrupted-run accounting.
    agent._store.mark_requested(rid)
    agent._store.pending_metadata(rid, meta)
    payload = {**agent._payload(context_messages), 'text': {'format': {'type': 'json_object'}}}
    result = replace(agent._invoke(payload, meta, ip, op), request_id=rid)
    agent._store.pending_result(rid, agent._record(result, meta))
    update = None
    if result.status == 'ok':
        try:
            update = parse_task_response(result.text, context['task_state'], text)
        except (ValueError, TypeError, RecursionError):
            result = replace(result, status='rejected', code='invalid_task_response',
                text='Ответ не прошёл проверку события, плана и полей задачи. Состояние и память не изменены; расход вызова учтён.',
                output_policy={**op, 'status': 'rejected'})
    receipt = None
    if result.status == 'ok' and needs_validation(context['task_state'], update):
        checked, receipt = agent._validate_task(context['task_state'], update, text, rid)
        meta['validation_request_id'] = checked.request_id
        if checked.status != 'ok':
            meta['validation_error'] = checked.code
            result = replace(result, status=checked.status, code='validation_failed',
                text=checked.text + ' Состояние и память не изменены.',
                output_policy={**op, 'status': 'rejected'})
    if result.status == 'ok' and has_rules:
        # Check the proposed state as well as the visible answer before any writes.
        candidate = {**update, 'validation': receipt['report']} if receipt else update
        outgoing = check(agent, 'output', json.dumps(candidate, ensure_ascii=False), snapshot, rid, guard_context)
        meta['invariant_output'] = dict(request_id=outgoing.request_id, status=outgoing.status, code=outgoing.code)
        if outgoing.status != 'ok':
            result = replace(result, status=outgoing.status, text=outgoing.text, code=outgoing.code,
                             output_policy={**op, 'status': 'rejected'})
    if result.status == 'ok' and receipt and not gate(context['task_state'], update, receipt['report'])[0]:
        try:
            # A valid partial report may persist, but only after both invariant
            # gates. No extraction or accepted dialogue answer on this path.
            agent._memory_store.apply_event(agent._task_id, update, context['task_state'],
                                           prompt=text, receipt=receipt)
        except StageNotReady as exc:
            meta['state_update'] = dict(event=update['event'], before=context['task_state'], after=exc.state)
            result = replace(result, status='rejected', code='stage_not_ready',
                text='Переход этапа не выполнен. ' + str(exc), output_policy={**op, 'status': 'rejected'})
        except ValueError:
            result = replace(result, status='rejected', code='invalid_task_update',
                text='Состояние изменилось во время проверки. Обновите страницу.',
                output_policy={**op, 'status': 'rejected'})
        except StorageError:
            result = replace(result, status='error', code='task_storage',
                text='Не удалось сохранить результаты проверки. Расход вызовов учтён.')
    staged = {}
    if result.status == 'ok':
        extracted = agent._extract(text, rid, staged=staged)
        if extracted.status != 'ok':
            result = replace(result, status='error', text=extracted.text + ' Ответ не принят.',
                             code='extraction_failed', output_policy={**op, 'status': 'rejected'})
        elif agent._config.context_mode == 'facts':
            updated = agent._update_facts(text, rid, staged=staged)
            if updated.status != 'ok':
                result = replace(result, status='error', text=updated.text + ' Ответ не принят.',
                                 code='facts_failed', output_policy={**op, 'status': 'rejected'})
    if result.status == 'ok':
        try:
            # State and extracted memory share one transaction. Facts and the
            # accepted message are recorded only after every validation passes.
            with agent._memory_store.transaction():
                after = agent._memory_store.apply_event(agent._task_id, update, context['task_state'],
                                                       prompt=text, receipt=receipt)
                changed, skipped = agent._memory_store.apply_operations(agent._task_id, staged['operations'],
                    f'dialogue:{agent._dialogue_id}/request:{rid}')
                extraction_id, extraction_meta = staged['extraction']
                extraction_meta['changed_refs'] = [dict(id=e['id'], revision=e['revision']) for e in changed]
                extraction_meta['skipped_locked'] = skipped
                agent._store.extraction_committed(extraction_id, extraction_meta)
                meta['state_update'] = dict(event=update['event'], evidence=update['evidence'],
                                           before=context['task_state'], after=after)
                result = replace(result, text=update['answer'])
                agent._store.finish(rid, agent._record(result, meta), result.text, memory=staged.get('facts'))
            return result
        except ValueError:
            result = replace(result, status='rejected', code='invalid_task_update',
                text='Не удалось принять изменение состояния или памяти. Предложенные изменения отменены; расход вызовов учтён.',
                output_policy={**op, 'status': 'rejected'})
        except StorageError:
            result = replace(result, status='error', code='task_storage',
                text='Не удалось сохранить состояние задачи. Расход вызова учтён.')
    agent._store.finish(rid, agent._record(result, meta), result.text if result.status == 'ok' else None)
    return result
