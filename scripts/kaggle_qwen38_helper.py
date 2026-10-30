"""Install over the verified, already loaded Qwen3.8 27B Q6_K engine.

Run ``jobrunner_qwen38_complete = install(engine, tokenizer, identity)`` in
the model's kernel. This neither loads weights nor creates a public endpoint.
Identity must come from the SHA-verified two-GPU loader; context is 4096 and
thinking is disabled. The 8B helper remains a separate supported protocol.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

MODEL = "unsloth/Qwen3.8-27B-GGUF:UD-Q6_K"
CONTEXT_TOKENS = 4096
MODEL_SHA256 = "c9c206812fbe4ac7b76a729e25928b63f2ae89d37f69da7a71c20aec763cd436"


def install(engine: Any, tokenizer: Any, identity: dict[str, Any]) -> Callable[[str], str]:
    if (
        identity.get("model") != MODEL
        or type(identity.get("gpu_count")) is not int
        or identity["gpu_count"] != 2
        or identity.get("model_sha256") != MODEL_SHA256
        or engine.n_ctx() != CONTEXT_TOKENS
        or engine.metadata.get("general.name") != "Qwen3.8-27B"
        or not callable(getattr(tokenizer, "apply_chat_template", None))
    ):
        raise ValueError("Expected the pinned Q6 model on two GPUs with a 4096-token context")

    def complete(payload_json: str) -> str:
        result: dict[str, Any] = {"model": MODEL, "status": "error"}
        try:
            payload = json.loads(payload_json)
            budget = payload["max_tokens"]
            temperature = payload["temperature"]
            if (
                payload["model"] != MODEL
                or type(budget) is not int
                or not 1 <= budget <= 1024
                or type(temperature) not in (int, float)
                or not 0 <= temperature <= 2
                or not isinstance(payload["system"], str)
                or not isinstance(payload["user"], str)
            ):
                return json.dumps(result)
            prompt = tokenizer.apply_chat_template(
                [
                    {"role": "system", "content": payload["system"]},
                    {"role": "user", "content": payload["user"]},
                ],
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
            token_ids = engine.tokenize(prompt.encode(), add_bos=False, special=True)
            if len(token_ids) + budget > CONTEXT_TOKENS:
                return json.dumps(dict(result, status="too_long"))
            engine.reset()
            output = engine.create_completion(
                prompt=token_ids,
                max_tokens=budget,
                temperature=temperature,
                top_p=0.8,
                top_k=20,
                repeat_penalty=1.0,
                seed=42,
                stop=["<|im_end|>", "<|endoftext|>"],
                echo=False,
            )
            choice = output["choices"][0]
            if choice["finish_reason"] == "length":
                return json.dumps(dict(result, status="truncated"))
            text = choice["text"].strip()
            if text:
                result.update(
                    status="ok",
                    text=text,
                    prompt_tokens=output["usage"]["prompt_tokens"],
                    completion_tokens=output["usage"]["completion_tokens"],
                )
        except Exception:
            # Exceptions can contain a private question. Return a fixed status only.
            pass
        return json.dumps(result)

    def health() -> dict[str, Any]:
        return {
            "state": "ready" if engine.n_ctx() == CONTEXT_TOKENS else "model_not_ready",
            "protocol": 1,
            "model": MODEL,
            "context_tokens": CONTEXT_TOKENS,
            "gpu_count": 2,
            "thinking": False,
            "model_sha256": identity["model_sha256"],
        }

    complete.jobrunner_health = health  # type: ignore[attr-defined]
    return complete
