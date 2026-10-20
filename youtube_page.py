"""📺 YouTube page of the vault viewer (/youtube) — download a YouTube channel, or only some of its tabs / playlists, as
music only, full video, or video only (no sound).

The download engine is Music Lab's (apps/music-lab/musiclab/channels.py), run on its own folder so the two apps
never mix: files go to the data drive (/media/vineeth/ClawdData/vault-youtube/<channel>/), never into the vault
(it's a git repo and the system disk is nearly full), with jobs as systemd --user units "vault-youtube-<channel>"
and no Claude video lessons. One Telegram message when a channel is done.
"""
import os
import re
import sys
from pathlib import Path

from flask import Blueprint, abort, jsonify, request, send_file

ENGINE = Path.home() / "clawd/apps/music-lab/musiclab"
os.environ.setdefault("CHANNELS_ROOT", "/media/vineeth/ClawdData/vault-youtube")
os.environ.setdefault("CHANNELS_UNIT", "vault-youtube")
os.environ.setdefault("CHANNELS_LESSONS", "0")

bp = Blueprint("youtube", __name__)
NAME = re.compile(r"[a-z0-9-]+")


def _C():
    if str(ENGINE) not in sys.path:
        sys.path.insert(0, str(ENGINE))
    import channels
    return channels


def _err(e, code=400):
    return jsonify({"error": str(e)}), code


@bp.get("/youtube/api/channels")
def api_list():
    try:
        return jsonify(_C().listing())
    except RuntimeError as e:
        return _err(e, 503)


@bp.post("/youtube/api/lookup")
def api_lookup():
    try:
        return jsonify(_C().lookup(str((request.get_json(silent=True) or {}).get("url") or "")))
    except ValueError as e:
        return _err(e)


@bp.get("/youtube/api/channels/<name>")
def api_state(name):
    if not NAME.fullmatch(name):
        abort(404)
    r = _C().state(name)
    return jsonify(r) if r else _err("no such channel", 404)


@bp.post("/youtube/api/channels/<name>/<action>")
def api_action(name, action):
    C = _C()
    if not NAME.fullmatch(name) or not (C.ROOT / name / "channel.json").exists():
        abort(404)
    b = request.get_json(silent=True) or {}
    try:
        if action == "start":                       # scope: null = whole channel, {tabs, playlists} = only those
            return jsonify(ok=True, msg=C.start(name, str(b.get("mode") or ""), b["scope"] if "scope" in b else C.KEEP))
        if action == "resume":
            return jsonify(ok=True, msg=C.start(name, C._jread(C.ROOT / name / "channel.json")["mode"]))
        if action == "pause":
            (C.ROOT / name / "pause").touch()
            return jsonify(ok=True)
        if action == "retry":
            return jsonify(ok=True, **C.set_retry(name, str(b.get("at") or "now")))
        if action == "playlist-links":
            return jsonify(ok=True, **C.playlist_links(name))
        if action == "forget":
            C.remove(name)
            return jsonify(ok=True)
    except ValueError as e:
        return _err(e)
    abort(404)


@bp.get("/youtube/media/<name>/<vid>")
def media(name, vid):
    """Play in the page (range requests), or ?dl=1 to save the file on this device, named after the video."""
    f = _C().file(name, vid)
    if not f:
        abort(404)
    if request.args.get("dl"):
        title = re.sub(r'[\\/:*?"<>|]+', " ", request.args.get("title") or vid).strip()[:120] or vid
        return send_file(f, as_attachment=True, download_name=f"{title}{f.suffix}", conditional=True)
    return send_file(f, conditional=True)


