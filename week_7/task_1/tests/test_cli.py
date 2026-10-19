import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from graphics.config import Settings
from graphics.store import Store


class AdminTests(unittest.TestCase):
    def test_create_user_hides_password_and_uses_real_store(self):
        from graphics.cli import main
        with tempfile.TemporaryDirectory() as folder:
            config = Settings(app_root=Path(folder), data_dir=Path(folder))
            output = io.StringIO()
            with patch('graphics.cli.Settings.from_env', return_value=config), \
                    patch('graphics.cli.getpass', side_effect=['private-test-pass', 'private-test-pass']), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(main(['create-user', 'owner']), 0)
            self.assertIsNotNone(Store(config.db_path).authenticate('owner', 'private-test-pass'))
            self.assertNotIn('private-test-pass', output.getvalue())

    def test_password_mismatch_does_not_create_user(self):
        from graphics.cli import main
        with tempfile.TemporaryDirectory() as folder:
            config = Settings(app_root=Path(folder), data_dir=Path(folder))
            with patch('graphics.cli.Settings.from_env', return_value=config), \
                    patch('graphics.cli.getpass', side_effect=['private-test-pass', 'different-test-pass']), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['create-user', 'owner']), 1)
            self.assertIsNone(Store(config.db_path).authenticate('owner', 'private-test-pass'))
