export const getToken = () => { try { return localStorage.getItem('mtoken') || ''; } catch { return ''; } };
export const setToken = (t) => { try { localStorage.setItem('mtoken', t); } catch {} };
// Administrador: sessão do Google (cookie HttpOnly) + cabeçalho X-CSRF. O token de recuperação do servidor é opcional e fica só nesta aba.
let csrf = '';
export const setCsrf = (c) => { csrf = c || ''; };
export const getRecovery = () => { try { return sessionStorage.getItem('mrec') || ''; } catch { return ''; } };
export const setRecovery = (t) => { try { t ? sessionStorage.setItem('mrec', t) : sessionStorage.removeItem('mrec'); } catch {} };
export async function admin(method, path, body) {
  const headers = { 'Content-Type': 'application/json' };
  const rec = getRecovery();
  if (rec) headers['X-Admin-Token'] = rec; else headers['X-CSRF'] = csrf;
  const r = await fetch('api/' + path, { method, headers, credentials: 'same-origin', body: body === undefined ? undefined : JSON.stringify(body) });
  let j = {};
  try { j = await r.json(); } catch {}
  if (r.status === 401) throw new Error('Entre com o Google na aba Configurações.');
  if (r.status === 429) throw new Error('Muitas tentativas. Aguarde alguns minutos.');
  if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : 'Não foi possível concluir (confira os campos).');
  return j;
}
export async function api(path) {
  const r = await fetch('api/' + path, { headers: { 'X-Token': getToken() }, cache: 'no-store' });
  if (r.status === 401) throw new Error('401');
  if (!r.ok) throw new Error(String(r.status));
  return r.json();
}
export async function post(path) {
  const r = await fetch('api/' + path, { method: 'POST', headers: { 'X-Token': getToken() } });
  if (!r.ok) throw new Error(String(r.status));
  return r.json();
}
