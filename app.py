"""
Radar de Ideias de Apps
=======================

Dashboard Streamlit que agrega tendencias diarias (Hacker News, Reddit
/r/startups, Google Trends) e pede a um LLM (Gemini ou OpenAI) para
transformar os topicos mais quentes em 3 ideias de apps monetizaveis.

Correr:
    streamlit run app.py

Variaveis de ambiente (.env ou export):
    GEMINI_API_KEY=...    # recomendado (gratuito com quota)
    OPENAI_API_KEY=...    # alternativa
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Iterable

import feedparser
import requests
import streamlit as st

# LLMs sao importadas lazy para nao forcar a instalacao de ambas.
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


DB_PATH = "ideas.db"
HN_TOP_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
REDDIT_STARTUPS_RSS = "https://www.reddit.com/r/startups/hot/.rss"
PRODUCT_HUNT_RSS = "https://www.producthunt.com/feed"


# ---------------------------------------------------------------------------
# Modelos de dados e persistencia
# ---------------------------------------------------------------------------


@dataclass
class Idea:
    name: str
    problem: str
    monetization: str
    trends_context: str = ""
    created_at: str = ""


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                problem TEXT NOT NULL,
                monetization TEXT NOT NULL,
                trends_context TEXT,
                created_at TEXT NOT NULL
            )
            """
        )


def save_idea(idea: Idea) -> None:
    idea.created_at = datetime.utcnow().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO ideas (name, problem, monetization, trends_context, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (idea.name, idea.problem, idea.monetization, idea.trends_context, idea.created_at),
        )


def list_saved_ideas() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM ideas ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_idea(idea_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM ideas WHERE id = ?", (idea_id,))


# ---------------------------------------------------------------------------
# Agregadores de tendencias
# ---------------------------------------------------------------------------


def fetch_hacker_news(limit: int = 10) -> list[str]:
    """Busca os titulos dos top stories do Hacker News via API publica."""
    try:
        ids = requests.get(HN_TOP_URL, timeout=10).json()[:limit]
        titles = []
        for item_id in ids:
            data = requests.get(HN_ITEM_URL.format(id=item_id), timeout=10).json()
            if data and data.get("title"):
                titles.append(data["title"])
        return titles
    except Exception as exc:
        st.warning(f"Hacker News indisponivel: {exc}")
        return []


def fetch_reddit_startups(limit: int = 10) -> list[str]:
    """Le o RSS publico do /r/startups (sem autenticacao)."""
    try:
        feed = feedparser.parse(REDDIT_STARTUPS_RSS)
        return [entry.title for entry in feed.entries[:limit]]
    except Exception as exc:
        st.warning(f"Reddit indisponivel: {exc}")
        return []


def fetch_product_hunt(limit: int = 10) -> list[str]:
    try:
        feed = feedparser.parse(PRODUCT_HUNT_RSS)
        return [entry.title for entry in feed.entries[:limit]]
    except Exception as exc:
        st.warning(f"Product Hunt indisponivel: {exc}")
        return []


def fetch_google_trends(limit: int = 10) -> list[str]:
    """Usa pytrends para apanhar daily trending searches (PT/US fallback)."""
    if TrendReq is None:
        st.info("pytrends nao instalado - saltar Google Trends.")
        return []
    try:
        pytrends = TrendReq(hl="en-US", tz=0)
        for geo in ("US", "PT", "GB"):
            try:
                df = pytrends.trending_searches(pn="united_states" if geo == "US" else geo)
                return df[0].head(limit).tolist()
            except Exception:
                continue
        return []
    except Exception as exc:
        st.warning(f"Google Trends indisponivel: {exc}")
        return []


def aggregate_trends(sources: dict[str, bool], per_source: int = 10) -> list[str]:
    """Agrega tendencias das fontes selecionadas e devolve ate 10 topicos unicos."""
    collected: list[str] = []
    if sources.get("hacker_news"):
        collected += fetch_hacker_news(per_source)
    if sources.get("reddit_startups"):
        collected += fetch_reddit_startups(per_source)
    if sources.get("product_hunt"):
        collected += fetch_product_hunt(per_source)
    if sources.get("google_trends"):
        collected += fetch_google_trends(per_source)

    # Remove duplicados mantendo ordem e limita a 10.
    seen: set[str] = set()
    unique: list[str] = []
    for title in collected:
        key = title.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(title.strip())
    return unique[:10]


# ---------------------------------------------------------------------------
# LLM: geracao de ideias
# ---------------------------------------------------------------------------


PROMPT_TEMPLATE = """Com base nestas tendencias de hoje:
{trends}

Gera 3 ideias de apps mobile ou webapps altamente monetizaveis.
Para cada ideia retorna estritamente JSON valido com o formato:

{{
  "ideas": [
    {{"name": "...", "problem": "...", "monetization": "..."}},
    {{"name": "...", "problem": "...", "monetization": "..."}},
    {{"name": "...", "problem": "...", "monetization": "..."}}
  ]
}}

Regras:
- "name" deve ser curto e memoravel.
- "problem" deve descrever o problema concreto em 1-2 frases.
- "monetization" deve ser especifico (ex: "SaaS B2B 29USD/mes", "Freemium + in-app purchase", "Marketplace com fee de 10%").
- Nao incluas texto fora do JSON.
"""


def _parse_ideas_payload(raw: str, trends_context: str) -> list[Idea]:
    """Extrai o JSON retornado pelo LLM de forma resiliente."""
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
            trends_context=trends_context,
        )
        for item in payload.get("ideas", [])
    ]


