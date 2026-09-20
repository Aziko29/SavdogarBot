"""AI providers: Gemini (google-genai) and OpenAI-compatible APIs, with cached clients and error mapping."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import openai
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from openai import AsyncOpenAI
from pydantic import BaseModel

from ai.errors import (
    BlockedContentError,
    InvalidKeyError,
    InvalidResponseError,
    ModelNotSupportedError,
    ProviderError,
    QuotaExceededError,
    RequestTooLargeError,
    TransientError,
)
from utils import key_id, mask_key

logger = logging.getLogger("ai.providers")

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
_JSON_HINT = "\nRespond with a single valid JSON object only, without markdown fences."
_MAX_DETAIL = 300

_GEMINI_KEY_HINTS = ("api key not valid", "api_key_invalid", "api key expired", "invalid api key")
_TOO_LARGE_HINTS = (
    "too large",
    "token limit",
    "exceeds the maximum number of tokens",
    "input token count",
    "payload size",
    "context length",
    "maximum context",
    "reduce the length",
)
_OPENAI_MODEL_HINTS = (
    "model_decommissioned",
    "decommissioned",
    "does not exist",
    "model not found",
    "not a valid model",
    "does not support image",
    "image input",
)
_BLOCKED_FINISH_REASONS = frozenset(
    {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT"}
)


@dataclass
class GenerateRequest:
    """Provider-independent generation request."""

    system_prompt: str
    user_text: str
    images: list[bytes] = field(default_factory=list)
    image_mime: str = "image/jpeg"
    temperature: float = 0.3
    json_schema: type[BaseModel] | None = None
    timeout_sec: float = 20
    alt: GenerateRequest | None = None  # lighter version used by the router on RequestTooLargeError


class Provider(Protocol):
    """A generation backend: one call = one (api_key, model) attempt."""

    name: str

    async def generate(self, api_key: str, model: str, req: GenerateRequest) -> str:
        """Return the raw JSON text produced by the model or raise a ProviderError subclass."""
        ...


# Client caches: clients are created lazily, once, and reused for every request.
_gemini_clients: dict[str, genai.Client] = {}
_oai_clients: dict[tuple[str, str], AsyncOpenAI] = {}


def _gemini_client(api_key: str) -> genai.Client:
    """Return the cached Gemini client for this key, creating it on first use."""
    kid = key_id(api_key)
    client = _gemini_clients.get(kid)
    if client is None:
        client = genai.Client(api_key=api_key)
        _gemini_clients[kid] = client
        logger.debug("Created Gemini client for key %s", mask_key(api_key))
    return client


def _oai_client(base_url: str, api_key: str) -> AsyncOpenAI:
    """Return the cached OpenAI-compatible client for (base_url, key), creating it on first use."""
    cache_key = (base_url, key_id(api_key))
    client = _oai_clients.get(cache_key)
    if client is None:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        _oai_clients[cache_key] = client
        logger.debug("Created OpenAI-compatible client for %s key %s", base_url, mask_key(api_key))
    return client


async def close_all_clients() -> None:
    """Close and forget every cached client (call once on shutdown)."""
    gemini = list(_gemini_clients.items())
    oai = list(_oai_clients.items())
    _gemini_clients.clear()
    _oai_clients.clear()
    for kid, gclient in gemini:
        try:
            aclose = getattr(gclient.aio, "aclose", None)
            if aclose is not None:
                await aclose()
            gclient.close()
        except Exception:  # shutdown cleanup must never abort the remaining closes
            logger.exception("Failed to close Gemini client %s", kid)
    for (base_url, kid), oclient in oai:
        try:
            await oclient.close()
        except Exception:  # shutdown cleanup must never abort the remaining closes
            logger.exception("Failed to close client %s/%s", base_url, kid)


def _clean_text(text: str) -> str:
    """Trim whitespace and strip a surrounding markdown code fence, if any."""
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    return match.group(1).strip() if match else stripped


# ----------------------------------------------------------------------------- Gemini


def _map_gemini_error(exc: genai_errors.APIError) -> ProviderError:
    """Translate a google.genai APIError into our error hierarchy."""
    code = int(exc.code or 0)
    status = str(exc.status or "").upper()
    message = str(exc.message or exc)
    low = message.lower()
    detail = f"gemini {code} {status}: {message[:_MAX_DETAIL]}"
    if code == 429 or status == "RESOURCE_EXHAUSTED":
        return QuotaExceededError(detail)
    if code == 404 or status == "NOT_FOUND":
        return ModelNotSupportedError(detail)
    if code in (401, 403) or status in ("UNAUTHENTICATED", "PERMISSION_DENIED") or any(
        hint in low for hint in _GEMINI_KEY_HINTS
    ):
        return InvalidKeyError(detail)
    if code == 413 or any(hint in low for hint in _TOO_LARGE_HINTS):
        return RequestTooLargeError(detail)
    if code >= 500 or code == 408 or status in ("UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL"):
        return TransientError(detail)
    return InvalidResponseError(detail)


def _extract_gemini_text(response: types.GenerateContentResponse) -> str:
    """Pull the JSON text out of a Gemini response; blocked or empty output raises BlockedContentError."""
    feedback = response.prompt_feedback
    if feedback is not None and feedback.block_reason:
        raise BlockedContentError(f"gemini prompt blocked: {feedback.block_reason}")
    candidates = response.candidates or []
    if not candidates:
        raise InvalidResponseError("gemini returned no candidates")
    candidate = candidates[0]
    reason = getattr(candidate.finish_reason, "name", str(candidate.finish_reason or "")).upper()
    if reason in _BLOCKED_FINISH_REASONS:
        raise BlockedContentError(f"gemini blocked the response: {reason}")
    parts = candidate.content.parts if candidate.content and candidate.content.parts else []
    text = "".join(p.text for p in parts if p.text and not getattr(p, "thought", False))
    if not text.strip():
        raise InvalidResponseError(f"gemini returned empty text (finish_reason={reason or 'n/a'})")
    return _clean_text(text)


class _GeminiProvider:
    """Google Gemini via the google-genai SDK (primary provider)."""

    name = "gemini"

    async def generate(self, api_key: str, model: str, req: GenerateRequest) -> str:
        """Run one Gemini attempt and return the raw JSON text."""
        client = _gemini_client(api_key)
        parts = [types.Part.from_bytes(data=img, mime_type=req.image_mime) for img in req.images]
        parts.append(types.Part.from_text(text=req.user_text))
        config_kwargs: dict[str, Any] = {
            "system_instruction": req.system_prompt,
            "temperature": req.temperature,
            "response_mime_type": "application/json",
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
        }
        if req.json_schema is not None:
            config_kwargs["response_schema"] = req.json_schema
        config = types.GenerateContentConfig(**config_kwargs)
        try:
            async with asyncio.timeout(req.timeout_sec):
                response = await client.aio.models.generate_content(
                    model=model,
                    contents=[types.Content(role="user", parts=parts)],
                    config=config,
                )
        except TimeoutError as exc:
            raise TransientError(f"gemini timeout after {req.timeout_sec}s") from exc
        except genai_errors.APIError as exc:
            raise _map_gemini_error(exc) from exc
        except (httpx.HTTPError, OSError) as exc:
            raise TransientError(f"gemini network error: {exc!r}") from exc
        return _extract_gemini_text(response)


# ----------------------------------------------------------------- OpenAI-compatible


def _status_text(exc: openai.APIStatusError) -> str:
    """Human-readable error text (message + body) of an OpenAI-style status error."""
    text = str(exc.message)
    body = str(exc.body) if exc.body else ""
    return text if not body or body in text else f"{text} {body}"


def _map_openai_status(name: str, exc: openai.APIStatusError) -> ProviderError:
    """Translate an OpenAI-compatible HTTP status error into our error hierarchy."""
    code = exc.status_code
    text = _status_text(exc)
    low = text.lower()
    detail = f"{name} {code}: {text[:_MAX_DETAIL]}"
    if code == 429:
        return QuotaExceededError(detail)
    if code == 413:
        return RequestTooLargeError(detail)
    if code in (404, 402):
        return ModelNotSupportedError(detail)
    if code in (401, 403):
        return InvalidKeyError(detail)
    if code >= 500 or code == 408:
        return TransientError(detail)
    if any(hint in low for hint in _TOO_LARGE_HINTS):
        return RequestTooLargeError(detail)
    if any(hint in low for hint in _OPENAI_MODEL_HINTS):
        return ModelNotSupportedError(detail)
    return InvalidResponseError(detail)


def _is_json_mode_rejection(exc: openai.APIStatusError) -> bool:
    """True if a 400/422 error looks like the provider refusing response_format / JSON mode."""
    low = _status_text(exc).lower()
    return "json" in low or "response_format" in low


def _build_messages(req: GenerateRequest) -> list[dict[str, Any]]:
    """Build chat messages; images become data: URLs, the JSON schema is appended to the system prompt."""
    system = req.system_prompt
    if req.json_schema is not None:
        schema = json.dumps(req.json_schema.model_json_schema(), separators=(",", ":"))
        system += f"\nThe JSON must match this JSON Schema:\n{schema}"
    if "json" not in system.lower():
        system += _JSON_HINT
    user_content: str | list[dict[str, Any]]
    if req.images:
        user_content = [{"type": "text", "text": req.user_text}]
        for image in req.images:
            encoded = base64.b64encode(image).decode("ascii")
            user_content.append(
                {"type": "image_url", "image_url": {"url": f"data:{req.image_mime};base64,{encoded}"}}
            )
    else:
        user_content = req.user_text
    return [{"role": "system", "content": system}, {"role": "user", "content": user_content}]


def _extract_openai_text(name: str, response: Any) -> str:
    """Pull the JSON text out of a chat completion; filtered or empty output raises."""
    choices = response.choices or []
    if not choices:
        raise InvalidResponseError(f"{name} returned no choices")
    choice = choices[0]
    if choice.finish_reason == "content_filter":
        raise BlockedContentError(f"{name} content filter triggered")
    message = choice.message
    text = message.content or ""
    if not text.strip():
        if getattr(message, "refusal", None):
            raise BlockedContentError(f"{name} refused: {str(message.refusal)[:_MAX_DETAIL]}")
        raise InvalidResponseError(f"{name} returned empty content")
    return _clean_text(text)


class _OpenAICompatProvider:
    """Any OpenAI-compatible chat-completions API (Groq, OpenRouter, OpenAI, Qwen, ...)."""

    def __init__(self, name: str, base_url: str) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")

    async def _create(
        self,
        client: AsyncOpenAI,
        model: str,
        messages: list[dict[str, Any]],
        req: GenerateRequest,
        json_mode: bool,
    ) -> Any:
        """Issue one chat.completions request."""
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": req.temperature,
            "timeout": req.timeout_sec,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return await client.chat.completions.create(**kwargs)

    async def generate(self, api_key: str, model: str, req: GenerateRequest) -> str:
        """Run one attempt (retrying once without JSON mode if rejected) and return the raw JSON text."""
        client = _oai_client(self.base_url, api_key)
        messages = _build_messages(req)
        try:
            async with asyncio.timeout(req.timeout_sec):
                try:
                    response = await self._create(client, model, messages, req, json_mode=True)
                except (openai.BadRequestError, openai.UnprocessableEntityError) as exc:
                    if not _is_json_mode_rejection(exc):
                        raise
                    logger.info("%s/%s rejected JSON mode; retrying without it", self.name, model)
                    response = await self._create(client, model, messages, req, json_mode=False)
        except TimeoutError as exc:
            raise TransientError(f"{self.name} timeout after {req.timeout_sec}s") from exc
        except openai.APIStatusError as exc:
            raise _map_openai_status(self.name, exc) from exc
        except openai.APIConnectionError as exc:  # includes APITimeoutError
            raise TransientError(f"{self.name} connection error: {exc!r}") from exc
        except (httpx.HTTPError, OSError) as exc:
            raise TransientError(f"{self.name} network error: {exc!r}") from exc
        except openai.OpenAIError as exc:
            raise InvalidResponseError(f"{self.name} SDK error: {exc!r}") from exc
        return _extract_openai_text(self.name, response)


GEMINI_PROVIDER: Provider = _GeminiProvider()


def make_openai_provider(name: str, base_url: str) -> Provider:
    """Create a provider for an OpenAI-compatible endpoint."""
    return _OpenAICompatProvider(name, base_url)
