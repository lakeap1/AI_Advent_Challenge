"""Observable pipeline contracts; external HTTP/LLM responses are controlled here."""
import asyncio
import hashlib
import json

import pytest

from composition.contracts import SearchInput
from composition.service import CompositionService, CompositionError
from composition.store import CompositionStore


class Sources:
    async def search_stackexchange(self, query, community, limit):
        return dict(provider='stackexchange', query=query, sources=[dict(
            title='Actual source A', url=f'https://{community}.stackexchange.com/a/42',
            excerpt='Unique upstream fact: use Non-Color for a normal texture.',
            author='Artist', date='2024-01-01')], metadata={})

    async def lookup_wikipedia(self, query, language, limit):
        return dict(provider='wikipedia', query=query, sources=[dict(
            title='Normal mapping', url=f'https://{language}.wikipedia.org/wiki/Normal_mapping',
            excerpt='Encyclopedia fact: normal mapping simulates surface detail without more geometry.')],
            metadata=dict(language=language))


class Model:
    def __init__(self, refusal=False):
        self.payloads = []
        self.refusal = refusal

    def create(self, payload, timeout):
        self.payloads.append(payload)
        content = ([dict(type='refusal', refusal='No')] if self.refusal else
                   [dict(type='output_text', text='Проверьте Non-Color у карты нормалей. Сравните шов до и после изменения.')])
        return dict(status='completed', model='gpt-5.6-luna', service_tier='default',
                    output=[dict(type='message', role='assistant', status='completed', content=content)],
                    usage=dict(input_tokens=100, output_tokens=20, total_tokens=120,
                               input_tokens_details=dict(cached_tokens=0, cache_write_tokens=0),
                               output_tokens_details=dict(reasoning_tokens=0)))


def setup(tmp_path, model=None):
    store = CompositionStore(tmp_path)
    run = store.create(1, 2, 'Как проверить швы карты нормалей?', 'normal map seams')
    service = CompositionService(store, providers=Sources(), transport=model or Model())
    return store, run, service


def test_processed_actual_sources_saved_exactly_without_overwrite(tmp_path):
    model = Model()
    store, run, service = setup(tmp_path, model)
    materials = asyncio.run(service.search(run['id'], run['question'], run['query']))
    summary = service.summarize(materials)
    assert 'Unique upstream fact' in json.dumps(model.payloads[0], ensure_ascii=False)
    assert 'без Markdown' in model.payloads[0]['instructions']
    saved = service.save(summary)
    content = (store.results / saved.filename).read_bytes()
    assert content.decode('utf-8') == summary.content == saved.content
    assert hashlib.sha256(content).hexdigest() == saved.sha256
    assert 'https://blender.stackexchange.com/a/42' in saved.content
    assert store.calls(1, 2)[0]['usage']['total_tokens'] == 120
    with pytest.raises(CompositionError):
        service.save(summary)
    assert (store.results / saved.filename).read_bytes() == content
    with pytest.raises(CompositionError):
        service.summarize(materials)
    assert len(model.payloads) == 1


def test_refused_output_keeps_paid_usage_but_cannot_save(tmp_path):
    store, run, service = setup(tmp_path, Model(refusal=True))
    materials = asyncio.run(service.search(run['id'], run['question'], run['query']))
    with pytest.raises(CompositionError):
        service.summarize(materials)
    call = store.calls(1, 2)[0]
    assert call['output_policy']['status'] == 'rejected'
    assert call['usage']['total_tokens'] == 120
    assert call['cost_usd'] is not None
    assert store.get(run['id'])['summary'] is None
    assert not list(store.results.glob('*.txt'))


def test_tampered_materials_and_summary_never_reach_model_or_disk(tmp_path):
    model = Model()
    store, run, service = setup(tmp_path, model)
    materials = asyncio.run(service.search(run['id'], run['question'], run['query']))
    altered = materials.model_copy(deep=True)
    altered.sources[0].excerpt = 'Injected replacement'
    with pytest.raises(CompositionError):
        service.summarize(altered)
    assert not model.payloads
    summary = service.summarize(materials)
    forged = summary.model_copy(update={'content': 'replacement'})
    with pytest.raises(CompositionError):
        service.save(forged)
    assert not list(store.results.glob('*.txt'))


