"""📖 One page per downloaded YouTube video, made for learning from it (/youtube/study/<channel>/<id>): the sound with
speed control, the video's chapters, its on-screen slides, a written lesson (words, grammar, tips), practice
(quiz, flashcards, speak-aloud) and the transcript that follows the sound. Every ▶ plays just that bit of the video.

Data, all on the data drive next to the downloads (<vault folder>/<channel>/), none of it in the vault:
  info/<id>.info.json           YouTube's details (title, chapters, description) — written by the download engine
  transcripts/<id>.whisper.json local Whisper transcript (tools/german/yt/transcribe_a1.py), or YouTube's captions
  frames/<id>/index.json + jpgs  slides cut from YouTube's storyboard (tools/german/yt/frames_a1.py)
  study/<id>.json               the written lesson (schema below); a video without one still gets sound/slides/transcript
  study/progress.json           {id: "YYYY-MM-DD"} for the videos marked done

Lesson schema (study/<id>.json; t = seconds into the video, every field optional):
  {"n": 1, "de": "Begrüßungen", "en": "Greetings", "goal": "...", "summary": "...", "level": "A1",
   "sections": [{"title", "t", "text", "items": [{"de", "en", "t", "end", "note"}],
                 "table": {"head": [...], "rows": [[...]]}}],
   "chapters": [{"start_time", "title"}]  (replaces YouTube's when its titles are bad), "tips": ["..."], "quiz": [{"q", "a", "wrong": [...], "why"}], "speak": [{"prompt", "answer"}],
   "notes": ["vault/relative/path.md"]}
Words for the flashcards = every section item.
"""
import datetime
import json
import re
from pathlib import Path

from flask import abort, jsonify, request, send_file

ID = re.compile(r"[A-Za-z0-9_-]{11}")
NAME = re.compile(r"[a-z0-9-]+")


def _j(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def study_index(d: Path) -> dict:
    """{id: "lesson" | "done"} for the channel page's cards."""
    s = d / "study"
    out = {f.stem: "lesson" for f in s.glob("*.json") if f.stem != "progress"} if s.exists() else {}
    for v in (_j(s / "progress.json", {}) or {}):
        out[v] = "done"
    return out


def _context(d: Path, vid: str, pl: str | None):
    """The playlist this video is watched in (?pl=, else the first one holding it): its order gives prev / next."""
    ch = _j(d / "channel.json", {}) or {}
    pls = [p for p in ch.get("playlists") or [] if p.get("entries")]
    p = next((p for p in pls if p["id"] == pl and any(e["id"] == vid for e in p["entries"])), None) \
        or next((p for p in pls if any(e["id"] == vid for e in p["entries"])), None)
    if not p:
        return None
    E = p["entries"]
    k = next(i for i, e in enumerate(E) if e["id"] == vid)
    lite = lambda e: e and {"id": e["id"], "title": e["title"]}
    return {"id": p["id"], "title": p["title"], "index": k + 1, "count": len(E),
            "prev": lite(E[k - 1] if k else None), "next": lite(E[k + 1] if k + 1 < len(E) else None)}


def register(app, render, C):
    """C() → the download engine (channels module) set up for this vault."""

    def chan(name, vid):
        if not NAME.fullmatch(name) or not ID.fullmatch(vid):
            abort(404)
        d = C().ROOT / name
        if not (d / "channel.json").exists():
            abort(404)
        return d

    @app.get("/youtube/study/<name>/<vid>")
    def yt_study_page(name, vid):
        d = chan(name, vid)
        info = _j(d / "info" / f"{vid}.info.json", {}) or {}
        return render(info.get("title") or vid, PAGE)

    @app.get("/youtube/study-api/<name>/<vid>")
    def yt_study_api(name, vid):
        d = chan(name, vid)
        Ch = C()
        info = _j(d / "info" / f"{vid}.info.json", {}) or {}
        f = Ch.file(name, vid)
        T = Ch.transcript(name, vid)
        fr = _j(d / "frames" / vid / "index.json", {}) or {}
        prog = _j(d / "study" / "progress.json", {}) or {}
        ctx = _context(d, vid, request.args.get("pl"))
        if ctx:
            ids = [e["id"] for p in (_j(d / "channel.json", {}) or {}).get("playlists") or [] if p["id"] == ctx["id"]
                   for e in p["entries"]]
            ctx["done"] = sum(v in prog for v in ids)
        return jsonify({
            "id": vid, "channel": name, "title": info.get("title") or vid, "duration": info.get("duration"),
            "upload_date": info.get("upload_date"), "chapters": info.get("chapters") or [],
            "description": (info.get("description") or "")[:3000],
            "media": f"/youtube/media/{name}/{vid}" if f else None, "ext": f.suffix[1:] if f else None,
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "lesson": _j(d / "study" / f"{vid}.json"),
            "slides": [{**s, "src": f"/youtube/frames/{name}/{vid}/{s['file']}"} for s in fr.get("slides") or []],
            "transcript": {"kind": T.get("kind"), "lang": T.get("lang"), "lines": T.get("lines") or []},
            "playlist": ctx, "done": prog.get(vid)})

    @app.post("/youtube/study-api/<name>/<vid>/done")
    def yt_study_done(name, vid):
        d = chan(name, vid)
        (d / "study").mkdir(exist_ok=True)
        p = d / "study" / "progress.json"
        prog = _j(p, {}) or {}
        if (request.get_json(silent=True) or {}).get("done", True):
            prog[vid] = datetime.date.today().isoformat()
        else:
            prog.pop(vid, None)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(prog, indent=1))
        tmp.replace(p)
        return jsonify(ok=True, done=prog.get(vid))

    @app.get("/youtube/frames/<name>/<vid>/<file>")
    def yt_study_frame(name, vid, file):
        d = chan(name, vid)
        if not re.fullmatch(r"\d+\.jpg", file) or not (d / "frames" / vid / file).exists():
            abort(404)
        return send_file(d / "frames" / vid / file, max_age=86400)


