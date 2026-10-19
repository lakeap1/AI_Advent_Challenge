"""The real query embedding boundary must participate in the common ledger."""
from pathlib import Path
import tempfile
import unittest

from graphics.config import Settings
from graphics.store import Store
from graphics.provider import GLMProvider, ProviderError
from graphics.retrieval import Retriever


class EmbeddingReceiptTests(unittest.TestCase):
    def test_embedding_is_admitted_at_sources_and_marked_before_query_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings(root, root / "data", api_key="fixture-key")
            store = Store(settings.db_path)
            user = store.create_user("tester", "long-fixture-password")
            branch = store.create_chat(user)["branch_id"]
            turn = store.begin_turn(user, branch, "What is a shader?")["id"]
            store.move_turn(turn, "input_check")
            store.move_turn(turn, "context")
            store.move_turn(turn, "sources")
            call = store.start_call(user, branch, turn, "embedding", settings.embedding_model)
            retriever = Retriever(settings)
            vector = [1.0] + [0.0] * 1023
            retriever._read_index = lambda domain: ([{"chunk_id": "known", "title": "Shaders", "section": "Material", "text": "shader material", "embedding": vector}], "fixture-digest")
            def embed(texts, query=False):
                self.assertTrue(store.ledger(user)["calls"][0]["request_dispatched"])
                return [vector], {"prompt_eval_count": 7}
            retriever._embed = embed
            result = retriever.search("shader", on_request=lambda: store.mark_call_dispatched(call))
            store.finish_call(call, {"input_tokens": 7, "output_tokens": 0, "total_tokens": 7}, 0.0)
            self.assertEqual(result["candidates"][0]["chunk_id"], "known")
            self.assertEqual(store.ledger(user)["total_tokens"], 7)
            store.check_budget(user)
            provider = GLMProvider(settings, store, transport=lambda payload: self.fail("Cloud call mislabeled as embedding"))
            with self.assertRaises(ProviderError):
                provider.call(user, branch, turn, "embedding", [])


if __name__ == "__main__":
    unittest.main()
