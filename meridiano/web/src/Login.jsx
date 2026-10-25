import { useState } from 'react';
import { api, setToken } from './api.js';

export default function Login({ error, onDone }) {
  const [t, setT] = useState('');
  const [msg, setMsg] = useState('');
  const go = async () => {
    setToken(t.trim()); setMsg('');
    try { await api('state'); onDone(); } catch { setToken(''); setMsg('Token inválido. Confira e tente de novo.'); }
  };
  return (
    <div className="wrap">
      <h1>Meridiano Digital</h1>
      <section className="card">
        <p><a href="api/auth/google/start"><button type="button">Entrar com Google</button></a></p>
        <p className="note">Entra só a conta do dono. Depois do login, o painel abre sem digitar token.</p>
        <details><summary>Entrar com o token de leitura</summary>
          <input type="password" autoComplete="off" placeholder="Token de leitura" value={t} onChange={(e) => setT(e.target.value)} style={{ width: '100%', margin: '6px 0' }} />
          <button disabled={!t.trim()} onClick={go}>Entrar</button>
        </details>
        {(error || msg) && <p className="note" role="alert">{error || msg}</p>}
      </section>
    </div>
  );
}
