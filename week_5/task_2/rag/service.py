"""One-shot plain or verified-index RAG requests with durable stage accounting."""
from dataclasses import asdict
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import time
import uuid

from agent.core import completed_text
from agent.transport import ResponsesTransport, TransportError
from agent.usage import estimate_cost, read_usage
from indexing.client import OpenAIEmbedder
from indexing.config import load_config as indexing_config

from .config import INSTRUCTIONS, load_config
from .retrieval import IndexError, read_index, rank, select_context
from .store import RagStore


_LABEL = re.compile(r"\[S([^\]]+)\]")


def _safe(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError):
        return None


def _record_text(value):
    """Keep rejected metadata encodable; validation still sees the original input."""
    if not isinstance(value, str):
        return ""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    return value


def _embedding_cost(usage, config):
    if not isinstance(usage, dict):
        return None, None
    incoming, total = usage.get("prompt_tokens"), usage.get("total_tokens")
    if type(incoming) is not int or type(total) is not int or incoming < 0 or total != incoming:
        return None, None
    detail = usage.get("prompt_tokens_details")
    if not isinstance(detail, dict):
        detail = {}
    cached = detail.get("cached_tokens")
    if cached is not None and (type(cached) is not int or cached < 0 or cached > incoming):
        return None, None
    if cached not in (None, 0):
        return {"input_tokens": incoming, "output_tokens": 0, "total_tokens": total,
                "cached_input_tokens": cached, "cached_input_tokens_status": "reported",
                "output_tokens_status": "not_applicable", "reasoning_tokens": None,
                "reasoning_tokens_status": "not_applicable"}, None
    cost = Decimal(incoming) * Decimal(config.embedding_input_usd_per_million) / Decimal(1_000_000)
    return {"input_tokens": incoming, "output_tokens": 0, "total_tokens": total,
            "cached_input_tokens": cached, "cached_input_tokens_status": "reported" if cached is not None else "not_reported",
            "output_tokens_status": "not_applicable", "reasoning_tokens": None,
            "reasoning_tokens_status": "not_applicable"}, format(cost.normalize(), "f")


