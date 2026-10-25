-- Fábrica de ferramentas: código escrito por agentes, validado e executado em subprocesso isolado.
CREATE TABLE IF NOT EXISTS custom_tools (
  id          SERIAL PRIMARY KEY,
  name        TEXT UNIQUE NOT NULL,
  version     INTEGER NOT NULL DEFAULT 1,
  description TEXT NOT NULL DEFAULT '',
  code        TEXT NOT NULL,
  tests_code  TEXT NOT NULL,
  level       TEXT NOT NULL DEFAULT 'A' CHECK (level IN ('A','B','C')),
  status      TEXT NOT NULL DEFAULT 'revisao' CHECK (status IN ('revisao','pendente_dono','ativa','recusada')),
  author_id   INTEGER REFERENCES employees(id),
  reason      TEXT NOT NULL DEFAULT '',
  reviewed_by INTEGER REFERENCES employees(id),
  review_note TEXT NOT NULL DEFAULT '',
  uses        INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT NOT NULL DEFAULT '',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS custom_tool_versions (
  id         SERIAL PRIMARY KEY,
  tool_id    INTEGER NOT NULL REFERENCES custom_tools(id) ON DELETE CASCADE,
  version    INTEGER NOT NULL,
  code       TEXT NOT NULL,
  tests_code TEXT NOT NULL,
  author_id  INTEGER REFERENCES employees(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tool_id, version)
);

-- Auditoria de toda execução (inclusive as dos testes de validação).
CREATE TABLE IF NOT EXISTS tool_runs (
  id          BIGSERIAL PRIMARY KEY,
  tool_id     INTEGER REFERENCES custom_tools(id) ON DELETE SET NULL,
  tool_name   TEXT NOT NULL,
  employee_id INTEGER REFERENCES employees(id),
  kind        TEXT NOT NULL CHECK (kind IN ('uso','teste')),
  args        TEXT NOT NULL DEFAULT '',
  ok          BOOLEAN NOT NULL,
  output      TEXT NOT NULL DEFAULT '',
  error       TEXT NOT NULL DEFAULT '',
  ms          INTEGER NOT NULL DEFAULT 0,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tool_runs_recent ON tool_runs(id DESC);

ALTER TABLE engine_status ADD COLUMN IF NOT EXISTS isolation TEXT NOT NULL DEFAULT 'não verificado';
