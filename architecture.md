# OpenElpis — System Architecture & Design

*A non-profit, open-source platform where clinicians, clinics, and companies contribute validated materials that power an AI research copilot for breast cancer — starting vertical for a platform built to expand to other cancers.*

> **Status:** Draft v2 — a blueprint to build from, not a final spec. Adapt as you learn.

**Project identity.** *Elpis* (Ἐλπίς) is the Greek personification of hope — the spirit that remained when Pandora's jar was opened. **OpenElpis** is open hope for breast cancer research: open source, open science, open to every qualified contributor. Web home: **openelpis.com** (primary) and **openelpis.life** (community / outreach).

**Scope — start narrow, expand later.** OpenElpis launches as **the breast-cancer research copilot**. Breast cancer is the most-researched cancer (huge validated literature; mature public datasets like TCGA-BRCA, METABRIC, CAMELYON), with an active clinician community and well-defined molecular subtypes — an ideal beachhead to prove the validation-and-provenance pipeline deeply in one vertical. The architecture itself stays disease-agnostic: once the trust pipeline is proven on breast cancer, the *same* machinery extends to other cancers. Where this document says "oncology"/"cancer" generically, read it as **breast cancer first**. Breast-specific *training* (Phase 3 specialist models on mammography/histopathology/omics) is where disease specialization genuinely pays off — the conversational copilot itself stays **RAG-first**, never trained on the corpus.

---

## 0. Guiding principles (read these first)

1. **Data minimization & no raw PHI early.** Start with material that is *not* identifiable patient data: published literature, de-identified datasets, anonymized case write-ups, structured aggregate statistics. Add raw imaging only after governance, de-identification, and legal basis are in place.
2. **Validation is the product.** A research platform is only as trustworthy as its weakest accepted contribution. Provenance, identity verification, and expert review are core features, not extras.
3. **The model reads from a curated corpus; it is not trained live on uploads.** Uploads → validation → versioned dataset / knowledge base → periodic, reviewed model use.
4. **Separate concerns by cost and risk.** Web app and workflow are cheap and live on the VPS. File storage and GPU compute are separate, scalable, and rented on demand.
5. **Everything is auditable.** Who uploaded what, who reviewed it, what license it carries, and which model version used it — all logged immutably.

---

## 1. Contributors and what they can submit

### Contributor types
| Type | Examples | Verification needed |
|---|---|---|
| Individual clinician / researcher | oncologist, radiologist, PhD | License/credential + institutional email + ORCID |
| Clinic / hospital | departments, tumor boards | Organizational KYC + signed data agreement |
| Company / lab | biotech, diagnostics, CRO | Organizational KYC + legal agreement |
| Open contributor | student, citizen scientist | Email only — restricted to public, non-PHI material |

### Material tiers (build in this order)
- **Tier A — Text & literature (start here).** Papers, reviews, anonymized case reports, treatment notes with no identifiers, structured summaries. Lowest risk, immediately useful for the RAG copilot.
- **Tier B — De-identified structured data.** Lab value tables, genomic variant lists, drug-response measurements — stripped of identifiers, schema-validated.
- **Tier C — Imaging & raw clinical data (defer).** MRI/CT/pathology slides (DICOM/WSI). Highest scientific value, highest legal/technical burden. Requires de-identification pipeline, data-use agreements, and ethics oversight before you accept a single file.

---

## 2. The validation & anti-abuse pipeline

This is the heart of the system. Five stages; manual review is acceptable and recommended at the start.

### Stage 0 — Contributor onboarding (gate the door)
- Verify identity and credentials before any upload is accepted into the corpus.
  - Clinicians: medical license number + country registry check, institutional email domain, ORCID.
  - Organizations: manual KYC (registration documents, signed Data Contribution Agreement).
- Assign a **trust tier** (T0 unverified → T3 verified institution). Trust tier controls what a user may submit and how much review their submissions require.

