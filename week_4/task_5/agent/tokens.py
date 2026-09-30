"""Локальная оценка текста; фактический расход берётся только из API usage."""
from collections import OrderedDict
from hashlib import sha256
from importlib.metadata import version
import os
from pathlib import Path
import tiktoken

class TokenCounter:
    def __init__(self):
        os.environ.setdefault('TIKTOKEN_CACHE_DIR', str(Path(__file__).resolve().parents[1] / 'data' / 'tiktoken'))
        self._encoding = tiktoken.get_encoding('o200k_base')
        self._cache = OrderedDict()
        self.description = {'method': 'local_text_estimate', 'encoding': 'o200k_base', 'version': version('tiktoken')}

    def count(self, text):
        key = sha256(text.encode('utf-8')).digest()
        if key not in self._cache:
            self._cache[key] = len(self._encoding.encode(text, disallowed_special=()))
            if len(self._cache) > 512:
                self._cache.popitem(last=False)
        self._cache.move_to_end(key)
        return self._cache[key]

    def history(self, messages):
        return sum(self.count(message['content']) for message in messages)

    def measure(self, prompt, messages, instructions):
        new, history, system = self.count(prompt), self.history(messages), self.count(instructions)
        return {**self.description, 'new_message_tokens_estimate': new,
                'history_tokens_estimate': history, 'instructions_tokens_estimate': system,
                'input_text_tokens_estimate': new + history + system}

def accounting(requests):
    called = [item for item in requests if item['usage_status'] != 'not_requested']
    known = [item['usage'] for item in called if item['usage'] is not None]
    return {**{f'known_{key}': sum(usage[key] for usage in known)
               for key in ('input_tokens', 'output_tokens', 'total_tokens')},
            'complete': len(known) == len(called), 'unknown_requests': len(called) - len(known)}
