from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage


def _build_langchain_messages(
    prompt: str,
    history: Sequence[BaseMessage] | None = None,
    max_messages: int = 24,
) -> list[BaseMessage]:
    from langchain_core.messages import HumanMessage

    messages: list[BaseMessage] = []
    if history:
        messages.extend(list(history)[-max_messages:])
    messages.append(HumanMessage(content=prompt))
    return messages


def _message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        if parts:
            return "\n".join(parts)
    return str(content)


def _stream_from_llm(llm: Any, messages: Sequence[BaseMessage]) -> Iterator[str]:
    has_chunk = False
    try:
        for chunk in llm.stream(messages):
            text = _message_content_to_text(getattr(chunk, "content", ""))
            if text:
                has_chunk = True
                yield text
    except Exception:
        response = llm.invoke(messages)
        text = _message_content_to_text(getattr(response, "content", ""))
        if text:
            yield text
        return

    if not has_chunk:
        response = llm.invoke(messages)
        text = _message_content_to_text(getattr(response, "content", ""))
        if text:
            yield text


@dataclass
class LLMModel:
    """A discovered LLM model with provider info and call methods."""

    name: str
    provider: str = "ollama"
    endpoint: str | None = None
    api_key: str | None = None
    api_env: str | None = None

    @property
    def is_local(self) -> bool:
        return self.provider.lower() == "ollama"

    def call(
        self,
        prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        history: Sequence[BaseMessage] | None = None,
    ) -> str:
        provider = self.provider.lower()
        if provider == "ollama":
            return self._call_ollama(prompt, temperature, max_tokens, history=history)
        if provider in {"openai", "openai-compatible", "custom", "groq"}:
            return self._call_openai_compatible(prompt, temperature, max_tokens, history=history)
        if provider == "anthropic":
            return self._call_anthropic(prompt, temperature, max_tokens, history=history)
        if provider == "gemini":
            return self._call_gemini(prompt, temperature, max_tokens, history=history)
        raise ValueError(f"Unsupported provider: {self.provider!r}")

    def call_stream(
        self,
        prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        history: Sequence[BaseMessage] | None = None,
    ) -> Iterator[str]:
        provider = self.provider.lower()
        if provider == "ollama":
            return self._call_stream_ollama(prompt, temperature, max_tokens, history=history)
        if provider in {"openai", "openai-compatible", "custom", "groq"}:
            return self._call_stream_openai_compatible(
                prompt, temperature, max_tokens, history=history
            )
        if provider == "anthropic":
            return self._call_stream_anthropic(prompt, temperature, max_tokens, history=history)
        if provider == "gemini":
            return self._call_stream_gemini(prompt, temperature, max_tokens, history=history)
        raise ValueError(f"Unsupported provider: {self.provider!r}")

    def callStream(
        self,
        prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        history: Sequence[BaseMessage] | None = None,
    ) -> Iterator[str]:
        return self.call_stream(
            prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            history=history,
        )

    def _call_ollama(
        self,
        prompt: str,
        temperature: float,
        max_tokens: int,
        history: Sequence[BaseMessage] | None = None,
    ) -> str:
        from langchain_ollama import ChatOllama

        kwargs: dict[str, Any] = {}
        if self.endpoint:
            kwargs["base_url"] = self.endpoint
        llm = ChatOllama(
            model=self.name,
            temperature=temperature,
            num_predict=max_tokens,
            **kwargs,
        )
        response = llm.invoke(_build_langchain_messages(prompt, history=history))
        return _message_content_to_text(response.content)

    def _call_openai_compatible(
        self,
        prompt: str,
        temperature: float,
        max_tokens: int,
        history: Sequence[BaseMessage] | None = None,
    ) -> str:
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {
            "model": self.name,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.endpoint:
            kwargs["base_url"] = self.endpoint

        llm = ChatOpenAI(**kwargs)
        response = llm.invoke(_build_langchain_messages(prompt, history=history))
        return _message_content_to_text(response.content)

    def _call_anthropic(
        self,
        prompt: str,
        temperature: float,
        max_tokens: int,
        history: Sequence[BaseMessage] | None = None,
    ) -> str:
        from langchain_anthropic import ChatAnthropic

        kwargs: dict[str, Any] = {
            "model": self.name,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.endpoint:
            kwargs["base_url"] = self.endpoint

        llm = ChatAnthropic(**kwargs)
        response = llm.invoke(_build_langchain_messages(prompt, history=history))
        return _message_content_to_text(response.content)

    def _call_gemini(
        self,
        prompt: str,
        temperature: float,
        max_tokens: int,
        history: Sequence[BaseMessage] | None = None,
    ) -> str:
        from langchain_google_genai import ChatGoogleGenerativeAI

        kwargs: dict[str, Any] = {
            "model": self.name,
            "temperature": temperature,
        }
        if self.api_key:
            kwargs["google_api_key"] = self.api_key
        if self.endpoint:
            kwargs["base_url"] = self.endpoint

        try:
            llm = ChatGoogleGenerativeAI(max_output_tokens=max_tokens, **kwargs)
        except TypeError:
            llm = ChatGoogleGenerativeAI(**kwargs)

        response = llm.invoke(_build_langchain_messages(prompt, history=history))
        return _message_content_to_text(response.content)

    def _call_stream_ollama(
        self,
        prompt: str,
        temperature: float,
        max_tokens: int,
        history: Sequence[BaseMessage] | None = None,
    ) -> Iterator[str]:
        from langchain_ollama import ChatOllama

        kwargs: dict[str, Any] = {}
        if self.endpoint:
            kwargs["base_url"] = self.endpoint
        llm = ChatOllama(
            model=self.name,
            temperature=temperature,
            num_predict=max_tokens,
            **kwargs,
        )
        yield from _stream_from_llm(llm, _build_langchain_messages(prompt, history=history))

    def _call_stream_openai_compatible(
        self,
        prompt: str,
        temperature: float,
        max_tokens: int,
        history: Sequence[BaseMessage] | None = None,
    ) -> Iterator[str]:
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {
            "model": self.name,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.endpoint:
            kwargs["base_url"] = self.endpoint

        llm = ChatOpenAI(**kwargs)
        yield from _stream_from_llm(llm, _build_langchain_messages(prompt, history=history))

    def _call_stream_anthropic(
        self,
        prompt: str,
        temperature: float,
        max_tokens: int,
        history: Sequence[BaseMessage] | None = None,
    ) -> Iterator[str]:
        from langchain_anthropic import ChatAnthropic

        kwargs: dict[str, Any] = {
            "model": self.name,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.endpoint:
            kwargs["base_url"] = self.endpoint

        llm = ChatAnthropic(**kwargs)
        yield from _stream_from_llm(llm, _build_langchain_messages(prompt, history=history))

    def _call_stream_gemini(
        self,
        prompt: str,
        temperature: float,
        max_tokens: int,
        history: Sequence[BaseMessage] | None = None,
    ) -> Iterator[str]:
        from langchain_google_genai import ChatGoogleGenerativeAI

        kwargs: dict[str, Any] = {
            "model": self.name,
            "temperature": temperature,
        }
        if self.api_key:
            kwargs["google_api_key"] = self.api_key
        if self.endpoint:
            kwargs["base_url"] = self.endpoint

        try:
            llm = ChatGoogleGenerativeAI(max_output_tokens=max_tokens, **kwargs)
        except TypeError:
            llm = ChatGoogleGenerativeAI(**kwargs)

        yield from _stream_from_llm(llm, _build_langchain_messages(prompt, history=history))

    def call_json(
        self,
        prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> Any:
        text = self.call(prompt=prompt, temperature=temperature, max_tokens=max_tokens)
        text = text.strip()
        print(text)
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        return json.loads(text)

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"LLMModel({self.name!r}, provider={self.provider!r})"


class LLMTools:
    """Minimal LLM helper that supports local models and common APIs."""

    def __init__(self, auto_discover: bool = True):
        self._models: dict[str, LLMModel] = {}
        if auto_discover:
            self._load_env_models()
            self._load_ollama_models()

    def add_model(
        self,
        name: str,
        provider: str = "custom",
        endpoint: str | None = None,
        api: str | None = None,
        api_env: str | None = None,
    ) -> LLMModel:
        if api is None:
            api = self._get_api_key(provider, api_env)
        model = LLMModel(
            name=name,
            provider=provider,
            endpoint=endpoint,
            api_key=api,
            api_env=api_env,
        )
        self._models[name] = model
        return model

    def get_models(self, preferred_models: list[str] | None = None) -> list[LLMModel]:
        models = list(self._models.values())
        if preferred_models:
            local_names = {m.name for m in models if m.is_local}

            def _match_score(model: LLMModel) -> tuple:
                is_preferred = any(
                    preferred.lower() in model.name.lower() for preferred in preferred_models
                )
                is_local = model.name in local_names
                if is_preferred and is_local:
                    return (0, model.name)
                if is_preferred:
                    return (1, model.name)
                if is_local:
                    return (2, model.name)
                return (3, model.name)

            models.sort(key=_match_score)
        else:
            models.sort(key=lambda model: model.name)
        return models

    def get_local_models(self, preferred_models: list[str] | None = None) -> list[LLMModel]:
        models = [model for model in self._models.values() if model.is_local]
        if preferred_models:

            def _match_score(model: LLMModel) -> tuple:
                is_preferred = any(
                    preferred.lower() in model.name.lower() for preferred in preferred_models
                )
                return (0 if is_preferred else 1, model.name)

            models.sort(key=_match_score)
        else:
            models.sort(key=lambda model: model.name)
        return models

    def _get_api_key(self, provider: str, api_env: str | None = None) -> str | None:
        if api_env:
            return os.getenv(api_env)
        mapping = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "google": "GOOGLE_API_KEY",
            "xai": "XAI_API_KEY",
            "groq": "GROQ_API_KEY",
            "openai-compatible": None,
            "custom": None,
        }
        env_name = mapping.get(provider)
        if env_name:
            return os.getenv(env_name)
        if provider == "gemini":
            return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        return None

    def _load_env_models(self) -> None:
        tasks: list[tuple[str, str | None, str, str, tuple]] = []

        openai_key = self._get_api_key("openai")
        if openai_key:
            endpoint = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE")
            base_url = endpoint or "https://api.openai.com/v1"
            tasks.append(
                (
                    "openai",
                    endpoint,
                    "OPENAI_API_KEY",
                    "openai",
                    (base_url, openai_key),
                )
            )

        anthropic_key = self._get_api_key("anthropic")
        if anthropic_key:
            endpoint = os.getenv("ANTHROPIC_BASE_URL")
            base_url = endpoint or "https://api.anthropic.com"
            tasks.append(
                (
                    "anthropic",
                    endpoint,
                    "ANTHROPIC_API_KEY",
                    "anthropic",
                    (base_url, anthropic_key),
                )
            )

        gemini_key = self._get_api_key("gemini")
        if gemini_key:
            endpoint = os.getenv("GEMINI_BASE_URL") or os.getenv("GOOGLE_API_BASE")
            api_env = "GEMINI_API_KEY" if os.getenv("GEMINI_API_KEY") else "GOOGLE_API_KEY"
            base_url = endpoint or "https://generativelanguage.googleapis.com"
            tasks.append(
                (
                    "gemini",
                    endpoint,
                    api_env,
                    "gemini",
                    (base_url, gemini_key),
                )
            )

        xai_key = self._get_api_key("xai")
        if xai_key:
            endpoint = os.getenv("XAI_BASE_URL", "https://api.x.ai/v1")
            tasks.append(
                (
                    "openai-compatible",
                    endpoint,
                    "XAI_API_KEY",
                    "openai",
                    (endpoint, xai_key),
                )
            )

        groq_key = self._get_api_key("groq")
        if groq_key:
            endpoint = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
            tasks.append(
                (
                    "groq",
                    endpoint,
                    "GROQ_API_KEY",
                    "openai",
                    (endpoint, groq_key),
                )
            )

        if not tasks:
            return

        def _run_list(kind: str, args: tuple) -> list[str]:
            if kind == "openai":
                return self._list_openai_models(*args)
            if kind == "anthropic":
                return self._list_anthropic_models(*args)
            if kind == "gemini":
                return self._list_gemini_models(*args)
            return []

        max_workers = min(6, len(tasks))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_run_list, kind, args): (provider, endpoint, api_env)
                for provider, endpoint, api_env, kind, args in tasks
            }
            for future in concurrent.futures.as_completed(futures):
                provider, endpoint, api_env = futures[future]
                names = future.result() or []
                for name in names:
                    self.add_model(
                        name,
                        provider=provider,
                        endpoint=endpoint,
                        api_env=api_env,
                    )

    def _load_ollama_models(self) -> None:
        for name in _get_ollama_models():
            self.add_model(name, provider="ollama")

    def _list_openai_models(self, base_url: str, api_key: str) -> list[str]:
        import requests

        url = f"{base_url.rstrip('/')}/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            resp = requests.get(url, headers=headers, timeout=60)
            resp.raise_for_status()
        except requests.RequestException:
            return []
        data = resp.json()
        return [
            model["id"]
            for model in data.get("data", [])
            if isinstance(model, dict) and model.get("id")
        ]

    def _list_anthropic_models(self, base_url: str, api_key: str) -> list[str]:
        import requests

        url = f"{base_url.rstrip('/')}/v1/models"
        headers = {
            "x-api-key": api_key,
            "Anthropic-Version": "2023-06-01",
        }
        try:
            resp = requests.get(url, headers=headers, timeout=60)
            resp.raise_for_status()
        except requests.RequestException:
            return []
        data = resp.json()
        return [
            model["id"]
            for model in data.get("data", [])
            if isinstance(model, dict) and model.get("id")
        ]

    def _list_gemini_models(self, base_url: str, api_key: str) -> list[str]:
        import requests

        url = f"{base_url.rstrip('/')}/v1beta/models?key={api_key}"
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
        except requests.RequestException:
            return []
        data = resp.json()
        names = []
        for model in data.get("models", []):
            if not isinstance(model, dict):
                continue
            name = model.get("name", "")
            if name.startswith("models/"):
                name = name.split("/", 1)[1]
            if name:
                names.append(name)
        return names


def _get_ollama_models() -> list[str]:
    """Return a list of locally available Ollama model names."""
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return []

    header = lines[0].lower()
    start_idx = 1 if header.startswith("name") else 0
    models = []
    for line in lines[start_idx:]:
        parts = line.split()
        if parts:
            models.append(parts[0])
    return models


_DEFAULT_LLM_TOOLS: LLMTools | None = None


def _get_default_llmtools() -> LLMTools:
    global _DEFAULT_LLM_TOOLS
    if _DEFAULT_LLM_TOOLS is None:
        _DEFAULT_LLM_TOOLS = LLMTools(auto_discover=True)
    return _DEFAULT_LLM_TOOLS


def get_models(preferred_models: list[str] | None = None) -> list[LLMModel]:
    return _get_default_llmtools().get_models(preferred_models=preferred_models)


def get_local_models(preferred_models: list[str] | None = None) -> list[LLMModel]:
    return _get_default_llmtools().get_local_models(preferred_models=preferred_models)
