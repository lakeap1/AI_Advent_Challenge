"""The operating threshold delays small summaries while retaining long-history safety."""
import json
from pathlib import Path
import tempfile
import unittest

from graphics.config import Settings
from graphics.provider import GLMProvider
from graphics.service import ChatService
from graphics.store import Store
from tests.test_service import FakeMCP, FakeRetriever


class OperatingContextTests(unittest.TestCase):
    def test_oversized_summary_retains_cost_and_failed_output_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings(root, root / 'data', api_key='fixture-key')
            store = Store(settings.db_path)
            user = store.create_user('summary', 'long-fixture-password')
            branch = store.create_chat(user)['branch_id']
            for index in range(7):
                store.append_message(user, branch, 'user', f'Учебная заметка {index}')

            def transport(payload):
                return {'model': settings.model,
                        'choices': [{'message': {'content': json.dumps({'summary': 'x' * (settings.summary_budget + 1)})}}],
                        'usage': {'prompt_tokens': 80, 'completion_tokens': 20, 'total_tokens': 100,
                                  'prompt_tokens_details': {'cached_tokens': 0}}}

            result = ChatService(settings, store, GLMProvider(settings, store, transport)).compress(user, branch)
            self.assertEqual(result['status'], 'error')
            call, = store.ledger(user)['calls']
            self.assertEqual(call['output_policy'], {'ok': False, 'code': 'summary_budget_exceeded'})
            self.assertIsNotNone(call['cost_estimated_usd'])
            self.assertEqual(call['usage']['total_tokens'], 100)
            snapshot = store.get_branch(user, branch)
            self.assertEqual(snapshot['summary_through'], 0)
            self.assertEqual(len(snapshot['messages']), 7)

    def test_summary_starts_at_three_quarters_of_history_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings(root, root / 'data', api_key='fixture-key')
            store = Store(settings.db_path)
            user = store.create_user('threshold', 'long-fixture-password')
            for width, expect_summary in [(960, False), (1100, True)]:
                with self.subTest(message_width=width):
                    branch = store.create_chat(user)['branch_id']
                    for index in range(7):
                        store.append_message(user, branch, 'user', str(index) + 'x' * (width - 1))
                    outputs = ([{'summary': 'Сохранены учебные заметки.'}] if expect_summary else []) + [
                        {'query': 'шейдер', 'working_state': {}, 'needs_clarification': False,
                         'clarification': '', 'answer_kind': 'explanation'},
                        {'keep': ['S1'], 'reason': 'соответствует вопросу'},
                        {'answer': 'Шейдер задаёт расчёт вида поверхности.', 'source_ids': ['S1']},
                        {'accepted': True, 'reason': 'подтверждено', 'used_source_ids': ['S1']}]
                    stages = []

                    def transport(payload):
                        stages.append(json.loads(payload['messages'][-1]['content'])['stage'])
                        return {'model': settings.model, 'choices': [{'message': {'content': json.dumps(outputs.pop(0))}}],
                                'usage': {'prompt_tokens': 80, 'completion_tokens': 20, 'total_tokens': 100,
                                          'prompt_tokens_details': {'cached_tokens': 0}}}

                    service = ChatService(settings, store, GLMProvider(settings, store, transport), FakeRetriever(), FakeMCP())
                    result = service.ask(user, branch, 'Что такое шейдер?')
                    self.assertEqual(result['status'], 'accepted')
                    self.assertEqual('summary' in stages, expect_summary)
                    self.assertEqual(len(store.get_branch(user, branch)['messages']), 9)
                    self.assertEqual(bool(store.get_branch(user, branch)['summary_through']), expect_summary)
                    self.assertFalse(outputs)


if __name__ == '__main__':
    unittest.main()