### Stage 1 — Automated intake checks (cheap, fast, catch the obvious)
Run asynchronously on every upload:
- **File safety:** type/format validation, virus/malware scan, size limits.
- **De-identification scan:** detect personal names, dates of birth, faces in images, and DICOM header PHI; auto-reject or quarantine anything that fails.
- **Duplicate / plagiarism detection:** content hashing + embedding similarity against existing corpus and known sources.
- **Schema & sanity checks:** for structured data, validate columns, units, ranges; flag impossible values (a common signature of fabricated data).
- **License & consent attestation:** uploader must affirm they have the right to share and that data is de-identified.

### Stage 2 — Manual expert review (the human gate)
- Submissions land in a **review queue** with the automated check results attached.
- Domain reviewers (recruited from your verified clinician contributors) approve / reject with a written rationale.
- **Double review** for anything sensitive or from a low-trust contributor.
- Reviewer actions are logged and themselves auditable.

### Stage 3 — Provenance, versioning & integrity
- Every accepted item gets an immutable provenance record: contributor, timestamp, source, license, review trail, content hash.
- Datasets are **versioned** (e.g., DVC or a dataset registry) so any model can declare exactly which data version it used — essential for reproducibility and for *retracting* bad data later.

### Stage 4 — Reputation & feedback loop
- Contributors build reputation from accepted, useful submissions; rejected/abusive submissions lower it.
- Low-reputation or flagged accounts get heavier review or suspension.
- A public "data integrity" report keeps the process transparent.

**Anti-scammer summary:** identity gate (Stage 0) + automated red flags (Stage 1) + human expert sign-off (Stage 2) + permanent provenance (Stage 3) + reputation (Stage 4). No single layer is trusted alone.

---

## 3. System architecture (components)

```
                       ┌─────────────────────────────┐
                       │     Public Website (SSR)     │  ← contributors, about, docs
                       │  + Contributor Portal (auth) │
                       └──────────────┬──────────────┘
                                      │ HTTPS
                       ┌──────────────▼──────────────┐
                       │        API / Backend         │  auth, uploads, review workflow
                       │      (FastAPI or Node)       │
                       └───┬───────────┬───────────┬──┘
                           │           │           │
        ┌──────────────────▼──┐  ┌─────▼─────┐  ┌──▼───────────────┐
        │  Postgres (metadata) │  │  Redis +  │  │ Object storage   │
        │ users, submissions,  │  │  workers  │  │ (S3-compatible:  │
        │ reviews, provenance  │  │ (Celery)  │  │ MinIO / R2 / B2) │  ← files live HERE, not on VPS disk
        └──────────────────────┘  └─────┬─────┘  └──────────────────┘
                                        │ async jobs: scan, de-id, embed
                       ┌────────────────▼────────────────┐
                       │   Validated Corpus & Datasets    │
                       │  • Vector store (RAG text KB)    │
                       │  • Versioned imaging/omics sets  │
                       │  • Knowledge graph (PrimeKG)     │
                       └────────────────┬────────────────┘
                                        │
                       ┌────────────────▼────────────────┐
                       │        AI Layer (the copilot)    │
                       │  Agent LLM (Qwen3) + RAG + tools │
                       │  calls specialist models:        │
                       │  ESMFold2, scGPT/Geneformer ...  │
                       └──────────────────────────────────┘
                          (LLM inference + training run on
                           on-demand rented GPU, not the VPS)
```

### Component responsibilities
- **Public website + portal** — marketing/about, contribution docs, contributor dashboard, review queue UI for reviewers, and the copilot query interface (gated for verified researchers).
- **API/backend** — authentication, upload handling (pre-signed URLs straight to object storage), review-workflow state machine, provenance writes.
- **Postgres** — all metadata: users, trust tiers, submissions, review decisions, provenance, audit log.
- **Redis + workers** — async jobs: malware scan, de-identification, embedding generation, duplicate detection.
- **Object storage (S3-compatible)** — the actual files. Use Cloudflare R2 / Backblaze B2 (cheap, off-VPS) or self-hosted MinIO at small scale. Never store large medical files on the VPS root disk.
- **Validated corpus** — only *approved* content: a vector store for text RAG, versioned datasets for imaging/omics, and a biomedical knowledge graph.
- **AI layer** — the agentic research copilot (your earlier pathway): Qwen3 orchestrator + RAG over the validated KB + tool calls to specialist models. Inference offloaded to rented GPU.

