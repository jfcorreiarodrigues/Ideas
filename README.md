# Radar de Ideias de Apps

Dashboard Streamlit que agrega tendencias diarias (Hacker News, Reddit
`/r/startups`, Product Hunt, Google Trends) e pede a um LLM (Gemini ou OpenAI)
para as transformar em 3 ideias de apps monetizaveis. Podes guardar as
melhores num SQLite local (`ideas.db`) para revisitar mais tarde.

## Instalacao

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Configurar a API key

Exporta a chave do provider que queres usar (basta uma):

```bash
export GEMINI_API_KEY="..."   # https://aistudio.google.com/apikey
# ou
export OPENAI_API_KEY="..."
```

Tambem podes colar a chave diretamente no campo da sidebar.

## Correr

```bash
streamlit run app.py
```

Abre o browser em `http://localhost:8501`, escolhe as fontes na sidebar e
clica em **Sondar mercado hoje**. As ideias geradas aparecem em 3 cartoes;
o botao *Guardar ideia* persiste-as em `ideas.db`.

## Deploy publico (gratis) - Streamlit Community Cloud

1. Vai a **https://share.streamlit.io** e faz login com o GitHub (`jfcorreiarodrigues`).
2. Clica em **"Create app"** -> **"Deploy from GitHub"** e escolhe:
   - Repository: `jfcorreiarodrigues/ideas`
   - Branch: `claude/app-idea-radar-UkGD5` (ou `main` depois de fazer merge)
   - Main file path: `app.py`
3. Em **"Advanced settings" -> Secrets**, cola:
   ```toml
   GEMINI_API_KEY = "a-tua-chave"
   ```
4. Clica **Deploy**. Em ~2 min tens o URL publico (algo como
   `https://ideas-<hash>.streamlit.app`).

A app detecta automaticamente a chave via `st.secrets`, sem codigo extra.

## Fontes de tendencias

- **Hacker News** - top stories via API publica (recomendado para builders).
- **Reddit /r/startups** - RSS publico, sem auth.
- **Product Hunt** - RSS dos lancamentos recentes.
- **Google Trends** - via `pytrends` (consumer trends, opcional).