@pytest.mark.parametrize('question,query', [('', 'normal'), ('text', ''), ('x'*2001, 'normal'), ('text', 'a\nB')])
def test_invalid_inputs_are_rejected(question, query):
    with pytest.raises(ValueError):
        SearchInput(question=question, query=query)


def test_identifier_cannot_be_a_path(tmp_path):
    store, run, service = setup(tmp_path)
    with pytest.raises((ValueError, CompositionError)):
        asyncio.run(service.search('../escape', run['question'], run['query']))
    assert not list(tmp_path.rglob('*.txt'))


def test_timeout_cost_is_unknown_and_scoped_to_original_dialogue(tmp_path):
    from agent.transport import TransportError
    class Timeout:
        def create(self, payload, timeout):
            raise TransportError('timeout', request_started=True)
    store, run, service = setup(tmp_path, Timeout())
    materials = asyncio.run(service.search(run['id'], run['question'], run['query']))
    with pytest.raises(CompositionError):
        service.summarize(materials)
    record = store.calls(1, 2)[0]
    assert record['usage_status'] == 'unavailable'
    assert record['usage'] is None and record['cost_usd'] is None
    state = dict(personalization=dict(selected_id=1),workspace=dict(active_dialogue=dict(id=2)),
                 summary=dict(known_cost_usd='0.5',unknown_cost_requests=0,cost_complete=True,api_requests=1))
    merged = store.augment(state)
    assert merged['summary'] == dict(known_cost_usd='0.5',unknown_cost_requests=1,cost_complete=False,api_requests=2)
    state['personalization']['selected_id'] = 2
    assert store.augment(state)['summary']['api_requests'] == 1
    assert not store.calls(1, 3)
    store.recover()
    assert store.get(run['id'])['status'] == 'interrupted'
    assert store.calls(1, 2)[0] == record


def test_empty_sources_stop_before_model(tmp_path):
    class Empty:
        async def search_stackexchange(self, *args):
            return dict(sources=[])
    model = Model()
    store, run, _ = setup(tmp_path)
    service = CompositionService(store, providers=Empty(), transport=model)
    with pytest.raises(CompositionError):
        asyncio.run(service.search(run['id'], run['question'], run['query']))
    assert not model.payloads and not store.calls(1, 2)
    assert store.get(run['id'])['materials'] is None


@pytest.mark.parametrize('source,url', [
    ('wikipedia_en','https://blender.stackexchange.com/a/42'),
    ('wikipedia_ru','https://en.wikipedia.org/wiki/Normal_mapping'),
    ('gamedev','https://computergraphics.stackexchange.com/a/42'),
    ('blender','https://blender.stackexchange.com.evil.example/a/42'),
    ('wikipedia_en','https://en.wikipedia.org/w/api.php'),
])
def test_wrong_source_or_unsafe_url_stops_before_processing(tmp_path, source, url):
    class WrongSource(Sources):
        async def search_stackexchange(self, *args):
            return dict(sources=[dict(title='Wrong',url=url,excerpt='Untrusted material')])
        lookup_wikipedia = search_stackexchange
    store = CompositionStore(tmp_path)
    run = store.create(1, 2, 'Разбор', 'normal mapping', source=source)
    model = Model()
    service = CompositionService(store, providers=WrongSource(), transport=model)
    with pytest.raises(ValueError):
        asyncio.run(service.search(run['id'], run['question'], run['query'], source))
    assert not model.payloads and not list(store.results.glob('*.txt'))
    assert store.get(run['id'])['materials'] is None


def test_search_cannot_change_source_selected_for_run(tmp_path):
    store = CompositionStore(tmp_path)
    run = store.create(1, 2, 'Разбор', 'normal mapping', source='wikipedia_en')
    model = Model()
    service = CompositionService(store, providers=Sources(), transport=model)
    with pytest.raises(CompositionError):
        asyncio.run(service.search(run['id'], run['question'], run['query'], 'blender'))
    assert not model.payloads and store.get(run['id'])['materials'] is None


