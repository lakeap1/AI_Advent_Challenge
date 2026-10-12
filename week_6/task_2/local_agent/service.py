"""Local graphics chat: policy, durable history and accounting."""

from pathlib import Path
from threading import RLock
import tomllib

from .store import LocalStore
from .transport import OllamaTransport


SYSTEM_INSTRUCTION = (
    'Ты помощник по компьютерной графике и художественной работе. '
    'Отвечай на русском языке обычным текстом с абзацами, без Markdown-разметки, '
    'заголовков, таблиц и ограждений кода. Если сведений недостаточно, скажи об этом прямо.'
)


class LocalChat:
    def __init__(self, data_dir, config_path=None, transport=None):
        path = Path(config_path) if config_path else Path(__file__).with_name('config.toml')
        with path.open('rb') as source:
            self.config = tomllib.load(source)
        runtime = self.config['runtime']
        if runtime['model'] not in ('qwen3.5:9b', 'qwen3.5:4b'):
            raise ValueError('Для локального чата разрешены только qwen3.5:9b и qwen3.5:4b.')
        if runtime['num_ctx'] <= runtime['num_predict'] + 128:
            raise ValueError('Бюджет контекста Ollama слишком мал для полного запроса.')
        if 'num_gpu' in runtime and (type(runtime['num_gpu']) is not int or runtime['num_gpu'] < 0):
            raise ValueError('num_gpu должен быть неотрицательным целым числом.')
        self.model = runtime['model']
        self.pricing = self.config['pricing']
        if (self.pricing['api_usd_per_million_input'] != '0' or
                self.pricing['api_usd_per_million_output'] != '0'):
            raise ValueError('Тариф локального API должен быть явно равен 0 USD.')
        self.pricing_snapshot = {
            'model': self.model,
            'service_mode': self.pricing['service_mode'],
            'input_usd_per_million': self.pricing['api_usd_per_million_input'],
            'output_usd_per_million': self.pricing['api_usd_per_million_output'],
            'source': self.pricing['source'],
            'checked_on': self.pricing['checked_on'],
            'infrastructure_cost': self.pricing['infrastructure_cost'],
        }
        self.store = LocalStore(data_dir)
        self.transport = transport if transport is not None else OllamaTransport(runtime['timeout_seconds'])
        self.lock = RLock()

    def state(self):
        with self.lock:
            receipts = self.store.receipts()
            requested = [item for item in receipts if item['usage_status'] != 'not_requested']
            unknown = any(item['usage_status'] in ('unknown', 'pending') for item in requested)
            blocked = self.store.blocked()
            return {
                'messages': self.store.messages(),
                'receipts': receipts,
                'ledger': {
                    'requests': len(requested),
                    'known_total_usd': '0',
                    'total_cost_usd': None if unknown else '0',
                    'complete': not unknown,
                    'unknown_usage': unknown,
                },
                'pricing': self.pricing_snapshot,
                'blocked': blocked is not None,
                'block_reason': ('Неизвестен расход токенов предыдущего запроса; дальнейшие вопросы остановлены.'
                                 if blocked else None),
            }

    def runtime_status(self):
        try:
            body = self.transport('GET', '/api/tags')
            models = body.get('models') if isinstance(body, dict) else None
            installed = (isinstance(models, list) and any(
                isinstance(item, dict) and item.get('name') == self.model for item in models))
            if not installed:
                return {'status': 'unavailable', 'model': self.model,
                        'error': f'Модель {self.model} не найдена в локальной Ollama.'}
            return {'status': 'ready', 'model': self.model, 'error': None}
        except Exception as error:
            return {'status': 'unavailable', 'model': self.model,
                    'error': f'Локальная Ollama недоступна: {error}'}

    def ask(self, prompt):
        with self.lock:
            if self.store.blocked():
                return self._result('blocked', None, None,
                                    'Неизвестен расход токенов предыдущего запроса. Вопрос не отправлен.')
            issue = self._input_issue(prompt)
            if issue:
                receipt = self.store.record_rejection(self.model, self.pricing['service_mode'], issue,
                                                      self.pricing_snapshot)
                return self._result('rejected', None, receipt, issue)
            history = self.store.messages()
            messages = [{'role': 'system', 'content': SYSTEM_INSTRUCTION}, *history,
                        {'role': 'user', 'content': prompt}]
            if sum(len(item['content']) for item in messages) > self.config['input_policy']['max_context_characters']:
                issue = 'Полная история превышает лимит контекста; запрос не отправлен.'
                receipt = self.store.record_rejection(self.model, self.pricing['service_mode'], issue,
                                                      self.pricing_snapshot)
                return self._result('rejected', None, receipt, issue)
            # Byte length bounds byte-level tokenization conservatively; reserve
            # room for message framing and the configured maximum output.
            byte_budget = sum(len(item['content'].encode('utf-8')) + 64 for item in messages) + 128
            if byte_budget > self.config['runtime']['num_ctx'] - self.config['runtime']['num_predict']:
                issue = 'Полная история превышает безопасный бюджет Ollama; запрос не отправлен.'
                receipt = self.store.record_rejection(self.model, self.pricing['service_mode'], issue,
                                                      self.pricing_snapshot)
                return self._result('rejected', None, receipt, issue)
            receipt_id = self.store.begin(self.model, self.pricing['service_mode'], self.pricing_snapshot)
            if receipt_id is None:
                return self._result('blocked', None, None,
                                    'Предыдущий запрос имеет неизвестный расход токенов.')
            payload = {'model': self.model, 'messages': messages, 'stream': False,
                       'think': False, 'options': {
                           'num_ctx': self.config['runtime']['num_ctx'],
                           'num_predict': self.config['runtime']['num_predict']}}
            if 'num_gpu' in self.config['runtime']:
                payload['options']['num_gpu'] = self.config['runtime']['num_gpu']
            try:
                response = self.transport('POST', '/api/chat', payload)
            except Exception as error:
                issue = f'Локальная Ollama недоступна: {error}'
                receipt = self.store.finish(receipt_id, status='error', error=issue,
                                            output_policy='not_run', usage=None, cost_usd=None)
                return self._result('error', None, receipt, issue)
            usage = self._usage(response)
            usage_complete = (usage is not None and usage['input_tokens'] is not None and
                              usage['output_tokens'] is not None)
            cost = '0' if usage_complete else None
            issue = self._output_issue(response)
            if not usage_complete:
                issue = ((issue + ' ') if issue else '') + (
                    'Ollama не сообщила полные счётчики токенов; дальнейшие вопросы остановлены.')
            if issue:
                receipt = self.store.finish(receipt_id, status='rejected' if usage_complete else 'error',
                                            error=issue, output_policy='rejected' if usage_complete else 'unknown',
                                            usage=usage, cost_usd=cost)
                return self._result('rejected' if usage_complete else 'error', None, receipt, issue)
            content = response['message']['content']
            receipt = self.store.finish(receipt_id, status='ok', error=None,
                                        output_policy='accepted', usage=usage, cost_usd=cost,
                                        messages=(('user', prompt), ('assistant', content)))
            return self._result('ok', content, receipt, None)

    def _input_issue(self, prompt):
        if not isinstance(prompt, str) or not prompt.strip():
            return 'Напишите непустой вопрос.'
        try:
            prompt.encode('utf-8')
        except UnicodeEncodeError:
            return 'Вопрос содержит некорректные символы Unicode.'
        if len(prompt) > self.config['input_policy']['max_characters']:
            return 'Вопрос превышает лимит 2000 символов.'
        return None

    def _output_issue(self, response):
        if not isinstance(response, dict) or response.get('model') != self.model:
            return 'Ollama вернула ответ другой модели или неверный формат.'
        if response.get('error'):
            return f"Ollama вернула ошибку: {response['error']}"
        if response.get('done') is not True or response.get('done_reason') == 'length':
            return 'Ollama не завершила ответ полностью.'
        message = response.get('message')
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            return 'Ollama вернула неверный формат ответа.'
        content = message.get('content')
        if not isinstance(content, str) or not content.strip():
            return 'Ollama вернула пустой ответ.'
        if len(content) > self.config['output_policy']['max_characters']:
            return 'Ответ превышает допустимую длину.'
        return None

    @staticmethod
    def _usage(response):
        if not isinstance(response, dict):
            return None
        incoming, outgoing = response.get('prompt_eval_count'), response.get('eval_count')
        incoming = incoming if type(incoming) is int and incoming >= 0 else None
        outgoing = outgoing if type(outgoing) is int and outgoing >= 0 else None
        cached = response.get('prompt_eval_cached_count')
        cached = cached if type(cached) is int and cached >= 0 and (incoming is None or cached <= incoming) else None
        return {'input_tokens': incoming, 'output_tokens': outgoing,
                'total_tokens': incoming + outgoing if incoming is not None and outgoing is not None else None,
                'total_source': 'derived_from_ollama_counters' if incoming is not None and outgoing is not None else None,
                'cached_input_tokens': cached, 'reasoning_tokens': None}

    def _result(self, status, answer, receipt, error):
        return {'status': status, 'answer': answer, 'receipt': receipt,
                'error': error, 'state': self.state()}
