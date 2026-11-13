"""One shared Claude client."""
import os

import anthropic

from . import config

_client = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add it.")
        _client = anthropic.Anthropic(api_key=key, base_url=config.API_BASE_URL)
    return _client


def text_of(response) -> str:
    """Join every text block of a response (web search splits text across blocks)."""
    return "".join(b.text for b in response.content if b.type == "text")
