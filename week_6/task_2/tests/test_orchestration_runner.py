"""Observable selection and dependency contract for the model-controlled flow."""
import asyncio
import json

import pytest

from orchestration.runner import OrchestrationError, execute_flow


class Router:
    def __init__(self):
        self.calls = []

    async def discover(self):
        return [dict(type='function', name='research__lookup_wikipedia', parameters={'type': 'object'}),
                dict(type='function', name='research__search_stackexchange', parameters={'type': 'object'}),
                dict(type='function', name='processing__compare_sources', parameters={'type': 'object'}),
                dict(type='function', name='processing__prepare_report', parameters={'type': 'object'}),
                dict(type='function', name='library__save_report', parameters={'type': 'object'}),
                dict(type='function', name='library__read_report', parameters={'type': 'object'})]

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == 'research__lookup_wikipedia':
            return dict(material_id='wiki-opaque', provider='wikipedia', sources=[dict(title='Normal mapping', url='https://en.wikipedia.org/wiki/Normal_mapping', excerpt='Normals in textures')])
        if name == 'research__search_stackexchange':
            return dict(material_id='se-opaque', provider='stackexchange', sources=[dict(title='Seams', url='https://blender.stackexchange.com/a/1', excerpt='Inspect tangent basis')])
        if name == 'processing__compare_sources':
            return dict(comparison_id='comparison-opaque', material_ids=arguments['material_ids'])
        if name == 'processing__prepare_report':
            return dict(report_id='report-opaque', material_ids=arguments['material_ids'], content='Inspect tangent basis.\nhttps://en.wikipedia.org/wiki/Normal_mapping\nhttps://blender.stackexchange.com/a/1')
        if name == 'library__save_report':
            return dict(report_id='report-opaque', content='Inspect tangent basis.\nhttps://en.wikipedia.org/wiki/Normal_mapping\nhttps://blender.stackexchange.com/a/1', sha256='58a42f6702bfaf18a5801ee15e650611fb6f9e19b3ecea4777ab75df85438b7b', bytes_written=105, filename='report.txt')
        return dict(report_id='report-opaque', content='Inspect tangent basis.\nhttps://en.wikipedia.org/wiki/Normal_mapping\nhttps://blender.stackexchange.com/a/1', sha256='58a42f6702bfaf18a5801ee15e650611fb6f9e19b3ecea4777ab75df85438b7b', bytes_written=105, filename='report.txt')


def response(name=None, arguments=None, call_id=None):
    output = ([dict(type='function_call', name=name, arguments=json.dumps(arguments), call_id=call_id, status='completed')]
              if name else [dict(type='message', role='assistant', status='completed', content=[dict(type='output_text', text='Готово.')])])
    return dict(status='completed', output=output)


def test_dynamic_six_calls_use_previous_results_and_verify_readback():
    router = Router()
    decisions = iter([
        response('research__search_stackexchange', dict(query='normal map seams', community='blender'), 'c1'),
        response('research__lookup_wikipedia', dict(query='normal mapping', language='en'), 'c2'),
        response('processing__compare_sources', dict(material_ids=['se-opaque', 'wiki-opaque']), 'c3'),
        response('processing__prepare_report', dict(text='Проверить tangent basis.', material_ids=['se-opaque', 'wiki-opaque']), 'c4'),
        response('library__save_report', dict(report_id='report-opaque'), 'c5'),
        response('library__read_report', dict(report_id='report-opaque'), 'c6'),
        response(),
    ])
    inputs = []
    async def choose(messages, tools, tool_choice):
        inputs.append((list(messages), tool_choice))
        return next(decisions)
    result = asyncio.run(execute_flow(router, choose, 'Швы normal map', before_save=lambda text: None))
    assert result['verified'] is True
    assert len(router.calls) == 6
    assert inputs[1][0][-1]['call_id'] == 'c1'
    assert inputs[2][0][-1]['call_id'] == 'c2'
    assert [e['qualified_name'] for e in result['events'] if e['type'] == 'call'] == [x[0] for x in router.calls]


@pytest.mark.parametrize('decisions', [
    [response('library__save_report', dict(report_id='foreign'), 'c1')],
    [response()],
    [response('research__lookup_wikipedia', dict(query='normal mapping', language='en'), 'c1'),
     response('research__lookup_wikipedia', dict(query='again', language='en'), 'c1')],
])
def test_fail_closed_before_side_effects(decisions):
    router = Router()
    async def choose(messages, tools, tool_choice):
        return decisions.pop(0)
    with pytest.raises(OrchestrationError):
        asyncio.run(execute_flow(router, choose, 'Question'))
    assert not any(name.startswith('library__') for name, _ in router.calls)


def test_persisted_callback_observes_final_call_status():
    router = Router()
    responses = iter([response('research__lookup_wikipedia',dict(query='normal mapping',language='en'),'c1'), response()])
    observed = []
    async def choose(messages,tools,tool_choice):
        return next(responses)
    with pytest.raises(OrchestrationError):
        asyncio.run(execute_flow(router,choose,'Question',on_event=lambda event: observed.append(dict(event))))
    calls = [event for event in observed if event['type']=='call']
    assert calls[-1]['status']=='success'
