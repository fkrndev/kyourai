"""Multi-provider model adapters — unified interface for all LLM providers.

Wraps pydantic-ai's provider system with:
  - Provider name → model resolution (e.g. "anthropic:claude-3.5-sonnet")
  - API key management (env vars + config)
  - Provider-specific defaults (timeouts, max_tokens, temperature)
  - Fallback chains (try provider A, fall back to B on failure)
  - Local model support (Ollama, LM Studio)

Supported providers:
  - openai       (gpt-4o, gpt-4o-mini, o1, o3, ...)
  - anthropic    (claude-3.5-sonnet, claude-3.5-haiku, ...)
  - google       (gemini-2.0-flash, gemini-1.5-pro, ...)
  - bedrock      (anthropic.claude-3-5-sonnet-20241022-v2:0, ...)
  - ollama       (llama3.2, qwen2.5, deepseek-r1, ...)
  - groq         (llama-3.3-70b, mixtral-8x7b, ...)
  - mistral      (mistral-large, codestral, ...)

Usage:
  from kyourai.providers import resolve_model
  model = resolve_model("anthropic:claude-3.5-sonnet")
  # → returns a pydantic-ai Model instance configured for Anthropic
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ProviderConfig:
    """Configuration for a single LLM provider."""
    name: str
    env_key: str  # environment variable for API key
    default_model: str
    base_url_env: str | None = None  # optional base URL env var
    max_tokens_default: int = 4096
    timeout_default: float = 120.0
    description: str = ""


# Registry of known providers
_PROVIDERS: dict[str, ProviderConfig] = {
    "openai": ProviderConfig(
        name="openai",
        env_key="OPENAI_API_KEY",
        default_model="gpt-4o",
        description="OpenAI GPT models",
    ),
    "anthropic": ProviderConfig(
        name="anthropic",
        env_key="ANTHROPIC_API_KEY",
        default_model="claude-3-5-sonnet-latest",
        max_tokens_default=8192,
        description="Anthropic Claude models",
    ),
    "google": ProviderConfig(
        name="google",
        env_key="GOOGLE_API_KEY",
        default_model="gemini-2.0-flash",
        description="Google Gemini models",
    ),
    "bedrock": ProviderConfig(
        name="bedrock",
        env_key="AWS_REGION",  # uses AWS credentials
        default_model="anthropic.claude-3-5-sonnet-20241022-v2:0",
        timeout_default=300.0,
        description="AWS Bedrock (requires AWS credentials)",
    ),
    "ollama": ProviderConfig(
        name="ollama",
        env_key="OLLAMA_HOST",  # not an API key, but host URL
        base_url_env="OLLAMA_HOST",
        default_model="llama3.2",
        timeout_default=300.0,
        description="Ollama local models (no API key needed)",
    ),
    "groq": ProviderConfig(
        name="groq",
        env_key="GROQ_API_KEY",
        default_model="llama-3.3-70b-versatile",
        description="Groq fast inference",
    ),
    "mistral": ProviderConfig(
        name="mistral",
        env_key="MISTRAL_API_KEY",
        default_model="mistral-large-latest",
        description="Mistral AI models",
    ),
}


# ---------------------------------------------------------------------------
# Model string parsing
# ---------------------------------------------------------------------------

def parse_model_string(model: str) -> tuple[str, str]:
    """Parse a model string into (provider, model_name).

    Examples:
      "openai:gpt-4o" → ("openai", "gpt-4o")
      "anthropic:claude-3.5-sonnet" → ("anthropic", "claude-3.5-sonnet")
      "ollama:llama3.2" → ("ollama", "llama3.2")
      "gpt-4o" → ("openai", "gpt-4o")  # default provider
    """
    if ":" in model:
        provider, model_name = model.split(":", 1)
        return provider.strip().lower(), model_name.strip()
    # No provider prefix — assume openai
    return "openai", model


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

def resolve_model(model: str, **kwargs: Any) -> Any:
    """Resolve a model string into a pydantic-ai Model instance.

    Args:
        model: Model string (e.g. "openai:gpt-4o", "anthropic:claude-3.5-sonnet")
        **kwargs: Extra arguments passed to the provider

    Returns:
        A pydantic-ai Model instance

    Raises:
        ValueError: If provider is unknown or API key is missing
    """
    provider_name, model_name = parse_model_string(model)
    config = _PROVIDERS.get(provider_name)

    if not config:
        # Unknown provider — try to pass through to pydantic-ai
        logger.warning("Unknown provider '%s', passing through to pydantic-ai", provider_name)
        from pydantic_ai.models import infer_model
        return infer_model(model)

    # Check API key (except for ollama which doesn't need one)
    if provider_name != "ollama" and provider_name != "bedrock":
        api_key = os.environ.get(config.env_key)
        if not api_key:
            raise ValueError(
                f"Provider '{provider_name}' requires {config.env_key} environment variable. "
                f"Set it with: export {config.env_key}='your-key-here'"
            )

    # Resolve via provider-specific logic
    return _create_model(provider_name, model_name, config, **kwargs)


def _create_model(
    provider_name: str,
    model_name: str,
    config: ProviderConfig,
    **kwargs: Any,
) -> Any:
    """Create a pydantic-ai Model instance for a specific provider."""

    if provider_name == "openai":
        from pydantic_ai.providers.openai import OpenAIProvider
        from pydantic_ai.models.openai import OpenAIModel
        provider = OpenAIProvider(api_key=os.environ.get(config.env_key))
        return OpenAIModel(model_name, provider=provider)

    elif provider_name == "anthropic":
        from pydantic_ai.providers.anthropic import AnthropicProvider
        from pydantic_ai.models.anthropic import AnthropicModel
        provider = AnthropicProvider(api_key=os.environ.get(config.env_key))
        return AnthropicModel(model_name, provider=provider)

    elif provider_name == "google":
        from pydantic_ai.providers.google import GoogleProvider
        from pydantic_ai.models.google import GoogleModel
        provider = GoogleProvider(api_key=os.environ.get(config.env_key))
        return GoogleModel(model_name, provider=provider)

    elif provider_name == "bedrock":
        from pydantic_ai.providers.bedrock import BedrockProvider
        from pydantic_ai.models.bedrock import BedrockModel
        provider = BedrockProvider()
        return BedrockModel(model_name, provider=provider)

    elif provider_name == "ollama":
        from pydantic_ai.providers.ollama import OllamaProvider
        from pydantic_ai.models.openai import OpenAIModel
        # Ollama uses OpenAI-compatible API
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        provider = OllamaProvider(base_url=host)
        return OpenAIModel(model_name, provider=provider)

    elif provider_name == "groq":
        from pydantic_ai.providers.groq import GroqProvider
        from pydantic_ai.models.openai import OpenAIModel
        provider = GroqProvider(api_key=os.environ.get(config.env_key))
        return OpenAIModel(model_name, provider=provider)

    elif provider_name == "mistral":
        from pydantic_ai.providers.mistral import MistralProvider
        from pydantic_ai.models.openai import OpenAIModel
        provider = MistralProvider(api_key=os.environ.get(config.env_key))
        return OpenAIModel(model_name, provider=provider)

    else:
        raise ValueError(f"Unsupported provider: {provider_name}")


# ---------------------------------------------------------------------------
# Fallback chain — try providers in order until one works
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class FallbackResult:
    """Result of a fallback chain attempt."""
    model: Any
    provider: str
    model_name: str
    success: bool = True
    error: str | None = None


async def resolve_with_fallback(
    models: list[str],
    **kwargs: Any,
) -> FallbackResult:
    """Try to resolve models in order, falling back on failure.

    Args:
        models: List of model strings to try (e.g. ["openai:gpt-4o", "anthropic:claude-3.5-sonnet"])
        **kwargs: Extra arguments for resolve_model

    Returns:
        FallbackResult with the first successful model or the last error
    """
    last_error: str | None = None

    for model_str in models:
        try:
            model = resolve_model(model_str, **kwargs)
            provider, model_name = parse_model_string(model_str)
            logger.info("Resolved model: %s:%s", provider, model_name)
            return FallbackResult(
                model=model,
                provider=provider,
                model_name=model_name,
            )
        except Exception as e:
            last_error = str(e)
            logger.warning("Failed to resolve %s: %s — trying fallback", model_str, e)

    return FallbackResult(
        model=None,
        provider="",
        model_name="",
        success=False,
        error=last_error or "All providers failed",
    )


# ---------------------------------------------------------------------------
# Provider info / introspection
# ---------------------------------------------------------------------------

def list_providers() -> list[dict[str, Any]]:
    """List all known providers with their status."""
    result: list[dict[str, Any]] = []
    for name, config in _PROVIDERS.items():
        has_key = bool(os.environ.get(config.env_key))
        result.append({
            "name": name,
            "env_key": config.env_key,
            "default_model": config.default_model,
            "has_key": has_key,
            "description": config.description,
        })
    return result


def get_provider_info(provider: str) -> dict[str, Any] | None:
    """Get info about a specific provider."""
    config = _PROVIDERS.get(provider)
    if not config:
        return None
    return {
        "name": config.name,
        "env_key": config.env_key,
        "default_model": config.default_model,
        "has_key": bool(os.environ.get(config.env_key)),
        "description": config.description,
    }
