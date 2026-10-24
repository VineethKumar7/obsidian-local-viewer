"""Obsidian-style graph view: which notes link to which.

    GET /api/graph                    -> the whole vault {nodes: [{id, name, group, deg}], links: [[a, b], ...]}
    GET /api/graph?path=<note>&depth=N -> the note, every note it links to or is linked from, N steps out (1-2)

Links are [[wiki links]] (with |alias / #heading, path-qualified or not, resolved like Obsidian: same folder first,
then the shortest path) and [text](relative.md) links between markdown notes. Embeds of audio/images/PDFs are not notes
and are skipped. The index is rebuilt only when a note is added, removed or changed (count + newest mtime).

app.py puts local_html(path) under every markdown note (after the page navigation) and vault_html() under the study
dashboard on the start page. Both draw on a <canvas> with a small built-in force layout: drag to pan, wheel / pinch to
zoom, drag a dot to move it, hover to light up its neighbours, click to open the note.
"""
import json
import os
import re
import threading
from urllib.parse import unquote

from flask import jsonify, request

_vault = None
_lock = threading.Lock()
_index = {"sig": None, "nodes": {}, "adj": {}}

WIKI = re.compile(r"!?\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")
MDLINK = re.compile(r"\]\(([^)\s]+?\.md)(?:#[^)]*)?\)")
NOT_NOTE = re.compile(r"\.[A-Za-z0-9]{2,10}$")  # x.mp3, x.png, x.pdf ... (but "S1-1.2 · Guten Tag" is a note)
MEDIA = {"mp3", "m4a", "wav", "ogg", "png", "jpg", "jpeg", "gif", "webp", "svg", "pdf", "mp4", "webm", "excalidraw", "canvas", "base"}


def _md_files():
	for root, dirs, files in os.walk(_vault):
		dirs[:] = [d for d in dirs if not d.startswith(".")]
		for f in files:
			if f.endswith(".md"):
				yield os.path.relpath(os.path.join(root, f), _vault)


def _signature(paths):
	newest = 0.0
	for p in paths:
		try:
			newest = max(newest, os.path.getmtime(os.path.join(_vault, p)))
		except OSError:
			pass
	return (len(paths), newest)


def _resolve(target, src, by_name, all_paths):
	"""A wiki-link target -> vault path of the note, or None."""
	t = target.strip().replace("\\", "/")
	if t.endswith(".md"):
		t = t[:-3]
	if not t or (NOT_NOTE.search(t) and t.rsplit(".", 1)[-1].lower() in MEDIA):
		return None
	if "/" in t:
		want = t + ".md"
		if want in all_paths:
			return want
		hits = [p for p in all_paths if p.endswith("/" + want)]
		return min(hits, key=len) if hits else None
	hits = by_name.get(t.lower())
	if not hits:
		return None
	here = os.path.dirname(src)
	same = [p for p in hits if os.path.dirname(p) == here]
	return same[0] if same else min(hits, key=lambda p: (p.count("/"), len(p)))


def _build():
	paths = sorted(_md_files())
	sig = _signature(paths)
	if sig == _index["sig"]:
		return _index
	all_paths = set(paths)
	by_name = {}
	for p in paths:
		by_name.setdefault(os.path.basename(p)[:-3].lower(), []).append(p)
	adj = {p: set() for p in paths}
	for p in paths:
		try:
			with open(os.path.join(_vault, p), encoding="utf-8", errors="ignore") as f:
				text = f.read()
		except OSError:
			continue
		for m in WIKI.finditer(text):
			q = _resolve(m.group(1), p, by_name, all_paths)
			if q and q != p:
				adj[p].add(q)
				adj[q].add(p)
		for m in MDLINK.finditer(text):
			href = unquote(m.group(1))
			if "://" in href:
				continue
			q = os.path.normpath(os.path.join(os.path.dirname(p), href)).lstrip("./") if not href.startswith("/") else href.lstrip("/")
			if q in all_paths and q != p:
				adj[p].add(q)
				adj[q].add(p)
	nodes = {p: {"id": p, "name": os.path.basename(p)[:-3], "group": p.split("/")[0] if "/" in p else "", "deg": len(adj[p])} for p in paths}
	_index.update(sig=sig, nodes=nodes, adj=adj)
	return _index


