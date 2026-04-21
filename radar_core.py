"""
Nucleo partilhado do Radar de Ideias.

Contem toda a logica independente do Streamlit: agregacao de tendencias,
chamadas a LLMs e persistencia do historico. Usado tanto pelo `app.py`
(interface) como pelo `daily_job.py` (cron diaria via GitHub Actions).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import feedparser
except ImportError:
    feedparser = None

try:
    import requests
except ImportError:
    requests = None

try:
    import google.generativeai as genai
except ImportError:
    genai = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    from pytrends.request import TrendReq
except ImportError:
    TrendReq = None


DATA_DIR = Path(__file__).parent / "data"
HISTORY_PATH = DATA_DIR / "history.json"
LATEST_PATH = DATA_DIR / "latest.json"

HN_TOP_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
REDDIT_STARTUPS_RSS = "https://www.reddit.com/r/startups/hot/.rss"
PRODUCT_HUNT_RSS = "https://www.producthunt.com/feed"
INDIE_HACKERS_RSS = "https://www.indiehackers.com/feed.xml"

# Subreddits onde pessoas descrevem problemas concretos do dia-a-dia ou do
# pequeno negocio - contra-peso as bolhas tech do HN/r/startups.
EVERYDAY_SUBREDDITS = {
    "SomebodyMakeThis": "app requests literais",
    "AppIdeas": "briefs de apps",
    "smallbusiness": "dores de SMB",
    "productivity": "utilitarios pessoais",
    "personalfinance": "dinheiro/consumo",
}


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------


@dataclass
class Idea:
    name: str
    problem: str
    monetization: str
    theme: str = "Geral"
    trends_context: str = ""
    created_at: str = ""


@dataclass
class DailyRun:
    date: str
    trends: list[str]
    ideas: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tendencias
# ---------------------------------------------------------------------------


def _safe(label: str, fn, *args, **kwargs):
    """Log simples para o cron; em Streamlit a UI trata dos avisos."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        print(f"[trends] {label} falhou: {exc}")
        return []


def fetch_hacker_news(limit: int = 10) -> list[str]:
    if requests is None:
        raise RuntimeError("requests nao instalado.")
    ids = requests.get(HN_TOP_URL, timeout=10).json()[:limit]
    titles: list[str] = []
    for item_id in ids:
        data = requests.get(HN_ITEM_URL.format(id=item_id), timeout=10).json()
        if data and data.get("title"):
            titles.append(data["title"])
    return titles


def fetch_reddit_startups(limit: int = 10) -> list[str]:
    if feedparser is None:
        raise RuntimeError("feedparser nao instalado.")
    feed = feedparser.parse(REDDIT_STARTUPS_RSS)
    return [entry.title for entry in feed.entries[:limit]]


def fetch_product_hunt(limit: int = 10) -> list[str]:
    if feedparser is None:
        raise RuntimeError("feedparser nao instalado.")
    feed = feedparser.parse(PRODUCT_HUNT_RSS)
    return [entry.title for entry in feed.entries[:limit]]


def fetch_indie_hackers(limit: int = 10) -> list[str]:
    if feedparser is None:
        raise RuntimeError("feedparser nao instalado.")
    feed = feedparser.parse(INDIE_HACKERS_RSS)
    return [entry.title for entry in feed.entries[:limit]]


def fetch_everyday_problems(per_sub: int = 4) -> list[str]:
    """Combina multiplos subreddits focados em problemas utilitarios."""
    if feedparser is None:
        raise RuntimeError("feedparser nao instalado.")
    titles: list[str] = []
    for sub, label in EVERYDAY_SUBREDDITS.items():
        try:
            feed = feedparser.parse(f"https://www.reddit.com/r/{sub}/hot/.rss")
            for entry in feed.entries[:per_sub]:
                titles.append(f"[r/{sub}] {entry.title}")
        except Exception as exc:
            print(f"[trends] r/{sub} falhou: {exc}")
    return titles


def fetch_google_trends(limit: int = 10) -> list[str]:
    if TrendReq is None:
        return []
    pytrends = TrendReq(hl="en-US", tz=0)
    for geo in ("united_states", "portugal", "united_kingdom"):
        try:
            df = pytrends.trending_searches(pn=geo)
            return df[0].head(limit).tolist()
        except Exception:
            continue
    return []


