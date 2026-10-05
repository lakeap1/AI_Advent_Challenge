"""Official source metadata must propagate without breaking legacy manifests."""
import json

import pytest

from indexing.corpus import CorpusError, read_corpus
from indexing.chunking import chunk_document


def test_manifest_official_url_and_title_propagate_to_chunks(tmp_path):
    (tmp_path / 'corpus').mkdir()
    (tmp_path / 'manual.md').write_text('# UV\nImage and node coordinates must match.', encoding='utf-8')
    (tmp_path / 'corpus' / 'manifest.json').write_text(json.dumps({'version': 1, 'files': [
        {'file': 'manual.md', 'source': 'https://docs.blender.org/manual/en/4.4/test.html',
         'title': 'Blender Manual 4.4 — UV'}]}), encoding='utf-8')
    corpus = read_corpus(tmp_path)
    document = corpus.documents[0]
    assert document.source == 'https://docs.blender.org/manual/en/4.4/test.html'
    assert document.title == 'Blender Manual 4.4 — UV'
    chunk = chunk_document(document, 'structural')[0]
    assert (chunk['source'], chunk['title'], chunk['section'], chunk['text'].splitlines()) == (
        document.source, document.title, ['UV'], ['# UV', 'Image and node coordinates must match.'])
    assert corpus.summary['files'][0]['source'] == document.source


def test_legacy_manifest_and_invalid_source_metadata(tmp_path):
    (tmp_path / 'corpus').mkdir()
    (tmp_path / 'manual.md').write_text('Safe text', encoding='utf-8')
    manifest = tmp_path / 'corpus' / 'manifest.json'
    manifest.write_text(json.dumps({'version': 1, 'files': ['manual.md']}), encoding='utf-8')
    assert read_corpus(tmp_path).documents[0].source == 'manual.md'
    for source in ('javascript:alert(1)', 'file:///etc/passwd', '', 23,
                   'https://[invalid/', 'https://docs.blender.org/\nunsafe'):
        manifest.write_text(json.dumps({'version': 1, 'files': [
            {'file': 'manual.md', 'source': source, 'title': 'Manual'}]}), encoding='utf-8')
        with pytest.raises(CorpusError):
            read_corpus(tmp_path)
