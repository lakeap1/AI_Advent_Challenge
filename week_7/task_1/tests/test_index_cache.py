import importlib.util
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from graphics.config import Settings
from graphics.retrieval import Retriever


def load_indexer():
    spec = importlib.util.spec_from_file_location('index_corpus', Path(__file__).resolve().parents[1] / 'scripts/index_corpus.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CachedIndexer


class IndexCacheTests(unittest.TestCase):
    def test_restart_reuses_only_same_text_model_and_digest(self):
        indexer = load_indexer()
        vector = [1.0] + [0.0] * 1023
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(app_root=Path(folder), data_dir=Path(folder))
            with patch.object(Retriever, '_model_digest', return_value='digest-a'), \
                    patch.object(Retriever, '_embed', return_value=([vector], {'prompt_eval_count': 7})) as send:
                indexer(settings)._embed(['original'], query=False)
                second = indexer(settings)
                values, receipt = second._embed(['original', 'new text'], query=False)
                self.assertEqual(send.call_count, 2)
                self.assertEqual(send.call_args.args[0], ['new text'])
                self.assertEqual(len(values), 2)
                self.assertEqual(receipt['cache_hits'], 1)
                self.assertEqual(receipt['prompt_eval_count'], 7)
            with patch.object(Retriever, '_model_digest', return_value='digest-b'), \
                    patch.object(Retriever, '_embed', return_value=([vector], {'prompt_eval_count': 7})) as send:
                indexer(settings)._embed(['original'], query=False)
                self.assertEqual(send.call_count, 1)

    def test_invalid_vector_is_not_persisted(self):
        indexer = load_indexer()
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(app_root=Path(folder), data_dir=Path(folder))
            with patch.object(Retriever, '_model_digest', return_value='digest-a'), \
                    patch.object(Retriever, '_embed', return_value=([[0.0] * 1024], {'prompt_eval_count': 7})):
                with self.assertRaises(ValueError):
                    indexer(settings)._embed(['original'], query=False)

    def test_embedding_thread_setting_is_sent_to_local_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = replace(Settings(app_root=Path(folder), data_dir=Path(folder)), embedding_threads=4)
            with patch('graphics.retrieval.httpx.Client') as client:
                client.return_value.__enter__.return_value.post.return_value.json.return_value = {
                    'embeddings': [[1.0] + [0.0] * 1023], 'prompt_eval_count': 1}
                Retriever(settings)._embed(['source'], query=False)
                payload = client.return_value.__enter__.return_value.post.call_args.kwargs['json']
                self.assertEqual(payload['options']['num_thread'], 4)
