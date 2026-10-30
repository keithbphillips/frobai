"""Minimal Ollama client (stdlib only)."""

import json
import urllib.error
import urllib.request


class LLMError(Exception):
    pass


class OllamaClient:
    def __init__(self, model=None, url="http://localhost:11434", timeout=120.0, num_ctx=16384):
        self.model = model
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.num_ctx = num_ctx
        self._think_supported = True

    def _request(self, path, body=None, timeout=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise LLMError("Ollama HTTP %d: %s" % (e.code, detail[:300])) from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise LLMError("cannot reach Ollama at %s (%s)" % (self.url, getattr(e, "reason", e))) from None
        except json.JSONDecodeError as e:
            raise LLMError("bad response from Ollama: %s" % e) from None

    def list_models(self, timeout=5.0):
        return self._request("/api/tags", timeout=timeout).get("models", [])

    def resolve_model(self):
        """Pick the configured model, or the smallest installed one."""
        models = self.list_models()
        if not models:
            raise LLMError("no Ollama models installed (try: ollama pull qwen3:4b)")
        names = {m["name"] for m in models}
        if self.model:
            if self.model not in names and self.model + ":latest" not in names:
                raise LLMError("model %r is not installed (have: %s)" % (self.model, ", ".join(sorted(names))))
            return self.model
        self.model = min(models, key=lambda m: m.get("size", 0))["name"]
        return self.model

    def chat_json(self, system, user, schema, temperature=0.2):
        if not self.model:
            self.resolve_model()
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "format": schema,
            "keep_alive": "30m",
            "options": {"temperature": temperature, "num_ctx": self.num_ctx},
        }
        if self._think_supported:
            body["think"] = False
        try:
            resp = self._request("/api/chat", body)
        except LLMError as e:
            if self._think_supported and "think" in str(e).lower():
                self._think_supported = False
                return self.chat_json(system, user, schema, temperature)
            raise
        content = resp.get("message", {}).get("content", "")
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            raise LLMError("model returned non-JSON output: %r" % content[:200]) from None

    def warm_up(self):
        """Load the model into memory so the first real request is fast."""
        if not self.model:
            self.resolve_model()
        self._request("/api/generate", {"model": self.model, "prompt": "", "keep_alive": "30m"})
