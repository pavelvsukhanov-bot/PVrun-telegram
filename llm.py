"""Groq chat-completion helper shared by the monthly report and the daily plan."""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

MODEL = "openai/gpt-oss-120b"


def ask_groq(prompt: str, max_tokens: int) -> str:
    """Returns the model's answer. Raises RuntimeError if Groq is unavailable."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY не задан")
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            # gpt-oss spends part of this budget on hidden reasoning
            "max_tokens": max_tokens,
        },
        timeout=90,
    )
    if not resp.ok:
        raise RuntimeError(f"Groq API недоступен ({resp.status_code}): {resp.text[:150]}")
    text = (resp.json()["choices"][0]["message"].get("content") or "").strip()
    if not text:
        raise RuntimeError("Groq вернул пустой ответ")
    return text
