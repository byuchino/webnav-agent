#!/usr/bin/env python
"""Say hello to the local Gemma 4 served by llama-server over its OpenAI-compatible API."""
import os
import sys

from openai import OpenAI

BASE_URL = os.environ.get("LLM_BASE_URL", "http://192.168.254.26:1234/v1")
MODEL = os.environ.get("LLM_MODEL", "google/gemma-4-e4b")

client = OpenAI(base_url=BASE_URL, api_key=os.environ.get("LLM_API_KEY", "sk-no-key-required"))

try:
    served = [m.id for m in client.models.list().data]
    print(f"served models: {served}")
    if MODEL not in served:
        print(f"warning: {MODEL} not in the served list", file=sys.stderr)
except Exception as e:
    print(f"could not list models ({e})", file=sys.stderr)
model = MODEL

resp = client.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": "Hello! Who are you? Answer in one short sentence."}],
    max_tokens=256,
    temperature=0,
)

print(f"\nmodel: {resp.model}")
print(f"finish_reason: {resp.choices[0].finish_reason}")
print(f"reply: {resp.choices[0].message.content}")