def graph(path=None, depth=1):
	with _lock:
		idx = _build()
	nodes, adj = idx["nodes"], idx["adj"]
	if path:
		if path not in nodes:
			return {"nodes": [], "links": [], "center": path}
		keep, ring = {path}, {path}
		for _ in range(max(1, min(depth, 3))):
			ring = {q for p in ring for q in adj[p]} - keep
			keep |= ring
	else:
		keep = set(nodes)
	links = sorted({tuple(sorted((p, q))) for p in keep for q in adj[p] if q in keep})
	return {"nodes": [nodes[p] for p in sorted(keep)], "links": [list(l) for l in links], "center": path}


def register(app, vault_path):
	global _vault
	_vault = vault_path

	@app.route("/api/graph")
	def api_graph():
		path = request.args.get("path") or None
		try:
			depth = int(request.args.get("depth", 1))
		except ValueError:
			depth = 1
		return jsonify(graph(path, depth))


def local_html(path):
	return _component("local", path)


def vault_html():
	return _component("vault", None)


def _component(mode, path):
	cid = "gv" + mode
	title = "🕸️ Connections of this note" if mode == "local" else "🕸️ Vault graph"
	hint = ("Notes this page links to or is linked from. " if mode == "local" else "Every note and how they link. ") + \
		"Drag to move · wheel / pinch to zoom · click a dot to open it."
	ctrls = ('<label>Steps out <select class="gvdepth"><option value="1">1</option><option value="2">2</option></select></label>'
		if mode == "local" else '<input class="gvfind" type="search" placeholder="Find a note…">')
	return (f'<div class="gv" id="{cid}" data-mode="{mode}" data-path="{_attr(path or "")}">'
		f'<div class="gvhead"><h2>{title}</h2><div class="gvctl">{ctrls}<button class="gvfit" title="Fit to screen">🎯</button>'
		f'<button class="gvbig" title="Full screen">⛶</button></div></div>'
		f'<div class="gvhint">{hint}</div><div class="gvwrap"><canvas></canvas><div class="gvtip" hidden></div>'
		f'<div class="gvmsg">Loading graph…</div></div><div class="gvlegend"></div></div>' + ASSETS)


def _attr(s):
	return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