class RagService:
    def __init__(self, task_root, data_dir, transport=None, embedder=None, config=None):
        self.task_root = Path(task_root)
        self.data_dir = Path(data_dir)
        self.config = (config or load_config()).validate()
        self.transport = transport or ResponsesTransport(os.environ.get("OPENAI_API_KEY"))
        self.embedder = embedder
        self.store = RagStore(self.data_dir)

    def close(self):
        self.store.close()

    def _new_record(self, question, mode, session_id, comparison_id):
        return {"request_id": str(uuid.uuid4()), "session_id": _record_text(session_id),
                "comparison_id": comparison_id, "question": _record_text(question),
                "mode": _record_text(mode), "status": "pending", "text": "", "code": "",
                "sources": [], "context": "", "usage": None, "cost_usd": None, "elapsed_seconds": None,
                "context_budget": {"method": self.config.context_budget_method,
                                   "limit_tokens": self.config.max_context_tokens, "used_utf8_bytes": 0},
                "input_policy": {"name": self.config.input_policy, "passed": None, "code": "pending"},
                "output_policy": {"name": self.config.output_policy, "passed": None, "code": "not_checked"},
                "model": self.config.model, "reasoning_effort": self.config.reasoning_effort,
                "service_tier": self.config.service_tier, "tariff": self.config.tariff, "stages": []}

    def _validation(self, question, mode, session_id):
        try:
            if not isinstance(session_id, str) or str(uuid.UUID(session_id)) != session_id.lower():
                return "invalid_session"
        except (ValueError, AttributeError):
            return "invalid_session"
        if mode not in ("plain", "rag"):
            return "invalid_mode"
        if not isinstance(question, str) or not question.strip():
            return "input_invalid"
        try:
            question.encode("utf-8")
        except UnicodeEncodeError:
            return "input_invalid"
        if len(question.strip()) > self.config.max_question_chars:
            return "input_too_long"
        return None

    def _stage(self, record, kind):
        stage = {"kind": kind, "status": "pending", "api_called": True, "model": self.config.embedding_model if kind == "query_embedding" else self.config.model,
                 "actual_model": None,
                 "service_tier": "standard" if kind == "query_embedding" else self.config.service_tier,
                 "tariff": self.config.tariff["embedding" if kind == "query_embedding" else "generation"],
                 "input_policy": {"passed": True, "code": "accepted"},
                 "output_policy": {"passed": None, "code": "pending"},
                 "provider_usage": None, "usage": None, "cost_usd": None, "elapsed_seconds": None}
        record["stages"].append(stage)
        self.store.save(record)
        return stage

    def _finish_stage(self, record, stage, status, code, start):
        stage["status"] = status
        stage["output_policy"] = {"passed": status == "ok", "code": code}
        stage["elapsed_seconds"] = time.monotonic() - start
        self.store.save(record)

    def _finish(self, record, status, text, code, start):
        record["status"], record["text"], record["code"] = status, text, code
        record["elapsed_seconds"] = time.monotonic() - start
        checked_output = any(stage["kind"] in ("generation", "query_embedding") for stage in record["stages"])
        record["output_policy"] = {"name": self.config.output_policy,
                                   "passed": status == "ok" if checked_output else None,
                                   "code": "accepted" if status == "ok" else code}
        calls = [stage for stage in record["stages"] if stage["api_called"]]
        if calls and all(stage["cost_usd"] is not None for stage in calls):
            record["cost_usd"] = float(sum(Decimal(stage["cost_usd"]) for stage in calls))
        self.store.save(record)
        return {key: record[key] for key in ("status", "text", "code", "request_id", "mode", "sources", "context", "context_budget", "usage", "cost_usd", "elapsed_seconds")}

    def _embedding(self, question, record):
        stage = self._stage(record, "query_embedding")
        start = time.monotonic()
        try:
            if self.embedder is None:
                self.embedder = OpenAIEmbedder(indexing_config())
            output = self.embedder.embed([question])
        except Exception as error:
            meta = getattr(error, "metadata", {})
            meta = meta if isinstance(meta, dict) else {}
            stage["api_called"] = meta.get("api_called", True) is not False
            stage["provider_usage"] = _safe(meta.get("usage"))
            stage["usage"], stage["cost_usd"] = _embedding_cost(meta.get("usage"), self.config)
            stage["actual_model"] = meta.get("actual_model") if isinstance(meta.get("actual_model"), str) else None
            if stage["actual_model"] != self.config.embedding_model or not stage["api_called"]:
                stage["cost_usd"] = None
            self._finish_stage(record, stage, "error", "embedding_failed", start)
            raise IndexError("Question embedding failed") from error
        raw = output.get("usage") if isinstance(output, dict) else None
        stage["provider_usage"] = _safe(raw)
        stage["usage"], stage["cost_usd"] = _embedding_cost(raw, self.config)
        stage["actual_model"] = output.get("model") if isinstance(output, dict) and isinstance(output.get("model"), str) else None
        if stage["actual_model"] != self.config.embedding_model:
            stage["cost_usd"] = None
        if not isinstance(output, dict) or output.get("model") != self.config.embedding_model or not isinstance(output.get("vectors"), list) or len(output["vectors"]) != 1:
            self._finish_stage(record, stage, "rejected", "invalid_embedding", start)
            raise IndexError("Question embedding was invalid")
        vector = output["vectors"][0]
        try:
            # The ranker checks dimensionality, finite coordinates and nonzero norm.
            rank([], vector, self.config)
        except (IndexError, TypeError) as error:
            self._finish_stage(record, stage, "rejected", "invalid_embedding", start)
            raise IndexError("Question embedding was invalid") from error
        self._finish_stage(record, stage, "ok", "accepted", start)
        return vector

    def _generation(self, question, record):
        content = question if record["mode"] == "plain" else f"Вопрос: {question}\n\nМатериалы для ответа:\n{record['context']}"
        payload = {"model": self.config.model, "instructions": INSTRUCTIONS,
                   "input": [{"role": "user", "content": content}],
                   "reasoning": {"effort": self.config.reasoning_effort},
                   "max_output_tokens": self.config.max_output_tokens, "store": False,
                   "truncation": "disabled", "service_tier": self.config.service_tier}
        record["prompt"] = payload
        stage = self._stage(record, "generation")
        start = time.monotonic()
        try:
            output = self.transport.create(payload, self.config.timeout_seconds)
        except TransportError as error:
            stage["api_called"] = error.request_started
            self._finish_stage(record, stage, "error", error.code, start)
            return "error", "Запрос модели не завершён.", error.code
        except Exception:
            self._finish_stage(record, stage, "error", "transport_failed", start)
            return "error", "Запрос модели не завершён.", "transport_failed"
        usage = read_usage(output)
        stage["provider_usage"] = _safe(output.get("usage") if isinstance(output, dict) else None)
        stage["usage"] = asdict(usage) if usage is not None else None
        stage["cost_usd"] = estimate_cost(output, usage, self.config.generation_config)
        record["usage"] = stage["usage"]
        checked = completed_text(output)
        if checked.status != "ok":
            self._finish_stage(record, stage, "rejected", checked.code, start)
            return "rejected", checked.text, checked.code
        valid = {source["label"] for source in record["sources"]}
        if any("S" + number not in valid for number in _LABEL.findall(checked.text)):
            self._finish_stage(record, stage, "rejected", "unknown_source", start)
            return "rejected", "Ответ содержит ссылку на непереданный источник.", "unknown_source"
        self._finish_stage(record, stage, "ok", "accepted", start)
        return "ok", checked.text, ""

    def _ask(self, question, mode, session_id, comparison_id=None):
        start = time.monotonic()
        record = self._new_record(question, mode, session_id, comparison_id)
        invalid = self._validation(question, mode, session_id)
        if invalid:
            record["input_policy"] = {"name": self.config.input_policy, "passed": False, "code": invalid}
            record["stages"].append({"kind": "input", "status": "rejected", "api_called": False,
                                     "input_policy": record["input_policy"], "output_policy": {"passed": None, "code": "not_called"},
                                     "cost_usd": None, "usage": None})
            return self._finish(record, "rejected", "Недопустимый вопрос, режим или идентификатор сессии.", invalid, start)
        question = question.strip()
        record["question"] = question
        record["input_policy"] = {"name": self.config.input_policy, "passed": True, "code": "accepted"}
        self.store.save(record)
        if mode == "rag":
            try:
                chunks = read_index(self.task_root, self.data_dir, self.config)
            except (IndexError, ValueError):
                return self._finish(record, "error", "Индекс отсутствует, устарел или повреждён.", "invalid_index", start)
            try:
                vector = self._embedding(question, record)
                record["sources"], record["context"] = select_context(rank(chunks, vector, self.config), self.config)
                if not record["sources"]:
                    raise IndexError("No source fits the declared context budget")
                record["context_budget"]["used_utf8_bytes"] = len(record["context"].encode("utf-8"))
                self.store.save(record)
            except (IndexError, ValueError, OSError):
                return self._finish(record, "error", "Не удалось получить проверенный контекст.", "retrieval_failed", start)
        status, text, code = self._generation(question, record)
        return self._finish(record, status, text, code, start)

    def ask(self, question, mode, session_id):
        return self._ask(question, mode, session_id)

    def compare(self, question, session_id):
        comparison_id = str(uuid.uuid4())
        plain = self._ask(question, "plain", session_id, comparison_id)
        if plain["status"] != "ok":
            return {"status": plain["status"], "plain": plain, "rag": None, "comparison_id": comparison_id}
        rag = self._ask(question, "rag", session_id, comparison_id)
        return {"status": rag["status"], "plain": plain, "rag": rag, "comparison_id": comparison_id}

    def state(self, session_id):
        if self._validation("x", "plain", session_id):
            raise ValueError("Invalid session ID")
        return self.store.state(session_id)

    def questions(self):
        path = self.task_root / "evaluation" / "questions.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def evaluation(self):
        path = self.data_dir / "evaluation.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
