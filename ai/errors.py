"""Exception hierarchy shared by AI providers and the router."""
from __future__ import annotations


class ProviderError(Exception):
    """Base class for every error raised by a single provider call."""


class QuotaExceededError(ProviderError):
    """Rate limit or quota exhausted for this key (HTTP 429 / RESOURCE_EXHAUSTED)."""


class ModelNotSupportedError(ProviderError):
    """The model does not exist or is not available to this key/account."""


class RequestTooLargeError(ProviderError):
    """The request exceeds the model's size or token limit."""


class InvalidKeyError(ProviderError):
    """The API key was rejected by the provider."""


class BlockedContentError(ProviderError):
    """The provider refused or filtered the content, or returned no text."""


class TransientError(ProviderError):
    """Temporary failure (5xx, timeout, network); worth retrying later."""


class InvalidResponseError(ProviderError):
    """The provider answered with an unusable response or an unclassified 4xx error."""


class AllProvidersFailed(Exception):
    """Raised by the router when no provider/key/model combination produced a result."""