def aggregate_trends(
    sources: dict[str, bool], per_source: int = 8, total_cap: int = 20
) -> list[str]:
    """Agrega tendencias. Prefixo entre parentesis indica a categoria."""
    buckets: list[list[str]] = []
    if sources.get("hacker_news"):
        buckets.append(
            [f"[tech] {t}" for t in _safe("hacker_news", fetch_hacker_news, per_source)]
        )
    if sources.get("reddit_startups"):
        buckets.append(
            [f"[startup] {t}" for t in _safe("reddit_startups", fetch_reddit_startups, per_source)]
        )
    if sources.get("product_hunt"):
        buckets.append(
            [f"[launch] {t}" for t in _safe("product_hunt", fetch_product_hunt, per_source)]
        )
    if sources.get("indie_hackers"):
        buckets.append(
            [f"[indie] {t}" for t in _safe("indie_hackers", fetch_indie_hackers, per_source)]
        )
    if sources.get("everyday_problems"):
        buckets.append(
            _safe("everyday_problems", fetch_everyday_problems, per_source // 2 or 3)
        )
    if sources.get("google_trends"):
        buckets.append(
            [f"[consumer] {t}" for t in _safe("google_trends", fetch_google_trends, per_source)]
        )

    # Intercala buckets por round-robin para garantir diversidade.
    collected: list[str] = []
    idx = 0
    while any(buckets) and len(collected) < total_cap * 2:
        bucket = buckets[idx % len(buckets)] if buckets else []
        if bucket:
            collected.append(bucket.pop(0))
        idx += 1
        if all(not b for b in buckets):
            break

    seen: set[str] = set()
    unique: list[str] = []
    for title in collected:
        key = title.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(title.strip())
    return unique[:total_cap]


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


PROMPT_TEMPLATE = """Analisa as seguintes tendencias capturadas hoje (cada linha
comeca com [categoria], onde as categorias podem ser: tech, startup, launch,
indie, r/<subreddit>, consumer):

{trends}

{avoid_block}

Gera {n} ideias de apps mobile ou webapps altamente monetizaveis.

Requisitos de DIVERSIDADE (criticos):
- NO MAXIMO 1 ideia pode ser uma meta-ferramenta de AI/LLM/developer-tools.
  As restantes devem resolver problemas utilitarios concretos de consumidores,
  pequenos negocios, nichos profissionais ou tarefas do dia-a-dia.
- Distribui as ideias por pelo menos {min_themes} temas diferentes.
- Prefere trends com prefixo [r/...] e [consumer] como inspiracao para as
  ideias nao-tech; usa [tech]/[indie] apenas como contexto macro.
- Evita duplicar padroes (ex: "AI coach para X" em varias ideias).

Cada ideia deve ter um "theme" de 1-3 palavras reutilizavel
(ex: "FinTech Consumer", "Small Business Ops", "Health Tracking",
"Parenting", "Productivity", "Creator Economy", "AI Dev Tools").

Retorna estritamente JSON valido:
{{
  "ideas": [
    {{"name": "...", "theme": "...", "problem": "...", "monetization": "..."}}
  ]
}}

Regras de formato:
- "name" curto e memoravel (max 4 palavras).
- "problem" em 1-2 frases concretas, descrevendo o utilizador alvo.
- "monetization" especifica (ex: "SaaS B2B 29USD/mes por utilizador",
  "Freemium + IAP 4.99USD", "Marketplace 10% fee").
- "theme" reutiliza labels existentes quando fizer sentido.
- NAO repitas ideias semelhantes as listadas em "evita ideias".
- Nao incluas texto fora do JSON.
"""


def _parse_ideas_payload(raw: str, trends_context: str) -> list[Idea]:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"Resposta sem JSON: {raw[:200]}")
    payload = json.loads(cleaned[start : end + 1])
    return [
        Idea(
            name=item["name"],
            problem=item["problem"],
            monetization=item["monetization"],
            theme=item.get("theme", "Geral"),
            trends_context=trends_context,
        )
        for item in payload.get("ideas", [])
    ]


def _build_prompt(trends: list[str], avoid_names: list[str], num_ideas: int = 6) -> str:
    if avoid_names:
        avoid_block = "Evita ideias semelhantes a estas ja geradas anteriormente:\n- " + "\n- ".join(
            avoid_names[:60]
        )
    else:
        avoid_block = ""
    min_themes = max(3, min(num_ideas - 1, 5))
    return PROMPT_TEMPLATE.format(
        trends="\n- " + "\n- ".join(trends),
        avoid_block=avoid_block,
        n=num_ideas,
        min_themes=min_themes,
    )


def _extract_retry_delay(exc: Exception, default: float = 60.0) -> float:
    """Extrai segundos de espera sugeridos pelo erro (Gemini/OpenAI)."""
    msg = str(exc)
    m = re.search(r"retry in ([0-9]+(?:\.[0-9]+)?)s", msg) or re.search(
        r"retry_delay[^0-9]*seconds:\s*([0-9]+)", msg
    )
    if m:
        return float(m.group(1))
    return default


