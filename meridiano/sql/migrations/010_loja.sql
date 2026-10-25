-- Loja pública: texto de venda por produto (campos estruturados, nunca HTML) e identificação do vendedor.
ALTER TABLE products ADD COLUMN IF NOT EXISTS legal_note TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS product_store (
  product_id  INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
  slug        TEXT UNIQUE NOT NULL,
  headline    TEXT NOT NULL,
  subtitle    TEXT NOT NULL DEFAULT '',
  pitch       TEXT NOT NULL DEFAULT '',
  benefits    TEXT NOT NULL DEFAULT '[]',   -- JSON em texto: bancos SQL_ASCII recusam \u escapes em JSONB
  audience    TEXT NOT NULL DEFAULT '',
  faq         TEXT NOT NULL DEFAULT '[]',
  written_by  INTEGER REFERENCES employees(id),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS store_settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