def generate_ideas_gemini(trends: list[str], api_key: str, model: str) -> list[Idea]:
    if genai is None:
        raise RuntimeError("google-generativeai nao instalado.")
    genai.configure(api_key=api_key)
    llm = genai.GenerativeModel(model)
    prompt = PROMPT_TEMPLATE.format(trends="\n- " + "\n- ".join(trends))
    response = llm.generate_content(prompt)
    return _parse_ideas_payload(response.text, trends_context="\n".join(trends))


def generate_ideas_openai(trends: list[str], api_key: str, model: str) -> list[Idea]:
    if OpenAI is None:
        raise RuntimeError("openai nao instalado.")
    client = OpenAI(api_key=api_key)
    prompt = PROMPT_TEMPLATE.format(trends="\n- " + "\n- ".join(trends))
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    return _parse_ideas_payload(
        response.choices[0].message.content,
        trends_context="\n".join(trends),
    )


# ---------------------------------------------------------------------------
# Interface Streamlit
# ---------------------------------------------------------------------------


def render_idea_card(idx: int, idea: Idea) -> None:
    with st.container(border=True):
        st.subheader(f"{idx}. {idea.name}")
        st.markdown(f"**Problema**  \n{idea.problem}")
        st.markdown(f"**Monetizacao**  \n{idea.monetization}")
        if st.button("Guardar ideia", key=f"save-{idx}-{idea.name}"):
            save_idea(idea)
            st.toast(f"Ideia '{idea.name}' guardada.", icon="*")


def render_saved_sidebar() -> None:
    st.sidebar.header("Ideias guardadas")
    saved = list_saved_ideas()
    if not saved:
        st.sidebar.caption("Ainda nao guardaste nenhuma ideia.")
        return
    for row in saved:
        with st.sidebar.expander(f"{row['name']}"):
            st.write(f"**Problema:** {row['problem']}")
            st.write(f"**Monetizacao:** {row['monetization']}")
            st.caption(f"Guardada em {row['created_at']}")
            if st.button("Apagar", key=f"del-{row['id']}"):
                delete_idea(row["id"])
                st.rerun()


def main() -> None:
    st.set_page_config(
        page_title="Radar de Ideias de Apps",
        page_icon="*",
        layout="wide",
    )
    init_db()

    st.title("Radar de Ideias de Apps")
    st.caption("Sonda tendencias do dia e transforma-as em ideias de produto monetizaveis.")

    # --- Sidebar: configuracao -------------------------------------------------
    st.sidebar.header("Configuracao")
    provider = st.sidebar.selectbox("Provider LLM", ["Gemini", "OpenAI"], index=0)
    key_name = "GEMINI_API_KEY" if provider == "Gemini" else "OPENAI_API_KEY"
    # Prioridade: st.secrets (Streamlit Cloud) > variavel de ambiente > input manual.
    default_key = ""
    try:
        default_key = st.secrets.get(key_name, "")
    except Exception:
        pass
    default_key = default_key or os.getenv(key_name, "")
    if provider == "Gemini":
        model = st.sidebar.text_input("Modelo", value="gemini-2.5-flash")
    else:
        model = st.sidebar.text_input("Modelo", value="gpt-4o-mini")
    api_key = st.sidebar.text_input("API key", value=default_key, type="password")

    st.sidebar.subheader("Fontes de tendencias")
    sources = {
        "hacker_news": st.sidebar.checkbox("Hacker News", value=True),
        "reddit_startups": st.sidebar.checkbox("Reddit /r/startups", value=True),
        "product_hunt": st.sidebar.checkbox("Product Hunt", value=False),
        "google_trends": st.sidebar.checkbox("Google Trends", value=False),
    }

    render_saved_sidebar()

    # --- Main: botao principal -------------------------------------------------
    col_btn, col_info = st.columns([1, 2])
    with col_btn:
        trigger = st.button("Sondar mercado hoje", type="primary", use_container_width=True)
    with col_info:
        st.caption("Clica para ir buscar as tendencias do dia e gerar 3 ideias frescas.")

    if trigger:
        if not api_key:
            st.error("Define a API key do provider selecionado na sidebar.")
            return
        if not any(sources.values()):
            st.error("Seleciona pelo menos uma fonte de tendencias.")
            return

        with st.spinner("A agregar tendencias..."):
            trends = aggregate_trends(sources)
        if not trends:
            st.error("Nao foi possivel obter tendencias das fontes selecionadas.")
            return

        with st.expander("Top 10 tendencias de hoje", expanded=False):
            for t in trends:
                st.write(f"- {t}")

        with st.spinner(f"A pedir ao {provider} para gerar ideias..."):
            try:
                if provider == "Gemini":
                    ideas = generate_ideas_gemini(trends, api_key, model)
                else:
                    ideas = generate_ideas_openai(trends, api_key, model)
            except Exception as exc:
                st.error(f"Falha ao gerar ideias: {exc}")
                return

        st.session_state["latest_ideas"] = [asdict(i) for i in ideas]

    # Render do resultado mais recente (persiste entre reruns causados por botoes).
    if "latest_ideas" in st.session_state and st.session_state["latest_ideas"]:
        st.divider()
        st.subheader("Ideias geradas")
        cols = st.columns(3)
        for i, payload in enumerate(st.session_state["latest_ideas"], start=1):
            with cols[(i - 1) % 3]:
                render_idea_card(i, Idea(**payload))


if __name__ == "__main__":
    main()