def _is_rate_limit(exc: Exception) -> bool:
    name = type(exc).__name__
    msg = str(exc).lower()
    return (
        "resourceexhausted" in name.lower()
        or "ratelimit" in name.lower()
        or "429" in msg
        or "quota" in msg
        or "rate limit" in msg
    )


def _call_with_retry(fn, *, max_attempts: int = 3, cap_seconds: float = 75.0):
    """Tenta executar fn(); se apanhar 429, espera o delay sugerido e repete."""
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if not _is_rate_limit(exc) or attempt == max_attempts:
                raise
            delay = min(_extract_retry_delay(exc, default=60.0) + 2, cap_seconds)
            print(
                f"[llm] Rate limit (tentativa {attempt}/{max_attempts}) - a esperar {delay:.0f}s"
            )
            time.sleep(delay)


def generate_ideas_gemini(
    trends: list[str],
    api_key: str,
    model: str,
    avoid_names: list[str] | None = None,
    num_ideas: int = 6,
) -> list[Idea]:
    if genai is None:
        raise RuntimeError("google-generativeai nao instalado.")
    genai.configure(api_key=api_key)
    llm = genai.GenerativeModel(model)
    prompt = _build_prompt(trends, avoid_names or [], num_ideas=num_ideas)
    response = _call_with_retry(lambda: llm.generate_content(prompt))
    return _parse_ideas_payload(response.text, trends_context="\n".join(trends))


def generate_ideas_openai(
    trends: list[str],
    api_key: str,
    model: str,
    avoid_names: list[str] | None = None,
    num_ideas: int = 6,
) -> list[Idea]:
    if OpenAI is None:
        raise RuntimeError("openai nao instalado.")
    client = OpenAI(api_key=api_key)
    prompt = _build_prompt(trends, avoid_names or [], num_ideas=num_ideas)
    response = _call_with_retry(
        lambda: client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
    )
    return _parse_ideas_payload(
        response.choices[0].message.content,
        trends_context="\n".join(trends),
    )


def generate_ideas(
    provider: str,
    trends: list[str],
    api_key: str,
    model: str,
    avoid_names: list[str] | None = None,
    num_ideas: int = 6,
) -> list[Idea]:
    if provider.lower() == "gemini":
        return generate_ideas_gemini(trends, api_key, model, avoid_names, num_ideas)
    return generate_ideas_openai(trends, api_key, model, avoid_names, num_ideas)


# ---------------------------------------------------------------------------
# Historico (JSON versionado no repo) e dedup
# ---------------------------------------------------------------------------


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_history() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def load_latest() -> dict | None:
    if not LATEST_PATH.exists():
        return None
    try:
        return json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def past_idea_names(history: list[dict], limit: int = 60) -> list[str]:
    names: list[str] = []
    for run in reversed(history):  # mais recentes primeiro
        for idea in run.get("ideas", []):
            names.append(idea["name"])
    return names[:limit]


def _normalize(name: str) -> str:
    return " ".join(name.lower().strip().split())


def dedupe_against_history(ideas: list[Idea], history: list[dict]) -> list[Idea]:
    """Remove ideias cujo nome normalizado ja exista no historico."""
    known = {_normalize(i["name"]) for run in history for i in run.get("ideas", [])}
    return [idea for idea in ideas if _normalize(idea.name) not in known]


def persist_run(trends: list[str], ideas: list[Idea]) -> dict:
    """Grava uma run no historico e como latest.json. Devolve o payload."""
    _ensure_data_dir()
    now = datetime.now(timezone.utc)
    run = {
        "date": now.date().isoformat(),
        "generated_at": now.isoformat(timespec="seconds"),
        "trends": trends,
        "ideas": [asdict(i) for i in ideas],
    }
    history = load_history()
    history.append(run)
    HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    LATEST_PATH.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    return run


def cluster_by_theme(ideas: list[dict]) -> dict[str, list[dict]]:
    clusters: dict[str, list[dict]] = {}
    for idea in ideas:
        clusters.setdefault(idea.get("theme", "Geral"), []).append(idea)
    return dict(sorted(clusters.items(), key=lambda kv: -len(kv[1])))


def default_sources() -> dict[str, bool]:
    return {
        "hacker_news": True,
        "reddit_startups": True,
        "indie_hackers": True,
        "everyday_problems": True,
        "product_hunt": False,
        "google_trends": False,
    }
