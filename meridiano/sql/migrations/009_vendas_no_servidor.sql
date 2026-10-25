-- Mudança de rumo: vendas no próprio servidor (Mercado Pago por API). Nada de contas em marketplaces.
ALTER TABLE owner_requests ADD COLUMN IF NOT EXISTS resolution TEXT NOT NULL DEFAULT '';
UPDATE owner_requests
   SET done = TRUE, resolution = 'substituído: as vendas passam a ser feitas no próprio servidor, com Mercado Pago'
 WHERE NOT done;
