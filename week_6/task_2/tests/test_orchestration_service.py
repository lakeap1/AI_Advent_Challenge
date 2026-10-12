"""Server-owned state rejects foreign IDs and verifies exact saved bytes."""
import asyncio
from pathlib import Path

import pytest

from orchestration.service import OrchestrationService, OrchestrationError
from orchestration.store import OrchestrationStore


class Providers:
    async def lookup_wikipedia(self, query, language, limit):
        return {'sources': [{'title':'Normal mapping','url':'https://en.wikipedia.org/wiki/Normal_mapping','excerpt':'Surface normals in textures.'}]}

    async def search_stackexchange(self, query, community, limit):
        return {'sources': [{'title':'Seam answer','url':'https://blender.stackexchange.com/a/42','excerpt':'Check tangent space.'}]}


def test_dependencies_and_exact_readback(tmp_path):
    store = OrchestrationStore(tmp_path)
    run = store.create(1, 2, 3, 'Normal mapping', 9)
    service = OrchestrationService(store, run['id'], providers=Providers())
    with pytest.raises(OrchestrationError):
        service.save_report('foreign')
    wiki = asyncio.run(service.lookup_wikipedia('normal mapping','en'))
    se = asyncio.run(service.search_stackexchange('normal map seams','blender'))
    with pytest.raises(OrchestrationError):
        service.compare_sources([wiki['material_id'], 'foreign'])
    compared = service.compare_sources([se['material_id'], wiki['material_id']])
    assert compared['material_ids'] == [se['material_id'], wiki['material_id']]
    report = service.prepare_report('Check tangent space.', [se['material_id'], wiki['material_id']])
    saved = service.save_report(report['report_id'])
    assert service.read_report(report['report_id']) == saved
    target = store.results / saved['filename']
    original = target.read_bytes()
    target.write_bytes(original.replace(b'Check', b'Checx'))
    with pytest.raises(OrchestrationError):
        service.read_report(report['report_id'])


def test_foreign_run_ids_are_rejected(tmp_path):
    store = OrchestrationStore(tmp_path)
    first = store.create(1, 2, 3, 'A', 9)
    second = store.create(1, 3, 4, 'B', 10)
    one = OrchestrationService(store, first['id'], providers=Providers())
    two = OrchestrationService(store, second['id'], providers=Providers())
    wiki = asyncio.run(one.lookup_wikipedia('normal mapping','en'))
    se = asyncio.run(one.search_stackexchange('normal map seam','blender'))
    with pytest.raises(OrchestrationError):
        two.compare_sources([wiki['material_id'], se['material_id']])