---

## 4. How uploaded materials actually reach the model

This is the crucial flow people get wrong:

1. **Text/literature (Tier A):** approved → chunked → embedded → added to the **vector store**. The copilot retrieves it at query time. *No retraining needed* — new knowledge is available immediately and every claim stays citation-traceable to its source.
2. **Structured & imaging data (Tier B/C):** approved → added to a **versioned dataset**. Specialist models (e.g., a vision model for imaging, scGPT/Geneformer for expression) are **fine-tuned periodically in controlled batches**, each run pinned to a dataset version and reviewed before the new model is promoted.
3. **Never** pipe raw uploads into live model weights. Curate first, version always, retrain deliberately.

---

## 5. Deployment on a VPS (realistic sizing)

### What the VPS runs well
- Reverse proxy (Caddy — automatic HTTPS, or Nginx)
- Web frontend + API backend
- Postgres, Redis, Celery workers
- MinIO (only at small scale; migrate to R2/B2 as files grow)
- A small **quantized** LLM (7–8B) for light tasks like routing or de-id NLP

A single solid VPS (~8 vCPU / 16–32 GB RAM / good NVMe) handles the entire web + workflow + text-RAG MVP **if** heavy model inference is offloaded.

### What the VPS must NOT do
- Store terabytes of imaging → object storage instead.
- Serve a large LLM or train models → rent **on-demand GPU** (RunPod, Vast.ai, Lambda) only when a job runs, or call a hosted open-model inference API. This keeps cost near zero between jobs.

### Baseline hardening
- TLS everywhere; encryption at rest for object storage and DB.
- Secrets in a vault/env manager, never in code.
- Role-based access control; reviewers and admins separated.
- Automated encrypted backups (DB + object storage) with tested restore.
- Full audit logging; rate limiting and WAF on the public endpoints.
- **Observability:** metrics, structured logs, uptime/error alerting (Prometheus + Grafana + Loki, or a donated SaaS tier such as Grafana Cloud / Sentry). You cannot trust a pipeline you cannot see.
- Containerize (Docker Compose to start; Kubernetes only if you truly outgrow one host).

---

### Sustainability & portability (design for the "credit cliff")

Donated cloud credits are real but **time-boxed** — most grants last 12–24 months. The architecture is therefore deliberately built on **portable, open-source components** (Postgres, Redis, MinIO/S3 API, Qdrant, Docker) with **no proprietary lock-in**. Practical consequences:

- Any S3-compatible store, any Postgres host, any Docker host can run this — so we can migrate between sponsors (or down to a cheap dedicated box like Hetzner) when a credit window ends, without rewriting anything.
- Infrastructure is defined as code (Docker Compose / Terraform) so a full redeploy to a new provider is a config change, not a project.
- Steady-state running cost is kept low by design (one VPS + object storage + bursty GPU), so the platform survives on small recurring grants or donations once initial credits lapse.

### Resource footprint (what a sponsor actually funds)

| Phase | Compute (24/7) | Object storage | GPU (bursty) | CDN/WAF | Rough retail cost |
|---|---|---|---|---|---|
| **1 — Foundation (no PHI)** | 1× 8 vCPU / 16–32 GB / 200 GB NVMe | 250 GB – 2 TB S3 | A few hundred GPU-hrs/yr for embeddings + light fine-tune | DDoS/WAF/CDN in front of public site | ~$200–400/mo (~$3–5k/yr) |
| **2 — Structured data** | + larger DB, more workers | 2 – 10 TB S3 | periodic specialist-model fine-tune batches | same | ~$500–900/mo |
| **3 — Imaging & models** | multi-node / managed K8s | 10 – 100+ TB S3 | sustained GPU for de-id + WSI/DICOM models | same | scales with adoption |

