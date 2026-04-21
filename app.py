"""
Radar de Ideias de Apps - Streamlit UI
======================================

- Mostra as ideias geradas automaticamente pelo job diario (data/latest.json).
- Botao "Sondar mercado agora" permite gerar on-demand (respeita historico).
- Ideias sao agrupadas por tema (clustering) e duplicadas sao filtradas.
- Utilizador pode guardar favoritas num SQLite local (ideas.db).

Correr local:
    streamlit run app.py

Deploy: Streamlit Community Cloud - a key e lida de st.secrets.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict
from datetime import datetime

import streamlit as st

from radar_core import (
    Idea,
    aggregate_trends,
    cluster_by_theme,
    dedupe_against_history,
    generate_ideas,
    load_history,
    load_latest,
    past_idea_names,
    persist_run,
)


DB_PATH = "ideas.db"


# ---------------------------------------------------------------------------
# Favoritos locais (SQLite)
# ---------------------------------------------------------------------------


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                problem TEXT NOT NULL,
                monetization TEXT NOT NULL,
                theme TEXT,
                trends_context TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(ideas)").fetchall()}
        if "theme" not in cols:
            conn.execute("ALTER TABLE ideas ADD COLUMN theme TEXT")


def save_favorite(idea: Idea) -> None:
    idea.created_at = datetime.utcnow().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO ideas (name, problem, monetization, theme, trends_context, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                idea.name,
                idea.problem,
                idea.monetization,
                idea.theme,
                idea.trends_context,
                idea.created_at,
            ),
        )


def list_favorites() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM ideas ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def delete_favorite(idea_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM ideas WHERE id = ?", (idea_id,))


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------


def resolve_api_key(provider: str) -> tuple[str, bool]:
    """Devolve (chave, has_secret). has_secret=True se veio de st.secrets."""
    key_name = "GEMINI_API_KEY" if provider == "Gemini" else "OPENAI_API_KEY"
    try:
        secret_val = st.secrets.get(key_name, "")
    except Exception:
        secret_val = ""
    if secret_val:
        return secret_val, True
    return os.getenv(key_name, ""), False


def render_idea_card(idea: Idea, key_suffix: str) -> None:
    with st.container(border=True):
        st.caption(f"Tema: {idea.theme}")
        st.subheader(idea.name)
        st.markdown(f"**Problema**  \n{idea.problem}")
        st.markdown(f"**Monetizacao**  \n{idea.monetization}")
        if st.button("Guardar ideia", key=f"save-{key_suffix}"):
            save_favorite(idea)
            st.toast(f"Guardada: {idea.name}")


def render_clustered(ideas: list[dict], prefix: str) -> None:
    clusters = cluster_by_theme(ideas)
    for theme, theme_ideas in clusters.items():
        st.markdown(f"### {theme} _({len(theme_ideas)})_")
        cols = st.columns(min(3, len(theme_ideas)))
        for i, payload in enumerate(theme_ideas):
            with cols[i % len(cols)]:
                render_idea_card(Idea(**payload), key_suffix=f"{prefix}-{theme}-{i}")


def render_favorites_sidebar() -> None:
    st.sidebar.header("Ideias guardadas")
    saved = list_favorites()
    if not saved:
        st.sidebar.caption("Ainda nao guardaste nenhuma ideia.")
        return
    for row in saved:
        label = f"{row['name']}"
        if row.get("theme"):
            label = f"[{row['theme']}] {label}"
        with st.sidebar.expander(label):
            st.write(f"**Problema:** {row['problem']}")
            st.write(f"**Monetizacao:** {row['monetization']}")
            st.caption(f"Guardada em {row['created_at']}")
            if st.button("Apagar", key=f"del-{row['id']}"):
                delete_favorite(row["id"])
                st.rerun()


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------


def tab_today(provider: str, api_key: str, model: str, num_ideas: int, sources: dict[str, bool]) -> None:
    latest = load_latest()
    if latest:
        st.caption(
            f"Ultima sondagem automatica: **{latest.get('generated_at', latest.get('date'))}**"
        )
    else:
        st.info("Ainda nao existe sondagem diaria. Clica abaixo para correr uma agora.")

    col_btn, col_info = st.columns([1, 3])
    with col_btn:
        trigger = st.button(
            "Sondar mercado agora", type="primary", use_container_width=True
        )
    with col_info:
        active = ", ".join(k for k, v in sources.items() if v) or "nenhuma"
        st.caption(f"Fontes activas: **{active}** - evita ideias ja propostas.")

    if trigger:
        if not api_key:
            st.error("Define a API key na sidebar (ou configura-a nos Secrets do Streamlit Cloud).")
            return
        if not any(sources.values()):
            st.error("Seleciona pelo menos uma fonte na sidebar.")
            return
        with st.spinner("A agregar tendencias..."):
            trends = aggregate_trends(sources)
        if not trends:
            st.error("Nao foi possivel obter tendencias.")
            return
        with st.expander(f"{len(trends)} tendencias recolhidas", expanded=False):
            for t in trends:
                st.write(f"- {t}")

        history = load_history()
        avoid = past_idea_names(history, limit=80)
        with st.spinner(f"A pedir ao {provider} {num_ideas} ideias frescas..."):
            try:
                ideas = generate_ideas(
                    provider, trends, api_key, model, avoid_names=avoid, num_ideas=num_ideas
                )
            except Exception as exc:
                st.error(f"Falha ao gerar ideias: {exc}")
                return
        ideas = dedupe_against_history(ideas, history)
        if not ideas:
            st.warning("Todas as ideias geradas ja existiam no historico. Tenta de novo.")
            return
        run = persist_run(trends, ideas)
        st.session_state["latest_run"] = run
        st.toast(f"{len(ideas)} novas ideias guardadas.")

    run = st.session_state.get("latest_run") or latest
    if run and run.get("ideas"):
        st.divider()
        st.subheader(f"Ideias de {run['date']}")
        render_clustered(run["ideas"], prefix="today")


def tab_history() -> None:
    history = load_history()
    if not history:
        st.info("Ainda nao existe historico.")
        return

    total_ideas = sum(len(r.get("ideas", [])) for r in history)
    st.caption(f"{len(history)} sondagens - {total_ideas} ideias geradas ate hoje.")

    # Filtro por tema agregado.
    all_ideas = [idea for run in history for idea in run.get("ideas", [])]
    themes = sorted({i.get("theme", "Geral") for i in all_ideas})
    selected = st.multiselect("Filtrar por tema", themes, default=themes)

    filtered = [i for i in all_ideas if i.get("theme", "Geral") in selected]
    st.markdown(f"**{len(filtered)} ideias** em {len(selected)} temas.")
    st.divider()
    render_clustered(filtered, prefix="history")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(
        page_title="Radar de Ideias de Apps", page_icon="*", layout="wide"
    )
    init_db()

    st.title("Radar de Ideias de Apps")
    st.caption(
        "Sonda automaticamente tendencias do dia e transforma-as em ideias monetizaveis."
    )

    # Sidebar: config
    st.sidebar.header("Configuracao")
    provider = st.sidebar.selectbox("Provider LLM", ["Gemini", "OpenAI"], index=0)
    default_model = "gemini-2.5-flash-lite" if provider == "Gemini" else "gpt-4o-mini"
    model = st.sidebar.text_input("Modelo", value=default_model)

    api_key, has_secret = resolve_api_key(provider)
    if has_secret:
        st.sidebar.success("API key carregada dos Secrets.")
    else:
        api_key = st.sidebar.text_input(
            "API key",
            value=api_key,
            type="password",
            help="Configura nos Secrets do Streamlit Cloud para persistir entre sessoes.",
        )

    num_ideas = st.sidebar.slider("Ideias por sondagem", 3, 10, 6)

    st.sidebar.subheader("Fontes de tendencias")
    sources = {
        "hacker_news": st.sidebar.checkbox("Hacker News (tech)", value=True),
        "reddit_startups": st.sidebar.checkbox("Reddit /r/startups", value=True),
        "indie_hackers": st.sidebar.checkbox("Indie Hackers", value=True),
        "everyday_problems": st.sidebar.checkbox(
            "Problemas do dia-a-dia (5 subreddits)", value=True
        ),
        "product_hunt": st.sidebar.checkbox("Product Hunt", value=False),
        "google_trends": st.sidebar.checkbox("Google Trends", value=False),
    }

    render_favorites_sidebar()

    tab1, tab2 = st.tabs(["Hoje", "Historico por tema"])
    with tab1:
        tab_today(provider, api_key, model, num_ideas, sources)
    with tab2:
        tab_history()


if __name__ == "__main__":
    main()
