# Radar de Ideias: AI · Vibe Coding · Robots

Dashboard Streamlit opinionado: em vez de pegar em trends aleatorios e
gerar SaaS genericos, sonda fontes alinhadas com uma tese editorial e
pede ao LLM ideias empreendedoras que so fazem sentido nesta janela.

**Tese editorial:**
- **2026 = ano das apps AI-native** (LLM/agente como produto, nao como
  feature colada).
- **2027 = ano dos robots** (humanoides, embodied AI, automacao fisica) -
  jogadas "robot-adjacent" hoje posicionam para essa onda.
- **Vibe coding** (Cursor, Claude Code, Codex) torna o software comodity;
  o moat passa a ser distribuicao, dados ou hardware.

Cada ideia gerada e classificada num archetype (AI-Native App, Vibe-Coded
MicroSaaS, Robotics-Adjacent, AI-Augmented Service, Hardware Companion,
AI-Native Marketplace, Infoproduct/Community) e tem um campo `thesis_fit`
a explicar porque so faz sentido nesta janela.

Fontes default: `r/LocalLLaMA`, `r/MachineLearning`, `r/singularity`,
`r/ChatGPTCoding`, `r/cursor`, `r/ClaudeAI`, `r/AI_Agents`, `r/robotics`,
`r/automate`, `r/Embodied` + Hacker News + `r/startups`. Podes ligar/desligar
qualquer uma na sidebar.

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
