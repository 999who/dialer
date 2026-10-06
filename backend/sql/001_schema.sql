-- EMANAGER Dialer · knowledge base + call logs
-- Run in Supabase: SQL Editor -> paste -> Run (idempotent).

create extension if not exists vector;

-- ---------------------------------------------------------------- knowledge base
-- kind:
--   regulation  fragment of an official company regulation / price list
--   qa          verified question -> answer pair (often cut from successful calls)
--   objection   objection -> recommended response
--   script      next step of the call script (qualification, closing on a date…)
create table if not exists kb_items (
  id           bigint generated always as identity primary key,
  kind         text not null check (kind in ('regulation','qa','objection','script')),
  topic        text not null default '',          -- e.g. 'cena', 'crm', 'umowa' -> shown as "OBIEKCJA · CENA"
  title        text not null default '',          -- chip label in the overlay, e.g. 'Regulamin rabatów § 4.2'
  question     text not null default '',          -- what the client says / asks (embedded)
  answer       text not null default '',          -- full answer for the LLM
  hint         text not null default '',          -- ready ≤12-word hint for the operator
  content      text not null default '',          -- regulation chunk text (embedded when question is empty)
  verified     boolean not null default true,     -- false => LLM must not quote exact amounts
  source_ref   text not null default '',          -- link / document reference
  language     text not null default 'pl',
  active       boolean not null default true,
  content_hash text unique,                       -- idempotent re-ingest
  embedding    vector(384) not null,              -- intfloat/multilingual-e5-small, normalised
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

create index if not exists kb_items_embedding_hnsw
  on kb_items using hnsw (embedding vector_cosine_ops) with (m = 16, ef_construction = 64);
create index if not exists kb_items_kind_idx on kb_items (kind) where active;

-- Top-k by cosine similarity. Order+limit first so the HNSW index is used,
-- then drop weak matches.
create or replace function match_kb(query_embedding vector(384), match_count int default 4,
                                    min_similarity float default 0.0)
returns table (id bigint, kind text, topic text, title text, question text, answer text, hint text,
               content text, verified boolean, source_ref text, similarity float)
language sql stable as $$
  select * from (
    select k.id, k.kind, k.topic, k.title, k.question, k.answer, k.hint, k.content, k.verified,
           k.source_ref, 1 - (k.embedding <=> query_embedding) as similarity
    from kb_items k
    where k.active
    order by k.embedding <=> query_embedding
    limit match_count
  ) t
  where t.similarity >= min_similarity
  order by t.similarity desc;
$$;

-- ---------------------------------------------------------------- call logs
-- Used for analytics, the "Otwórz transkrypcję" button, and for cutting new
-- Q&A pairs out of successful calls (tools/extract_qa.py).
create table if not exists calls (
  id            uuid primary key,
  agent_id      text not null default '',
  phone_masked  text not null default '',
  started_at    timestamptz not null default now(),
  ended_at      timestamptz,
  summary       text,
  outcome       text                               -- fill from CRM later: 'meeting', 'lost', …
);

create table if not exists call_utterances (
  id        bigint generated always as identity primary key,
  call_id   uuid not null references calls(id) on delete cascade,
  speaker   text not null check (speaker in ('operator','client')),
  text      text not null,
  t_start   real not null,
  t_end     real not null,
  created_at timestamptz not null default now()
);
create index if not exists call_utterances_call_idx on call_utterances (call_id, t_start);

create table if not exists call_hints (
  id           uuid primary key,
  call_id      uuid not null references calls(id) on delete cascade,
  category     text not null,
  hint         text not null,
  kb_item_ids  bigint[] not null default '{}',
  latency_ms   int,
  useful       boolean,        -- 👍 / 👎 in the overlay
  copied       boolean,        -- "Kopiuj" pressed
  created_at   timestamptz not null default now()
);
create index if not exists call_hints_call_idx on call_hints (call_id);

-- The backend connects with the Postgres connection string (service role),
-- so RLS stays on and nothing is exposed through the public REST API.
alter table kb_items        enable row level security;
alter table calls           enable row level security;
alter table call_utterances enable row level security;
alter table call_hints      enable row level security;
