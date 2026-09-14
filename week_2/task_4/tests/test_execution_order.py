from test_compression import build

def test_summary_precedes_dependent_answer_in_display_order():
    agent,_=build()
    for prompt in ('Бриф','UV','Текстура'): agent.run(prompt)
    records=agent.state()['requests']
    assert [r['metadata']['kind'] for r in records]==['answer','answer','summary','answer']
    assert records[-2]['metadata']['parent_request_id']==records[-1]['id']
