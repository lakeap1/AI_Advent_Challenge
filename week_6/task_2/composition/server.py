"""A genuine stdio MCP server. stdout belongs exclusively to the protocol."""
import asyncio
import os
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .contracts import Materials, Saved, Summary, SourceChoice
from .service import CompositionService
from .store import CompositionStore


def create_server(service):
    mcp = MCPServer('graphics-composition', description='Find graphics references on Wikipedia or Stack Exchange, process with Luna, save the accepted result.')

    @mcp.tool(description='Получить до трёх фрагментов из выбранного source: Wikipedia en/ru или Stack Exchange Blender/Computer Graphics/Game Development. Возвращает материалы с источниками.', structured_output=True)
    async def search_graphics(run_id: str, question: str, query: str, source: SourceChoice = 'blender') -> Materials:
        try:
            return await service.search(run_id, question, query, source)
        except Exception:
            raise ToolError('Поиск остановлен: проверьте тему, доступность выбранного источника и ограничения API.') from None

    @mcp.tool(description='Подготовить краткую русскую памятку одним вызовом Luna по фактическому Materials первого инструмента; сохранить usage и политики.', structured_output=True)
    async def summarize_graphics(materials: Materials) -> Summary:
        try:
            return await asyncio.to_thread(service.summarize, materials)
        except Exception as exc:
            from .service import CompositionError
            message = str(exc) if isinstance(exc, CompositionError) else 'Обработка остановлена: неверные материалы или ошибка журнала.'
            raise ToolError(message) from None

    @mcp.tool(description='Сохранить точный принятый Summary второго инструмента в уникальный UTF-8 TXT внутри каталога результатов, без перезаписи; проверить содержимое.', structured_output=True)
    async def save_summary(summary: Summary) -> Saved:
        try:
            return service.save(summary)
        except Exception as exc:
            from .service import CompositionError
            message = str(exc) if isinstance(exc, CompositionError) else 'Сохранение остановлено: неверный результат или ошибка диска.'
            raise ToolError(message) from None

    return mcp


def main():
    root = Path(__file__).resolve().parents[1]
    store = CompositionStore(os.getenv('CHAT_DATA_DIR', str(root / 'data')))
    create_server(CompositionService(store)).run('stdio')


if __name__ == '__main__':
    main()