These figures are what the sponsorship proposal ([partnership-proposal.md](partnership-proposal.md)) asks providers to underwrite with credits or donated services.

## 6. Suggested tech stack

| Layer | Recommendation | Why |
|---|---|---|
| Frontend | Next.js (or SvelteKit) | SSR, fast, good auth ecosystem |
| Backend | FastAPI (Python) | Same language as the ML stack |
| Auth | Authentik / Keycloak / Clerk | Roles, verification, SSO-ready |
| DB | PostgreSQL | Reliable, relational, JSON support |
| Queue | Celery + Redis | Mature async job handling |
| Object storage | Cloudflare R2 or Backblaze B2 | Cheap egress, S3-compatible |
| Dataset versioning | DVC | Reproducible dataset versions |
| Vector store | Qdrant or pgvector | Self-hostable RAG backend |
| Knowledge graph | Neo4j + PrimeKG/Hetionet | Repurposing reasoning |
| Agent/LLM | Qwen3 + LangGraph | Tool-using, self-hostable |
| GPU compute | RunPod / Vast.ai (on-demand) | Pay only when training/inferring |
| Containerization | Docker Compose | Simple, portable |

---

## 7. Phased rollout

**Phase 1 — Foundation (no PHI).**
Website + contributor portal + identity verification + manual review queue + Tier A text contributions → RAG copilot. Prove the trust pipeline works on low-risk data.

**Phase 2 — Structured data.**
Add Tier B de-identified datasets, automated de-id and schema validation, dataset versioning, reputation system.

**Phase 3 — Imaging & specialist models.**
Only after legal/ethics foundation exists: DICOM/WSI de-identification pipeline, data-use agreements, ethics oversight, and periodic specialist-model fine-tuning.

**Phase 4 — Scale & federation.**
Consider **federated learning** (models travel to hospital data, raw data never leaves the institution) to sidestep centralizing PHI entirely — the gold standard for multi-institution medical AI.

---

## 8. Governance, legal & ethics (do not skip)

- **You will need legal counsel** specializing in health-data (KVKK in Turkey, GDPR in the EU, HIPAA where US patients are involved). This is not optional for a platform touching clinical data.
- **Data Contribution Agreements** for every organizational contributor, defining license, de-identification warranties, and revocation rights.
- **Ethics / IRB oversight** before accepting any patient-derived data, even de-identified.
- **A clear non-profit governance structure** (board, data-governance committee) — funders and hospital partners will require it.
- **A registered legal entity is a prerequisite for funding and donated cloud credits.** Almost every "for nonprofits" program (Microsoft, Google, AWS, GitHub) requires a 501(c)(3) **or recognized local equivalent**, verified through TechSoup / Goodstack / Percent. For a Turkey-based founder the realistic paths are: (a) register a Turkish **dernek** (association, needs ≥7 founders) or **vakıf** (foundation, needs endowment capital); (b) use a **fiscal sponsor** (Open Source Collective, NumFOCUS) to receive grants without forming an entity — fastest, ~10% fee, but some cloud-credit programs only honor *your* entity's status; or (c) **partner with a university/research institute**, which also unlocks academic and EU funders (Turkey is fully associated to Horizon Europe). Stand up a proper **GitHub org + OSI license** first — it is the precondition for nearly all of these. See [funding-and-credits.md](funding-and-credits.md) for the full playbook.
- **Liability framing:** the platform produces *research hypotheses for qualified professionals*, never diagnosis or treatment advice. State this prominently and enforce it in the product.
- Consider partnering with an existing research institution early — it gives you ethics infrastructure, credibility, and data-sharing legitimacy you can't build alone.

*Note: this is general information, not legal advice — confirm specifics with a qualified lawyer in each relevant jurisdiction.*