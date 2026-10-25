-- Mensagens antigas (loop pedindo arquivos .txt) saem do contexto dos agentes; só as novas entram.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE;
UPDATE messages SET archived = TRUE WHERE NOT archived;

-- Plano de seções de cada produto: o agente escreve uma seção completa por turno.
CREATE TABLE IF NOT EXISTS product_sections (
  id             SERIAL PRIMARY KEY,
  product_id     INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  position       INTEGER NOT NULL,
  title          TEXT NOT NULL,
  content        TEXT NOT NULL DEFAULT '',
  needs_rewrite  BOOLEAN NOT NULL DEFAULT FALSE,
  rewrite_reason TEXT NOT NULL DEFAULT '',
  written_by     INTEGER REFERENCES employees(id),
  written_at     TIMESTAMPTZ,
  UNIQUE (product_id, position)
);
