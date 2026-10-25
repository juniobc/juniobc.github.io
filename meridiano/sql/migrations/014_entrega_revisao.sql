-- Entrega por link único e temporário; revisão da Presidente antes de abrir a compra.
CREATE TABLE IF NOT EXISTS downloads (
  id         SERIAL PRIMARY KEY,
  order_id   INTEGER NOT NULL UNIQUE REFERENCES orders(id),
  token      TEXT UNIQUE NOT NULL,                     -- aleatório; é a chave dos botões de download
  expires_at TIMESTAMPTZ NOT NULL,
  max_count  INTEGER NOT NULL DEFAULT 5,
  count      INTEGER NOT NULL DEFAULT 0,
  revoked    BOOLEAN NOT NULL DEFAULT FALSE,           -- reembolso ou chargeback revogam
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE product_store ADD COLUMN IF NOT EXISTS review_status TEXT NOT NULL DEFAULT 'pendente' CHECK (review_status IN ('pendente','aprovada','reescrever'));
ALTER TABLE product_store ADD COLUMN IF NOT EXISTS review_note TEXT NOT NULL DEFAULT '';
ALTER TABLE product_store ADD COLUMN IF NOT EXISTS approved_by INTEGER REFERENCES employees(id);
ALTER TABLE product_store ADD COLUMN IF NOT EXISTS approved_hash TEXT;   -- da página aprovada: mudou o texto ou o preço, a aprovação cai
