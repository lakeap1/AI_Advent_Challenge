"""Single-request OpenAI embeddings boundary with strict vector validation."""
import math
import os

import httpx


class EmbeddingError(ValueError):
    def __init__(self, message, *, usage=None, api_called=True, actual_model=None,
                 invalid_output=False):
        super().__init__(message)
        self.metadata = {"usage": usage, "api_called": api_called,
                         "actual_model": actual_model, "invalid_output": invalid_output}


class OpenAIEmbedder:
    def __init__(self, config, api_key=None, client=None):
        self.config = config.validate()
        self._api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
        self._client = client if client is not None else httpx.Client(timeout=config.timeout_seconds)

    def embed(self, texts):
        if not isinstance(texts, (list, tuple)) or not texts or len(texts) > self.config.batch_size or any(not isinstance(t, str) or not t.strip() for t in texts):
            raise EmbeddingError("Invalid embedding input batch", api_called=False)
        if not self._api_key:
            raise EmbeddingError("OPENAI_API_KEY is required to build the index", api_called=False)
        body = {"model": self.config.model, "input": list(texts),
                "dimensions": self.config.dimensions, "encoding_format": "float"}
        try:
            response = self._client.post("https://api.openai.com/v1/embeddings", json=body,
                                         headers={"Authorization": f"Bearer {self._api_key}"},
                                         timeout=self.config.timeout_seconds)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise EmbeddingError("Embedding API request failed or returned invalid JSON") from error
        usage = payload.get("usage") if isinstance(payload, dict) else None
        actual_model = payload.get("model") if isinstance(payload, dict) and isinstance(payload.get("model"), str) else None
        if not isinstance(payload, dict) or payload.get("model") != self.config.model or not isinstance(payload.get("data"), list):
            raise EmbeddingError("Embedding API returned an invalid model or data", usage=usage,
                                 actual_model=actual_model, invalid_output=True)
        entries = payload["data"]
        if len(entries) != len(texts):
            raise EmbeddingError("Embedding API returned wrong vector count", usage=usage,
                                 actual_model=actual_model, invalid_output=True)
        ordered = [None] * len(texts)
        for entry in entries:
            if not isinstance(entry, dict) or type(entry.get("index")) is not int or not 0 <= entry["index"] < len(texts) or ordered[entry["index"]] is not None:
                raise EmbeddingError("Embedding API returned invalid indices", usage=usage,
                                     actual_model=actual_model, invalid_output=True)
            vector = entry.get("embedding")
            if (not isinstance(vector, list) or len(vector) != self.config.dimensions or
                any(type(number) not in (int, float) or not math.isfinite(number) for number in vector) or
                not any(number != 0 for number in vector)):
                raise EmbeddingError("Embedding API returned invalid vector", usage=usage,
                                     actual_model=actual_model, invalid_output=True)
            ordered[entry["index"]] = vector
        if any(value is None for value in ordered):
            raise EmbeddingError("Embedding API omitted an index", usage=usage,
                                 actual_model=actual_model, invalid_output=True)
        return {"vectors": ordered, "usage": usage, "model": payload["model"]}