@pytest.mark.parametrize('usable', [False, True])
def test_wikipedia_empty_extracts_do_not_hide_usable_materials(tmp_path, usable):
    class Extracts(Sources):
        async def lookup_wikipedia(self, *args):
            rows=[dict(title='Empty article',url='https://en.wikipedia.org/wiki/Empty',excerpt='  ')]
            if usable:
                rows.append(dict(title='Ambient occlusion',url='https://en.wikipedia.org/wiki/Ambient_occlusion',
                                 excerpt='Usable encyclopedia extract about occlusion.'))
            return dict(provider='wikipedia',sources=rows)
    store=CompositionStore(tmp_path)
    run=store.create(1,2,'Обзор ambient occlusion','ambient occlusion',source='wikipedia_en')
    model=Model()
    service=CompositionService(store,providers=Extracts(),transport=model)
    if usable:
        materials=asyncio.run(service.search(run['id'],run['question'],run['query'],'wikipedia_en'))
        assert [s.title for s in materials.sources] == ['Ambient occlusion']
        result=service.save(service.summarize(materials))
        assert 'https://en.wikipedia.org/wiki/Ambient_occlusion' in result.content
        assert 'Empty article' not in result.content
    else:
        with pytest.raises(CompositionError):
            asyncio.run(service.search(run['id'],run['question'],run['query'],'wikipedia_en'))
        assert not model.payloads and not list(store.results.glob('*.txt'))


def test_legacy_database_retains_saved_result_after_source_migration(tmp_path):
    import sqlite3
    store, run, service = setup(tmp_path)
    materials = asyncio.run(service.search(run['id'], run['question'], run['query']))
    saved = service.save(service.summarize(materials))
    store.event(run['id'], 'save', 'success')
    # Reproduce the prior schema/materials; the accepted artifact bytes stay on disk.
    legacy_materials = materials.model_dump(exclude={'source'})
    with sqlite3.connect(store.path) as db:
        db.execute('ALTER TABLE runs DROP COLUMN source')
        db.execute('UPDATE runs SET materials=?', (json.dumps(legacy_materials),))
    reopened = CompositionStore(tmp_path)
    restored = reopened.get(run['id'])
    assert restored['source'] == 'blender'
    assert restored['materials'] == legacy_materials
    assert restored['status'] == 'success' and restored['saved']['content'] == saved.content
    assert (reopened.results/saved.filename).read_text(encoding='utf-8') == saved.content
    assert len(reopened.calls(1, 2)) == 1


def test_rejected_input_is_logged_without_llm(tmp_path):
    from agent.composition import CompositionAgent
    store = CompositionStore(tmp_path)
    result = CompositionAgent(store).create(1, 2, '', 'normal map')
    assert result['status'] == 'rejected' and result['stage'] == 'input'
    assert not store.calls(1, 2)


def test_concurrent_duplicate_and_safe_result_directory(tmp_path):
    store, run, _ = setup(tmp_path)
    with pytest.raises(ValueError, match='уже выполняется'):
        store.create(1, 2, 'Другой вопрос', 'other')
    second = store.create(1, 3, 'Другой диалог', 'other')
    assert second['id'] != run['id']
    assert store.results.resolve().is_relative_to(tmp_path.resolve())


def test_combined_accounting_adds_known_cost_and_ignores_not_requested(tmp_path):
    store, run, service = setup(tmp_path)
    materials = asyncio.run(service.search(run['id'], run['question'], run['query']))
    service.summarize(materials)
    store.event(run['id'], 'save', 'error', 'Test: disk failure after paid summary')
    from agent.transport import TransportError
    class NoKey:
        def create(self, payload, timeout):
            raise TransportError('not_configured', request_started=False)
    second = store.create(1, 2, run['question'], run['query'])
    other = CompositionService(store, providers=Sources(), transport=NoKey())
    inputs = asyncio.run(other.search(second['id'], second['question'], second['query']))
    with pytest.raises(CompositionError):
        other.summarize(inputs)
    state = dict(personalization=dict(selected_id=1),workspace=dict(active_dialogue=dict(id=2)),
                 summary=dict(known_cost_usd='0.1',unknown_cost_requests=0,cost_complete=True,api_requests=1))
    merged = store.augment(state)
    assert merged['summary'] == dict(known_cost_usd='0.100044',unknown_cost_requests=0,cost_complete=True,api_requests=2)
    assert len(merged['composition_calls']) == 2
