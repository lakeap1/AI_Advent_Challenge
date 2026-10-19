"""Build the structural index with a restartable, model-bound document vector cache."""
import argparse
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from graphics.chunking import structural_chunks
from graphics.config import Settings
from graphics.retrieval import Retriever


class CachedIndexer(Retriever):
    """Cache only document embeddings; ordinary chat search uses Retriever directly."""
    def __init__(self, settings):
        super().__init__(settings)
        self.cache_path = settings.data_dir / 'document-vectors.sqlite3'
        self.cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.cache_path.is_symlink():
            raise ValueError('Unsafe vector cache path')
        self._digest = None
        self.cache_hits = self.embedded = 0
        with closing(sqlite3.connect(self.cache_path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector TEXT NOT NULL)')
            db.commit()

    def _model_digest(self):
        if self._digest is None:
            self._digest = super()._model_digest()
        return self._digest

    def _valid(self, vector):
        if (not isinstance(vector, list) or len(vector) != self.settings.embedding_dimensions
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)
                or not any(value != 0 for value in vector)):
            raise ValueError('Invalid cached or returned embedding vector')

    def _embed(self, texts, *, query):
        if query:
            return super()._embed(texts, query=True)
        identity = f'{self.settings.embedding_model}:{self._model_digest()}:{self.settings.embedding_dimensions}:'
        keys = [hashlib.sha256((identity + text).encode('utf-8')).hexdigest() for text in texts]
        vectors = [None] * len(texts)
        with closing(sqlite3.connect(self.cache_path)) as db:
            for index, key in enumerate(keys):
                row = db.execute('SELECT vector FROM vectors WHERE key=?', (key,)).fetchone()
                if row is not None:
                    vectors[index] = json.loads(row[0])
                    self._valid(vectors[index])
        missing = [index for index, value in enumerate(vectors) if value is None]
        receipt = {'prompt_eval_count': 0}
        if missing:
            fresh, receipt = super()._embed([texts[index] for index in missing], query=False)
            if len(fresh) != len(missing):
                raise ValueError('Embedding response count mismatch')
            for vector in fresh:
                self._valid(vector)
            with closing(sqlite3.connect(self.cache_path)) as db:
                for index, vector in zip(missing, fresh):
                    vectors[index] = vector
                    db.execute('INSERT OR REPLACE INTO vectors VALUES (?,?)', (keys[index], json.dumps(vector)))
                db.commit()
        hits = len(texts) - len(missing)
        self.cache_hits += hits
        self.embedded += len(missing)
        return vectors, {**receipt, 'cache_hits': hits, 'embedded': len(missing)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--warm-cache', action='store_true', help='Prepare available documents without publishing a partial index')
    parser.add_argument('--threads', type=int, choices=[1, 2, 4], default=4,
                        help='CPU threads for document indexing only; ordinary query search keeps its own setting')
    args = parser.parse_args()
    settings = replace(Settings.from_env(), embedding_threads=args.threads)
    indexer = CachedIndexer(settings)
    started = time.monotonic()
    if args.warm_cache:
        manifest = json.loads(indexer.manifest_path.read_text(encoding='utf-8'))
        chunks = []
        for source in manifest['sources']:
            path = indexer._source_path(source)
            if path.is_file():
                chunks.extend(structural_chunks(path.read_text(encoding='utf-8'), source))
        for offset in range(0, len(chunks), 16):
            _, receipt = indexer._embed([chunk['text'] for chunk in chunks[offset:offset + 16]], query=False)
            print(json.dumps({'prepared': min(offset + 16, len(chunks)), 'chunks': len(chunks), **receipt}), flush=True)
        result = {'mode': 'cache_only', 'index_published': False, 'chunks': len(chunks)}
    else:
        result = indexer.build_index()
        result['mode'] = 'complete_index'
    result.update(model=settings.embedding_model, model_digest=indexer._model_digest(),
                  dimensions=settings.embedding_dimensions, embedding_threads=settings.embedding_threads,
                  cache_hits=indexer.cache_hits, embedded=indexer.embedded,
                  elapsed_seconds=round(time.monotonic() - started, 3))
    target = settings.data_dir / ('cache-warmup.json' if args.warm_cache else 'index-build.json')
    target.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
