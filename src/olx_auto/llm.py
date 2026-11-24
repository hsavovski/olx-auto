"""OpenRouter chat completions through the openai SDK (spec §5), with the cost of every call.

Tests replace `LLM.chat` with scripted replies; nothing here is mocked at the HTTP level.
"""
import json
import re
from dataclasses import dataclass, field

import openai

BASE_URL = "https://openrouter.ai/api/v1"


class LLMError(Exception):
    pass


@dataclass
class Reply:
    message: dict  # assistant message, ready to append to the conversation
    model: str
    usage: dict = field(default_factory=dict)  # prompt_tokens, completion_tokens, cost (USD)
    citations: list[dict] = field(default_factory=list)  # [{url, title}] from OpenRouter's web plugin

    @property
    def cost(self) -> float:
        return float(self.usage.get("cost") or 0.0)

    @property
    def tool_calls(self) -> list[dict]:
        return self.message.get("tool_calls") or []

    @property
    def text(self) -> str:
        return self.message.get("content") or ""


class LLM:
    def __init__(self, api_key: str):
        if not api_key:
            raise LLMError("OPENROUTER_API_KEY is not set in .env")
        self.client = openai.OpenAI(base_url=BASE_URL, api_key=api_key, timeout=180, max_retries=2)

    def chat(self, model: str, messages: list[dict], *, extra_body: dict | None = None, **kwargs) -> Reply:
        try:
            r = self.client.chat.completions.create(
                model=model, messages=messages,
                # OpenRouter returns usage.cost in USD
                extra_body={"usage": {"include": True}, **(extra_body or {})},
                **kwargs)
        except openai.APIError as e:
            raise LLMError(f"{model}: {e}") from e
        if not r.choices:
            raise LLMError(f"{model}: empty response")
        m = r.choices[0].message
        message = {"role": "assistant", "content": m.content or ""}
        if m.tool_calls:
            message["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                for c in m.tool_calls]
        usage = r.usage.model_dump() if r.usage else {}
        citations = [{"url": a.url_citation.url, "title": a.url_citation.title}
                     for a in m.annotations or [] if a.type == "url_citation"]
        return Reply(message, r.model or model,
                     {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "cost")}, citations)


def parse_json(text: str) -> dict:
    """Models sometimes wrap JSON in ``` fences or add a sentence around it."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        value = json.loads(match.group(0) if match else text)
    except ValueError:
        raise LLMError(f"Expected JSON, got: {text[:300]}") from None
    if not isinstance(value, dict):
        raise LLMError(f"Expected a JSON object, got: {text[:300]}")
    return value
