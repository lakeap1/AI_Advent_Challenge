"""Server-side dependency checks and file persistence."""
import hashlib
import json
import os

from knowledge_server.providers import KnowledgeProviders
from .runner import OrchestrationError


class OrchestrationService:
    def __init__(self, store, run_id, *, providers=None):
        self.store, self.run_id = store, run_id
        self.providers = providers or KnowledgeProviders()

    def _running(self):
        if self.store.get(self.run_id)['status'] != 'running':
            raise OrchestrationError('Запуск завершён; новые вызовы запрещены.')

    async def lookup_wikipedia(self, query, language='en'):
        self._running()
        result = await self.providers.lookup_wikipedia(query, language, 3)
        if not result.get('sources'):
            raise OrchestrationError('Wikipedia не вернула пригодных источников.')
        data = {'provider':'wikipedia','query':query,'language':language,'sources':result['sources']}
        mid = self.store.put_artifact(self.run_id, 'material', data)
        return {'material_id':mid, **data}

    async def search_stackexchange(self, query, community='blender'):
        self._running()
        result = await self.providers.search_stackexchange(query, community, 3)
        if not result.get('sources'):
            raise OrchestrationError('Stack Exchange не вернул пригодных ответов.')
        data = {'provider':'stackexchange','query':query,'community':community,'sources':result['sources']}
        mid = self.store.put_artifact(self.run_id, 'material', data)
        return {'material_id':mid, **data}

    def compare_sources(self, material_ids):
        self._running()
        if not isinstance(material_ids, list) or len(material_ids) != 2 or len(set(material_ids)) != 2:
            raise OrchestrationError('Нужны два разных material_id.')
        try:
            materials = [self.store.artifact(self.run_id, mid, 'material') for mid in material_ids]
        except ValueError as exc:
            raise OrchestrationError(str(exc)) from None
        if {m['provider'] for m in materials} != {'wikipedia','stackexchange'}:
            raise OrchestrationError('Нужны Wikipedia и Stack Exchange.')
        sources = [s for m in materials for s in m['sources']]
        data = {'material_ids':material_ids,'sources':sources,
                'comparison':'Теоретический источник описывает принцип; практический источник показывает ограничения и проверки.'}
        cid = self.store.put_artifact(self.run_id, 'comparison', data)
        return {'comparison_id':cid, **data}

    def prepare_report(self, text, material_ids):
        self._running()
        if not isinstance(text, str) or not text.strip() or len(text) > 15000 or not isinstance(material_ids, list) or len(material_ids) != 2:
            raise OrchestrationError('Некорректный текст отчёта или материалы.')
        try:
            text.encode('utf-8')
        except UnicodeEncodeError:
            raise OrchestrationError('Некорректный Unicode.') from None
        # A successful comparison for the same ordered IDs must already exist.
        with self.store._db() as db:
            rows = db.execute("SELECT data FROM artifacts WHERE run_id=? AND kind='comparison'", (self.run_id,)).fetchall()
        if not any(json.loads(row['data'])['material_ids'] == material_ids for row in rows):
            raise OrchestrationError('Сравнение этих материалов ещё не выполнено.')
        materials = [self.store.artifact(self.run_id, mid, 'material') for mid in material_ids]
        sources = [s for m in materials for s in m['sources']]
        links = '\n'.join(f"{s['title']}: {s['url']}" for s in sources)
        content = text.strip() + '\n\nИсточники:\n' + links + '\n'
        data = {'material_ids':material_ids,'content':content,'sha256':hashlib.sha256(content.encode('utf-8')).hexdigest(),
                'sources':sources}
        report_id = self.store.put_artifact(self.run_id, 'report', data)
        return {'report_id':report_id, **data}

    def save_report(self, report_id):
        self._running()
        try:
            report = self.store.artifact(self.run_id, report_id, 'report')
        except ValueError as exc:
            raise OrchestrationError(str(exc)) from None
        target = self.store.results / (report_id + '.txt')
        if target.resolve().parent != self.store.results:
            raise OrchestrationError('Файл вышел за каталог результатов.')
        data = report['content'].encode('utf-8')
        try:
            with target.open('xb') as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            actual = target.read_bytes()
        except FileExistsError:
            raise OrchestrationError('Перезапись файла запрещена.') from None
        except OSError:
            raise OrchestrationError('Не удалось записать отчёт.') from None
        if actual != data or hashlib.sha256(actual).hexdigest() != report['sha256']:
            raise OrchestrationError('Проверка записанного файла не прошла.')
        saved = {'report_id':report_id,'filename':target.name,'content':report['content'],
                 'sha256':report['sha256'],'bytes_written':len(actual)}
        self.store.put_artifact(self.run_id, 'saved', saved)
        return saved

    def read_report(self, report_id):
        self._running()
        with self.store._db() as db:
            rows = db.execute("SELECT data FROM artifacts WHERE run_id=? AND kind='saved'", (self.run_id,)).fetchall()
        saved = next((json.loads(row['data']) for row in rows if json.loads(row['data'])['report_id'] == report_id), None)
        if saved is None:
            raise OrchestrationError('Этот отчёт не сохранён в текущем запуске.')
        target = self.store.results / saved['filename']
        if target.resolve().parent != self.store.results:
            raise OrchestrationError('Файл вышел за каталог результатов.')
        try:
            actual = target.read_bytes()
        except OSError:
            raise OrchestrationError('Сохранённый файл недоступен.') from None
        if actual != saved['content'].encode('utf-8') or len(actual) != saved['bytes_written'] or hashlib.sha256(actual).hexdigest() != saved['sha256']:
            raise OrchestrationError('Сохранённый файл изменился или повреждён.')
        return saved
