#!/usr/bin/env python3
"""
Classify Korean service-robot fetch requests with a local Ollama model.

The LLM only decides fetch (four objects) or unknown. Stop is never decided
here; transcript_rules.is_stop_command runs first and handles it.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any
import urllib.request


DEFAULT_OLLAMA_URL = 'http://127.0.0.1:11434'
DEFAULT_MODEL = 'qwen3:4b-instruct'
PROMPT_PATH = Path(__file__).resolve().parent / 'prompts' / 'system_prompt.txt'

# The prompt lives in a text file so that edits show up as plain diffs and the
# file itself can be hashed when the classifier is frozen for evaluation.
SYSTEM_PROMPT = PROMPT_PATH.read_text(encoding='utf-8').rstrip('\n')

OUTPUT_SCHEMA: dict[str, Any] = {
    'type': 'object',
    'properties': {
        'action': {'type': 'string', 'enum': ['fetch', 'unknown']},
        'object': {
            'type': 'string',
            'enum': ['coke', 'vaseline', 'tissue', 'airpod', ''],
        },
    },
    'required': ['action', 'object'],
    'additionalProperties': False,
    'oneOf': [
        {
            'properties': {
                'action': {'const': 'fetch'},
                'object': {'const': 'coke'},
            }
        },
        {
            'properties': {
                'action': {'const': 'fetch'},
                'object': {'const': 'vaseline'},
            }
        },
        {
            'properties': {
                'action': {'const': 'fetch'},
                'object': {'const': 'tissue'},
            }
        },
        {
            'properties': {
                'action': {'const': 'fetch'},
                'object': {'const': 'airpod'},
            }
        },
        {
            'properties': {
                'action': {'const': 'unknown'},
                'object': {'const': ''},
            }
        },
    ],
}

ALLOWED_RESULTS = frozenset({
    ('fetch', 'coke'),
    ('fetch', 'vaseline'),
    ('fetch', 'tissue'),
    ('fetch', 'airpod'),
    ('unknown', ''),
})


@dataclass(frozen=True)
class ClassifierConfig:
    """Ollama connection settings; defaults match the frozen v2 behaviour."""

    ollama_url: str = DEFAULT_OLLAMA_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: float = 120.0
    keep_alive: str = '10m'

    @property
    def chat_url(self) -> str:
        return self.ollama_url.rstrip('/') + '/api/chat'

    @property
    def tags_url(self) -> str:
        return self.ollama_url.rstrip('/') + '/api/tags'


def validate_result(value: Any) -> dict[str, str]:
    """Return a canonical result or raise ValueError for anything off-schema."""
    if not isinstance(value, dict) or set(value) != {'action', 'object'}:
        raise ValueError(f'Invalid result structure: {value!r}')

    action = value.get('action')
    obj = value.get('object')
    if not isinstance(action, str) or not isinstance(obj, str):
        raise ValueError(f'Result values must be strings: {value!r}')
    if (action, obj) not in ALLOWED_RESULTS:
        raise ValueError(f'Invalid action/object combination: {value!r}')
    return {'action': action, 'object': obj}


def build_request_payload(text: str, config: ClassifierConfig) -> dict[str, Any]:
    """Build the Ollama /api/chat request body for one utterance."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError('text must be a non-empty string')
    return {
        'model': config.model,
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': text.strip()},
        ],
        'stream': False,
        'think': False,
        'format': OUTPUT_SCHEMA,
        'keep_alive': config.keep_alive,
        'options': {
            'temperature': 0,
            'seed': 0,
            'num_ctx': 2048,
            'num_predict': 32,
        },
    }


def classify_command_with_metrics(
    text: str,
    config: ClassifierConfig | None = None,
) -> tuple[dict[str, str], dict[str, float]]:
    """Classify one utterance and return the result with Ollama timings."""
    config = config or ClassifierConfig()
    payload = build_request_payload(text, config)
    request = urllib.request.Request(
        config.chat_url,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
    )

    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
        api_result = json.load(response)
    wall_seconds = time.perf_counter() - started

    content = api_result['message']['content']
    result = validate_result(json.loads(content))
    metrics = {
        'wall_seconds': wall_seconds,
        'total_seconds': api_result.get('total_duration', 0) / 1_000_000_000,
        'load_seconds': api_result.get('load_duration', 0) / 1_000_000_000,
        'prompt_eval_seconds': api_result.get('prompt_eval_duration', 0) / 1_000_000_000,
        'eval_seconds': api_result.get('eval_duration', 0) / 1_000_000_000,
    }
    return result, metrics


def classify_command(
    text: str,
    config: ClassifierConfig | None = None,
) -> dict[str, str]:
    """Classify one utterance into fetch or unknown (never stop)."""
    result, _ = classify_command_with_metrics(text, config)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('text', help='Natural-language command to classify')
    args = parser.parse_args()
    print(json.dumps(classify_command(args.text), ensure_ascii=False, separators=(',', ':')))


if __name__ == '__main__':
    main()
