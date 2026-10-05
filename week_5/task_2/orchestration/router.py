"""Tools/list discovery and schema-checked qualified MCP dispatch."""
import asyncio
import os
from pathlib import Path
import sys
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError
from mcp import StdioServerParameters
from mcp.types import PaginatedRequestParams

from .runner import OrchestrationError


ROLES = ('research','processing','library')


def server_specs(data_dir: Path, run_id: str):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'CHAT_DATA_DIR':str(Path(data_dir).resolve()),
           'ORCHESTRATION_RUN_ID':run_id, 'PYTHONIOENCODING':'utf-8'}
    return {role: StdioServerParameters(command=sys.executable,
            args=['-m','orchestration.server','--role',role], cwd=str(root), env=env)
            for role in ROLES}


class OrchestrationRouter:
    def __init__(self, sessions, *, server_info=None, session_ids=None):
        if set(sessions) != set(ROLES):
            raise OrchestrationError('Нужны три самостоятельные MCP-сессии.')
        self.sessions = sessions
        self.server_info = server_info or {}
        self.session_ids = session_ids or {role:uuid4().hex for role in ROLES}
        self.catalog = []
        self._tools = {}

    async def discover(self):
        catalog = []
        mapping = {}
        definitions = []
        for role in ROLES:
            cursor = None
            seen_cursors = set()
            tools = []
            for _ in range(20):
                try:
                    async with asyncio.timeout(15):
                        result = await self.sessions[role].list_tools(params=PaginatedRequestParams(cursor=cursor) if cursor else None)
                except Exception as exc:
                    raise OrchestrationError(f'{role}: tools/list недоступен: {type(exc).__name__}') from None
                tools.extend(result.tools)
                cursor = result.next_cursor
                if cursor is None:
                    break
                if cursor in seen_cursors:
                    raise OrchestrationError('MCP tools/list повторил cursor.')
                seen_cursors.add(cursor)
            else:
                raise OrchestrationError('MCP tools/list превысил лимит страниц.')
            entry = {'server':role,'session_id':self.session_ids[role],
                     'server_info':self.server_info.get(role,{}),'tools':[]}
            for tool in tools:
                qname = role+'__'+tool.name
                if qname in mapping:
                    raise OrchestrationError('MCP каталог содержит повторное имя.')
                schema = tool.input_schema
                output_schema = tool.output_schema
                if not isinstance(schema,dict) or not isinstance(output_schema,dict):
                    raise OrchestrationError('MCP tool не предоставил обе JSON-схемы.')
                try:
                    Draft202012Validator.check_schema(schema)
                    Draft202012Validator.check_schema(output_schema)
                except Exception:
                    raise OrchestrationError('MCP каталог содержит некорректную JSON-схему.') from None
                mapping[qname] = (role, tool)
                entry['tools'].append({'name':tool.name,'qualified_name':qname,
                    'description':tool.description or '', 'input_schema':schema,'output_schema':output_schema})
                definitions.append({'type':'function','name':qname,'description':tool.description or qname,
                                    'parameters':schema})
            catalog.append(entry)
        self._tools, self.catalog = mapping, catalog
        return definitions

    async def call(self, qualified, args):
        if qualified not in self._tools:
            raise OrchestrationError('Неизвестное квалифицированное имя MCP-инструмента.')
        role, tool = self._tools[qualified]
        try:
            Draft202012Validator(tool.input_schema).validate(args)
        except ValidationError as exc:
            raise OrchestrationError('Аргументы не соответствуют JSON-схеме MCP-инструмента.') from None
        try:
            async with asyncio.timeout(25):
                result = await self.sessions[role].call_tool(tool.name, arguments=args)
        except Exception as exc:
            raise OrchestrationError(f'{role}: tools/call недоступен: {type(exc).__name__}') from None
        if result.is_error:
            message = next((c.text for c in result.content if getattr(c,'type',None)=='text'), 'MCP инструмент вернул ошибку.')
            raise OrchestrationError(str(message)[:300])
        data = result.structured_content
        if not isinstance(data,dict):
            raise OrchestrationError('MCP не вернул структурированный результат.')
        try:
            Draft202012Validator(tool.output_schema).validate(data)
        except ValidationError:
            raise OrchestrationError('Результат MCP не соответствует объявленной JSON-схеме.') from None
        return data
