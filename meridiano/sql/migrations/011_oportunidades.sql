-- Mesa de oportunidades: ideias de como ganhar dinheiro de forma legítima, testadas rápido e descartadas sem apego.
CREATE TABLE IF NOT EXISTS opportunities (
  id           SERIAL PRIMARY KEY,
  idea         TEXT NOT NULL,
  audience     TEXT NOT NULL DEFAULT '',
  how_it_earns TEXT NOT NULL DEFAULT '',
  effort       TEXT NOT NULL DEFAULT 'médio' CHECK (effort IN ('baixo','médio','alto')),
  risk         TEXT NOT NULL DEFAULT 'médio' CHECK (risk IN ('baixo','médio','alto')),
  needs_owner  TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT 'ideia' CHECK (status IN ('ideia','teste','ativa','descartada')),
  learning     TEXT NOT NULL DEFAULT '',          -- aprendizado informado pelo agente (NÃO é medição)
  metrics      TEXT NOT NULL DEFAULT '{}',        -- medições reais; só o código do servidor escreve aqui
  created_by   INTEGER REFERENCES employees(id),
  updated_by   INTEGER REFERENCES employees(id),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS opportunities_status ON opportunities(status);
