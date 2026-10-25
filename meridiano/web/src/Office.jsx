import { useEffect, useRef } from 'react';

/* Escritório vivo. Tudo que mostra uma AÇÃO vem do banco (atividade real, mensagens reais).
   Só o passeio pela copa/corredor de quem está parado é animação ambiente (rotulado no painel). */
const W = 240, CX = 7, PX = 40, PY = 34, COLS = 5, X0 = 34;
const hash = (s) => [...String(s)].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7);
const SKIN = ['#f1c9a5', '#e0ac82', '#c68a5e', '#9a6a45', '#6f4a2e'];
const HAIR = ['#222', '#4a3020', '#7a4a20', '#c9a24a', '#a33', '#ddd'];
export const look = (slug) => { const h = hash(slug); return { skin: SKIN[h % 5], hair: HAIR[(h >> 3) % 6], long: ((h >> 6) & 1) === 1, shirt: `hsl(${(h >> 8) % 360} 55% 52%)` }; };
const areaColor = (a) => `hsl(${hash(a) % 360} 24% 21%)`;

export function computeLayout(emps) {
  const areas = [];
  emps.forEach((e) => { let a = areas.find((x) => x.name === e.area); if (!a) areas.push(a = { name: e.area, list: [] }); a.list.push(e); });
  const halls = [46];                       // hall[0]: abaixo da sala de reunião
  const rooms = [{ name: 'Reunião', y0: 0, hall: 46 }];
  const desks = {};
  let y = 50;
  areas.forEach((a, i) => {
    const rows = Math.ceil(a.list.length / COLS);
    const room = { name: a.name, y0: y, hall: y + 12 + rows * PY + 6, color: areaColor(a.name), rows };
    a.list.forEach((e, j) => { desks[e.slug] = { x: X0 + (j % COLS) * PX, y: y + 12 + Math.floor(j / COLS) * PY + 22, room: i + 1 }; });
    rooms.push(room); halls.push(room.hall); y = room.hall + 4;
  });
  const copa = { y0: y, hall: y + 14 };
  rooms.push({ name: 'Copa', y0: y, hall: copa.hall }); halls.push(copa.hall);
  return { H: copa.hall + 22, rooms, halls, desks, copaRoom: rooms.length - 1, door: { x: 5, y: copa.hall } };
}

const SLOTS = [[84, 36], [100, 36], [116, 36], [132, 36], [90, 6], [126, 6]];
const clip = (t, n) => (t.length > n ? t.slice(0, n - 1) + '…' : t);

function pathTo(sp, L, tx, ty, troom) {
  let room = sp.room;
  if (sp.path.length) {                      // interrompido no meio do caminho: parte do corredor mais próximo
    let bd = 1e9; L.halls.forEach((h, i) => { if (Math.abs(h - sp.y) < bd) { bd = Math.abs(h - sp.y); room = i; } });
  }
  sp.destRoom = troom;
  const hf = L.halls[room] ?? sp.y, ht = L.halls[troom] ?? ty;
  const pts = [[sp.x, hf], [CX, hf], [CX, ht], [tx, ht], [tx, ty]];
  return pts.filter((p, i) => i === 0 || p[0] !== pts[i - 1][0] || p[1] !== pts[i - 1][1]);
}