PAGE = r'''
<style>
.yts{--l:rgba(128,128,128,.32);--m:#8a9096;--a:#1fa594;--a2:rgba(31,165,148,.13);--o:#e8a33d;--r:#e05555;--g:#3fb37f;
  --bg:#fff;--card:rgba(128,128,128,.07);max-width:980px;margin:0 auto;padding:16px 16px 110px;line-height:1.5}
body.dark-theme .yts{--bg:#252526}
.yts a{color:var(--a)}.yts .m{color:var(--m);font-size:13px}
.yts .crumbs{display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:13.5px;margin-bottom:12px}
.yts .hero{display:grid;grid-template-columns:minmax(0,300px) 1fr;gap:20px;align-items:center}
.yts .hero img{width:100%;border-radius:10px;aspect-ratio:16/9;object-fit:cover;background:#111}
.yts .badge{display:inline-block;background:var(--o);color:#1b1b1b;font-weight:700;font-size:12px;letter-spacing:.04em;padding:3px 9px;border-radius:12px}
.yts h1{margin:8px 0 2px;font-size:30px;line-height:1.15;border:none;padding:0}.yts h1 .de{color:var(--o)}
.yts .en{font-size:17px;color:var(--m)}
.yts .goal{margin-top:10px;font-size:15px}
.yts .chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.yts .chip{font-size:12.5px;padding:3px 10px;border-radius:12px;border:1px solid var(--l)}
.yts .btn{padding:8px 14px;border-radius:8px;border:1px solid var(--l);background:rgba(128,128,128,.13);color:inherit;cursor:pointer;font:inherit;text-decoration:none;display:inline-flex;gap:6px;align-items:center}
.yts .btn.p{background:var(--a);border-color:var(--a);color:#fff;font-weight:600}.yts .btn.ok{background:var(--g);border-color:var(--g);color:#fff}
.yts .btn:disabled{opacity:.45;cursor:default}
.yts .acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.yts .nav{position:sticky;top:0;z-index:5;display:flex;gap:2px;overflow-x:auto;margin:22px -16px 0;padding:0 16px;border-bottom:1px solid var(--l);
  background:var(--bg);scrollbar-width:none}
.yts .nav a{padding:10px 12px;white-space:nowrap;color:inherit;text-decoration:none;border-bottom:2px solid transparent;font-size:14px}
.yts .nav a.on{border-color:var(--a);color:var(--a)}
.yts section{scroll-margin-top:52px;margin-top:26px}
.yts h2{font-size:20px;margin:0 0 10px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;border:none;padding:0}
.yts h3{font-size:16px;margin:0 0 8px;display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.yts .box{border:1px solid var(--l);border-radius:12px;padding:16px;background:var(--card);margin-bottom:14px}
.yts .sum{font-size:15.5px}
.yts .chaps{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:6px}
.yts .chap{display:flex;gap:10px;align-items:center;padding:7px 10px;border-radius:8px;border:1px solid var(--l);cursor:pointer;background:none;color:inherit;font:inherit;text-align:left}
.yts .chap:hover,.yts .chap.now{border-color:var(--a);background:var(--a2)}
.yts .ts{font-variant-numeric:tabular-nums;font-size:12px;color:var(--m);min-width:38px}
.yts .tsb{font-size:12px;color:var(--a);background:none;border:none;cursor:pointer;padding:0;font:inherit;font-size:12px}
.yts .words{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:8px}
.yts .w{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border-radius:10px;border:1px solid var(--l);background:rgba(128,128,128,.05)}
.yts .w .de{color:var(--o);font-weight:650;font-size:16.5px;line-height:1.25}.yts .w .tr{font-size:14px}.yts .w .nt{font-size:12.5px;color:var(--m);margin-top:2px}
.yts .play{flex:none;width:32px;height:32px;border-radius:50%;border:1px solid var(--a);background:none;color:var(--a);cursor:pointer;font-size:13px;display:flex;align-items:center;justify-content:center}
.yts .play.on{background:var(--a);color:#fff}
.yts .text{white-space:pre-line;margin:0 0 10px}
.yts table{border-collapse:collapse;width:100%;margin:6px 0 4px;font-size:14.5px}
.yts th,.yts td{border:1px solid var(--l);padding:7px 10px;text-align:left;vertical-align:top}.yts th{background:rgba(128,128,128,.12)}
.yts .tblw{overflow-x:auto}
.yts .tips li{margin:4px 0}
.yts .slides{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
.yts .sl{position:relative;border-radius:8px;overflow:hidden;cursor:pointer;border:2px solid transparent;background:#111;padding:0}
.yts .sl img{display:block;width:100%;aspect-ratio:16/9;object-fit:cover}.yts .sl.now{border-color:var(--a)}
.yts .sl span{position:absolute;left:6px;bottom:6px;background:rgba(0,0,0,.75);color:#fff;font-size:11.5px;padding:1px 6px;border-radius:4px}
.yts .q{margin-bottom:14px}.yts .q .opts{display:grid;gap:6px;margin-top:6px}
.yts .opt{text-align:left;padding:10px 12px;border-radius:8px;border:1px solid var(--l);background:none;color:inherit;font:inherit;cursor:pointer}
.yts .opt.right{border-color:var(--g);background:rgba(63,179,127,.15)}.yts .opt.wrong{border-color:var(--r);background:rgba(224,85,85,.13)}
.yts .why{font-size:13.5px;color:var(--m);margin-top:6px}
.yts .fc{perspective:900px;max-width:420px;margin:0 auto}
.yts .fci{position:relative;height:180px;transition:transform .45s;transform-style:preserve-3d;cursor:pointer}.yts .fci.flip{transform:rotateY(180deg)}
.yts .fcf{position:absolute;inset:0;backface-visibility:hidden;border:1px solid var(--l);border-radius:14px;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:16px;text-align:center;background:rgba(128,128,128,.09)}
.yts .fcf.b{transform:rotateY(180deg)}.yts .fcf .big{font-size:24px;font-weight:650}.yts .fcf .big.de{color:var(--o)}
.yts .fcbar{display:flex;gap:8px;justify-content:center;margin-top:12px;flex-wrap:wrap}
.yts .sp{display:flex;gap:10px;align-items:flex-start;padding:10px 0;border-bottom:1px solid var(--l)}.yts .sp:last-child{border:none}
.yts .sp details summary{cursor:pointer;color:var(--a);font-size:13.5px}.yts .sp .ans{color:var(--o);font-weight:600}
.yts .tr-lines{max-height:440px;overflow:auto;border:1px solid var(--l);border-radius:10px;padding:6px}
.yts .ln{display:flex;gap:10px;padding:4px 8px;border-radius:6px;cursor:pointer}.yts .ln:hover{background:rgba(128,128,128,.1)}
.yts .ln.now{background:var(--a2)}
.yts .pn{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:30px}
.yts .pn a{display:block;padding:12px 14px;border:1px solid var(--l);border-radius:10px;text-decoration:none;color:inherit}.yts .pn a:hover{border-color:var(--a)}
.yts .pn .nx{text-align:right}.yts .pn small{color:var(--m);display:block}
.yts .bar{height:6px;background:var(--l);border-radius:3px;overflow:hidden;margin-top:6px;max-width:260px}.yts .bar i{display:block;height:100%;background:var(--g)}
.yts .dock{position:fixed;bottom:12px;z-index:50;border:1px solid var(--l);border-radius:14px;
  padding:8px 12px;display:flex;gap:10px;align-items:center;background:var(--bg);box-shadow:0 6px 24px rgba(0,0,0,.25)}
.yts .dock .pp{width:40px;height:40px;border-radius:50%;border:none;background:var(--a);color:#fff;font-size:16px;cursor:pointer;flex:none}
.yts .dock input[type=range]{flex:1;min-width:60px;accent-color:var(--a)}
.yts .dock .now{font-size:12.5px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.yts .dock select{background:transparent;color:inherit;border:1px solid var(--l);border-radius:6px;padding:4px}
.yts .empty{color:var(--m);font-style:italic}
@media (max-width:700px){.yts .hero{grid-template-columns:1fr}.yts h1{font-size:24px}.yts .dock .now{display:none}.yts .pn{grid-template-columns:1fr}}
</style>
<div class="yts" id="yts"><p class="m">Loading…</p></div>
<script>
(() => {
const root = document.getElementById('yts');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const mmss = t => { t = Math.max(0, Math.floor(t || 0)); return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`; };
const [, , , name, vid] = location.pathname.split('/');
const pl = new URLSearchParams(location.search).get('pl');
let D, audio, stopAt = null, playingBtn = null;

function md(s){   // **bold**, *italic*, `code` — the lesson text is written by hand, nothing else
  return esc(s).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/\*(.+?)\*/g, '<i>$1</i>').replace(/`(.+?)`/g, '<code>$1</code>');
}
function words(L){ return (L?.sections || []).flatMap(s => (s.items || []).filter(i => i.de && i.en)); }

function render(){
  const L = D.lesson, P = D.playlist; if (L?.chapters?.length) D.chapters = L.chapters; const ch = D.chapters;
  const qs = pl ? `?pl=${encodeURIComponent(pl)}` : '';
  const link = e => `/youtube/study/${name}/${e.id}${qs}`;
  const W = words(L);
  const secs = [['ov', 'Overview'], ...(L?.sections?.length ? [['learn', 'Learn']] : []), ...(D.slides.length ? [['slides', 'Slides']] : []),
    ...(L && (L.quiz?.length || W.length || L.speak?.length) ? [['practice', 'Practice']] : []), ['transcript', 'Transcript']];
  const ytAt = t => `https://www.youtube.com/watch?v=${vid}&t=${Math.floor(t || 0)}s`;
  root.innerHTML = `
  <div class="crumbs"><a href="/youtube?c=${encodeURIComponent(name)}">‹ ${esc(name)}</a>${P ? `<span class="m">· ${esc(P.title)}</span>` : ''}</div>
  <div class="hero"><img src="${esc(D.thumbnail)}" alt="" referrerpolicy="no-referrer">
    <div>${L?.n != null ? `<span class="badge">${esc(L.level || '')} · LESSON ${esc(L.n)}</span>` : P ? `<span class="badge">${P.index} / ${P.count}</span>` : ''}
      <h1>${L?.de ? `<span class="de">${esc(L.de)}</span>` : esc(D.title)}</h1>${L?.en ? `<div class="en">${esc(L.en)}</div>` : ''}
      ${L?.goal ? `<div class="goal">🎯 ${md(L.goal)}</div>` : ''}
      <div class="chips"><span class="chip">⏱ ${mmss(D.duration)}</span>${ch.length ? `<span class="chip">${ch.length} chapters</span>` : ''}
        ${W.length ? `<span class="chip">${W.length} words & phrases</span>` : ''}${D.slides.length ? `<span class="chip">${D.slides.length} slides</span>` : ''}
        ${P ? `<span class="chip">course: ${P.done} / ${P.count} done</span>` : ''}</div>
      ${P ? `<div class="bar"><i style="width:${Math.round(P.done / P.count * 100)}%"></i></div>` : ''}
      <div class="acts">${D.media ? `<button class="btn p" id="go">▶ Listen</button>` : ''}
        <button class="btn ${D.done ? 'ok' : ''}" id="done">${D.done ? '✓ Done ' + esc(D.done) : '☐ Mark as done'}</button>
        <a class="btn" href="${ytAt(0)}" target="_blank" rel="noopener" id="yt">Watch on YouTube ↗</a></div></div></div>
  <nav class="nav" id="nav">${secs.map(([k, l]) => `<a href="#s-${k}" data-k="${k}">${l}</a>`).join('')}</nav>

  <section id="s-ov">
    ${L?.summary ? `<div class="box"><h2>📝 What this lesson teaches</h2><div class="sum">${md(L.summary)}</div></div>` : !L ? `<div class="box empty">No written lesson for this video yet — the sound, slides and transcript are below.</div>` : ''}
    ${ch.length ? `<div class="box"><h2>📑 Chapters</h2><div class="chaps">${ch.map((c, i) => `<button class="chap" data-t="${c.start_time}" data-ci="${i}"><span class="ts">${mmss(c.start_time)}</span>${esc(c.title)}</button>`).join('')}</div></div>` : ''}
    ${L?.tips?.length ? `<div class="box"><h2>💡 Remember</h2><ul class="tips">${L.tips.map(t => `<li>${md(t)}</li>`).join('')}</ul></div>` : ''}
    ${L?.notes?.length ? `<div class="box"><h2>🔗 In your notes</h2><ul>${L.notes.map(n => `<li><a href="/view/${n.split('/').map(encodeURIComponent).join('/')}">${esc(n.replace(/\.md$/, '').split('/').pop())}</a></li>`).join('')}</ul></div>` : ''}
  </section>

  ${L?.sections?.length ? `<section id="s-learn"><h2>📚 Learn</h2>${L.sections.map(s => `<div class="box sec"${s.t != null ? ` data-t="${s.t}"` : ''}>
      <h3>${esc(s.title)}${s.t != null ? `<button class="tsb" data-t="${s.t}">▶ ${mmss(s.t)}</button>` : ''}</h3>
      ${s.text ? `<p class="text">${md(s.text)}</p>` : ''}
      ${s.items?.length ? `<div class="words">${s.items.map(i => `<div class="w">${i.t != null ? `<button class="play" data-t="${i.t}" data-e="${i.end ?? ''}" title="Hear it (${mmss(i.t)})">▶</button>` : ''}
        <div><div class="de">${esc(i.de)}</div>${i.en ? `<div class="tr">${md(i.en)}</div>` : ''}${i.note ? `<div class="nt">${md(i.note)}</div>` : ''}</div></div>`).join('')}</div>` : ''}
      ${s.table ? `<div class="tblw"><table>${s.table.head ? `<tr>${s.table.head.map(h => `<th>${md(h)}</th>`).join('')}</tr>` : ''}${s.table.rows.map(r => `<tr>${r.map(c => `<td>${md(c)}</td>`).join('')}</tr>`).join('')}</table></div>` : ''}
    </div>`).join('')}</section>` : ''}

  ${D.slides.length ? `<section id="s-slides"><h2>🖼 Slides <span class="m">what the video shows — tap one to hear that moment</span></h2>
    <div class="slides">${D.slides.map(s => `<button class="sl" data-t="${s.t}" data-e="${s.end}"><img src="${esc(s.src)}" alt="" loading="lazy"><span>${mmss(s.t)}</span></button>`).join('')}</div></section>` : ''}

  ${L && (L.quiz?.length || W.length || L.speak?.length) ? `<section id="s-practice"><h2>🏋️ Practice</h2>
    ${L.quiz?.length ? `<div class="box"><h3>Quiz <span class="m" id="qscore"></span></h3><div id="quiz"></div></div>` : ''}
    ${W.length ? `<div class="box"><h3>Flashcards <span class="m" id="fcn"></span></h3><div id="fc"></div></div>` : ''}
    ${L.speak?.length ? `<div class="box"><h3>Say it out loud 🗣</h3>${L.speak.map(s => `<div class="sp"><div>🗨</div><div><div>${md(s.prompt)}</div>
       <details><summary>Show an answer</summary><div class="ans">${esc(s.answer)}</div></details></div></div>`).join('')}</div>` : ''}
  </section>` : ''}

  <section id="s-transcript"><h2>💬 Transcript <span class="m">${D.transcript.kind === 'whisper' ? 'made here with Whisper — German words can be misspelt, the Learn tab has them right' : esc(D.transcript.kind || '')}</span></h2>
    ${D.transcript.lines.length ? `<div class="tr-lines" id="trl">${D.transcript.lines.map((l, i) => `<div class="ln" data-t="${l.t}" data-i="${i}"><span class="ts">${mmss(l.t)}</span><span>${esc(l.text)}</span></div>`).join('')}</div>`
      : '<p class="empty">No transcript yet.</p>'}
  </section>

  ${P ? `<div class="pn">${P.prev ? `<a href="${link(P.prev)}"><small>‹ Previous</small>${esc(P.prev.title)}</a>` : '<span></span>'}
    ${P.next ? `<a class="nx" href="${link(P.next)}"><small>Next ›</small>${esc(P.next.title)}</a>` : ''}</div>` : ''}

  ${D.media ? `<div class="dock"><button class="pp" id="pp" aria-label="Play">▶</button><span class="ts" id="cur">0:00</span>
    <input type="range" id="seek" min="0" max="${D.duration || 0}" step="1" value="0"><span class="ts">${mmss(D.duration)}</span>
    <span class="now m" id="nowch"></span>
    <select id="rate" title="Speed">${[0.6, 0.75, 0.9, 1, 1.25].map(r => `<option ${r === 1 ? 'selected' : ''} value="${r}">${r}×</option>`).join('')}</select>
    <audio id="au" preload="metadata" src="${esc(D.media)}"></audio></div>` : ''}`;
  wire();
  if (L?.quiz?.length) quiz(L.quiz);
  if (W.length) cards(W);
}

function play(t, end, btn){
  if (!audio) return;
  if (playingBtn) playingBtn.classList.remove('on');
  playingBtn = btn || null; if (btn) btn.classList.add('on');
  stopAt = end ? +end : null;
  audio.currentTime = +t; audio.play();
}
function wire(){
  audio = document.getElementById('au');
  const pp = document.getElementById('pp'), seek = document.getElementById('seek'), cur = document.getElementById('cur');
  root.querySelectorAll('[data-t]').forEach(b => b.addEventListener('click', () => {
    const t = +b.dataset.t, e = b.dataset.e;
    // a word / slide plays only its own bit (to the next word when no end is written); chapters & lines keep going
    let end = e ? +e : null;
    if (b.classList.contains('play') && !e) { const all = [...root.querySelectorAll('.play')].map(x => +x.dataset.t).filter(x => x > t); end = all.length ? Math.min(Math.min(...all), t + 8) : t + 5; }
    play(t, b.classList.contains('play') || b.classList.contains('sl') ? end : null, b.classList.contains('play') ? b : null);
  }));
  const go = document.getElementById('go'); if (go) go.onclick = () => play(audio.currentTime || 0);
  document.getElementById('done').onclick = async () => {
    const r = await fetch(`/youtube/study-api/${name}/${vid}/done`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({done: !D.done})}).then(r => r.json());
    D.done = r.done; if (D.playlist) D.playlist.done += r.done ? 1 : -1; const y = scrollY; render(); scrollTo(0, y);
  };
  const nav = document.getElementById('nav'), tabs = [...nav.querySelectorAll('a')];
  const spy = () => { let on = tabs[0]; for (const a of tabs) { const s = document.getElementById('s-' + a.dataset.k); if (s && s.getBoundingClientRect().top < 120) on = a; } tabs.forEach(a => a.classList.toggle('on', a === on)); };
  document.addEventListener('scroll', spy, {passive: true, capture: true}); spy();   // the viewer scrolls an inner pane, not the window
  if (!audio) return;
  const dock = root.querySelector('.dock');   // fixed to the screen, but as wide as the page column (the sidebar can open/close)
  const fit = () => { const r = root.getBoundingClientRect(); dock.style.left = r.left + 16 + 'px'; dock.style.width = r.width - 32 - (innerWidth < 700 ? 52 : 0) + 'px'; };   // phone: clear the viewer's round buttons
  fit(); addEventListener('resize', fit); new ResizeObserver(fit).observe(root);
  const rate = document.getElementById('rate'); rate.onchange = () => audio.playbackRate = +rate.value;
  pp.onclick = () => audio.paused ? audio.play() : audio.pause();
  audio.onplay = () => pp.textContent = '❚❚'; audio.onpause = () => { pp.textContent = '▶'; if (playingBtn) playingBtn.classList.remove('on'); };
  seek.oninput = () => { stopAt = null; audio.currentTime = +seek.value; };
  const lines = [...root.querySelectorAll('.ln')], chs = [...root.querySelectorAll('.chap')], sls = [...root.querySelectorAll('.sl')], trl = document.getElementById('trl');
  const ytl = document.getElementById('yt');
  let lastLine = -1;
  audio.ontimeupdate = () => {
    const t = audio.currentTime;
    if (stopAt != null && t >= stopAt) { audio.pause(); stopAt = null; }
    seek.value = Math.floor(t); cur.textContent = mmss(t);
    ytl.href = `https://www.youtube.com/watch?v=${vid}&t=${Math.floor(t)}s`;
    const c = D.chapters.findLastIndex(c => c.start_time <= t);
    chs.forEach((b, i) => b.classList.toggle('now', i === c));
    document.getElementById('nowch').textContent = c >= 0 ? D.chapters[c].title : '';
    sls.forEach(b => b.classList.toggle('now', +b.dataset.t <= t && t < +b.dataset.e));
    const li = D.transcript.lines.findLastIndex(l => l.t <= t + 0.2);
    if (li !== lastLine) { lastLine = li; lines.forEach((x, i) => x.classList.toggle('now', i === li));
      if (li >= 0 && trl && !audio.paused) { const el = lines[li]; trl.scrollTop = el.offsetTop - trl.offsetTop - trl.clientHeight / 3; } }
  };
}

function shuffle(a){ a = [...a]; for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; }
function quiz(Q){
  const box = document.getElementById('quiz'); let right = 0, answered = 0;
  box.innerHTML = Q.map((q, k) => `<div class="q" data-k="${k}"><div><b>${k + 1}.</b> ${md(q.q)}</div><div class="opts">${shuffle([q.a, ...(q.wrong || [])]).map(o => `<button class="opt" data-ok="${o === q.a ? 1 : 0}">${esc(o)}</button>`).join('')}</div><div class="why" hidden>${md(q.why || '')}</div></div>`).join('')
    + `<button class="btn" id="qagain">↻ Try again</button>`;
  box.querySelectorAll('.q').forEach(qd => qd.querySelectorAll('.opt').forEach(b => b.onclick = () => {
    if (qd.dataset.done) return; qd.dataset.done = 1; answered++;
    if (b.dataset.ok === '1') right++; else b.classList.add('wrong');
    qd.querySelector('[data-ok="1"]').classList.add('right'); const w = qd.querySelector('.why'); if (w.textContent.trim()) w.hidden = false;
    document.getElementById('qscore').textContent = `${right} / ${answered} right${answered === Q.length ? (right === Q.length ? ' — perfect! 🎉' : '') : ''}`;
  }));
  document.getElementById('qagain').onclick = () => { document.getElementById('qscore').textContent = ''; quiz(Q); };
}
function cards(W){
  const box = document.getElementById('fc'); let deck = shuffle(W), k = 0, again = [], dir = 'en';
  const show = () => {
    if (k >= deck.length) { if (again.length) { deck = shuffle(again); again = []; k = 0; } else { box.innerHTML = `<p style="text-align:center">All ${W.length} done ✓ <button class="btn" id="fcre">↻ Again</button></p>`; document.getElementById('fcre').onclick = () => { deck = shuffle(W); k = 0; show(); }; document.getElementById('fcn').textContent = ''; return; } }
    const c = deck[k], front = dir === 'en' ? `<div class="big">${md(c.en)}</div><div class="m">say it in German · tap to turn</div>` : `<div class="big de">${esc(c.de)}</div><div class="m">what does it mean? · tap to turn</div>`;
    const back = `<div class="big de">${esc(c.de)}</div><div>${md(c.en)}</div>${c.note ? `<div class="m">${md(c.note)}</div>` : ''}`;
    document.getElementById('fcn').textContent = `${k + 1} / ${deck.length}${again.length ? ` · ${again.length} to repeat` : ''}`;
    box.innerHTML = `<div class="fc"><div class="fci" id="fci"><div class="fcf">${front}</div><div class="fcf b">${back}</div></div></div>
      <div class="fcbar">${c.t != null ? `<button class="btn" id="fch">▶ Hear it</button>` : ''}<button class="btn" id="fcx">✗ Again</button><button class="btn p" id="fco">✓ Knew it</button>
      <button class="btn" id="fcd" title="Switch direction">${dir === 'en' ? 'EN → DE' : 'DE → EN'}</button></div>`;
    document.getElementById('fci').onclick = e => e.currentTarget.classList.toggle('flip');
    const h = document.getElementById('fch'); if (h) h.onclick = () => { const all = W.map(x => x.t).filter(x => x > c.t); play(c.t, c.end ?? (all.length ? Math.min(Math.min(...all), c.t + 8) : c.t + 5)); };
    document.getElementById('fcx').onclick = () => { again.push(c); k++; show(); };
    document.getElementById('fco').onclick = () => { k++; show(); };
    document.getElementById('fcd').onclick = () => { dir = dir === 'en' ? 'de' : 'en'; show(); };
  };
  show();
}

fetch(`/youtube/study-api/${name}/${vid}${pl ? '?pl=' + encodeURIComponent(pl) : ''}`).then(r => r.ok ? r.json() : Promise.reject(r.status))
  .then(d => { D = d; document.title = (d.lesson?.de ? `${d.lesson.de} · ` : '') + d.title; render();
    const t0 = +(new URLSearchParams(location.search).get('t') || 0);   // ?t=130 (links from the vault notes): start the sound there
    if (t0 && audio) { audio.currentTime = t0; [...document.querySelectorAll('.sec[data-t]')].reverse().find(s => +s.dataset.t <= t0)?.scrollIntoView(); }
    else if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView(); })
  .catch(e => root.innerHTML = `<p style="color:var(--r)">Couldn't load this video (${esc(e)}).</p>`);
})();
</script>
'''
