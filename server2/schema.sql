-- ============================================================================
-- OpenElpis — server2 PostgreSQL schema
-- Auth (clinics/doctors/hospitals) + uploaded materials, RAG-ready.
--
-- DESIGN (answers "how should uploaded material be stored so the model can
-- search it, then pass it to Groq?"):
--   1. A specialist uploads a file  -> row in `materials` (status='pending_review'),
--      the raw file goes to object/disk storage (materials.storage_key), NOT the DB.
--   2. A human reviewer approves    -> status='approved' (the trust gate; nothing is
--      searchable until this happens).
--   3. On approval the app splits the document into passages -> rows in
--      `material_chunks` (each ~300-800 tokens).
--   4. SEARCH:
--        v0 (works today, no GPU): Postgres full-text over chunk `tsv` (keyword).
--        v1 (semantic): cosine search over chunk `embedding` (pgvector).
--      Both filter to materials.status='approved' and can pre-filter on
--      materials.metadata (subtype, year, study type...) for precision at scale.
--   5. The top-k chunks (+ their material_id for citations) are passed to Groq
--      with the user's question; Groq answers grounded in those passages.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS citext;     -- case-insensitive email
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- fuzzy / trigram keyword matching
CREATE EXTENSION IF NOT EXISTS vector;     -- pgvector: semantic search (v1)
-- gen_random_uuid() is built into PostgreSQL 13+ (no extension needed).

-- ── Organizations: clinics, hospitals, labs, universities, individuals ──────
CREATE TABLE IF NOT EXISTS organizations (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name                text NOT NULL,
  org_type            text NOT NULL CHECK (org_type IN
                        ('clinic','hospital','lab','university','individual','other')),
  country             text,
  website             text,
  verification_status text NOT NULL DEFAULT 'pending'
                        CHECK (verification_status IN ('pending','verified','rejected')),
  created_at          timestamptz NOT NULL DEFAULT now()
);

-- ── Users: the people who sign up (doctors, researchers, reviewers, admins) ──
CREATE TABLE IF NOT EXISTS users (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id              uuid REFERENCES organizations(id) ON DELETE SET NULL,
  email               citext UNIQUE NOT NULL,
  password_hash       text NOT NULL,                 -- argon2/bcrypt — NEVER plaintext
  full_name           text NOT NULL,
  role                text NOT NULL DEFAULT 'contributor'
                        CHECK (role IN ('contributor','reviewer','admin')),
  credential_type     text,                          -- 'medical_license','orcid','institutional_email'
  credential_ref      text,                          -- license no / ORCID (verified out-of-band)
  verification_status text NOT NULL DEFAULT 'pending'
                        CHECK (verification_status IN ('pending','verified','rejected')),
  is_active           boolean NOT NULL DEFAULT true,
  email_verified      boolean NOT NULL DEFAULT false,
  created_at          timestamptz NOT NULL DEFAULT now(),
  last_login_at       timestamptz
);

-- ── Sessions: refresh-token store (so logins can be revoked). Access tokens
--    themselves are short-lived JWTs and are NOT stored. Store only a HASH. ──
CREATE TABLE IF NOT EXISTS sessions (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash    text NOT NULL,
  user_agent    text,
  ip            inet,
  created_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL,
  revoked_at    timestamptz
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);

-- ── Materials: one row per uploaded file/contribution (metadata + provenance).
--    The raw bytes live in storage_key (local disk now; swap to R2/S3 later). ──
CREATE TABLE IF NOT EXISTS materials (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  uploaded_by       uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  org_id            uuid REFERENCES organizations(id) ON DELETE SET NULL,
  title             text NOT NULL,
  description       text,
  source_type       text NOT NULL DEFAULT 'literature' CHECK (source_type IN
                      ('literature','dataset','finding','report','guideline','other')),
  storage_backend   text NOT NULL DEFAULT 'local' CHECK (storage_backend IN ('local','r2','s3')),
  storage_key       text NOT NULL,                   -- path or object key of the raw file
  original_filename text,
  mime_type         text,
  size_bytes        bigint,
  sha256            char(64) UNIQUE,                 -- dedupe + integrity (provenance)
  language          text DEFAULT 'en',
  metadata          jsonb NOT NULL DEFAULT '{}',     -- subtype, year, study_type... (retrieval filters)
  -- the trust gate: nothing is retrievable until a human reviewer approves --
  status            text NOT NULL DEFAULT 'pending_review' CHECK (status IN
                      ('pending_review','processing','approved','rejected','error')),
  review_notes      text,
  reviewed_by       uuid REFERENCES users(id) ON DELETE SET NULL,
  reviewed_at       timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS materials_status_idx     ON materials(status);
CREATE INDEX IF NOT EXISTS materials_uploader_idx   ON materials(uploaded_by);
CREATE INDEX IF NOT EXISTS materials_metadata_idx   ON materials USING gin (metadata jsonb_path_ops);

-- ── Material chunks: the retrieval units (passages). This is what the copilot
--    searches and feeds to Groq. tsv = keyword (v0); embedding = semantic (v1). ──
CREATE TABLE IF NOT EXISTS material_chunks (
  id            bigserial PRIMARY KEY,
  material_id   uuid NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
  chunk_index   int  NOT NULL,
  content       text NOT NULL,
  token_count   int,
  tsv           tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
  -- dim 384 = small CPU-friendly embedder (bge-small-en / all-MiniLM-L6-v2).
  -- CHANGE this number to match whatever embedder you pick (768, 1024, ...).
  embedding     vector(384),
  metadata      jsonb NOT NULL DEFAULT '{}',         -- page, section, heading
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (material_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS chunks_tsv_idx   ON material_chunks USING gin (tsv);              -- keyword (v0)
CREATE INDEX IF NOT EXISTS chunks_trgm_idx  ON material_chunks USING gin (content gin_trgm_ops);
-- Semantic index (v1): create AFTER you have data + a chosen embedder. HNSW build
-- needs RAM, so on the 1 GB box build it during a quiet moment (or use ivfflat):
--   CREATE INDEX chunks_embedding_idx ON material_chunks USING hnsw (embedding vector_cosine_ops);

-- ── Audit log: provenance / who did what (signup, login, upload, approve...) ──
CREATE TABLE IF NOT EXISTS audit_log (
  id            bigserial PRIMARY KEY,
  user_id       uuid REFERENCES users(id) ON DELETE SET NULL,
  action        text NOT NULL,
  entity_type   text,
  entity_id     text,
  detail        jsonb NOT NULL DEFAULT '{}',
  ip            inet,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_created_idx ON audit_log(created_at);

-- ── Privileges: the app connects as openelpis_app (DML only; not table owner) ──
GRANT USAGE ON SCHEMA public TO openelpis_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO openelpis_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO openelpis_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO openelpis_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO openelpis_app;
