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
    # Campos de acionabilidade (adicionados em 2026-04: permitem medir se a
    # ideia e exequivel por um solo dev e com que evidencia). Defaults vazios
    # para compatibilidade com entradas antigas do historico.
    evidence: str = ""
    mvp_scope: list[str] = field(default_factory=list)
    first_validation: str = ""
    effort: str = ""
    actionability_score: int = 0
    target_user: str = ""


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


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub(" ", text or "").replace("&#32;", " ").strip()


def fetch_everyday_problems(per_sub: int = 4) -> list[str]:
    """Combina multiplos subreddits focados em problemas utilitarios.

    Em subreddits como r/SomebodyMakeThis e r/AppIdeas, o sinal de dor esta
    no corpo do post, nao no titulo. Concatenamos titulo + primeira frase do
    summary (limitado) para o LLM receber a descricao real do problema.
    """
    if feedparser is None:
        raise RuntimeError("feedparser nao instalado.")
    items: list[str] = []
    for sub, _label in EVERYDAY_SUBREDDITS.items():
        try:
            feed = feedparser.parse(f"https://www.reddit.com/r/{sub}/hot/.rss")
            for entry in feed.entries[:per_sub]:
                title = entry.title.strip()
                body = _strip_html(getattr(entry, "summary", ""))
                # Primeiras ~280 chars do body; chega para captar a dor.
                snippet = " ".join(body.split())[:280]
                if snippet and snippet.lower() != title.lower():
                    items.append(f"[r/{sub}] {title} — {snippet}")
                else:
                    items.append(f"[r/{sub}] {title}")
        except Exception as exc:
            print(f"[trends] r/{sub} falhou: {exc}")
    return items


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
    """Agrega tendencias. Prefixo entre parentesis indica a categoria.

    Ordem de prioridade: problem-signals (r/SomebodyMakeThis, r/smallbusiness,
    r/AppIdeas) primeiro; depois [consumer] e [startup]; e [tech]/[indie]/
    [launch] apenas para contexto macro, capados a `tech_cap`. Isto evita que
    o LLM gere ideias tipo "app sobre noticia do HN" por falta de sinais de
    dor reais.
    """
    problem_buckets: list[list[str]] = []
    context_buckets: list[list[str]] = []

    if sources.get("everyday_problems"):
        problem_buckets.append(
            _safe("everyday_problems", fetch_everyday_problems, per_source // 2 or 3)
        )
    if sources.get("google_trends"):
        problem_buckets.append(
            [f"[consumer] {t}" for t in _safe("google_trends", fetch_google_trends, per_source)]
        )
    if sources.get("reddit_startups"):
        problem_buckets.append(
            [f"[startup] {t}" for t in _safe("reddit_startups", fetch_reddit_startups, per_source)]
        )

    if sources.get("hacker_news"):
        context_buckets.append(
            [f"[tech] {t}" for t in _safe("hacker_news", fetch_hacker_news, per_source)]
        )
    if sources.get("indie_hackers"):
        context_buckets.append(
            [f"[indie] {t}" for t in _safe("indie_hackers", fetch_indie_hackers, per_source)]
        )
    if sources.get("product_hunt"):
        context_buckets.append(
            [f"[launch] {t}" for t in _safe("product_hunt", fetch_product_hunt, per_source)]
        )

    def drain(buckets: list[list[str]], cap: int) -> list[str]:
        out: list[str] = []
        idx = 0
        while any(buckets) and len(out) < cap:
            bucket = buckets[idx % len(buckets)]
            if bucket:
                out.append(bucket.pop(0))
            idx += 1
            if all(not b for b in buckets):
                break
        return out

    # Problem-signals ocupam pelo menos 2/3 do pool; context preenche o resto.
    problem_cap = max(1, (total_cap * 2) // 3)
    context_cap = total_cap - problem_cap
    collected = drain(problem_buckets, problem_cap)
    # Se faltou problem-signal (RSS do Reddit caiu), reutiliza slots em context.
    short_by = problem_cap - len(collected)
    collected.extend(drain(context_buckets, context_cap + short_by))

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
comeca com [categoria], onde as categorias podem ser: r/<subreddit>, consumer,
startup, tech, indie, launch):

{trends}

{avoid_block}

Vais gerar {n} ideias de apps para um SOLO developer construir. O objectivo
e acionabilidade, nao criatividade. Cada ideia deve ser construivel em 1-2
semanas e validavel em menos de 1 dia.

REGRAS DE EXCLUSAO (rejeita mentalmente ideias que batam nestas):
- NAO gerar "app de noticias" nem ideias baseadas em um evento one-off
  (ex: aquisicao X, crise Y, lancamento Z). Problemas recorrentes apenas.
- NAO gerar ideias dependentes de hardware especializado que o utilizador
  final nao tem hoje (ex: "lentes microfluidicas", "sensor X caro").
- NAO gerar "coach de AI generico para <area>" nem meta-ferramentas de
  developer tools, a menos que a trend seja uma dor explicita e especifica.
- NAO gerar ideias que precisem de dados proprietarios, licencas medicas,
  parcerias com instituicoes ou regulacao pesada para o MVP.
- Se uma trend e apenas um titulo sem dor, IGNORA — nao forces uma ideia.

PRIORIDADES:
- Pelo menos 2/3 das ideias devem derivar de [r/...] ou [consumer].
- NO MAXIMO 1 ideia pode ser developer-tools ou meta-AI.
- Distribui por pelo menos {min_themes} temas diferentes.

Para CADA ideia, preenche rigorosamente este schema JSON:

{{
  "ideas": [
    {{
      "name": "...",
      "theme": "...",
      "target_user": "...",
      "problem": "...",
      "evidence": "...",
      "mvp_scope": ["feature 1", "feature 2", "feature 3"],
      "first_validation": "...",
      "effort": "S|M|L",
      "monetization": "...",
      "actionability_score": 1-5
    }}
  ]
}}

Instrucoes campo-a-campo:
- "name": max 4 palavras, memoravel.
- "theme": 1-3 palavras reutilizaveis (ex: "Small Business Ops",
  "Parenting", "FinTech Consumer", "Creator Economy", "Productivity").
- "target_user": perfil concreto em 6-12 palavras (ex: "Freelance lash
  artist com 20-50 clientes recorrentes"), nao "pessoas que gostam de X".
- "problem": 1-2 frases descrevendo a dor e com que frequencia acontece.
- "evidence": cita textualmente (<=180 chars) o trecho da trend que prova
  a dor, precedido pelo prefixo da categoria. Se nao houver evidencia
  concreta na lista, ESCREVE "weak signal" — o score deve refletir isso.
- "mvp_scope": array de EXACTAMENTE 3 bullets ultra-concretos (nao "AI",
  nao "dashboard completo"; tem de ser features que um solo dev entrega
  em 2 semanas com stack simples: Next.js/Supabase, Streamlit, Flutter+
  Firebase, no-code). Ex: "Form Google + Airtable para captar pedidos",
  "Calendario mensal com 2 lembretes por cliente", "Export CSV para SMS".
- "first_validation": UM teste concreto para o solo dev fazer em <1 dia,
  SEM escrever codigo. Ex: "Post em r/<sub> com mockup + link para
  waitlist; alvo: 25 emails em 48h". Deve incluir um threshold de sucesso.
- "effort": "S" = fim-de-semana, "M" = 1-2 semanas, "L" = 3-4 semanas.
  Ideias L so sao aceitaveis se o actionability_score >= 4.
- "monetization": especifica e realista para o nivel de dor descrito
  (ex: "SaaS B2B 19USD/mes/loja", "IAP 4.99USD one-off", "Marketplace 8%").
  NAO inventar "plano enterprise 1999USD/mes" sem base.
- "actionability_score": inteiro 1-5 segundo esta rubrica:
    5 = trend cita literalmente a dor + MVP 1 semana + canal de
        distribuicao obvio.
    4 = dor clara na trend + MVP curto + canal conhecido.
    3 = dor plausivel + MVP feasible mas sem canal obvio.
    2 = dor vaga OU MVP complexo.
    1 = especulativo.
  Se score < 3, NAO incluas a ideia — gera outra no lugar.

Formato de saida:
- JSON valido, UTF-8, SEM prefixo/sufixo, SEM markdown/code fences.
- NAO repitas nem re-nomeies ideias em "evita ideias".
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
    ideas: list[Idea] = []
    for item in payload.get("ideas", []):
        mvp = item.get("mvp_scope") or []
        if isinstance(mvp, str):
            mvp = [mvp]
        try:
            score = int(item.get("actionability_score", 0))
        except (TypeError, ValueError):
            score = 0
        ideas.append(
            Idea(
                name=item["name"],
                problem=item["problem"],
                monetization=item["monetization"],
                theme=item.get("theme", "Geral"),
                trends_context=trends_context,
                evidence=item.get("evidence", ""),
                mvp_scope=[str(x).strip() for x in mvp if str(x).strip()],
                first_validation=item.get("first_validation", ""),
                effort=item.get("effort", ""),
                actionability_score=score,
                target_user=item.get("target_user", ""),
            )
        )
    return ideas


def filter_actionable(ideas: list[Idea], min_score: int = 3) -> list[Idea]:
    """Remove ideias com score < min_score. Se o LLM nao preencheu score
    (dados antigos ou falha do modelo), mantem as ideias (score==0 == unknown)
    para nao perder tudo; so filtra quando existe sinal explicito."""
    return [i for i in ideas if i.actionability_score == 0 or i.actionability_score >= min_score]


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
    # Prioriza problem-signals; hacker_news/indie fornecem apenas contexto
    # macro e sao capados pelo aggregate_trends.
    return {
        "everyday_problems": True,
        "reddit_startups": True,
        "google_trends": True,
        "hacker_news": True,
        "indie_hackers": False,
        "product_hunt": False,
    }
