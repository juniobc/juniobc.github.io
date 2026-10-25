export const getToken = () => { try { return localStorage.getItem('mtoken') || ''; } catch { return ''; } };
export const setToken = (t) => { try { localStorage.setItem('mtoken', t); } catch {} };
export async function api(path) {
  const r = await fetch('api/' + path, { headers: { 'X-Token': getToken() }, cache: 'no-store' });
  if (r.status === 401) throw new Error('401');
  if (!r.ok) throw new Error(String(r.status));
  return r.json();
}
