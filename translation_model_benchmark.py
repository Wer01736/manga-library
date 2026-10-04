"""Fast offline translation gate used before manga image processing.

This script deliberately contains the Japanese samples in a UTF-8 source file
instead of an inline PowerShell command.  That prevents Windows code-page
conversion from corrupting the model input.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import torch
from opencc import OpenCC
from transformers import AutoModelForCausalLM, AutoTokenizer


MODEL_ID = "Qwen/Qwen2-1.5B-Instruct"
SAMPLES = [
    {"kind": "dialogue", "source": "はじめまして", "must_include": ["初次", "你好"]},
    {"kind": "narration", "source": "本日あなたの治験を担当致します", "must_include": ["今天", "今日", "試驗"]},
    {"kind": "number_unit", "source": "身長163cm、体重60㎏", "must_include": ["163", "60"]},
    {"kind": "sound_effect", "source": "ピシッ", "must_include": ["啪", "喀", "咔"]},
    {"kind": "long_sentence", "source": "佐久羽製薬所属研究医師の●美と申します", "must_include": ["佐久羽", "製藥", "醫"]},
]

SYSTEM_PROMPT = (
    "Translate one Japanese manga text into concise, natural Traditional "
    "Chinese used in Taiwan. Preserve numbers, names, tone, symbols and sound "
    "effects. Output only the translation, with no explanation or source text."
)


def has_japanese(text: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff]", text))


def main() -> int:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    started = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_ID,
        dtype=torch.float16,
        device_map="auto",
        local_files_only=True,
    )
    model.eval()
    converter = OpenCC("s2twp")
    tokenizer.padding_side = "left"
    rendered_prompts = []
    for sample in SAMPLES:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": sample["source"]},
        ]
        rendered_prompts.append(
            tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
        )
    inputs = tokenizer(rendered_prompts, return_tensors="pt", padding=True).to(model.device)
    input_length = inputs.input_ids.shape[1]
    with torch.inference_mode():
        output = model.generate(
            **inputs,
            max_new_tokens=64,
            do_sample=False,
            pad_token_id=tokenizer.eos_token_id,
        )
    translations = tokenizer.batch_decode(
        output[:, input_length:], skip_special_tokens=True
    )

    results = []
    for sample, translated in zip(SAMPLES, translations):
        translated = translated.strip().strip('"\'')
        translated = converter.convert(translated)
        checks = {
            "nonempty": bool(translated),
            "no_prompt_echo": "Translate one Japanese" not in translated,
            "no_japanese": not has_japanese(translated),
            "expected_content": any(
                token in translated for token in sample["must_include"]
            ),
        }
        results.append({**sample, "translation": translated, "checks": checks})

    payload = {
        "model": MODEL_ID,
        "device": str(model.device),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "passed": all(all(item["checks"].values()) for item in results),
        "results": results,
    }
    output_path = Path(__file__).with_name("translation_model_benchmark_result.json")
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