PAGE = r'''
<style>
.ytp{--l:rgba(128,128,128,.35);--m:#8a9096;--a:#1fa594;--r:#e05555;max-width:1100px;margin:0 auto;padding:16px}
.ytp h1{margin:0 0 4px}.ytp .m{color:var(--m);font-size:13px}.ytp a{color:var(--a)}
.ytp .box{border:1px solid var(--l);border-radius:8px;padding:14px;margin:14px 0}
.ytp input[type=text]{flex:1;min-width:200px;padding:8px 10px;border-radius:6px;border:1px solid var(--l);background:transparent;color:inherit}
.ytp .btn{padding:8px 14px;border-radius:6px;border:1px solid var(--l);background:rgba(128,128,128,.14);color:inherit;cursor:pointer;font:inherit;text-decoration:none}
.ytp .btn.p{background:var(--a);border-color:var(--a);color:#fff;font-weight:600}.ytp .btn:disabled{opacity:.45;cursor:default}
.ytp .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.ytp .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}
.ytp .card{border:1px solid var(--l);border-radius:8px;overflow:hidden;text-decoration:none;color:inherit;display:block}
.ytp .card .cv{display:grid;grid-template-columns:1fr 1fr;aspect-ratio:16/9;background:#111}.ytp .card .cv img{width:100%;height:100%;object-fit:cover}
.ytp .card .b{padding:10px}.ytp .bar{height:6px;background:var(--l);border-radius:3px;overflow:hidden;margin-top:6px}.ytp .bar i{display:block;height:100%;background:var(--a)}
.ytp .chips{display:flex;gap:6px;flex-wrap:wrap}.ytp .chip{padding:6px 12px;border-radius:16px;border:1px solid var(--l);background:transparent;color:inherit;cursor:pointer;font:inherit}
.ytp .chip.on{border-color:var(--a);color:var(--a)}
.ytp .seg{display:inline-flex;border:1px solid var(--l);border-radius:8px;overflow:hidden;margin-top:10px}
.ytp .seg button{padding:8px 14px;border:none;background:transparent;color:inherit;cursor:pointer;font:inherit}.ytp .seg button.on{background:var(--a);color:#fff}
.ytp .sub{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--m);margin:16px 0 6px}
.ytp .tchips{display:flex;gap:8px;flex-wrap:wrap}.ytp .tchip{padding:6px 12px;border-radius:16px;border:1px solid var(--l);background:transparent;color:inherit;cursor:pointer;font:inherit}
.ytp .tchip.on{border-color:var(--a);background:rgba(31,165,148,.12)}.ytp .tchip .m{margin-left:4px}
.ytp .plbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:6px}.ytp .plbar input{flex:1;min-width:180px}
.ytp .pls{max-height:380px;overflow:auto;border:1px solid var(--l);border-radius:8px}
.ytp .pl{display:flex;gap:10px;align-items:center;padding:7px 10px;margin:0;cursor:pointer;border-bottom:1px solid var(--l)}.ytp .pl:last-child{border-bottom:none}
.ytp .pl:hover{background:rgba(128,128,128,.08)}.ytp .pl.on{background:rgba(31,165,148,.12)}.ytp .pl input{margin:0;flex:none}
.ytp .pl .pth{width:64px;height:36px;flex:none;border-radius:4px;background:rgba(128,128,128,.2) center/cover no-repeat}
.ytp .pl .pt{flex:1;min-width:0;line-height:1.3}.ytp .pl .ptt{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.ytp .pl .pn{display:block;color:var(--m);font-size:12px}
@media (max-width:600px){.ytp .pl .pth{width:48px;height:27px}.ytp .pl{padding:7px 8px;gap:8px}}
.ytp .sum{margin:14px 0 0;font-weight:600}
.ytp .fmt{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-top:10px}
.ytp .fmt button{text-align:left;padding:12px;border:1px solid var(--l);border-radius:8px;background:rgba(128,128,128,.08);color:inherit;font:inherit;cursor:pointer}
.ytp .fmt button:disabled{opacity:.45;cursor:default}.ytp .fmt button.sel{outline:2px solid var(--a)}
.ytp .tabs{display:flex;gap:4px;flex-wrap:wrap;border-bottom:1px solid var(--l);margin:16px 0 12px}
.ytp .tabs button{padding:8px 12px;background:none;border:none;border-bottom:2px solid transparent;color:inherit;cursor:pointer;font:inherit}.ytp .tabs button.on{border-color:var(--a)}
.ytp .vids{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}
.ytp .v .th{position:relative;aspect-ratio:16/9;border-radius:6px;overflow:hidden;background:#111}.ytp .v .th img{width:100%;height:100%;object-fit:cover}
.ytp .v .s{position:absolute;left:6px;top:6px;background:rgba(0,0,0,.75);color:#fff;font-size:11.5px;padding:2px 6px;border-radius:4px}
.ytp .v.off img,.ytp .v.gone img{opacity:.4}.ytp .v .t{font-size:13.5px;margin:6px 0 4px;line-height:1.3}
.ytp .v .a{display:flex;gap:6px}.ytp .v .a .btn{padding:4px 10px;font-size:12.5px}
.ytp .player{position:fixed;inset:0;background:rgba(0,0,0,.85);display:flex;align-items:center;justify-content:center;z-index:9999;padding:16px}
.ytp .player video{max-width:100%;max-height:80vh}.ytp .player .x{position:absolute;top:12px;right:16px;font-size:22px;background:none;border:none;color:#fff;cursor:pointer}
@media (max-width:600px){.ytp{padding:10px}}
</style>
<div class="ytp" id="ytp"></div>
<script>
(() => {
const $ = s => document.querySelector(s), root = $('#ytp');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const api = async (u, body) => { const r = await fetch('/youtube/api/' + u, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.error || r.statusText); return j; };
const gb = x => x >= 1 ? x.toFixed(1) + ' GB' : Math.max(1, Math.round(x * 1000)) + ' MB';
const MODES = [['audio', '🎵 Music only', 'Audio of each video (.m4a)'], ['video', '🎬 Full video', 'Up to 1080p with sound (.mp4)'],
  ['silent', '🎞 Video only (no sound)', 'The picture only, up to 1080p (.mp4)']];
const LABEL = {audio: '🎵 Music only', video: '🎬 Full video', silent: '🎞 Video only (no sound)'};
const MB_MIN = {audio: 1.0, video: 18.0, silent: 17.0};
const ST = {off: 'not chosen', done: '✓', linked: '✓', downloading: '⬇', waiting: 'waiting', retry: '⟳ retry', blocked: '🚦 blocked', gone: 'unavailable'};
let timer, pick = {name: null}, view = {tab: null, pl: null, q: '', n: 60};      // n: cards shown (a big channel at once is ~200 000 px tall)
const chan = () => new URLSearchParams(location.search).get('c');

async function list(){
  let all = [], err = null;
  try { all = await api('channels'); } catch (e) { err = e.message; }
  root.innerHTML = `<h1>📺 YouTube</h1><p class="m">Download a YouTube channel — all of it, or only the tabs and playlists you pick — as music only, full video, or video without sound. Files go to the data drive, not into the vault.</p>
    <form class="box row" id="f"><input type="text" id="u" required placeholder="Paste a YouTube channel link — youtube.com/@name"><button class="btn p" id="fb">Look up channel</button><div id="fm" class="m" style="flex-basis:100%"></div></form>
    ${err ? `<div class="box" style="border-color:var(--r);color:var(--r)">${esc(err)}</div>` : ''}
    ${all.length ? `<div class="cards">${all.map(c => `<a class="card" href="?c=${encodeURIComponent(c.name)}"><div class="cv">${c.covers.map(u => `<img src="${esc(u)}" alt="" loading="lazy" referrerpolicy="no-referrer">`).join('')}</div>
      <div class="b"><b>${esc(c.title)}</b><div class="m">${c.mode ? LABEL[c.mode] + ' · ' + esc(c.scope_text) : 'not started — choose what to download'}</div>
      ${c.mode ? `<div class="m">${c.running ? esc(c.status.stage || 'downloading') : c.paused ? 'paused' : esc(c.status.stage || '')} · ${c.done} of ${c.total}</div><div class="bar"><i style="width:${c.total ? Math.round(c.done / c.total * 100) : 0}%"></i></div>` : ''}</div></a>`).join('')}</div>` : '<p class="m">No channels yet.</p>'}`;
  $('#f').onsubmit = async e => { e.preventDefault(); $('#fb').disabled = true; $('#fm').textContent = 'Reading the channel — saving every video, short, live stream and playlist link…';
    try { const c = await api('lookup', {url: $('#u').value}); location.search = '?c=' + encodeURIComponent(c.name); }
    catch (err) { $('#fm').textContent = err.message; $('#fm').style.color = 'var(--r)'; $('#fb').disabled = false; } };
  clearInterval(timer); if (all.some(c => c.running)) timer = setInterval(() => document.activeElement !== $('#u') && list(), 8000);
}

function pickInit(C){
  if (pick.name === C.name) return;
  const sc = C.scope;
  pick = {name: C.name, all: !sc, tabs: new Set(sc ? sc.tabs : ['videos', 'shorts', 'live'].filter(t => C.counts[t])), pls: new Set(sc ? sc.playlists : []), open: !C.mode};
}
function chosen(C){
  const lists = pick.all ? [...Object.values(C.tabs), ...C.playlists.map(p => p.entries || [])]
    : [...[...pick.tabs].map(t => C.tabs[t] || []), ...C.playlists.filter(p => pick.pls.has(p.id)).map(p => p.entries || [])];
  const dur = {}; lists.flat().forEach(e => dur[e.id] = dur[e.id] || e.duration_s || (e.tab === 'shorts' ? 40 : 240));
  return {n: Object.keys(dur).length, mins: Object.values(dur).reduce((a, b) => a + b, 0) / 60};
}
const plThumb = p => p.entries?.[0]?.thumbnail || p.thumbnail || '';    // a playlist's own cover often 404s
const plMatch = (p, q) => !q || p.title.toLowerCase().includes(q);
function picker(C){
  const sel = chosen(C), size = m => sel.mins * MB_MIN[m] / 1000, fits = m => size(m) < C.free_gb - 5, locked = C.mode && C.done > 0;
  const tabs = [['videos', 'Videos'], ['shorts', 'Shorts'], ['live', 'Live']].filter(([k]) => C.counts[k]);
  const unread = C.playlists.some(p => p.entries == null), q = (pick.q || '').toLowerCase();
  return `<div class="box" id="ask"><b>What should I download?</b> <span class="m">Free on the data drive: ${gb(C.free_gb)}</span><br>
    <div class="seg"><button class="${pick.all ? 'on' : ''}" data-sc="all">Whole channel</button><button class="${pick.all ? '' : 'on'}" data-sc="pick">Pick tabs & playlists</button></div>
    ${pick.all ? '' : `${tabs.length ? `<div class="sub">Tabs</div><div class="tchips">${tabs.map(([k, l]) => `<button class="tchip ${pick.tabs.has(k) ? 'on' : ''}" data-pt="${k}">${pick.tabs.has(k) ? '✓ ' : ''}${l}<span class="m">${C.counts[k]}</span></button>`).join('')}</div>` : ''}
      ${C.playlists.length ? `<div class="sub">Playlists · ${pick.pls.size} of ${C.playlists.length} chosen <span id="plr" style="text-transform:none;letter-spacing:0">${pick.err ? '· ' + esc(pick.err) : unread ? '· reading what each playlist holds…' : ''}</span></div>
        <div class="plbar"><input type="text" id="plq" placeholder="Search ${C.playlists.length} playlists…" value="${esc(pick.q || '')}"><button class="btn" id="plall">Select shown</button><button class="btn" id="plnone">Clear</button></div>
        <div class="pls">${C.playlists.map(p => `<label class="pl ${pick.pls.has(p.id) ? 'on' : ''}" data-plt="${esc(p.title.toLowerCase())}" style="${plMatch(p, q) ? '' : 'display:none'}"><input type="checkbox" data-pp="${esc(p.id)}" ${pick.pls.has(p.id) ? 'checked' : ''}>
          <span class="pth" style="background-image:url('${esc(plThumb(p))}')"></span><span class="pt"><span class="ptt">${esc(p.title)}</span>${p.entries ? `<span class="pn">${p.entries.length} videos</span>` : ''}</span></label>`).join('')}
          <p class="m" id="plnone-msg" style="padding:10px;margin:0;${C.playlists.some(p => plMatch(p, q)) ? 'display:none' : ''}">No playlist matches.</p></div>` : '<p class="m">This channel has no public playlists.</p>'}`}
    <p class="sum">${sel.n ? `${sel.n} video${sel.n === 1 ? '' : 's'} · about ${(sel.mins / 60).toFixed(1)} h` : 'Nothing chosen yet.'}</p>
    <div class="fmt">${MODES.map(([m, l, d]) => `<button data-m="${m}" class="${C.mode === m ? 'sel' : ''}" ${!sel.n || !fits(m) || (locked && m !== C.mode) ? 'disabled' : ''}><b>${l}</b><div class="m">${d} · about ${gb(size(m))}${!fits(m) ? ' — not enough space' : locked && m !== C.mode ? ' — this channel is already ' + LABEL[C.mode].toLowerCase() : ''}</div></button>`).join('')}</div>
    <p class="m" style="margin:10px 0 0">${C.mode ? 'Files already downloaded stay, whatever you choose. <a href="#" id="keep">Keep it as it is</a>' : '<a href="#" id="forget">Cancel</a>'}</p></div>`;
}
function wirePicker(C){
  const redraw = () => { const y = scrollY, ls = $('.pls')?.scrollTop, f = document.activeElement?.id; page(C); scrollTo(0, y);
    if ($('.pls') && ls) $('.pls').scrollTop = ls; if (f === 'plq') { const q = $('#plq'); q.focus(); q.setSelectionRange(q.value.length, q.value.length); } };
  root.querySelectorAll('[data-sc]').forEach(b => b.onclick = () => { pick.all = b.dataset.sc === 'all'; redraw(); });
  root.querySelectorAll('[data-pt]').forEach(b => b.onclick = () => { const k = b.dataset.pt; pick.tabs.has(k) ? pick.tabs.delete(k) : pick.tabs.add(k); redraw(); });
  root.querySelectorAll('[data-pp]').forEach(b => b.onchange = () => { pick.pls[b.checked ? 'add' : 'delete'](b.dataset.pp); redraw(); });
  if ($('#plq')) $('#plq').oninput = e => { pick.q = e.target.value; const q = pick.q.toLowerCase(); let n = 0;   // filter in place: no redraw, keeps focus
    root.querySelectorAll('.pl').forEach(r => { const ok = !q || r.dataset.plt.includes(q); r.style.display = ok ? '' : 'none'; n += ok; });
    $('#plnone-msg').style.display = n ? 'none' : ''; };
  const shown = () => C.playlists.filter(p => plMatch(p, (pick.q || '').toLowerCase())).map(p => p.id);
  if ($('#plall')) $('#plall').onclick = () => { shown().forEach(id => pick.pls.add(id)); redraw(); };
  if ($('#plnone')) $('#plnone').onclick = () => { (pick.q ? shown() : [...pick.pls]).forEach(id => pick.pls.delete(id)); redraw(); };
  if (!pick.all && !pick.err && !pick.reading && C.playlists.some(p => p.entries == null)) { pick.reading = true;
    api(`channels/${C.name}/playlist-links`, {}).catch(e => { pick.err = e.message; }).then(() => { pick.reading = false; if ($('#ask')) load(); }); }
  root.querySelectorAll('[data-m]').forEach(b => b.onclick = async () => { root.querySelectorAll('#ask button').forEach(x => x.disabled = true);
    try { await api(`channels/${C.name}/start`, {mode: b.dataset.m, scope: pick.all ? null : {tabs: [...pick.tabs], playlists: [...pick.pls]}}); pick.open = false; }
    catch (e) { alert(e.message); } setTimeout(load, 1200); });
  if ($('#keep')) $('#keep').onclick = e => { e.preventDefault(); pick.open = false; pick.name = null; load(); };
  if ($('#forget')) $('#forget').onclick = async e => { e.preventDefault(); await api(`channels/${C.name}/forget`, {}).catch(() => {}); location.search = ''; };
}
function play(C, e){
  const src = `/youtube/media/${C.name}/${e.id}`, m = document.createElement('div'); m.className = 'player';
  m.innerHTML = `<button class="x" aria-label="Close">✕</button>${e.ext === 'm4a' ? `<div style="text-align:center;color:#fff"><p>${esc(e.title)}</p><audio controls autoplay src="${src}"></audio></div>` : `<video controls autoplay playsinline src="${src}"></video>`}`;
  m.onclick = ev => { if (ev.target === m || ev.target.classList.contains('x')) m.remove(); }; root.appendChild(m);
}
function page(C){
  pickInit(C);
  if (!view.tab) view.tab = C.counts.videos ? 'videos' : C.counts.shorts ? 'shorts' : 'playlists';
  const st = C.status || {}, pct = C.total ? Math.round(C.done / C.total * 100) : 0, ql = view.q.toLowerCase();
  const vcard = e => { const ok = ['done', 'linked'].includes(e.state);
    return `<div class="v ${e.state}"><div class="th"><img src="${esc(e.thumbnail)}" alt="" loading="lazy" referrerpolicy="no-referrer">${ok ? '' : `<span class="s">${ST[e.state] || e.state}${e.state === 'downloading' && st.pct != null ? ' ' + st.pct + ' %' : ''}</span>`}</div>
      <div class="t">${esc(e.title)}</div>${ok ? `<div class="a"><button class="btn" data-play="${e.id}">▶ Play</button><a class="btn" href="/youtube/media/${C.name}/${e.id}?dl=1&title=${encodeURIComponent(e.title)}">⬇ Save</a></div>` : ''}</div>`; };
  const f = L => L.filter(e => !ql || e.title.toLowerCase().includes(ql));
  const grid = (L, none) => { const M = f(L); return M.length ? `<div class="vids">${M.slice(0, view.n).map(vcard).join('')}</div>`
    + (M.length > view.n ? `<p style="text-align:center"><button class="btn" id="more">Show ${Math.min(60, M.length - view.n)} more · ${M.length - view.n} left</button></p>` : '') : `<p class="m">${none}</p>`; };
  let body;
  if (view.tab === 'playlists') { const P = C.playlists.find(p => p.id === view.pl);
    body = P ? `<button class="btn" id="plb">‹ All playlists</button><h3>${esc(P.title)}</h3>${grid(P.entries || [], P.entries ? 'Nothing here.' : 'Links not saved yet.')}`
      : C.playlists.length ? `<div class="vids">${C.playlists.filter(p => plMatch(p, ql)).map(p => { const n = p.entries?.length, d = (p.entries || []).filter(e => ['done', 'linked'].includes(e.state)).length;
          return `<div class="v" data-pl="${esc(p.id)}" style="cursor:pointer"><div class="th">${plThumb(p) ? `<img src="${esc(plThumb(p))}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ''}<span class="s">▤ ${n ?? ''}</span></div><div class="t">${esc(p.title)}</div><div class="m">${n != null ? `${d} of ${n} downloaded` : ''}</div></div>`; }).join('')}</div>`
      : '<p class="m">This channel has no public playlists.</p>';
  } else body = grid(C.tabs[view.tab] || [], 'Nothing here.');
  root.innerHTML = `<p><a href="/youtube">‹ All channels</a></p>
    <div class="row"><div style="flex:1;min-width:200px"><h1>${esc(C.title)}</h1><div class="m">${esc(C.handle || '')} · <a href="${esc(C.url)}" target="_blank" rel="noopener noreferrer">on YouTube ↗</a></div></div>
      ${C.mode && !pick.open ? (C.running ? '<button class="btn" id="pause">⏸ Pause</button>' : C.done < C.total ? `<button class="btn p" id="resume">▶ ${C.paused ? 'Resume' : 'Continue'}</button>` : '') : ''}</div>
    ${!C.mode || pick.open ? picker(C) : `<div class="box"><div class="row" style="justify-content:space-between"><b>${LABEL[C.mode]} · ${esc(C.scope_text)} · ${esc(C.blocked ? '🚦 rate limited by YouTube' : C.paused && !C.running ? 'paused' : st.stage || '')}</b>
        <span class="m">${C.done} of ${C.total} · ${pct} %${st.next_at && C.blocked ? ' · next try ' + esc(st.next_at) : ''}</span></div><div class="bar" style="height:8px"><i style="width:${pct}%"></i></div>
      <div class="m" style="margin-top:8px">${C.running ? 'Pause to change what to download.' : '<a href="#" id="change">Change what to download</a>'}${C.blocked && !C.running ? ' · <a href="#" id="now">Try now</a>' : ''}</div>
      ${C.log.length ? `<details style="margin-top:6px"><summary class="m">Log</summary><pre class="m" style="white-space:pre-wrap">${C.log.map(esc).join('\n')}</pre></details>` : ''}</div>`}
    <div class="tabs">${[['videos', 'Videos'], ['shorts', 'Shorts'], ['live', 'Live'], ['playlists', 'Playlists']].filter(([k]) => k === 'playlists' || C.counts[k]).map(([k, l]) => `<button class="${view.tab === k ? 'on' : ''}" data-tab="${k}">${l} <span class="m">${C.counts[k]}</span></button>`).join('')}</div>
    <input type="text" id="q" placeholder="${view.tab === 'playlists' && !view.pl ? 'Search playlists…' : 'Search titles…'}" value="${esc(view.q)}" style="max-width:340px;margin-bottom:12px">
    ${body}`;
  if ($('#ask')) wirePicker(C);
  root.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => { view.tab = b.dataset.tab; view.pl = null; view.n = 60; page(C); });
  root.querySelectorAll('[data-pl]').forEach(b => b.onclick = () => { view.pl = b.dataset.pl; view.n = 60; page(C); });
  if ($('#more')) $('#more').onclick = () => { const y = scrollY; view.n += 60; page(C); scrollTo(0, y); };
  root.querySelectorAll('[data-play]').forEach(b => b.onclick = () => play(C, [...Object.values(C.tabs).flat(), ...C.playlists.flatMap(p => p.entries || [])].find(e => e.id === b.dataset.play)));
  if ($('#plb')) $('#plb').onclick = () => { view.pl = null; page(C); };
  $('#q').oninput = e => { view.q = e.target.value; view.n = 60; page(C); const q = $('#q'); q.focus(); q.setSelectionRange(q.value.length, q.value.length); };
  if ($('#pause')) $('#pause').onclick = async () => { $('#pause').disabled = true; $('#pause').textContent = 'Pausing after this batch…'; await api(`channels/${C.name}/pause`, {}); };
  if ($('#resume')) $('#resume').onclick = async () => { $('#resume').disabled = true; await api(`channels/${C.name}/resume`, {}).catch(e => alert(e.message)); setTimeout(load, 1500); };
  if ($('#now')) $('#now').onclick = async e => { e.preventDefault(); await api(`channels/${C.name}/retry`, {at: 'now'}).catch(err => alert(err.message)); setTimeout(load, 1500); };
  if ($('#change')) $('#change').onclick = e => { e.preventDefault(); pick.name = null; pickInit(C); pick.open = true; page(C); };
  clearInterval(timer); if (C.running) timer = setInterval(() => !$('#ask') && !root.querySelector('.player') && document.activeElement !== $('#q') && load(), 5000);
}
async function load(){
  const n = chan();
  if (!n) return list();
  try { page(await api('channels/' + encodeURIComponent(n))); }
  catch (e) { root.innerHTML = `<p><a href="/youtube">‹ All channels</a></p><p style="color:var(--r)">${esc(e.message)}</p>`; }
}
load();
})();
</script>
'''


def register(app, render):
    """render(title, content_html) → the viewer's normal page (sidebar + file tree) around content."""
    app.register_blueprint(bp)
    app.add_url_rule("/youtube", "youtube_page", lambda: render("YouTube", PAGE))
