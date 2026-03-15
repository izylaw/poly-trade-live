import re
import logging

import requests

logger = logging.getLogger("poly-trade")


class LLMClient:
    def __init__(self, base_url: str = "http://100.96.38.49:11434",
                 model: str = "qwen3.5:27b", timeout: int = 30,
                 api_key: str = "", context_size: int = 8192):
        self.base_url = base_url.rstrip("/").removesuffix("/v1")
        self.model = model
        self.timeout = timeout
        self.api_key = api_key
        self.context_size = context_size

    def complete(self, system: str, user: str, max_tokens: int = 1500) -> dict:
        """Send a prompt via OpenAI-compatible API and return parsed response.

        Returns {"content": str, "input_tokens": int, "output_tokens": int}
        Works with Ollama, Anthropic-compatible proxies, or any OpenAI-compatible endpoint.
        """
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "stream": False,
        }

        # Set context window for Ollama (ignored by non-Ollama providers)
        if self.context_size > 0:
            payload["options"] = {"num_ctx": self.context_size}

        logger.debug(f"LLM prompt:\n--- SYSTEM ---\n{system}\n--- USER ---\n{user}\n--- END ---")

        response = requests.post(
            f"{self.base_url}/v1/chat/completions",
            json=payload,
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()

        content = ""
        choices = data.get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content", "")
        content = self._strip_code_fences(content)

        usage = data.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)

        total = input_tokens + output_tokens
        ctx_warn = ""
        if self.context_size > 0 and total > self.context_size * 0.9:
            ctx_warn = f" | WARNING: near context limit ({total}/{self.context_size})"

        logger.info(
            f"LLM call: model={self.model} | "
            f"tokens={input_tokens}in/{output_tokens}out | "
            f"ctx={self.context_size}{ctx_warn}"
        )
        logger.debug(f"LLM response:\n{content}")

        return {
            "content": content,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        """Remove markdown code fences (```json ... ```) from response."""
        stripped = re.sub(r"^```(?:json)?\s*\n?", "", text.strip())
        stripped = re.sub(r"\n?```\s*$", "", stripped)
        return stripped.strip()
