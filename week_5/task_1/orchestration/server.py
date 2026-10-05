"""Three role-specific stdio MCP servers sharing a run-owned SQLite journal."""
import argparse
import os
from typing import Any

from pydantic import BaseModel

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .service import OrchestrationService
from .store import OrchestrationStore


class Material(BaseModel):
    material_id: str
    provider: str
    query: str
    sources: list[dict[str, Any]]
    language: str | None = None
    community: str | None = None


class Comparison(BaseModel):
    comparison_id: str
    material_ids: list[str]
    sources: list[dict[str, Any]]
    comparison: str


class Report(BaseModel):
    report_id: str
    material_ids: list[str]
    content: str
    sha256: str
    sources: list[dict[str, Any]]


class Saved(BaseModel):
    report_id: str
    filename: str
    content: str
    sha256: str
    bytes_written: int


def create_server(role, service):
    if role not in ('research','processing','library'):
        raise ValueError('Неизвестная роль MCP-сервера.')
    mcp = MCPServer('graphics-'+role, description='Помощник по компьютерной графике: '+role)

    if role == 'research':
        @mcp.tool(description='Найти теорию компьютерной графики в Wikipedia. Вернуть material_id и фактические источники.', structured_output=True)
        async def lookup_wikipedia(query: str, language: str = 'en') -> Material:
            try:
                return Material.model_validate(await service.lookup_wikipedia(query, language))
            except Exception as exc:
                raise ToolError(str(exc)[:300]) from None

        @mcp.tool(description='Найти практический случай по графике/арту в Blender, Computer Graphics или Game Development Stack Exchange.', structured_output=True)
        async def search_stackexchange(query: str, community: str = 'blender') -> Material:
            try:
                return Material.model_validate(await service.search_stackexchange(query, community))
            except Exception as exc:
                raise ToolError(str(exc)[:300]) from None
    elif role == 'processing':
        @mcp.tool(description='Сравнить два фактически найденных материала текущего запуска, один Wikipedia и один Stack Exchange.', structured_output=True)
        def compare_sources(material_ids: list[str]) -> Comparison:
            try:
                return Comparison.model_validate(service.compare_sources(material_ids))
            except Exception as exc:
                raise ToolError(str(exc)[:300]) from None

        @mcp.tool(description='Подготовить русский plain-text отчёт по уже сравнённым material_ids. Ссылки добавляет сервер.', structured_output=True)
        def prepare_report(text: str, material_ids: list[str]) -> Report:
            try:
                return Report.model_validate(service.prepare_report(text, material_ids))
            except Exception as exc:
                raise ToolError(str(exc)[:300]) from None
    else:
        @mcp.tool(description='Записать ранее подготовленный отчёт текущего запуска в уникальный UTF-8 TXT без перезаписи.', structured_output=True)
        def save_report(report_id: str) -> Saved:
            try:
                return Saved.model_validate(service.save_report(report_id))
            except Exception as exc:
                raise ToolError(str(exc)[:300]) from None

        @mcp.tool(description='Прочитать фактически сохранённый TXT и проверить байты, размер и SHA-256.', structured_output=True)
        def read_report(report_id: str) -> Saved:
            try:
                return Saved.model_validate(service.read_report(report_id))
            except Exception as exc:
                raise ToolError(str(exc)[:300]) from None
    return mcp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--role', choices=('research','processing','library'), required=True)
    args = parser.parse_args()
    store = OrchestrationStore(os.environ['CHAT_DATA_DIR'])
    service = OrchestrationService(store, os.environ['ORCHESTRATION_RUN_ID'])
    create_server(args.role, service).run('stdio')


if __name__ == '__main__':
    main()
