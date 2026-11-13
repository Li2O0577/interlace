"""Model services behind one small interface: ``chat(system, messages, tools, max_tokens)``.

An ``AgentNode`` takes any object with that method; the providers here are the ones that come with the
package. ``FakeProvider`` answers from a function, for tests and for trying a graph without a model service.
"""
from .providers import (AnthropicProvider, ChatResult, FakeProvider, OpenAICompatProvider, Provider, ProviderError,
                        build)

__all__ = ["AnthropicProvider", "ChatResult", "FakeProvider", "OpenAICompatProvider", "Provider", "ProviderError", "build"]