# Shared CSS + JS (each component finds its own <div class="gv">; the script only defines GV once per page).
ASSETS = r"""
<style>
.gv{margin:34px 0 10px;border:1px solid var(--gv-l,#d0d7de);border-radius:14px;padding:14px 16px;background:var(--gv-bg,#fafafa)}
body.dark-theme .gv,.gv.dark{--gv-l:#3c3c3c;--gv-bg:#1b1b1b;--gv-m:#9a9a9a;--gv-ink:#e0e0e0}
.gv{--gv-m:#6b7280;--gv-ink:#222}
.gv .gvhead{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.gv h2{margin:0;font-size:1.15em;border:none;padding:0;color:var(--gv-ink)}
.gv .gvctl{display:flex;gap:8px;align-items:center;font-size:14px;color:var(--gv-m)}
.gv .gvctl button,.gv .gvctl select,.gv .gvctl input{font:inherit;font-size:14px;padding:5px 9px;border-radius:8px;border:1px solid var(--gv-l);background:transparent;color:var(--gv-ink);cursor:pointer}
.gv .gvctl input{cursor:text;width:150px}
.gv .gvhint{color:var(--gv-m);font-size:13px;margin:4px 0 8px}
.gv .gvwrap{position:relative;height:380px;border-radius:10px;overflow:hidden;background:var(--gv-bg);touch-action:none}
.gv[data-mode=vault] .gvwrap{height:560px}
.gv canvas{display:block;width:100%;height:100%;cursor:grab}
.gv .gvtip{position:absolute;pointer-events:none;background:rgba(20,20,20,.9);color:#fff;font-size:12.5px;padding:4px 8px;border-radius:6px;max-width:320px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.gv .gvmsg[hidden],.gv .gvtip[hidden]{display:none}
.gv .gvmsg{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--gv-m);font-size:14px;pointer-events:none}
.gv .gvlegend{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:8px;font-size:12.5px;color:var(--gv-m)}
.gv .gvlegend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;vertical-align:-1px}
.gv.full{position:fixed;inset:0;z-index:10050;margin:0;border-radius:0;display:flex;flex-direction:column}
.gv.full .gvwrap{flex:1;height:auto}
</style>
<script>
window.GV || (window.GV = (() => {
const PAL = ['#7c3aed', '#0ea5e9', '#16a34a', '#f59e0b', '#64748b', '#ec4899', '#14b8a6', '#8b5cf6', '#84cc16', '#f97316', '#06b6d4', '#a855f7'];
function mount(root) {
  if (root.dataset.on) return; root.dataset.on = 1;
  const mode = root.dataset.mode, center = root.dataset.path || null;
  const cv = root.querySelector('canvas'), ctx = cv.getContext('2d'), tip = root.querySelector('.gvtip'), msg = root.querySelector('.gvmsg');
  let N = [], L = [], byId = {}, nb = {}, view = {x: 0, y: 0, k: 1}, hover = null, drag = null, alpha = 1, raf = 0, found = null;
  const dark = () => document.body.classList.contains('dark-theme') || getComputedStyle(root).getPropertyValue('--gv-ink').trim() === '#e0e0e0';
  const size = () => { const r = cv.getBoundingClientRect(), d = devicePixelRatio || 1; cv.width = r.width * d; cv.height = r.height * d; ctx.setTransform(d, 0, 0, d, 0, 0); return r; };
  function load(depth) {
    msg.textContent = 'Loading graph…'; msg.hidden = false;
    fetch('/api/graph?' + new URLSearchParams(center ? {path: center, depth} : {})).then(r => r.json()).then(g => {
      const groups = [...new Set(g.nodes.map(n => n.group))];
      const col = gname => PAL[groups.indexOf(gname) % PAL.length];
      const old = byId; byId = {}; nb = {};
      N = g.nodes.map((n, i) => { const o = old[n.id], a = i * 2.39996, r = 12 * Math.sqrt(i + 1);
        return byId[n.id] = {...n, c: col(n.group), x: o ? o.x : (n.id === center ? 0 : Math.cos(a) * r), y: o ? o.y : (n.id === center ? 0 : Math.sin(a) * r), vx: 0, vy: 0}; });
      L = g.links.map(([a, b]) => ({s: byId[a], t: byId[b]})).filter(l => l.s && l.t);
      L.forEach(l => { (nb[l.s.id] ||= new Set()).add(l.t.id); (nb[l.t.id] ||= new Set()).add(l.s.id); });
      root.querySelector('.gvlegend').innerHTML = groups.length > 1 ? groups.map(gname => `<span><i style="background:${col(gname)}"></i>${esc(gname || '(vault root)')}</span>`).join('') : '';
      msg.hidden = N.length > 1; if (N.length <= 1) msg.textContent = center ? 'This note has no links to other notes yet.' : 'No notes.';
      alpha = 1; fitted = false; kick();
    }).catch(() => msg.textContent = "Couldn't load the graph.");
  }
  const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  let fitted = false;
  function tick() {   // tiny force layout: springs on links, repulsion between all dots, a soft pull to the middle
    const n = N.length, rep = mode === 'vault' ? 2600 : 1400, len = mode === 'vault' ? 70 : 70;
    for (let i = 0; i < n; i++) { const a = N[i];
      for (let j = i + 1; j < n; j++) { const b = N[j]; let dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy + .01;
        if (d2 > 160000) continue; const f = rep / d2 * alpha, d = Math.sqrt(d2); dx /= d; dy /= d; a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f; } }
    for (const l of L) { let dx = l.t.x - l.s.x, dy = l.t.y - l.s.y, d = Math.sqrt(dx * dx + dy * dy) || 1, f = (d - len) * .04 * alpha;
      dx /= d; dy /= d; l.s.vx += dx * f; l.s.vy += dy * f; l.t.vx -= dx * f; l.t.vy -= dy * f; }
    for (const a of N) { a.vx -= a.x * .004 * alpha; a.vy -= a.y * .004 * alpha;
      if (a === drag?.node || (center && a.id === center && mode === 'local')) { a.vx = a.vy = 0; if (a.id === center && a !== drag?.node) { a.x *= .9; a.y *= .9; } continue; }
      a.vx *= .6; a.vy *= .6; a.x += Math.max(-20, Math.min(20, a.vx)); a.y += Math.max(-20, Math.min(20, a.vy)); }
    alpha *= mode === 'vault' ? .992 : .985;
  }
  function fit() {
    if (!N.length) return; const r = cv.getBoundingClientRect();
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity; for (const a of N) { x0 = Math.min(x0, a.x); x1 = Math.max(x1, a.x); y0 = Math.min(y0, a.y); y1 = Math.max(y1, a.y); }
    const pad = 60; view.k = Math.min(2.2, Math.min((r.width - pad) / Math.max(x1 - x0, 1), (r.height - pad) / Math.max(y1 - y0, 1)));
    view.x = r.width / 2 - (x0 + x1) / 2 * view.k; view.y = r.height / 2 - (y0 + y1) / 2 * view.k;
  }
  const rad = a => (a.id === center ? 9 : 3.5 + Math.min(9, Math.sqrt(a.deg) * 1.3));
  function draw() {
    const r = size(), dk = dark(), hot = hover ? nb[hover.id] || new Set() : null;
    ctx.clearRect(0, 0, r.width, r.height); ctx.save(); ctx.translate(view.x, view.y); ctx.scale(view.k, view.k);
    for (const l of L) { const on = hover && (l.s === hover || l.t === hover);
      ctx.strokeStyle = on ? (dk ? '#c4b5fd' : '#7c3aed') : dk ? 'rgba(200,200,200,.18)' : 'rgba(80,80,80,.18)'; ctx.lineWidth = (on ? 1.8 : 1) / view.k;
      if (hover && !on) ctx.globalAlpha = .35; ctx.beginPath(); ctx.moveTo(l.s.x, l.s.y); ctx.lineTo(l.t.x, l.t.y); ctx.stroke(); ctx.globalAlpha = 1; }
    const showAll = mode === 'local' || view.k > 2.4 || N.length < 40;
    for (const a of N) { const dim = hover && a !== hover && !hot.has(a.id); ctx.globalAlpha = dim ? .2 : 1;
      ctx.beginPath(); ctx.arc(a.x, a.y, rad(a), 0, 7); ctx.fillStyle = a.id === center ? '#ef4444' : a.c; ctx.fill();
      if (a === found) { ctx.lineWidth = 3 / view.k; ctx.strokeStyle = '#facc15'; ctx.stroke(); }
      const lab = showAll || a === hover || a === found || (hot && hot.has(a.id)) || a.deg >= (mode === 'vault' ? 12 : 1) && view.k > .7;
      if (lab && !dim) { ctx.font = `${(a.id === center ? 13 : 11.5) / view.k}px system-ui, sans-serif`; ctx.fillStyle = dk ? '#ddd' : '#333'; ctx.textAlign = 'center';
        const t = a.name.length > 34 ? a.name.slice(0, 32) + '…' : a.name; ctx.fillText(t, a.x, a.y + rad(a) + 12 / view.k); }
      ctx.globalAlpha = 1; }
    ctx.restore();
  }
  function kick() { if (!raf) raf = requestAnimationFrame(loop); }
  function loop() { raf = 0; if (alpha > .02 || drag) { for (let i = 0; i < (N.length > 200 ? 1 : 2); i++) tick(); if (!fitted && alpha < .5) { fit(); fitted = true; } kick(); } draw(); }
  const toWorld = (ev) => { const r = cv.getBoundingClientRect(); return {x: (ev.clientX - r.left - view.x) / view.k, y: (ev.clientY - r.top - view.y) / view.k, sx: ev.clientX - r.left, sy: ev.clientY - r.top}; };
  const hit = p => { let best = null, bd = Infinity; for (const a of N) { const d = Math.hypot(a.x - p.x, a.y - p.y); if (d < rad(a) + 6 / view.k && d < bd) { bd = d; best = a; } } return best; };
  const pts = new Map(); let pinch = null, moved = 0;
  cv.addEventListener('pointerdown', ev => { cv.setPointerCapture(ev.pointerId); pts.set(ev.pointerId, ev); moved = 0;
    if (pts.size === 2) { const [a, b] = [...pts.values()]; pinch = {d: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY), k: view.k}; drag = null; return; }
    const p = toWorld(ev), n = hit(p); drag = n ? {node: n} : {pan: true, x: ev.clientX - view.x, y: ev.clientY - view.y}; if (n) { alpha = Math.max(alpha, .3); kick(); } });
  cv.addEventListener('pointermove', ev => {
    if (pts.has(ev.pointerId)) pts.set(ev.pointerId, ev);
    if (pinch && pts.size === 2) { const [a, b] = [...pts.values()], d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY), r = cv.getBoundingClientRect();
      const cx = (a.clientX + b.clientX) / 2 - r.left, cy = (a.clientY + b.clientY) / 2 - r.top, k = Math.max(.1, Math.min(6, pinch.k * d / pinch.d));
      view.x = cx - (cx - view.x) * k / view.k; view.y = cy - (cy - view.y) * k / view.k; view.k = k; kick(); return; }
    const p = toWorld(ev);
    if (drag) { moved++; if (drag.node) { drag.node.x = p.x; drag.node.y = p.y; alpha = Math.max(alpha, .3); } else { view.x = ev.clientX - drag.x; view.y = ev.clientY - drag.y; } kick(); return; }
    const n = hit(p); if (n !== hover) { hover = n; cv.style.cursor = n ? 'pointer' : 'grab'; kick(); }
    if (n) { tip.hidden = false; tip.textContent = n.id.replace(/\.md$/, ''); tip.style.left = Math.min(p.sx + 12, cv.clientWidth - 200) + 'px'; tip.style.top = p.sy + 12 + 'px'; } else tip.hidden = true; });
  const up = ev => { pts.delete(ev.pointerId); if (pts.size < 2) pinch = null;
    if (drag && drag.node && moved < 4) location.href = '/view/' + drag.node.id.split('/').map(encodeURIComponent).join('/');
    drag = null; kick(); };
  cv.addEventListener('pointerup', up); cv.addEventListener('pointercancel', up);
  cv.addEventListener('pointerleave', () => { if (!drag) { hover = null; tip.hidden = true; kick(); } });
  cv.addEventListener('wheel', ev => { ev.preventDefault(); const r = cv.getBoundingClientRect(), cx = ev.clientX - r.left, cy = ev.clientY - r.top, k = Math.max(.1, Math.min(6, view.k * Math.exp(-ev.deltaY * .0015)));
    view.x = cx - (cx - view.x) * k / view.k; view.y = cy - (cy - view.y) * k / view.k; view.k = k; kick(); }, {passive: false});
  root.querySelector('.gvfit').onclick = () => { fit(); kick(); };
  root.querySelector('.gvbig').onclick = () => { root.classList.toggle('full'); setTimeout(() => { fit(); kick(); }, 30); };
  addEventListener('keydown', ev => { if (ev.key === 'Escape' && root.classList.contains('full')) { root.classList.remove('full'); setTimeout(() => { fit(); kick(); }, 30); } });
  const dsel = root.querySelector('.gvdepth'); if (dsel) dsel.onchange = () => load(+dsel.value);
  const find = root.querySelector('.gvfind'); if (find) find.oninput = () => { const q = find.value.trim().toLowerCase();
    found = q ? N.find(a => a.name.toLowerCase().includes(q)) || null : null;
    if (found) { const r = cv.getBoundingClientRect(); view.k = Math.max(view.k, 1.6); view.x = r.width / 2 - found.x * view.k; view.y = r.height / 2 - found.y * view.k; } kick(); };
  new ResizeObserver(() => kick()).observe(cv);
  load(1);
}
function scan() { document.querySelectorAll('.gv').forEach(root => {
  if (root.dataset.on || root.dataset.wait) return; root.dataset.wait = 1;   // draw when it scrolls into view
  new IntersectionObserver((es, ob) => { if (es.some(e => e.isIntersecting)) { ob.disconnect(); mount(root); } }, {rootMargin: '200px'}).observe(root);
}); }
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', scan); else scan();
return {scan};
})());
window.GV && window.GV.scan();
</script>
"""
