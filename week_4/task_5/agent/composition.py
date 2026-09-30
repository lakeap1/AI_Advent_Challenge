"""Fixed host orchestration: real MCP connection, discovery and dependent calls."""
import asyncio
import os
from pathlib import Path
import sys

from composition.contracts import Materials, Saved, SearchInput, Summary
from composition.service import CompositionError


TOOLS = ('search_graphics', 'summarize_graphics', 'save_summary')


class CompositionAgent:
    def __init__(self, store):
        self.store = store
        self.root = Path(__file__).resolve().parents[1]

    def create(self, profile_id, dialogue_id, question, query, branch_id=1, *, source='blender'):
        # Even rejected input gets a durable policy outcome; no model is called.
        try:
            value = SearchInput(question=question, query=query, source=source)
        except (ValueError, TypeError):
            run = self.store.create(profile_id, dialogue_id, '', '', branch_id)
            self.store.event(run['id'], 'input', 'rejected', 'Input policy: вопрос 1–2000 символов, поисковая тема 1–200 символов, одной строкой.')
            return self.store.get(run['id'])
        return self.store.create(profile_id, dialogue_id, value.question, value.query, branch_id, source=value.source)

    def execute(self, run_id, before_save=None):
        try:
            asyncio.run(self.connect_and_run(run_id, before_save))
        except Exception:
            run = self.store.get(run_id)
            if run['status'] == 'running':
                self.store.event(run_id, run['stage'], 'error',
                                 'MCP-соединение прервано или истекло время ожидания. Зависимые шаги остановлены; автоматического повтора нет.')

    async def connect_and_run(self, run_id, before_save=None):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        env = {**os.environ, 'CHAT_DATA_DIR': str(self.store.root), 'PYTHONIOENCODING': 'utf-8'}
        server = StdioServerParameters(command=sys.executable, args=['-m', 'composition.server'], cwd=str(self.root), env=env)
        self.store.event(run_id, 'connection', 'running', 'Подключение к локальному MCP-серверу…')
        async with asyncio.timeout(100):
            # SDK diagnostics are not tool content and can contain external text; keep off UI/logs.
            with open(os.devnull, 'w') as diagnostics:
                async with stdio_client(server, errlog=diagnostics) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await self.run_session(run_id, session, before_save=before_save)

    async def run_session(self, run_id, session, before_save=None):
        """Runs against an initialized MCP ClientSession; also used by the wire integration test."""
        run = self.store.get(run_id)
        if run['status'] != 'running':
            raise CompositionError('Запуск не находится в состоянии выполнения.')
        discovered = await session.list_tools()
        catalog = [t.model_dump(by_alias=True, mode='json') for t in discovered.tools]
        self.store.put(run_id, 'catalog', catalog)
        if not set(TOOLS) <= {t.name for t in discovered.tools}:
            raise CompositionError('Сервер не предоставил три обязательных инструмента.')
        self.store.event(run_id, 'connection', 'done', 'MCP подключён; три инструмента обнаружены.')
        arguments = dict(run_id=run_id, question=run['question'], query=run['query'], source=run['source'])
        for name, model, stage in zip(TOOLS, (Materials, Summary, Saved), ('search', 'summarize', 'save')):
            if stage == 'save' and before_save is not None:
                # Agent-owned output rules must accept the processed text before a file write.
                rejected = before_save(summary.content)
                if rejected:
                    self.store.event(run_id, 'summarize', 'rejected', rejected)
                    return
            self.store.event(run_id, stage, 'running', name)
            result = await session.call_tool(name, arguments=arguments)
            if result.is_error:
                # Server errors are intentionally sanitized, no raw provider body or secrets.
                message = next((c.text for c in result.content if getattr(c, 'type', '') == 'text'), 'Ошибка инструмента.')
                self.store.event(run_id, stage, 'error', message[:500])
                return
            try:
                data = result.structured_content
                checked = model.model_validate(data)
                if checked.run_id != run_id:
                    raise ValueError('Wrong run')
                if stage == 'search' and (checked.question, checked.query, checked.source) != (run['question'], run['query'], run['source']):
                    raise ValueError('Search input substitution')
                if stage == 'summarize' and checked.sources != materials.sources:
                    raise ValueError('Source substitution')
                if stage == 'save' and (checked.content != summary.content or checked.sha256 != summary.sha256):
                    raise ValueError('Saved content mismatch')
            except (ValueError, TypeError):
                self.store.event(run_id, stage, 'error', 'Output policy: результат MCP не соответствует контракту. Зависимые шаги остановлены.')
                return
            self.store.event(run_id, stage, 'done', name)
            if stage == 'search':
                materials = checked
                arguments = dict(materials=checked.model_dump())
            elif stage == 'summarize':
                summary = checked
                arguments = dict(summary=checked.model_dump())
            else:
                actual = (self.store.results / checked.filename).read_bytes()
                if actual != checked.content.encode('utf-8'):
                    self.store.event(run_id, stage, 'error', 'Содержимое файла не совпадает с результатом MCP.')
                    return
                self.store.event(run_id, stage, 'success', 'Памятка сохранена; содержимое файла проверено.')
