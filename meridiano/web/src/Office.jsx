import { useEffect, useRef } from 'react';

// Escritório em pixel art: mesas em grade, uma por funcionário real. Quem está
// "working" no banco pisca a tela; quem acabou de falar mostra um balão com a fala real.
const W = 240, COLS = 5, CW = 46, RH = 44;
const hash = (s) => [...s].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7);
const skin = (s) => `hsl(${hash(s) % 360} 55% 55%)`;

export function layout(n) {
  return Array.from({ length: n }, (_, i) => ({ x: 22 + (i % COLS) * CW, y: 40 + Math.floor(i / COLS) * RH }));
}

export default function Office({ employees, speech, selected, onSelect }) {
  const ref = useRef(null);
  const st = useRef({ employees, speech, selected });
  st.current = { employees, speech, selected };
  const H = Math.max(120, 40 + Math.ceil(Math.max(employees.length, 1) / COLS) * RH + 10);

  useEffect(() => {
    const cv = ref.current, g = cv.getContext('2d');
    g.imageSmoothingEnabled = false;
    let raf, t = 0;
    const R = (c, x, y, w, h) => { g.fillStyle = c; g.fillRect(x | 0, y | 0, w, h); };
    const frame = () => {
      t += 1;
      const { employees: es, speech: sp, selected: sel } = st.current;
      R('#142522', 0, 0, W, cv.height);
      R('#1f3733', 0, 0, W, 6);
      const pos = layout(es.length);
      es.forEach((e, i) => {
        const { x, y } = pos[i];
        const working = e.status === 'working';
        R('#6d4c30', x - 11, y - 10, 22, 9); R('#8a6340', x - 11, y - 10, 22, 2);
        R('#222', x - 4, y - 16, 9, 6); R(working && (t >> 4) % 2 ? '#fff' : '#5fd0c0', x - 3, y - 15, 7, 4);
        R('#3b3b46', x - 4, y + 4, 9, 3);
        R(skin(e.slug), x - 4, y - 2 + (working ? ((t >> 3) % 2) : 0), 8, 8);
        R('#e8c9a0', x - 3, y - 7, 6, 5);
        if (sel === e.slug) { g.strokeStyle = '#5fd0c0'; g.strokeRect(x - 13.5, y - 18.5, 27, 29); }
        const line = sp[e.slug];
        if (line) {
          g.font = '6px monospace';
          const txt = line.length > 22 ? line.slice(0, 21) + '…' : line;
          const w = g.measureText(txt).width + 6, bx = Math.min(W - w - 2, Math.max(2, x - w / 2));
          R('#fff', bx, y - 30, w, 10); g.fillStyle = '#111'; g.fillText(txt, bx + 3, y - 23);
        }
      });
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [H]);

  const click = (ev) => {
    const r = ref.current.getBoundingClientRect();
    const px = ((ev.clientX - r.left) / r.width) * W, py = ((ev.clientY - r.top) / r.height) * H;
    const pos = layout(employees.length);
    const i = pos.findIndex((p) => Math.abs(px - p.x) < 14 && py > p.y - 20 && py < p.y + 12);
    onSelect(i >= 0 ? employees[i].slug : null);
  };
  return <canvas ref={ref} width={W} height={H} onClick={click} aria-label="Escritório virtual com os funcionários" />;
}
