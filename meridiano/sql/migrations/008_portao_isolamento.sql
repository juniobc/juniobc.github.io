-- Portão que falha fechado: ferramentas só ligam quando o autoteste de isolamento passa.
ALTER TABLE custom_tools DROP CONSTRAINT IF EXISTS custom_tools_status_check;
ALTER TABLE custom_tools ADD CONSTRAINT custom_tools_status_check
  CHECK (status IN ('rascunho','revisao','pendente_dono','ativa','recusada'));
ALTER TABLE engine_status ADD COLUMN IF NOT EXISTS tools_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE engine_status ADD COLUMN IF NOT EXISTS tools_reason TEXT NOT NULL DEFAULT 'não verificado';
ALTER TABLE engine_status ADD COLUMN IF NOT EXISTS tools_help TEXT NOT NULL DEFAULT '';
ALTER TABLE engine_status ADD COLUMN IF NOT EXISTS tools_backend TEXT NOT NULL DEFAULT '';
