-- Chamadas feitas antes da correção de "resposta vazia": o agente não gravou nenhuma
-- mensagem nem tarefa, então não foram úteis e não devem consumir o limite diário.
UPDATE usage_log u SET ok = false, error = 'vazio (antes da correção)'
WHERE u.ok AND u.created_at >= date_trunc('day', now())
  AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.from_id = u.employee_id)
  AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.created_by = u.employee_id);
