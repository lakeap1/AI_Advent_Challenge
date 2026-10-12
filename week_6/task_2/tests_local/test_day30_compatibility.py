from pathlib import Path

import pytest

from local_agent import LocalChat


def test_day30_model_is_allowlisted_without_changing_day27_default(tmp_path):
    original = Path(__file__).parents[1] / 'local_agent' / 'config.toml'
    config = tmp_path / 'day30.toml'
    config.write_text(original.read_text(encoding='utf-8').replace(
        'model = "qwen3.5:9b"', 'model = "qwen3.5:4b"'), encoding='utf-8')
    assert LocalChat(tmp_path / 'new', config_path=config, transport=lambda *a: {}).model == 'qwen3.5:4b'
    assert LocalChat(tmp_path / 'old', transport=lambda *a: {}).model == 'qwen3.5:9b'

    config.write_text(config.read_text(encoding='utf-8').replace('qwen3.5:4b', 'arbitrary:latest'), encoding='utf-8')
    with pytest.raises(ValueError, match='разрешен'):
        LocalChat(tmp_path / 'invalid', config_path=config, transport=lambda *a: {})
