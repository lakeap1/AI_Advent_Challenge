from pathlib import Path
import tempfile
import unittest

from graphics.store import Store


class CompressionStateTests(unittest.TestCase):
    def test_internal_summary_completion_releases_branch_without_publishing_an_answer(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / "service.sqlite3")
            user = store.create_user("owner", "long-fixture-password")
            branch = store.create_chat(user)["branch_id"]
            turn = store.begin_turn(user, branch, "/compress")["id"]
            store.move_turn(turn, "input_check")
            store.move_turn(turn, "context")
            store.end_turn(turn, "compressed")
            with self.assertRaises((ValueError, PermissionError)):
                store.append_message(user, branch, "assistant", "An internal summary is not an accepted reply", turn_id=turn)
            updated = store.update_branch(user, branch, {"conditions": {"Unity": "6.3 URP"}})
            self.assertEqual(updated["conditions"], {"Unity": "6.3 URP"})
            resumed = Store(Path(temporary) / "service.sqlite3")
            next_turn = resumed.begin_turn(user, branch, "Продолжим разговор")
            self.assertEqual(next_turn["status"], "new")


if __name__ == "__main__":
    unittest.main()
