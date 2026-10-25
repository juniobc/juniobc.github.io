-- Meridiano Digital: esquema do banco. Idempotente (pode rodar de novo).
CREATE TABLE IF NOT EXISTS employees (
  id           SERIAL PRIMARY KEY,
  slug         TEXT UNIQUE NOT NULL,
  name         TEXT NOT NULL,
  role         TEXT NOT NULL,            -- cargo
  area         TEXT NOT NULL,            -- departamento
  instructions TEXT NOT NULL,
  model        TEXT NOT NULL,
  hired_by     INTEGER REFERENCES employees(id),
  hire_reason  TEXT NOT NULL DEFAULT '',
  active       BOOLEAN NOT NULL DEFAULT TRUE,
  status       TEXT NOT NULL DEFAULT 'idle',  -- idle | working
  activity     TEXT NOT NULL DEFAULT '',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tasks (
  id          SERIAL PRIMARY KEY,
  title       TEXT NOT NULL,
  details     TEXT NOT NULL DEFAULT '',
  assignee_id INTEGER REFERENCES employees(id),
  created_by  INTEGER REFERENCES employees(id),
  status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','cancelled')),
  result      TEXT NOT NULL DEFAULT '',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  done_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS products (
  id          SERIAL PRIMARY KEY,
  name        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  file_path   TEXT NOT NULL,
  created_by  INTEGER REFERENCES employees(id),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
  id          BIGSERIAL PRIMARY KEY,
  from_id     INTEGER REFERENCES employees(id),
  to_id       INTEGER REFERENCES employees(id),   -- NULL = mural geral
  body        TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Livro-caixa. Só o dono (ou integração de pagamento) lança receitas.
-- Agentes não têm permissão de escrita aqui: não há tool para isso.
CREATE TABLE IF NOT EXISTS ledger (
  id          BIGSERIAL PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('receita','despesa')),
  amount_cents BIGINT NOT NULL CHECK (amount_cents > 0),
  description TEXT NOT NULL,
  source      TEXT NOT NULL CHECK (source IN ('dono','pagamento')),
  external_ref TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ledger_ext_ref ON ledger(external_ref) WHERE external_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS usage_log (
  id         BIGSERIAL PRIMARY KEY,
  employee_id INTEGER REFERENCES employees(id),
  model      TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  ok         BOOLEAN NOT NULL DEFAULT TRUE,
  error      TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS usage_day ON usage_log(created_at);
CREATE INDEX IF NOT EXISTS messages_recent ON messages(id DESC);
