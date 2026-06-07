"""Tiny OpenAI-compatible chat client (stdlib only, no `openai`/`httpx`).

Used for the three LLM touch-points, all OpenAI-compatible (DeepSeek):
  * synthesis  — the one strong call (cfg.synth_model, e.g. deepseek-v4-pro)
  * rerank     — optional cheap call (cfg.rerank_model, e.g. deepseek-v4-flash)
  * expansion  — optional cheap call (cfg.rerank_model)

Note (validated): deepseek-v4-* are *reasoning* models — with a small max_tokens they
spend the budget "thinking" and return empty content. So we default to a generous
max_tokens and the rerank/expand adapters add a "give the conclusion, don't over-reason"
system nudge.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Callable, Optional


def chat(
    system: str,
    user: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    max_tokens: int = 4096,
    temperature: float = 0.3,
    timeout: float = 180.0,
    proxy: Optional[str] = None,
) -> str:
    """One blocking chat completion. Returns the assistant text ("" on empty).

    `proxy` (e.g. "http://host:port") routes this request through a forward proxy
    via CONNECT — TLS stays end-to-end to the provider, so the proxy never sees the
    key. Used to reach geo-blocked providers (Groq/OpenAI) from the China server.
    """
    url = (base_url or "https://api.deepseek.com").rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": (
            ([{"role": "system", "content": system}] if system else [])
            + [{"role": "user", "content": user}]
        ),
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % (api_key or ""),
    }
    if proxy:
        # Explicit CONNECT tunnel. urllib's ProxyHandler does NOT reliably tunnel
        # HTTPS — it leaks the request to the origin IP (-> geo-block 403). This
        # mirrors `curl -x`: plain connect to the proxy, CONNECT to the target,
        # then TLS end-to-end to the target (the proxy never sees the key/body).
        import http.client
        import ssl as _ssl
        from urllib.parse import urlparse
        pu, tu = urlparse(proxy), urlparse(url)
        conn = http.client.HTTPSConnection(
            pu.hostname, pu.port or 8080, timeout=timeout,
            context=_ssl.create_default_context())
        conn.set_tunnel(tu.hostname, tu.port or 443)
        try:
            conn.request("POST", tu.path + (("?" + tu.query) if tu.query else ""),
                         body=body, headers=headers)
            r = conn.getresponse()
            raw = r.read().decode("utf-8")
            if r.status >= 400:
                raise RuntimeError("HTTP %s via proxy: %s" % (r.status, raw[:200]))
        finally:
            conn.close()
        data = json.loads(raw)
    else:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    return (data["choices"][0]["message"].get("content") or "")


def synth_caller(cfg) -> Callable[[str, str], str]:
    """(system, user) -> text using the strong synthesis model.

    Uses synth_* credentials/proxy when set (e.g. Groq gpt-oss-120b via the overseas
    relay), else falls back to the shared llm_* (DeepSeek) direct.
    """
    base = getattr(cfg, "synth_base_url", None) or cfg.llm_base_url
    key = getattr(cfg, "synth_api_key", None) or cfg.llm_api_key or ""
    proxy = getattr(cfg, "synth_proxy", None)
    model = getattr(cfg, "synth_model", "deepseek-v4-pro")

    def _c(system: str, user: str) -> str:
        try:
            return chat(system, user, model=model, base_url=base, api_key=key,
                        max_tokens=8000, temperature=0.3, proxy=proxy)
        except Exception:
            # Always produce a report: fall back to DeepSeek dsv4pro (China-direct,
            # no rate cap) if the primary fails — e.g. Groq free-tier TPM 413.
            if not proxy and base == cfg.llm_base_url:
                raise  # we're already on the fallback target; don't loop
            return chat(system, user, model="deepseek-v4-pro",
                        base_url=cfg.llm_base_url, api_key=cfg.llm_api_key or "",
                        max_tokens=8192, temperature=0.3)
    return _c


_RERANK_NUDGE = "先直接给出结论（JSON），不要展开长篇推理。"


def rerank_caller(cfg) -> Optional[Callable[[str], str]]:
    """(prompt) -> text using the cheap model. None if no key configured.

    Matches rank.select's single-arg llm_call contract; folds the reasoning-model
    nudge into the system prompt and uses a generous token budget.
    """
    if not cfg.llm_api_key:
        return None

    def _c(prompt: str) -> str:
        return chat(_RERANK_NUDGE, prompt, model=getattr(cfg, "rerank_model", "deepseek-v4-flash"),
                    base_url=cfg.llm_base_url, api_key=cfg.llm_api_key,
                    max_tokens=4096, temperature=0.2)
    return _c


def expand_caller(cfg) -> Optional[Callable[[str, str], str]]:
    """(system, user) -> text using the cheap model, for query expansion."""
    if not cfg.llm_api_key:
        return None

    def _c(system: str, user: str) -> str:
        return chat(system, user, model=getattr(cfg, "rerank_model", "deepseek-v4-flash"),
                    base_url=cfg.llm_base_url, api_key=cfg.llm_api_key,
                    max_tokens=2048, temperature=0.4)
    return _c
