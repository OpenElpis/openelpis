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

-- ============================================================================
-- COMMUNITY LAYER (added June 2026): invitation-gated signup, a small trusted
-- network for clinicians (forum, friends, direct messages), and the shareable
-- "saved answer" unit. All additive + idempotent — safe to re-apply.
-- ============================================================================

-- ── Invitations: signup is invite-only. Any active member can mint a one-time
--    link (14-day expiry); admins can target an email / set the role. Only the
--    sha256 HASH of the token is stored — the raw token lives only in the link. ──
CREATE TABLE IF NOT EXISTS invitations (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  token_hash     text UNIQUE NOT NULL,
  created_by     uuid REFERENCES users(id) ON DELETE SET NULL,
  email          citext,                          -- optional: locks the invite to one address
  intended_role  text NOT NULL DEFAULT 'contributor'
                   CHECK (intended_role IN ('contributor','reviewer','admin')),
  note           text,
  expires_at     timestamptz NOT NULL,
  used_at        timestamptz,
  used_by        uuid REFERENCES users(id) ON DELETE SET NULL,
  revoked_at     timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS invitations_creator_idx ON invitations(created_by);

-- ── Access requests: someone WITHOUT an invite asks to join; an admin reviews
--    and (if a real clinician) issues an invitation. ──
CREATE TABLE IF NOT EXISTS access_requests (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  full_name       text NOT NULL,
  email           citext NOT NULL,
  org_name        text,
  org_type        text,
  country         text,
  credential_type text,
  credential_ref  text,
  message         text,
  status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','rejected','invited')),
  reviewed_by     uuid REFERENCES users(id) ON DELETE SET NULL,
  reviewed_at     timestamptz,
  invitation_id   uuid REFERENCES invitations(id) ON DELETE SET NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS access_requests_status_idx ON access_requests(status);

-- ── Connections: the friend graph — one row per unordered pair, any direction. ──
CREATE TABLE IF NOT EXISTS connections (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  requester_id  uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  addressee_id  uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status        text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','accepted','declined','blocked')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  responded_at  timestamptz,
  CHECK (requester_id <> addressee_id)
);
-- one connection per unordered pair (a↔b is the same as b↔a):
CREATE UNIQUE INDEX IF NOT EXISTS connections_pair_idx
  ON connections (LEAST(requester_id,addressee_id), GREATEST(requester_id,addressee_id));

-- ── Direct messages: 1:1 chat between members. A message can carry a "share"
--    (a material or a saved answer) so members can discuss it. ──
CREATE TABLE IF NOT EXISTS direct_messages (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  sender_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  recipient_id  uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  body          text,
  share_kind    text CHECK (share_kind IN ('material','answer')),
  share_ref     jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  read_at       timestamptz,
  CHECK (body IS NOT NULL OR share_kind IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS dm_thread_idx ON direct_messages(recipient_id, sender_id, created_at);
CREATE INDEX IF NOT EXISTS dm_sender_idx ON direct_messages(sender_id, recipient_id, created_at);

-- ── Saved answers: a copilot result a member kept / shared. (The real RAG
--    copilot isn't built yet — `sources` is empty for placeholder answers.) ──
CREATE TABLE IF NOT EXISTS saved_answers (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  question      text NOT NULL,
  answer        text NOT NULL,
  sources       jsonb NOT NULL DEFAULT '[]',
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS saved_answers_user_idx ON saved_answers(user_id);

-- ── Forum: question topics + threaded replies. A topic/post can carry a share. ──
CREATE TABLE IF NOT EXISTS forum_topics (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  author_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  category         text NOT NULL DEFAULT 'general'
                     CHECK (category IN ('general','cases','research','platform')),
  title            text NOT NULL,
  body             text NOT NULL,
  tags             text[] NOT NULL DEFAULT '{}',
  reply_count      int NOT NULL DEFAULT 0,
  last_activity_at timestamptz NOT NULL DEFAULT now(),
  is_pinned        boolean NOT NULL DEFAULT false,
  is_locked        boolean NOT NULL DEFAULT false,
  status           text NOT NULL DEFAULT 'open' CHECK (status IN ('open','removed')),
  share_kind       text CHECK (share_kind IN ('material','answer')),
  share_ref        jsonb,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS forum_topics_activity_idx ON forum_topics(last_activity_at DESC);
CREATE INDEX IF NOT EXISTS forum_topics_category_idx ON forum_topics(category);

CREATE TABLE IF NOT EXISTS forum_posts (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  topic_id      uuid NOT NULL REFERENCES forum_topics(id) ON DELETE CASCADE,
  author_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  body          text NOT NULL,
  share_kind    text CHECK (share_kind IN ('material','answer')),
  share_ref     jsonb,
  status        text NOT NULL DEFAULT 'visible' CHECK (status IN ('visible','removed')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  edited_at     timestamptz
);
CREATE INDEX IF NOT EXISTS forum_posts_topic_idx ON forum_posts(topic_id, created_at);

-- ── users: community profile + invite provenance (additive columns) ──
ALTER TABLE users ADD COLUMN IF NOT EXISTS invited_by uuid REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS specialty  text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS bio        text;

-- ── Privileges: the app connects as openelpis_app (DML only; not table owner) ──
GRANT USAGE ON SCHEMA public TO openelpis_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO openelpis_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO openelpis_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO openelpis_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO openelpis_app;
