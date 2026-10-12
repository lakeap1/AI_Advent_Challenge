"""Single-owner request admission and read-only state projection."""

from collections import deque
from math import ceil
from threading import BoundedSemaphore, Lock
import time


class PrivateGateway:
    RATE_LIMIT = 10
    RATE_WINDOW = 60
    MAX_WAITING = 3
    WAIT_TIMEOUT = 180

    def __init__(self, chat, transport, clock=None):
        self.chat = chat
        self.transport = transport
        self.clock = clock or time.monotonic
        self._gate = Lock()
        self._active = Lock()
        self._slots = BoundedSemaphore(1 + self.MAX_WAITING)
        self._attempts = deque()
        self._counters = dict(received=0, admitted=0, inference_calls=0,
                              rate_rejected=0, context_rejected=0, busy_rejected=0)
        self.waiting = 0
        self.slots_available = 1 + self.MAX_WAITING

    def counted_transport(self, method, path, payload=None):
        if (method, path) == ('POST', '/api/chat'):
            with self._gate:
                self._counters['inference_calls'] += 1
        return self.transport(method, path, payload)

    def state(self):
        # LocalChat.state() holds its lock across inference. Store reads use
        # separate short SQLite connections, so a pending call stays visible.
        receipts = self.chat.store.receipts()
        requested = [r for r in receipts if r['usage_status'] != 'not_requested']
        unknown = any(r['usage_status'] in ('unknown', 'pending') for r in requested)
        blocked = self.chat.store.blocked() is not None
        with self._gate:
            counters = dict(self._counters)
            waiting = self.waiting
        return {
            'messages': self.chat.store.messages(),
            'receipts': receipts,
            'ledger': {'requests': len(requested), 'known_total_usd': '0',
                       'total_cost_usd': None if unknown else '0',
                       'complete': not unknown, 'unknown_usage': unknown},
            'pricing': self.chat.pricing_snapshot,
            'blocked': blocked,
            'block_reason': ('Неизвестен расход токенов предыдущего запроса; дальнейшие вопросы остановлены.'
                             if blocked else None),
            'limits': {'ask_attempts': self.RATE_LIMIT, 'window_seconds': self.RATE_WINDOW,
                       'active_inference': 1, 'waiting': self.MAX_WAITING,
                       'wait_timeout_seconds': self.WAIT_TIMEOUT,
                       'max_prompt_characters': self.chat.config['input_policy']['max_characters'],
                       'num_ctx': self.chat.config['runtime']['num_ctx'],
                       'num_predict': self.chat.config['runtime']['num_predict']},
            'counters': {**counters, 'waiting': waiting},
            'deployment': {'access': 'SSH tunnel to loopback', 'owner': 'shared single chat',
                           'ollama': '127.0.0.1:11435', 'api_cost_usd': '0',
                           'infrastructure_cost': self.chat.pricing['infrastructure_cost']},
        }

    def runtime(self):
        forced_cpu = self.chat.config['runtime'].get('num_gpu') == 0
        info = {'status': 'unavailable', 'model': self.chat.model, 'version': None,
                'installed': False, 'processor': 'CPU' if forced_cpu else None,
                'num_gpu': self.chat.config['runtime'].get('num_gpu'), 'size_vram': None,
                'placement': ('CPU forced by num_gpu=0; device telemetry not inferred from size_vram'
                              if forced_cpu else 'device placement unverified'),
                'error': None}
        try:
            version = self.transport('GET', '/api/version')
            tags = self.transport('GET', '/api/tags')
            info['version'] = version.get('version')
            models = tags.get('models')
            info['installed'] = isinstance(models, list) and any(
                isinstance(model, dict) and model.get('name') == self.chat.model for model in models)
            if info['installed']:
                info['status'] = 'ready'
                try:
                    processes = self.transport('GET', '/api/ps').get('models', [])
                    for model in processes:
                        if model.get('name') == self.chat.model:
                            vram = model.get('size_vram')
                            if type(vram) is int:
                                info['size_vram'] = vram
                            break
                except Exception:
                    pass  # readiness does not depend on an optional ps observation
            else:
                info['error'] = f'Модель {self.chat.model} не установлена.'
        except Exception as error:
            info['error'] = f'Локальная Ollama недоступна: {error}'
        return info

    def ask(self, prompt):
        with self._gate:
            now = self.clock()
            self._counters['received'] += 1
            while self._attempts and now - self._attempts[0] >= self.RATE_WINDOW:
                self._attempts.popleft()
            if len(self._attempts) >= self.RATE_LIMIT:
                self._counters['rate_rejected'] += 1
                retry = max(1, ceil(self.RATE_WINDOW - (now - self._attempts[0])))
                return self._failure('rate_limited', 'Превышен лимит: 10 вопросов за 60 секунд.', 429,
                                     retry_after=retry)
            self._attempts.append(now)
        if not self._slots.acquire(blocking=False):
            with self._gate:
                self._counters['busy_rejected'] += 1
            return self._failure('queue_full', 'Очередь запросов заполнена.', 503)
        with self._gate:
            self.slots_available -= 1
            self.waiting += 1
        is_waiting = True
        try:
            if not self._active.acquire(timeout=self.WAIT_TIMEOUT):
                with self._gate:
                    self._counters['busy_rejected'] += 1
                return self._failure('queue_timeout', 'Ожидание модели превысило 180 секунд.', 503)
            try:
                with self._gate:
                    self.waiting -= 1
                    is_waiting = False
                    self._counters['admitted'] += 1
                result = self.chat.ask(prompt)
            finally:
                self._active.release()
            status = {'ok': 200, 'rejected': 400, 'blocked': 409, 'error': 503}[result['status']]
            if (result['status'] == 'rejected' and result['receipt'] is not None and
                    result['receipt']['input_policy'] == 'rejected'):
                with self._gate:
                    self._counters['context_rejected'] += 1
            result['state'] = self.state()
            return result, status, {}
        finally:
            with self._gate:
                self.slots_available += 1
                if is_waiting:
                    self.waiting -= 1
            self._slots.release()

    def _failure(self, code, error, status, retry_after=None):
        headers = {'Retry-After': str(retry_after)} if retry_after is not None else {}
        return {'status': 'error', 'code': code, 'error': error}, status, headers
