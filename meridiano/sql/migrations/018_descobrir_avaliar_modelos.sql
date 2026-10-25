-- Descobrir modelos grátis (Nous/OpenRouter), propostas de agentes e avaliação padrão de cada perfil. Aditiva.
ALTER TABLE llm_profiles ADD COLUMN IF NOT EXISTS needs_eval BOOLEAN NOT NULL DEFAULT FALSE;   -- perfil novo: só liga depois de passar na avaliação

CREATE TABLE IF NOT EXISTS llm_candidates (
  id            SERIAL PRIMARY KEY,
  source        TEXT NOT NULL DEFAULT 'descoberta' CHECK (source IN ('descoberta','agente')),
  provider      TEXT NOT NULL DEFAULT 'openai_compat' CHECK (provider IN ('openai_compat','anthropic')),
  base_url      TEXT NOT NULL,
  model         TEXT NOT NULL,
  key_name      TEXT NOT NULL,
  name          TEXT NOT NULL DEFAULT '',
  context_length INTEGER,
  free          BOOLEAN NOT NULL DEFAULT FALSE,           -- preço zero informado pelo provedor (descoberta); proposta de agente nasce não-grátis
  tools         TEXT NOT NULL DEFAULT 'desconhecido' CHECK (tools IN ('sim','nao','desconhecido')),
  price_in_per_m  NUMERIC(10,4) NOT NULL DEFAULT 0,
  price_out_per_m NUMERIC(10,4) NOT NULL DEFAULT 0,
  proposer_id   INTEGER REFERENCES employees(id),
  reason        TEXT NOT NULL DEFAULT '',
  status        TEXT NOT NULL DEFAULT 'novo' CHECK (status IN ('novo','adotado','descartado')),
  profile_id    INTEGER REFERENCES llm_profiles(id),
  discovered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (base_url, model)
);

CREATE TABLE IF NOT EXISTS llm_evals (
  id             SERIAL PRIMARY KEY,
  profile_id     INTEGER NOT NULL REFERENCES llm_profiles(id) ON DELETE CASCADE,
  status         TEXT NOT NULL DEFAULT 'rodando' CHECK (status IN ('rodando','concluida','erro')),
  started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at    TIMESTAMPTZ,
  passed         INTEGER NOT NULL DEFAULT 0,
  total          INTEGER NOT NULL DEFAULT 0,
  avg_latency_ms INTEGER,
  tokens         INTEGER NOT NULL DEFAULT 0,
  cost_usd       NUMERIC(12,6) NOT NULL DEFAULT 0,
  note           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS llm_evals_profile ON llm_evals(profile_id, id DESC);

CREATE TABLE IF NOT EXISTS llm_eval_cases (
  id                SERIAL PRIMARY KEY,
  eval_id           INTEGER NOT NULL REFERENCES llm_evals(id) ON DELETE CASCADE,
  case_name         TEXT NOT NULL,
  ok                BOOLEAN NOT NULL,
  latency_ms        INTEGER NOT NULL DEFAULT 0,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  detail            TEXT NOT NULL DEFAULT ''
);