export default function Office({ employees, selected, onSelect, reduced }) {
  const ref = useRef(null);
  const S = useRef({ sprites: {}, seen: null, L: null });
  const L = computeLayout(employees);
  S.current.L = L;
  const emps = useRef(employees); emps.current = employees;
  const sel = useRef(selected); sel.current = selected;

  // sincroniza sprites com os dados reais a cada atualização (a cada 5 s)
  useEffect(() => {
    const st = S.current, now = Date.now();
    if (!employees.length) return;
    const slugs = new Set(employees.map((e) => e.slug));
    employees.forEach((e) => {
      let sp = st.sprites[e.slug];
      const home = L.desks[e.slug];
      if (!sp) {
        const entering = st.seen !== null;       // contratado depois da primeira carga: entra pela porta
        sp = st.sprites[e.slug] = entering ? { x: L.door.x, y: L.door.y, room: L.copaRoom, path: [] } : { x: home.x, y: home.y, room: home.room, path: [] };
        sp.look = look(e.slug); sp.name = e.name; sp.amb = 10 + Math.random() * 30; sp.key = ''; sp.away = false; sp.ambOut = false;
        if (entering) sp.path = pathTo(sp, L, home.x, home.y, home.room);
      }
      sp.leaving = false; sp.emp = e; sp.home = home;
      const age = e.atividade_em ? now - new Date(e.atividade_em).getTime() : Infinity;
      const recent = age < 120000 && e.atividade_tipo && e.atividade_tipo !== 'parado';
      sp.kind = recent ? e.atividade_tipo : null;
      let goal = null;
      if (recent && e.atividade_tipo === 'conversando' && e.atividade_alvo && L.desks[e.atividade_alvo]) {
        const t = L.desks[e.atividade_alvo]; goal = { x: t.x + 16, y: t.y, room: t.room };
      } else if (recent && e.atividade_tipo === 'reunião') {
        const slot = SLOTS[hash(e.slug) % SLOTS.length]; goal = { x: slot[0], y: slot[1], room: 0 };
      }
      const key = e.slug + '|' + e.atividade_em;
      if (recent && key !== sp.key) {            // atividade nova: balão com o texto real
        sp.key = key;
        if (e.atividade_tipo === 'conversando') sp.bubble = { text: e.atividade_texto, until: now + 9000 };
        if (goal) { sp.path = pathTo(sp, L, goal.x, goal.y, goal.room); sp.away = true; sp.ambOut = false; }
        else if (sp.away || sp.ambOut) { sp.path = pathTo(sp, L, home.x, home.y, home.room); sp.away = false; sp.ambOut = false; }
        sp.amb = 20 + Math.random() * 30;
      } else if (!recent && sp.away && !sp.path.length) {
        sp.path = pathTo(sp, L, home.x, home.y, home.room); sp.away = false;   // acabou o assunto: volta para a mesa
      }
    });
    Object.keys(st.sprites).forEach((k) => {   // demitido: levanta e sai pela porta
      if (!slugs.has(k) && !st.sprites[k].leaving) { const sp = st.sprites[k]; sp.leaving = true; sp.path = pathTo(sp, L, L.door.x, L.door.y, L.copaRoom); sp.kind = null; }
    });
    // se o layout mudou (gente nova/saiu), sprites sentados acompanham a mesa
    employees.forEach((e) => { const sp = st.sprites[e.slug], h = L.desks[e.slug]; if (sp && !sp.path.length && !sp.away && !sp.ambOut) { sp.x = h.x; sp.y = h.y; sp.room = h.room; } });
    st.seen = slugs;
  }, [employees]);

  useEffect(() => {
    const cv = ref.current, g = cv.getContext('2d');
    g.imageSmoothingEnabled = false;
    let raf, last = performance.now(), t = 0;
    const R = (c, x, y, w, h) => { g.fillStyle = c; g.fillRect(Math.round(x), Math.round(y), w, h); };
    const frame = (nowMs) => {
      g.imageSmoothingEnabled = false;
      const dt = Math.min(0.1, (nowMs - last) / 1000); last = nowMs; t += dt;
      const st = S.current, Ly = st.L, speed = reduced ? 400 : 32;
      // movimento
      Object.entries(st.sprites).forEach(([slug, sp]) => {
        const nx = sp.path[0];
        if (nx) {
          const dx = nx[0] - sp.x, dy = nx[1] - sp.y, d = Math.hypot(dx, dy), step = speed * dt;
          if (d <= step) { sp.x = nx[0]; sp.y = nx[1]; sp.path.shift(); } else { sp.x += (dx / d) * step; sp.y += (dy / d) * step; }
          sp.walk = true;
        } else {
          sp.walk = false;
          if (sp.leaving) { delete st.sprites[slug]; return; }
          if (sp.destRoom !== undefined) { sp.room = sp.destRoom; }
          // ambiente: parado, de vez em quando passeia pela copa ou corredor (só animação)
          if (!reduced && !sp.kind && !sp.away && sp.emp) {
            sp.amb -= dt;
            if (sp.amb <= 0) {
              if (sp.ambOut) { sp.path = pathTo(sp, Ly, sp.home.x, sp.home.y, sp.home.room); sp.ambOut = false; sp.amb = 25 + Math.random() * 40; }
              else {
                const spot = Math.random() < 0.5 ? { x: 60 + Math.random() * 120, y: Ly.rooms[Ly.copaRoom].hall, room: Ly.copaRoom } : { x: 40 + Math.random() * 150, y: Ly.halls[sp.home.room], room: sp.home.room };
                sp.path = pathTo(sp, Ly, spot.x, spot.y, spot.room); sp.ambOut = true; sp.amb = 6 + Math.random() * 8;
              }
            }
          }
        }
        if (sp.bubble && sp.bubble.until < Date.now()) sp.bubble = null;
      });
      // cenário
      R('#142522', 0, 0, W, Ly.H); R('#1f3733', CX - 4, 0, 8, Ly.H);
      Ly.rooms.forEach((r, i) => {
        if (i === 0) { R('#1a2e2b', 14, 0, W - 14, 44); R('#6d4c30', 82, 14, 62, 14); R('#8a6340', 82, 14, 62, 2); }
        else if (i < Ly.rooms.length - 1) R(r.color + '', 14, r.y0, W - 14, r.hall - r.y0 - 2);
        else { R('#1a2e2b', 14, r.y0, W - 14, 36); R('#8a3b3b', 24, r.y0 + 4, 40, 10); R('#cfd8dc', 120, r.y0 + 4, 16, 12); R('#5a432b', 160, r.y0 + 6, 40, 7); R('#3b3b46', 4, r.hall + 2, 6, 8); R('#c9a24a', 3, r.hall - 8, 8, 14); }
        R('#1f3733', 14, r.hall - 2, W - 14, 12);
        g.font = '7px monospace'; g.fillStyle = '#ffffffaa'; g.fillText(clip(r.name, 30), 18, r.y0 + 8);
      });
      Object.entries(Ly.desks).forEach(([slug, d]) => {
        R('#6d4c30', d.x - 13, d.y - 21, 26, 11); R('#8a6340', d.x - 13, d.y - 21, 26, 2);
        R('#222', d.x - 4, d.y - 27, 9, 6); R('#5fd0c0', d.x - 3, d.y - 26, 7, 4);
        const e = emps.current.find((x) => x.slug === slug);
        if (e) { g.font = '5px monospace'; g.fillStyle = '#e8f1ef'; g.fillText(clip(e.name, 9), d.x - 12, d.y + 9); }
        R('#3b3b46', d.x - 4, d.y - 2, 9, 3);
      });
      // bonecos (ordem por y)
      Object.values(st.sprites).sort((a, b) => a.y - b.y).forEach((sp) => {
        const { skin, hair, long, shirt } = sp.look, bob = sp.walk ? Math.floor(t * 8) % 2 : 0, x = sp.x, y = sp.y - 16 + bob;
        if (sel.current === sp.emp?.slug) { g.strokeStyle = '#5fd0c0'; g.strokeRect(Math.round(x) - 7.5, Math.round(y) - 3.5, 15, 22); }
        R(shirt, x - 4, y + 7, 8, 8); R('#2b2b3a', x - 4, y + 15, 3, 3 - bob); R('#2b2b3a', x + 1, y + 15, 3, 2 + bob);
        R(skin, x - 3, y, 6, 6); R(hair, x - 3, y - 1, 6, 2); if (long) { R(hair, x - 4, y, 1, 6); R(hair, x + 3, y, 1, 6); }
        if (!sp.leaving && !sp.walk && sp.kind === 'escrevendo_produto') { R('#ddd', x - 5, y + 16, 10, 2); }
        if (!sp.walk && sp.kind) {
          const ic = { escrevendo_produto: '⌨', pesquisando: '🔍', reunião: '👥', contratando: '➕', demitindo: '➖', planejando: '📋' }[sp.kind];
          if (ic && !sp.bubble) { g.font = '7px sans-serif'; g.fillStyle = '#fff'; g.fillText(ic, x - 4, y - 3); }
        }
        if (sp.bubble) {
          g.font = '5px monospace';
          const words = clip(sp.bubble.text || '', 34), l1 = words.slice(0, 17), l2 = words.slice(17), w = Math.max(g.measureText(l1).width, g.measureText(l2).width) + 6;
          const bx = Math.max(16, Math.min(W - w - 2, x - w / 2)), by = y - (l2 ? 17 : 11);
          R('#fff', bx, by, w, l2 ? 13 : 8); g.fillStyle = '#111'; g.fillText(l1, bx + 3, by + 6); if (l2) g.fillText(l2, bx + 3, by + 11);
        }
      });
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [reduced]);

  const click = (ev) => {
    const r = ref.current.getBoundingClientRect(), px = ((ev.clientX - r.left) / r.width) * W, py = ((ev.clientY - r.top) / r.height) * L.H;
    let best = null, bd = 1e9;
    Object.values(S.current.sprites).forEach((sp) => { if (!sp.emp || sp.leaving) return; const d = Math.hypot(px - sp.x, py - (sp.y - 8)); if (d < bd) { bd = d; best = sp.emp.slug; } });
    onSelect(bd < 20 ? best : null);   // alvo de toque generoso (20 px lógicos ≈ 40+ px na tela)
  };
  return <canvas ref={ref} width={W} height={L.H} onClick={click} aria-label="Escritório virtual com os funcionários" />;
}
