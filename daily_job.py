"""
Job diario: corre no GitHub Actions, gera 3 ideias e persiste em data/.

Requer variaveis de ambiente:
    GEMINI_API_KEY (ou OPENAI_API_KEY)
    LLM_PROVIDER   (default: gemini)
    LLM_MODEL      (default: gemini-2.5-flash ou gpt-4o-mini)
"""

from __future__ import annotations

import os
import sys

from radar_core import (
    aggregate_trends,
    default_sources,
    dedupe_against_history,
    generate_ideas,
    load_history,
    past_idea_names,
    persist_run,
)


def main() -> int:
    provider = os.getenv("LLM_PROVIDER", "gemini").lower()
    if provider == "gemini":
        api_key = os.getenv("GEMINI_API_KEY", "")
        model = os.getenv("LLM_MODEL", "gemini-2.5-flash")
    else:
        api_key = os.getenv("OPENAI_API_KEY", "")
        model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    if not api_key:
        print(f"[daily] Falta API key para provider '{provider}'.", file=sys.stderr)
        return 1

    print(f"[daily] provider={provider} model={model}")
    trends = aggregate_trends(default_sources())
    if not trends:
        print("[daily] Sem tendencias - a abortar.", file=sys.stderr)
        return 2
    print(f"[daily] {len(trends)} tendencias recolhidas.")

    history = load_history()
    avoid = past_idea_names(history, limit=60)
    print(f"[daily] A evitar {len(avoid)} nomes do historico.")

    ideas = generate_ideas(provider, trends, api_key, model, avoid_names=avoid)
    ideas = dedupe_against_history(ideas, history)
    if not ideas:
        print("[daily] Todas as ideias geradas eram duplicadas. A abortar.", file=sys.stderr)
        return 3

    run = persist_run(trends, ideas)
    print(f"[daily] Gravadas {len(ideas)} ideias para {run['date']}.")
    for i in ideas:
        print(f"  - [{i.theme}] {i.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
