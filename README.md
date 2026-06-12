<p align="center">
  <img src="site/logo.svg" width="84" alt="OpenElpis">
</p>

<h1 align="center">OpenElpis</h1>

<p align="center"><b>Open hope for breast cancer research.</b><br>
A non-profit, open-source platform building a trustworthy, <b>citation-grounded</b> AI research copilot for breast cancer — powered by validated, expert-reviewed medical knowledge.</p>

<p align="center">
  🌐 <a href="https://openelpis.com">openelpis.com</a> ·
  ✉️ hello@openelpis.com ·
  📍 Türkiye
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <a href="https://openelpis.com"><img alt="Live site" src="https://img.shields.io/badge/site-openelpis.com-0e7c7b.svg"></a>
  <img alt="Status" src="https://img.shields.io/badge/status-early%20%C2%B7%20building%20in%20the%20open-e35d8a.svg">
</p>

---

> ⚕️ **Liability framing:** OpenElpis produces **research hypotheses for qualified professionals** — never medical diagnosis or treatment advice.

## What it is

Breast cancer is the most-researched cancer in the world, yet thousands of papers a week, de-identified findings, and hard-won clinical insight never reach the people who could use them. OpenElpis is the **trustworthy middle layer**: verified clinicians and labs contribute *validated* material, which then powers a citation-grounded AI copilot that **retrieves from a curated, peer-reviewed corpus and shows its sources — it never invents them**.

The principle: **validated in, citation-grounded out.** Every contribution is identity-verified, expert-reviewed, and provenance-tracked; no patient-identifiable data enters the system until governance and law fully allow it. Breast cancer is the starting vertical — the same trust pipeline is built to extend to other cancers.

Full design: **[architecture.md](architecture.md)**.

## What's in this repo

| Path | What it is |
|---|---|
| [`site/`](site/) | The website served at **openelpis.com** — a static, 8-language marketing site (`build-i18n.py` generates the per-language pages for SEO) plus the **clinician portal** front-end (`site/portal/`, signup / login / upload). |
| [`server2/schema.sql`](server2/schema.sql) | The PostgreSQL + **pgvector** data model: organizations, users, uploaded materials, and the RAG **`material_chunks`** (keyword + embedding columns) — i.e. how a contribution becomes searchable. |
| [`server2/app/`](server2/app/) | The clinician-portal **API** (FastAPI): email/password auth (argon2 + JWT cookie) and material upload, with a human-review gate before anything enters the corpus. |
| [`architecture.md`](architecture.md) | System design & data-governance blueprint. |

## Architecture (Phase 1)

A deliberately small, portable footprint:

- **Static site + clinician portal** → served by [Caddy](https://caddyserver.com) (auto-HTTPS).
- **API** → FastAPI (this repo, `server2/app`).
- **Database** → PostgreSQL 16 + **pgvector** (`server2/schema.sql`).
- **Retrieval** → keyword (Postgres full-text) now; semantic (pgvector embeddings) next.
- **Chat inference** → an external LLM API (e.g. Groq) — the platform stays a thin layer; the model is swappable.
- **Trust pipeline** → every upload is `pending_review` until a human approves it; then it's chunked and becomes retrievable, with every answer traceable to a reviewed source.

## Status

**Early / building in the open.** The website and the clinician portal (signup, login, upload with a review gate) are live; the retrieval→answer pipeline is in progress. Expect rough edges — that's the point of working in the open.

## Contributing & contact

OpenElpis is an **independent, non-profit initiative** (not yet a registered legal entity), founded by **Ali Tajbakhsh** ([openelpis.com/founder](https://openelpis.com/founder/)). We welcome partners — technical, clinical, and academic — and contributors. Reach us at **hello@openelpis.com** or open an issue.

## License

[Apache License 2.0](LICENSE). © OpenElpis contributors.
