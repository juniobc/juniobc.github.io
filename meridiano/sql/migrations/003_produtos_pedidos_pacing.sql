-- Produtos passam a guardar o conteúdo; pedidos ao dono; controle de ciclos por funcionário.
ALTER TABLE products ADD COLUMN IF NOT EXISTS content TEXT NOT NULL DEFAULT '';
ALTER TABLE products ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'rascunho';
ALTER TABLE products ADD COLUMN IF NOT EXISTS price_cents BIGINT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE employees ADD COLUMN IF NOT EXISTS last_turn_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS owner_requests (
  id         SERIAL PRIMARY KEY,
  from_id    INTEGER REFERENCES employees(id),
  body       TEXT NOT NULL,
  done       BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Limpa duplicatas que os agentes criaram antes da checagem de duplicidade.
DELETE FROM products p WHERE EXISTS (
  SELECT 1 FROM products q WHERE lower(btrim(q.name)) = lower(btrim(p.name)) AND q.id < p.id);
CREATE UNIQUE INDEX IF NOT EXISTS products_name_uq ON products (lower(btrim(name)));

UPDATE tasks SET status = 'cancelled', result = 'duplicada (limpeza automática)'
WHERE status = 'open' AND id NOT IN (
  SELECT MIN(id) FROM tasks WHERE status = 'open' GROUP BY lower(btrim(title)));

-- Tarefas que ficaram presas pedindo "arquivos .txt fonte" que não existem.
UPDATE tasks SET status = 'cancelled', result = 'não existem arquivos prontos: o próprio agente escreve o conteúdo com write_product'
WHERE status = 'open' AND (title ILIKE '%.txt%' OR details ILIKE '%.txt%');
