<div align="center">

# 🎰 Sweepstakes Agent

**An AI-powered agent that automatically discovers and enters free, legitimate sweepstakes on your behalf**

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat&logo=python&logoColor=white)
![browser-use](https://img.shields.io/badge/browser--use-0.12+-purple?style=flat)
![License](https://img.shields.io/badge/License-MIT-green?style=flat)

</div>

---

Sweepstakes Agent is an AI-powered automation tool that uses [browser-use](https://github.com/browser-use/browser-use) to discover and enter free, legitimate sweepstakes and contests. It scans sweepstakes aggregators, evaluates contests, auto-fills forms, and tracks your entries — all with a Streamlit dashboard.

## ✨ Features

- **Autonomous discovery** — scans sweepstakes aggregators in parallel to find new contests
- **AI form-filling** — reads and fills entry forms using browser automation
- **Entry history** — tracks all contests entered with status and timestamps
- **Cost tracking** — monitors API usage and estimated costs
- **Dashboard UI** — Streamlit interface with stats, discovery, entry, and history tabs
- **Built on browser-use** — reliable browser automation with LLM guidance

## 🚀 Quick Start

```bash
git clone https://github.com/RhythrosaLabs/sweepstakes-agent.git
cd sweepstakes-agent
pip install -r requirements.txt
```

Add your API keys to `.env`:
```
OPENAI_API_KEY=your_key
```

```bash
streamlit run app.py
```

## 🛠️ Tech Stack

- **Python 3.11+** — core language
- **browser-use** — LLM-guided browser automation
- **OpenAI / GPT-4** — AI decision-making
- **Playwright** — headless browser
- **Streamlit** — dashboard UI

## 🤝 Contributing

PRs welcome. Open an issue first for major changes.

## 📄 License

MIT

## 💛 Support

If Sweepstakes Agent wins you something good, consider supporting development:

👉 [Donate via PayPal](https://paypal.me/noodlebake) — @noodlebake

🌐 [Portfolio: rhythrosalabs.github.io](https://rhythrosalabs.github.io) (more apps, music and sound design)

---
<div align="center">Made with ❤️ by <a href="https://github.com/RhythrosaLabs">RhythrosaLabs</a></div>
