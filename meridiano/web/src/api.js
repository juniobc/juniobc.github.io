export const getToken = () => { try { return localStorage.getItem('mtoken') || ''; } catch { return ''; } };
export const setToken = (t) => { try { localStorage.setItem('mtoken', t); } catch {} };
export const getAdmin = () => { try { return localStorage.getItem('matoken') || ''; } catch { return ''; } };
export const setAdmin = (t) => { try { localStorage.setItem('matoken', t); } catch {} };
// Ações de administrador (token guardado só neste navegador, como o de leitura).
export async function admin(method, path, body) {
  const r = await fetch('api/' + path, { method, headers: { 'Content-Type': 'application/json', 'X-Admin-Token': getAdmin() }, body: body === undefined ? undefined : JSON.stringify(body) });
  let j = {};
  try { j = await r.json(); } catch {}
  if (r.status === 401) throw new Error('Token de administrador ausente ou inválido.');
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
// Envia o token do Mercado Pago. O token de admin e o do Mercado Pago ficam só na memória desta chamada: nada é guardado no navegador.
export async function sendMpToken(adminToken, accessToken) {
  const r = await fetch('api/secrets/mp-token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Admin-Token': adminToken },
    body: JSON.stringify({ access_token: accessToken }),
  });
  let j = {};
  try { j = await r.json(); } catch {}
  if (!r.ok) throw new Error(r.status === 401 ? 'Token de administrador inválido.' : (j.detail || 'Não foi possível enviar.'));
  return j;
}
export async function sendStoreSettings(adminToken, sellerName, contactEmail) {
  const r = await fetch('api/store/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Admin-Token': adminToken },
    body: JSON.stringify({ seller_name: sellerName, contact_email: contactEmail }),
  });
  let j = {};
  try { j = await r.json(); } catch {}
  if (!r.ok) throw new Error(r.status === 401 ? 'Token de administrador inválido.' : (j.detail || (r.status === 422 ? 'Confira o nome e o e-mail.' : 'Não foi possível enviar.')));
  return j;
}
