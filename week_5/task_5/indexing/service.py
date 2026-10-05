"""Two-strategy indexing orchestration and publish-after-accept lifecycle."""
from dataclasses import asdict
import json
import statistics
import time
import uuid
from pathlib import Path

from .chunking import chunk_document
from .config import load_config
from .corpus import CorpusError, read_corpus


def _usage(raw, rate):
    if not isinstance(raw, dict):
        return None, None, None
    prompt = raw.get("prompt_tokens")
    total = raw.get("total_tokens")
    if type(prompt) is not int or type(total) is not int or prompt < 0 or total != prompt:
        return None, None, None
    return prompt, total, prompt * rate / 1_000_000


def _safe_usage(raw):
    try:
        return json.loads(json.dumps(raw, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError):
        return None


def _cached_tokens(raw, input_tokens):
    if not isinstance(raw, dict):
        return None, "not_reported"
    detail = raw.get("prompt_tokens_details")
    if not isinstance(detail, dict) or "cached_tokens" not in detail:
        return None, "not_reported"
    value = detail["cached_tokens"]
    if type(value) is not int or value < 0 or input_tokens is None or value > input_tokens:
        return None, "invalid"
    return value, "reported"


class IndexingService:
    def __init__(self, task_root, store, embedder, config=None):
        self.task_root = Path(task_root)
        self.store = store
        self.embedder = embedder
        self.config = config if config is not None else load_config()

    def inspect(self):
        return read_corpus(self.task_root).summary

    def build(self, force=False):
        if type(force) is not bool:
            raise ValueError("force must be boolean")
        config = self.config.validate()
        try:
            corpus = read_corpus(self.task_root)
        except CorpusError as error:
            self.store.reject_input(error.metadata["policy_code"], config.contract, str(error))
            raise
        if corpus.summary["characters"] < config.min_corpus_characters:
            self.store.reject_input("insufficient_corpus", config.contract, "Corpus is below the declared 30-page minimum")
            raise CorpusError("Corpus is below the declared 30-page minimum", "insufficient_corpus")
        run_id = self.store.claim_run(corpus.summary, config.contract, force=force)
        if run_id is None:
            return {**self.summary(), "status": "complete", "reused": True}
        metrics = {}
        try:
            self.store.save_documents(run_id, [asdict(doc) for doc in corpus.documents])
            for strategy in ("fixed", "structural"):
                start = time.monotonic()
                chunks = [chunk for doc in corpus.documents for chunk in chunk_document(doc, strategy, config.chunk_size, config.overlap)]
                if not chunks:
                    raise ValueError("No indexable text in corpus")
                for offset in range(0, len(chunks), config.batch_size):
                    batch = chunks[offset:offset + config.batch_size]
                    record = {"batch_id": uuid.uuid4().hex, "run_id": run_id, "strategy": strategy,
                              "status": "pending", "api_called": True,
                              "input_policy": {"passed": True, "code": "accepted"},
                              "output_policy": {"passed": None, "code": "pending"}, "policy": "pending",
                              "input_tokens": None, "output_tokens": 0, "output_tokens_status": "not_applicable",
                              "cached_input_tokens": None, "cached_input_tokens_status": "not_reported",
                              "reasoning_tokens": None, "reasoning_tokens_status": "not_applicable",
                              "total_tokens": None, "cost_usd": None, "provider_usage": None,
                              "model": config.model,
                              "tariff": config.tariff, "elapsed_seconds": None}
                    self.store.start_batch(record)
                    begin = time.monotonic()
                    try:
                        result = self.embedder.embed([chunk["text"] for chunk in batch])
                    except Exception as error:
                        metadata = getattr(error, "metadata", {})
                        if not isinstance(metadata, dict):
                            metadata = {}
                        record["api_called"] = metadata.get("api_called", True)
                        raw_usage = metadata.get("usage")
                        record["provider_usage"] = _safe_usage(raw_usage)
                        record["input_tokens"], record["total_tokens"], record["cost_usd"] = _usage(raw_usage, config.input_usd_per_million)
                        record["cached_input_tokens"], record["cached_input_tokens_status"] = _cached_tokens(raw_usage, record["input_tokens"])
                        if record["cached_input_tokens_status"] == "invalid" or (record["cached_input_tokens"] or 0) > 0:
                            record["cost_usd"] = None
                        record["status"] = "rejected" if record["api_called"] else "input_rejected"
                        record["input_policy"] = {"passed": bool(record["api_called"]), "code": "accepted" if record["api_called"] else "invalid_batch_or_key"}
                        record["output_policy"] = {"passed": False if record["api_called"] else None,
                                                   "code": "provider_or_transport_failure" if record["api_called"] else "not_called"}
                        record["policy"] = record["status"]
                        record["elapsed_seconds"] = time.monotonic() - begin
                        self.store.finish_batch(record)
                        raise ValueError(str(error)) from error
                    raw_usage = result.get("usage")
                    record["provider_usage"] = _safe_usage(raw_usage)
                    record["input_tokens"], record["total_tokens"], record["cost_usd"] = _usage(raw_usage, config.input_usd_per_million)
                    record["cached_input_tokens"], record["cached_input_tokens_status"] = _cached_tokens(raw_usage, record["input_tokens"])
                    if record["cached_input_tokens_status"] == "invalid" or (record["cached_input_tokens"] or 0) > 0:
                        record["cost_usd"] = None
                    record["status"] = "complete"
                    record["output_policy"] = {"passed": True, "code": "accepted"}
                    record["policy"] = "accepted"
                    record["elapsed_seconds"] = time.monotonic() - begin
                    self.store.finish_batch(record)
                    complete = [{**chunk, "embedding": vector, "model": config.model, "dimensions": config.dimensions}
                                for chunk, vector in zip(batch, result["vectors"], strict=True)]
                    self.store.save_chunks(run_id, strategy, offset, complete)
                metrics[strategy] = self._metrics(chunks, corpus.summary["characters"], run_id, strategy, time.monotonic()-start)
            self.store.publish(run_id, metrics)
            return {**self.summary(), "reused": False}
        except Exception as error:
            self.store.fail_run(run_id, str(error))
            raise

    def _metrics(self, chunks, characters, run_id, strategy, elapsed):
        sizes = [len(c["text"]) for c in chunks]
        batches = [row for row in self.store.ledger() if row["run_id"] == run_id and row["strategy"] == strategy and row["api_called"]]
        complete = all(row["cost_usd"] is not None for row in batches)
        return {"chunk_count": len(chunks), "min_characters": min(sizes),
                "median_characters": statistics.median(sizes), "max_characters": max(sizes),
                "multi_section_chunks": sum(len(c["section"]) > 1 for c in chunks),
                "fallback_chunks": sum(bool(c["fallback"]) for c in chunks),
                "indexed_characters": sum(sizes),
                "repetition_factor": sum(sizes) / characters,
                "elapsed_seconds": elapsed,
                "input_tokens": sum(row["input_tokens"] for row in batches) if all(row["input_tokens"] is not None for row in batches) else None,
                "cost_usd": sum(row["cost_usd"] for row in batches if row["cost_usd"] is not None) if complete else None,
                "cost_complete": complete}

    def summary(self):
        return self.store.summary()

    def chunks(self, strategy, offset=0, limit=20):
        return self.store.chunks(strategy, offset, limit)

    def export(self):
        return self.store.export()
