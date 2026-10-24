"""Run this helper in the Kaggle notebook after loading Qwen3 8B in 4-bit mode.

Install with `jobrunner_complete = install(model, tokenizer)`. No listening
port, tunnel, model download, prompt logging, or notebook execution history.
Stopping/restarting the notebook invalidates the connection and helper.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

MODEL = "unsloth/Qwen3-8B-bnb-4bit"
CONTEXT_TOKENS = 4096


def install(model: Any, tokenizer: Any) -> Callable[[str], str]:
    import torch

    expected = {"model_type": "qwen3", "hidden_size": 4096, "num_hidden_layers": 36}
    if (
        not getattr(model, "is_loaded_in_4bit", False)
        or model.device.type != "cuda"
        or any(getattr(model.config, key, None) != value for key, value in expected.items())
    ):
        raise ValueError("Load Qwen3 8B in 4-bit mode on a CUDA GPU first.")

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
                or not isinstance(temperature, (int, float))
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
            inputs = tokenizer(prompt, return_tensors="pt", add_special_tokens=False)
            prompt_tokens = inputs["input_ids"].shape[1]
            if prompt_tokens + budget > CONTEXT_TOKENS:
                result["status"] = "too_long"
                return json.dumps(result)
            inputs = inputs.to(model.device)
            options = dict(
                max_new_tokens=budget,
                do_sample=temperature > 0,
                pad_token_id=tokenizer.eos_token_id,
            )
            if temperature > 0:
                options.update(temperature=temperature, top_p=0.8, top_k=20)
            with torch.inference_mode():
                output = model.generate(**inputs, **options)
            text = tokenizer.decode(output[0, prompt_tokens:], skip_special_tokens=True).strip()
            result.update(status="ok", text=text)
        except Exception:
            # A tokenizer/model exception can quote the private prompt. The
            # notebook and API get a status only; the owner checks readiness.
            pass
        return json.dumps(result)

    return complete
