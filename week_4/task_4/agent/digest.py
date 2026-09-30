"""Validated MCP boundary and durable, immutable local digest mirror."""
import asyncio
from collections import Counter
import json
import math
import os
from pathlib import Path
import sqlite3
from threading import RLock
import time
import tomllib
from urllib.parse import urlsplit


class DigestError(RuntimeError):
    pass


SOURCES = ('computergraphics', 'graphicdesign', 'blender')


def require(condition):
    if not condition:
        raise DigestError('Результат MCP отклонён проверкой структуры сводки.')


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


class DigestAgent:
    def __init__(self, data_dir, url=None):
        self.url = url or os.getenv('DIGEST_MCP_URL', 'http://127.0.0.1:8018/mcp')
        parsed = urlsplit(self.url)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or parsed.username or parsed.password:
            raise ValueError('MCP сводок должен быть доступен через локальный SSH-туннель.')
        self.policy = tomllib.loads(Path(__file__).with_name('digest_policy.toml').read_text(encoding='utf-8'))
        self.path = Path(data_dir) / 'digest-audit.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        with sqlite3.connect(self.path) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY, at REAL, tool TEXT, input_policy TEXT, output_policy TEXT, status TEXT, llm_status TEXT);
                CREATE TABLE IF NOT EXISTS mirror(run_id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mirror_state(id INTEGER PRIMARY KEY CHECK(id=1), cursor INTEGER NOT NULL DEFAULT 0, state TEXT);
                INSERT OR IGNORE INTO mirror_state(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS digest_selection(profile_id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL);''')

    def _audit(self, tool, input_status, output_status, status):
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO calls(at,tool,input_policy,output_policy,status,llm_status) VALUES (?,?,?,?,?,?)',
                       (time.time(), tool, input_status, output_status, status, 'not_requested'))

    def _input(self, tool, args):
        if tool not in self.policy['input_policy']['tools'] or not isinstance(args, dict):
            raise ValueError('Недопустимая операция сводки.')
        if tool == 'configure_digest':
            if set(args) - {'sources'}:
                raise ValueError('Расписание фиксировано: 00, 06, 12, 18 часов Asia/Omsk.')
            sources = args.get('sources')
            if sources is not None and (not isinstance(sources, list) or not sources or
                    any(s not in SOURCES for s in sources) or len(set(sources)) != len(sources)):
                raise ValueError('Выберите Computer Graphics, Graphic Design и/или Blender.')
        elif tool == 'list_digests':
            if set(args) - {'after_run_id', 'limit'} or type(args.get('after_run_id', 0)) is not int or args.get('after_run_id', 0) < 0 or type(args.get('limit', 20)) is not int or not 1 <= args.get('limit', 20) <= 20:
                raise ValueError('Неверная страница истории сводок.')
        elif tool == 'get_digest':
            if set(args) != {'run_id'} or type(args['run_id']) is not int or args['run_id'] <= 0:
                raise ValueError('Неверный номер сводки.')
        elif args:
            raise ValueError('Эта операция не принимает параметры.')
        return args

    def _validate_digest(self, digest):
        require(isinstance(digest, dict))
        require(type(digest.get('run_id')) is int and digest['run_id'] > 0)
        require(isinstance(digest.get('text'), str) and len(digest['text']) <= 30000)
        require(type(digest.get('partial')) is bool)
        require(number(digest.get('generated_at')))
        require(number(digest.get('window_start')) and number(digest.get('window_end')) and digest['window_start'] <= digest['window_end'])
        questions = digest.get('questions')
        require(isinstance(questions, list) and len(questions) <= self.policy['output_policy']['max_questions'])
        ids = set()
        for q in questions:
            require(isinstance(q, dict) and type(q.get('question_id')) is int and q['question_id'] > 0)
            source = q.get('source')
            require(source in SOURCES)
            key = (source, q['question_id'])
            require(key not in ids)
            ids.add(key)
            require(isinstance(q.get('title'), str) and len(q['title']) <= 500)
            require(isinstance(q.get('excerpt', ''), str) and len(q.get('excerpt', '')) <= 4000)
            require(type(q.get('answer_count')) is int and q['answer_count'] >= 0)
            require(number(q.get('created_at')))
            require(isinstance(q.get('tags'), list) and len(q['tags']) <= 10 and all(isinstance(t, str) and len(t) <= 100 for t in q['tags']))
            url = urlsplit(q.get('url', ''))
            path = '/questions/' + str(q['question_id'])
            require(url.scheme == 'https' and url.netloc == source + '.stackexchange.com' and (url.path == path or url.path.startswith(path + '/')))
        require(type(digest.get('question_count')) is int and digest['question_count'] == len(questions))
        require(type(digest.get('unanswered_count')) is int and digest['unanswered_count'] == sum(q['answer_count'] == 0 for q in questions))
        tags = Counter(t for q in questions for t in set(q['tags']))
        require(digest.get('top_tags') == [{'tag': t, 'count': n} for t, n in sorted(tags.items(), key=lambda item: (-item[1], item[0]))[:10]])
        counts, statuses = digest.get('source_counts'), digest.get('source_status')
        require(isinstance(counts, dict) and isinstance(statuses, dict) and bool(statuses))
        require(set(counts) == set(statuses) and all(s in SOURCES for s in statuses))
        actual = Counter(q['source'] for q in questions)
        require(set(actual) <= set(statuses))
        require(all(type(n) is int and n == actual[s] for s, n in counts.items()))
        require(all(v in ('ok', 'partial', 'error', 'skipped_backoff') for v in statuses.values()))
        require(digest['partial'] == any(v != 'ok' for v in statuses.values()))
        errors = digest.get('source_errors', {})
        require(isinstance(errors, dict) and set(errors) <= set(statuses))
        require(all(statuses[s] == 'error' and isinstance(v, str) and len(v) <= 500 for s,v in errors.items()))
        return digest

    def _output(self, value, tool='get_digest_state', args=None):
        require(isinstance(value, dict))
        require(len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')) <= self.policy['output_policy']['max_bytes'])
        if tool == 'get_digest':
            self._validate_digest(value.get('digest'))
            require(value['digest']['run_id'] == args['run_id'])
        elif tool == 'list_digests':
            digests = value.get('digests')
            require(isinstance(digests, list) and len(digests) <= args.get('limit', 20))
            require(type(value.get('has_more')) is bool and type(value.get('next_cursor')) is int)
            cursor = args.get('after_run_id', 0)
            for d in digests:
                self._validate_digest(d)
                require(d['run_id'] > cursor)
                cursor = d['run_id']
            require(value['next_cursor'] == cursor and (not value['has_more'] or bool(digests)))
        else:
            s, runs = value.get('schedule'), value.get('runs')
            require(isinstance(s, dict) and type(s.get('enabled')) is bool)
            require(s.get('mode') == 'cron' and s.get('timezone') == 'Asia/Omsk' and s.get('hours') == [0,6,12,18] and s.get('window_hours') == 24)
            require(isinstance(s.get('sources'), list) and bool(s['sources']) and all(x in SOURCES for x in s['sources']))
            require(s.get('next_due') is None or number(s['next_due']))
            require(s.get('backoff_until') is None or number(s['backoff_until']))
            require(isinstance(runs, list) and len(runs) <= self.policy['output_policy']['max_runs'])
            for r in runs:
                require(isinstance(r, dict) and r.get('status') in ('running','success','partial','empty','error','interrupted'))
                require(type(r.get('id')) is int and r['id'] > 0 and number(r.get('started_at')))
            if value.get('latest') is not None:
                self._validate_digest(value['latest'])
            if tool in ('collect_digest', 'run_scheduled_digest'):
                require(type(value.get('executed')) is bool)
        return value

    def call(self, tool, args=None):
        try:
            checked = self._input(tool, {} if args is None else args)
        except (ValueError, TypeError):
            self._audit(tool, 'rejected', 'not_run', 'rejected')
            raise ValueError('Некорректные параметры операции сводки.') from None
        try:
            value = asyncio.run(self._request(tool, checked))
        except Exception as exc:
            self._audit(tool, 'passed', 'not_run', 'transport_error')
            raise DigestError('MCP сводок недоступен. Проверьте сервис и SSH-туннель. Cron продолжает работать на капсуле.') from exc
        try:
            result = self._output(value, tool, checked)
        except (DigestError, ValueError, TypeError, KeyError) as exc:
            self._audit(tool, 'passed', 'rejected', 'rejected')
            raise DigestError('Результат MCP отклонён output policy; новая сводка не выдана.') from exc
        self._audit(tool, 'passed', 'passed', 'ok')
        return result

    async def _request(self, tool, args):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        async with asyncio.timeout(120 if tool in ('collect_digest', 'run_scheduled_digest') else 20):
            async with streamable_http_client(self.url) as (read, write, *_):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, arguments=args)
        if result.is_error:
            raise DigestError('MCP tool error')
        return result.structured_content

    def cache_digests(self, digests, cursor=None):
        with self.lock, sqlite3.connect(self.path) as db:
            for d in digests:
                self._validate_digest(d)
                payload = json.dumps(d, ensure_ascii=False, sort_keys=True)
                old = db.execute('SELECT payload FROM mirror WHERE run_id=?', (d['run_id'],)).fetchone()
                if old and old[0] != payload:
                    raise DigestError('Сохранённая сводка изменилась на сервере. Подмена контекста остановлена.')
                db.execute('INSERT OR IGNORE INTO mirror VALUES(?,?)', (d['run_id'], payload))
            if cursor is not None:
                db.execute('UPDATE mirror_state SET cursor=? WHERE id=1', (cursor,))

    def sync(self):
        with self.lock:
            state = self.call('get_digest_state')
            # Commit cursor with each page, never derive it from latest.
            with sqlite3.connect(self.path) as db:
                cursor = db.execute('SELECT cursor FROM mirror_state WHERE id=1').fetchone()[0]
            for _ in range(100):
                page = self.call('list_digests', dict(after_run_id=cursor, limit=5))
                self.cache_digests(page['digests'], page['next_cursor'])
                cursor = page['next_cursor']
                if not page['has_more']:
                    break
            with sqlite3.connect(self.path) as db:
                db.execute('UPDATE mirror_state SET state=? WHERE id=1', (json.dumps(state, ensure_ascii=False),))
            return state

    def cached_state(self):
        with sqlite3.connect(self.path) as db:
            row = db.execute('SELECT state FROM mirror_state WHERE id=1').fetchone()
        return json.loads(row[0]) if row[0] else None

    def cached_digests(self):
        with sqlite3.connect(self.path) as db:
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM mirror ORDER BY run_id')]

    def get_cached(self, run_id):
        if type(run_id) is not int or run_id <= 0:
            raise ValueError('Выберите сводку из истории.')
        with sqlite3.connect(self.path) as db:
            row = db.execute('SELECT payload FROM mirror WHERE run_id=?', (run_id,)).fetchone()
        if not row:
            raise ValueError('Сводка ещё не загружена. Обновите историю.')
        return json.loads(row[0])

    def selected(self, profile_id):
        with sqlite3.connect(self.path) as db:
            row = db.execute('SELECT run_id FROM digest_selection WHERE profile_id=?', (profile_id,)).fetchone()
        return row[0] if row else None

    def select(self, profile_id, run_id):
        self.get_cached(run_id)
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO digest_selection VALUES(?,?) ON CONFLICT(profile_id) DO UPDATE SET run_id=excluded.run_id', (profile_id, run_id))
