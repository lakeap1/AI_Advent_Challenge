"""Real MCP server with controlled upstream services, used only by wire tests."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from composition.server import create_server
from composition.service import CompositionService
from composition.store import CompositionStore
from test_composition import Model, Sources

store = CompositionStore(os.environ['TEST_COMPOSITION_DIR'])


class ObservedService(CompositionService):
    def note(self, step, arguments):
        with (store.root / 'wire-observations.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(step=step, arguments=arguments), ensure_ascii=False) + '\n')

    async def search(self, run_id, question, query, source='blender'):
        self.note('search', dict(run_id=run_id, question=question, query=query, source=source))
        return await super().search(run_id, question, query, source)

    def summarize(self, materials):
        self.note('summarize', materials.model_dump())
        return super().summarize(materials)

    def save(self, summary):
        self.note('save', summary.model_dump())
        return super().save(summary)


class RecordedModel(Model):
    def create(self, payload, timeout):
        (store.root/'summary-payload.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        return super().create(payload, timeout)


create_server(ObservedService(store, providers=Sources(), transport=RecordedModel(refusal=os.environ.get('TEST_REFUSAL') == '1'))).run('stdio')
