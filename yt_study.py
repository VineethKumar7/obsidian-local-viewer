"""📖 One page per downloaded YouTube video, made for learning from it (/youtube/study/<channel>/<id>): the sound with
speed control, the video's chapters, its on-screen slides, a written lesson (words, grammar, tips), practice
(quiz, flashcards, speak-aloud) and the transcript that follows the sound. Every ▶ plays just that bit of the video.

Data, all on the data drive next to the downloads (<vault folder>/<channel>/), none of it in the vault:
  info/<id>.info.json           YouTube's details (title, chapters, description) — written by the download engine
  transcripts/<id>.whisper.json local Whisper transcript (tools/german/yt/transcribe_a1.py), or YouTube's captions
  frames/<id>/index.json + jpgs  slides cut from YouTube's storyboard (tools/german/yt/frames_a1.py)
  frames/<id>/big/<t>.jpg        the same slides enlarged 4× for the slide viewer (tools/german/yt/frames_big.py)
  study/<id>.json               the written lesson (schema below); a video without one still gets sound/slides/transcript
  study/progress.json           {id: "YYYY-MM-DD"} for the videos marked done

Lesson schema (study/<id>.json; t = seconds into the video, every field optional):
  {"n": 1, "de": "Begrüßungen", "en": "Greetings", "goal": "...", "summary": "...", "level": "A1",
   "sections": [{"title", "t", "text", "items": [{"de", "en", "t", "end", "note"}],
                 "table": {"head": [...], "rows": [[...]]}}],
   "chapters": [{"start_time", "title"}]  (replaces YouTube's when its titles are bad), "tips": ["..."], "quiz": [{"q", "a", "wrong": [...], "why"}], "speak": [{"prompt", "answer"}],
   "notes": ["vault/relative/path.md"]}
Words for the flashcards = every section item.
Quiz + flashcard answers go to the German practice progress when that module runs in this viewer and knows the lesson
(/practice/api/video/<channel>/<id>, any playlist: tools/german/yt/videos.py exercise_lessons); anywhere else the page just doesn't record them.
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


def progress_file(d: Path) -> Path:
    """study/progress.json, or study/progress-<id>.json when the vault has profiles (app.py VIEWER_PROFILE)."""
    try:
        from flask import current_app
        pid = current_app.config.get("VIEWER_PROFILE", lambda: None)()
    except RuntimeError:  # outside a request
        pid = None
    return d / "study" / (f"progress-{pid}.json" if pid else "progress.json")


def study_index(d: Path) -> dict:
    """{id: "lesson" | "done"} for the channel page's cards."""
    s = d / "study"
    out = {f.stem: "lesson" for f in s.glob("*.json") if not f.stem.startswith("progress")} if s.exists() else {}
    for v in (_j(progress_file(d), {}) or {}):
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


def _lesson(d: Path, vid: str):
    """study/<id>.json plus the course lessons that teach the same thing (study/course-notes.json, from build_course.py)."""
    L = _j(d / "study" / f"{vid}.json")
    extra = (_j(d / "study" / "course-notes.json", {}) or {}).get(vid) or []
    if L is not None and (extra or L.get("notes")):
        # the video's own vault note opens this very page, so it is left out
        L["notes"] = [n for n in dict.fromkeys((L.get("notes") or []) + extra) if not n.startswith("Videos/")]
    return L


def _course(d: Path, pl: str, prog: dict) -> list:
    """Every video of the playlist for the side panel: [{"id", "n", "title", "de", "en", "done"}] (de/en from its written lesson)."""
    ch = _j(d / "channel.json", {}) or {}
    E = next((p["entries"] for p in ch.get("playlists") or [] if p["id"] == pl), [])
    out = []
    for k, e in enumerate(E):
        L = _j(d / "study" / f"{e['id']}.json", {}) or {}
        out.append({"id": e["id"], "n": L.get("n", k + 1), "title": e.get("title") or e["id"], "de": L.get("de"),
                    "en": L.get("en"), "done": e["id"] in prog})
    return out


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
        # the video's vault note (written by build_course.py): its 📍 Navigation + 🕸️ Connections go under the page
        note = (_j(d / "study" / "vault-notes.json", {}) or {}).get(vid)
        footer = app.config.get("NOTE_FOOTER")
        foot = f'<div class="yts-foot">{footer(note)}</div>' if note and footer else ""
        return render(info.get("title") or vid, PAGE + foot)

    @app.get("/youtube/study-api/<name>/<vid>")
    def yt_study_api(name, vid):
        d = chan(name, vid)
        Ch = C()
        info = _j(d / "info" / f"{vid}.info.json", {}) or {}
        f = Ch.file(name, vid)
        T = Ch.transcript(name, vid)
        fr = _j(d / "frames" / vid / "index.json", {}) or {}
        prog = _j(progress_file(d), {}) or {}
        ctx = _context(d, vid, request.args.get("pl"))
        if ctx:
            ids = [e["id"] for p in (_j(d / "channel.json", {}) or {}).get("playlists") or [] if p["id"] == ctx["id"]
                   for e in p["entries"]]
            ctx["done"] = sum(v in prog for v in ids)
            ctx["all"] = _course(d, ctx["id"], prog)
        return jsonify({
            "id": vid, "channel": name, "title": info.get("title") or vid, "duration": info.get("duration"),
            "upload_date": info.get("upload_date"), "chapters": info.get("chapters") or [],
            "description": (info.get("description") or "")[:3000],
            "media": f"/youtube/media/{name}/{vid}" if f else None, "ext": f.suffix[1:] if f else None,
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "lesson": _lesson(d, vid),
            "slides": [{**s, "src": f"/youtube/frames/{name}/{vid}/{s['file']}"} for s in fr.get("slides") or []],
            "transcript": {"kind": T.get("kind"), "lang": T.get("lang"), "lines": T.get("lines") or []},
            "playlist": ctx, "done": prog.get(vid)})

    @app.post("/youtube/study-api/<name>/<vid>/done")
    def yt_study_done(name, vid):
        d = chan(name, vid)
        (d / "study").mkdir(exist_ok=True)
        p = progress_file(d)
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
        big = d / "frames" / vid / "big" / file
        return send_file(big if request.args.get("big") and big.exists() else d / "frames" / vid / file, max_age=86400)


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
.yts .w .de{color:var(--o);font-weight:650;font-size:16.5px;line-height:1.25}.yts .w .ml{font-size:14.5px;color:var(--a);line-height:1.5}.yts .w .tr{font-size:14px}.yts .w .nt{font-size:12.5px;color:var(--m);margin-top:2px}
.yts .play{flex:none;width:32px;height:32px;border-radius:50%;border:1px solid var(--a);background:none;color:var(--a);cursor:pointer;font-size:13px;display:flex;align-items:center;justify-content:center}
.yts .play.on{background:var(--a);color:#fff}
.yts .w.saying{background:var(--o);border-color:var(--o);box-shadow:0 0 0 2px var(--o)}
.yts .w.saying .de,.yts .w.saying .ml,.yts .w.saying .tr,.yts .w.saying .nt,.yts .w.saying .tr *,.yts .w.saying .nt *{color:#111}.yts .w.saying .play{border-color:#111;color:#111}
.yts .text{white-space:pre-line;margin:0 0 10px}
.yts table{border-collapse:collapse;width:100%;margin:6px 0 4px;font-size:14.5px}
.yts th,.yts td{border:1px solid var(--l);padding:7px 10px;text-align:left;vertical-align:top}.yts th{background:rgba(128,128,128,.12)}
.yts .tblw{overflow-x:auto}
.yts .tips li{margin:4px 0}
.yts .slides{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
.yts .sl{position:relative;border-radius:8px;overflow:hidden;cursor:pointer;border:2px solid transparent;background:#111;padding:0}
.yts .sl img{display:block;width:100%;aspect-ratio:16/9;object-fit:cover}.yts .sl.now{border-color:var(--a)}
.yts .sl span{position:absolute;left:6px;bottom:6px;background:rgba(0,0,0,.75);color:#fff;font-size:11.5px;padding:1px 6px;border-radius:4px}
.yts .lb{position:fixed;inset:0;z-index:10000;background:rgba(10,12,14,.97);display:flex;flex-direction:column;align-items:center;justify-content:center;padding:14px;gap:12px;color:#eee}
.yts .lb img{width:min(96vw,1100px,calc((100vh - 220px) * 16 / 9));aspect-ratio:16/9;border-radius:8px;background:#111;object-fit:contain}
.yts .lb .lbw{max-width:min(96vw,1100px);max-height:24vh;overflow:auto;text-align:center;font-size:15px}
.yts .lb .lbw b{color:var(--o);font-size:18px}.yts .lb .lbw .saying{background:var(--o);color:#111;border-radius:6px}.yts .lb .lbw .saying b{color:#111}.yts .lb .lbbar{display:flex;gap:8px;flex-wrap:wrap;justify-content:center}
.yts .lb .btn{background:#2a2d31;color:#eee;border-color:#555}
.yts .q{margin-bottom:14px}.yts .q .opts{display:grid;gap:6px;margin-top:6px}
.yts .opt{text-align:left;padding:10px 12px;border-radius:8px;border:1px solid var(--l);background:none;color:inherit;font:inherit;cursor:pointer}
.yts .opt.right{border-color:var(--g);background:rgba(63,179,127,.15)}.yts .opt.wrong{border-color:var(--r);background:rgba(224,85,85,.13)}
.yts .why{font-size:13.5px;color:var(--m);margin-top:6px}
.yts .fc{max-width:460px;margin:0 auto}
.yts .fci{cursor:pointer;user-select:none}
.yts .fcf{min-height:180px;border:1px solid var(--l);border-radius:14px;display:flex;flex-direction:column;gap:6px;align-items:center;justify-content:center;padding:18px;text-align:center;background:rgba(128,128,128,.09)}
.yts .fcf.b,.yts .fci.flip .fcf.f{display:none}.yts .fci.flip .fcf.b{display:flex;animation:fcturn .28s ease-out}
@keyframes fcturn{from{transform:perspective(900px) rotateY(-90deg);opacity:.3}to{transform:none;opacity:1}}
.yts .fcf .big{font-size:22px;font-weight:650;line-height:1.3}.yts .fcf .big.de{color:var(--o)}
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
.yts .dock .skip{height:36px;padding:0 9px;border:1px solid var(--l);border-radius:18px;background:transparent;color:inherit;font-size:13px;white-space:nowrap;cursor:pointer;flex:none;touch-action:manipulation}
.yts .dock .skip:hover{border-color:var(--a);background:var(--a2)}
.yts .dock.two{flex-wrap:wrap;row-gap:6px}.yts .dock.two::before{content:"";flex-basis:100%;order:1}
.yts .dock.two .ts,.yts .dock.two input[type=range]{order:2}.yts .dock.two select{margin-left:auto}.yts .dock.two .now{display:none}
.yts .dock input[type=range]{flex:1;min-width:60px;accent-color:var(--a)}
.yts .dock .now{font-size:12.5px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.yts .dock select{background:transparent;color:inherit;border:1px solid var(--l);border-radius:6px;padding:4px}
.yts .empty{color:var(--m);font-style:italic}
.yts-foot{max-width:1320px;margin:-80px auto 0;padding:0 16px 120px}
/* course panel: every video of the playlist, this one highlighted (right column on wide screens, a drawer on narrow ones) */
.yts.side{max-width:1320px;display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:28px;align-items:start}
.yts .ytmain{min-width:0}
.yts .yside{position:sticky;top:8px;max-height:calc(100vh - 100px);display:flex;flex-direction:column;border:1px solid var(--l);border-radius:12px;background:var(--bg);overflow:hidden}
.yts .yside h3{margin:0;padding:12px 14px 4px;font-size:15px}.yts .yside .m{padding:0 14px 8px;display:block}
.yts .yside .bar{margin:0 14px 10px;max-width:none}
.yts .ylist{overflow-y:auto;padding:0 6px 8px;flex:1}
.yts .yv{display:flex;gap:10px;align-items:flex-start;padding:7px 8px;border-radius:8px;color:inherit !important;text-decoration:none;font-size:13.5px;line-height:1.3}
.yts .yv:hover{background:rgba(128,128,128,.12)}
.yts .yv.cur{background:var(--a2);outline:1px solid var(--a)}
.yts .yv .num{flex:none;width:28px;height:28px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:12.5px;border:1.5px solid var(--l)}
.yts .yv.cur .num{background:var(--a);border-color:var(--a);color:#fff}.yts .yv.done .num{border-color:var(--g);color:var(--g)}
.yts .yv.cur.done .num{color:#fff}
.yts .yv .tt{min-width:0}.yts .yv .tde{font-weight:600}.yts .yv .ten{color:var(--m);font-size:12px}
.yts .yv .ok{margin-left:auto;color:var(--g);flex:none}
.yts .yopen,.yts .yclose{display:none}
@media (max-width:1000px){
  .yts.side{display:block;max-width:980px}
  .yts .yside{position:fixed;top:0;right:0;bottom:0;width:min(340px,88vw);max-height:none;z-index:10000;border-radius:0;transform:translateX(105%);transition:transform .25s;box-shadow:-8px 0 30px rgba(0,0,0,.35)}
  .yts .yside.open{transform:none}
  .yts .yopen{display:inline-flex;margin-left:auto;padding:5px 10px;font-size:13px}
  .yts .yclose{display:block;position:absolute;top:8px;right:8px;border:none;background:none;color:inherit;font-size:22px;cursor:pointer}
}
@media (max-width:700px){.yts .hero{grid-template-columns:1fr}.yts h1{font-size:24px}.yts .dock .now{display:none}.yts .dock{bottom:66px}.yts .pn{grid-template-columns:1fr}}
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
const meaning = en => en.replace(/^\S{1,2} — /, '');   // alphabet items: "S — sun" read as the word's sound; only the meaning goes on this line
function words(L){ return (L?.sections || []).flatMap(s => (s.items || []).filter(i => i.de && i.en)); }

function render(){
  const L = D.lesson, P = D.playlist; if (L?.chapters?.length) D.chapters = L.chapters; const ch = D.chapters;
  const qs = pl ? `?pl=${encodeURIComponent(pl)}` : '';
  const link = e => `/youtube/study/${name}/${e.id}${qs}`;
  const W = words(L);
  const secs = [['ov', 'Overview'], ...(L?.sections?.length ? [['learn', 'Learn']] : []), ...(D.slides.length ? [['slides', 'Slides']] : []),
    ...(L && (L.quiz?.length || W.length || L.speak?.length) ? [['practice', 'Practice']] : []), ['transcript', 'Transcript']];
  const ytAt = t => `https://www.youtube.com/watch?v=${vid}&t=${Math.floor(t || 0)}s`;
  const A = P?.all || [];
  root.classList.toggle('side', A.length > 1);
  root.innerHTML = `<div class="ytmain">
  <div class="crumbs"><a href="/youtube?c=${encodeURIComponent(name)}">‹ ${esc(name)}</a>${P ? `<span class="m">· ${esc(P.title)}</span>` : ''}
    ${A.length > 1 ? `<button class="btn yopen" id="yopen">📚 ${P.index} / ${P.count} · all videos</button>` : ''}</div>
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
    ${L?.notes?.length ? `<div class="box"><h2>📚 Same topic in your course lessons</h2><ul>${L.notes.map(n => `<li><a href="/view/${n.split('/').map(encodeURIComponent).join('/')}">${esc(n.replace(/\.md$/, '').split('/').pop())}</a></li>`).join('')}</ul></div>` : ''}
  </section>

  ${L?.sections?.length ? `<section id="s-learn"><h2>📚 Learn</h2>${L.sections.map(s => `<div class="box sec"${s.t != null ? ` data-t="${s.t}"` : ''}>
      <h3>${esc(s.title)}${s.t != null ? `<button class="tsb" data-t="${s.t}">▶ ${mmss(s.t)}</button>` : ''}</h3>
      ${s.text ? `<p class="text">${md(s.text)}</p>` : ''}
      ${s.items?.length ? `<div class="words">${s.items.map(i => `<div class="w">${i.t != null ? `<button class="play" data-t="${i.t}" data-e="${i.end ?? ''}" title="Hear it (${mmss(i.t)})">▶</button>` : ''}
        <div><div class="de">${esc(i.de)}</div>${i.ml ? `<div class="ml" lang="ml"><small>DE</small> ${esc(i.ml)}</div>` : ''}${i.en ? `<div class="tr"><small class="m">Meaning:</small> ${md(meaning(i.en))}</div>` : ''}${i.men ? `<div class="ml" lang="ml"><small>EN</small> ${esc(i.men)}</div>` : ''}${i.note ? `<div class="nt">${md(i.note)}</div>` : ''}</div></div>`).join('')}</div>` : ''}
      ${s.table ? `<div class="tblw"><table>${s.table.head ? `<tr>${s.table.head.map(h => `<th>${md(h)}</th>`).join('')}</tr>` : ''}${s.table.rows.map(r => `<tr>${r.map(c => `<td>${md(c)}</td>`).join('')}</tr>`).join('')}</table></div>` : ''}
    </div>`).join('')}</section>` : ''}

  ${D.slides.length ? `<section id="s-slides"><h2>🖼 Slides <span class="m">what the video shows — tap one to see it big and hear that moment</span></h2>
    <div class="slides">${D.slides.map((s, i) => `<button class="sl" data-i="${i}"><img src="${esc(s.src)}" alt="" loading="lazy"><span>${mmss(s.t)}</span></button>`).join('')}</div></section>` : ''}

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

  ${D.media ? `<div class="dock"><button class="skip" id="back5" aria-label="Back 5 seconds">↺ 5s</button><button class="pp" id="pp" aria-label="Play">▶</button><button class="skip" id="fwd5" aria-label="Forward 5 seconds">5s ↻</button><span class="ts" id="cur">0:00</span>
    <input type="range" id="seek" min="0" max="${D.duration || 0}" step="1" value="0"><span class="ts">${mmss(D.duration)}</span>
    <span class="now m" id="nowch"></span>
    <select id="rate" title="Speed">${[0.6, 0.75, 0.9, 1, 1.25].map(r => `<option ${r === 1 ? 'selected' : ''} value="${r}">${r}×</option>`).join('')}</select>
    <audio id="au" preload="metadata" src="${esc(D.media)}"></audio></div>` : ''}</div>
  ${A.length > 1 ? `<aside class="yside" id="yside"><button class="yclose" id="yclose" aria-label="Close">✕</button>
    <h3>📚 ${esc(P.title)}</h3><span class="m">Video ${P.index} of ${P.count} · ${P.done} done</span>
    <div class="bar"><i style="width:${Math.round(P.done / P.count * 100)}%"></i></div>
    <div class="ylist">${A.map(e => `<a class="yv${e.id === vid ? ' cur' : ''}${e.done ? ' done' : ''}" href="${link(e)}"${e.id === vid ? ' aria-current="page"' : ''}>
      <span class="num">${esc(e.n)}</span><span class="tt">${e.de ? `<div class="tde">${esc(e.de)}</div><div class="ten">${esc(e.en || e.title)}</div>` : `<div>${esc(e.title)}</div>`}</span>${e.done ? '<span class="ok">✓</span>' : ''}</a>`).join('')}</div></aside>` : ''}`;
  wire();
  const side = document.getElementById('yside');
  if (side) {
    const cur = side.querySelector('.yv.cur'), list = side.querySelector('.ylist');
    if (cur) list.scrollTop = cur.offsetTop - list.clientHeight / 2 + cur.offsetHeight / 2;   // this video in the middle of the list
    document.getElementById('yopen').onclick = () => side.classList.add('open');
    document.getElementById('yclose').onclick = () => side.classList.remove('open');
    document.addEventListener('click', e => { if (side.classList.contains('open') && !side.contains(e.target) && e.target.id !== 'yopen') side.classList.remove('open'); });   // tap outside closes the drawer
  }
  startTracking();
  if (L?.quiz?.length) quiz(L.quiz);
  if (W.length) cards(W);
}

function setBtn(b, on){   // a playing ▶ turns into ■ (tap it again to stop)
  if (!b) return; b.classList.toggle('on', on);
  if (b.dataset.lbl == null) b.dataset.lbl = b.textContent;
  b.textContent = on ? (b.classList.contains('play') ? '■' : '■ Stop') : b.dataset.lbl;
}
function play(t, end, btn){
  if (!audio) return;
  if (btn && btn === playingBtn && !audio.paused) { audio.pause(); return; }   // second tap = stop
  const old = playingBtn; playingBtn = null; setBtn(old, false);
  stopAt = end ? +end : null;
  audio.currentTime = +t; audio.play();
  playingBtn = btn || null; setBtn(btn, true);
}
function wire(){
  audio = document.getElementById('au');
  const pp = document.getElementById('pp'), seek = document.getElementById('seek'), cur = document.getElementById('cur');
  root.querySelectorAll('button[data-t], .ln').forEach(b => b.addEventListener('click', () => {   // not .sec: a ▶ inside a section must not restart it
    const t = +b.dataset.t, e = b.dataset.e;
    // a word plays only its own bit (its exact end from words_ts.py, else up to the next word); chapters & lines keep going
    let end = e ? +e : null;
    if (b.classList.contains('play') && !e) { const all = [...root.querySelectorAll('.play')].map(x => +x.dataset.t).filter(x => x > t); end = all.length ? Math.min(Math.min(...all), t + 8) : t + 5; }
    play(t, b.classList.contains('play') ? end : null, b.classList.contains('play') ? b : null);
  }));
  root.querySelectorAll('.sl').forEach(b => b.onclick = () => slide(+b.dataset.i));
  const go = document.getElementById('go');
  if (go) go.onclick = () => audio && !audio.paused && !playingBtn ? audio.pause() : play(audio.currentTime || 0);
  document.getElementById('done').onclick = async () => {
    const r = await fetch(`/youtube/study-api/${name}/${vid}/done`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({done: !D.done})}).then(r => r.json());
    D.done = r.done; if (D.playlist) { D.playlist.done += r.done ? 1 : -1; const me = (D.playlist.all || []).find(e => e.id === vid); if (me) me.done = !!r.done; } const y = scrollY; render(); scrollTo(0, y);
  };
  const nav = document.getElementById('nav'), tabs = [...nav.querySelectorAll('a')];
  const spy = () => { let on = tabs[0]; for (const a of tabs) { const s = document.getElementById('s-' + a.dataset.k); if (s && s.getBoundingClientRect().top < 120) on = a; } tabs.forEach(a => a.classList.toggle('on', a === on)); };
  document.addEventListener('scroll', spy, {passive: true, capture: true}); spy();   // the viewer scrolls an inner pane, not the window
  if (!audio) return;
  const dock = root.querySelector('.dock');   // fixed to the screen, but as wide as the page column (the sidebar can open/close)
  const fit = () => { const r = root.querySelector('.ytmain').getBoundingClientRect(); dock.style.left = r.left + 'px'; dock.style.width = r.width - (innerWidth < 700 ? 52 : 0) + 'px'; dock.classList.toggle('two', dock.offsetWidth < 480); };   // phone: clear the viewer's round buttons
  fit(); addEventListener('resize', fit); new ResizeObserver(fit).observe(root);
  const rate = document.getElementById('rate'); rate.onchange = () => audio.playbackRate = +rate.value;
  pp.onclick = () => audio.paused ? audio.play() : audio.pause();
  audio.onplay = () => { pp.textContent = '❚❚'; if (go && !playingBtn) go.textContent = '❚❚ Pause'; };
  audio.onpause = () => { pp.textContent = '▶'; if (go) go.textContent = '▶ Listen'; const b = playingBtn; playingBtn = null; setBtn(b, false); };
  seek.oninput = () => { stopAt = null; audio.currentTime = +seek.value; };
  const skip = s => { stopAt = null; audio.currentTime = Math.max(0, Math.min(audio.duration || D.duration || 0, audio.currentTime + s)); };
  document.getElementById('back5').onclick = () => skip(-5);
  document.getElementById('fwd5').onclick = () => skip(5);
  const lines = [...root.querySelectorAll('.ln')], chs = [...root.querySelectorAll('.chap')], sls = [...root.querySelectorAll('.sl')], trl = document.getElementById('trl');
  const ytl = document.getElementById('yt');
  let lastLine = -1;
  // the Learn word being said right now lights up (every time it is said: says from build_lessons.py, else its own ▶ stretch)
  const learn = document.getElementById('s-learn');
  const cards = learn ? [...learn.querySelectorAll('.w')] : [];
  const spans = (D.lesson?.sections || []).flatMap(s => s.items || []).map(i => i.says || (i.t != null ? [[i.t, i.end ?? i.t + 3]] : []));
  let lit = '', handScroll = 0;
  ['wheel', 'touchmove'].forEach(e => document.addEventListener(e, () => handScroll = Date.now(), {passive: true, capture: true}));
  const mark = () => {
    const t = audio.currentTime, on = cards.filter((c, i) => spans[i].some(([a, b]) => a <= t && t < b));
    const key = on.map(c => cards.indexOf(c)).join();
    if (key === lit) return;
    lit = key; cards.forEach(c => c.classList.toggle('saying', on.includes(c)));
    root.querySelectorAll('.lb [data-k]').forEach(d => d.classList.toggle('saying', on.includes(cards[+d.dataset.k])));   // the big slide's word list too
    if (!on.length || audio.paused || root.querySelector('.lb') || Date.now() - handScroll < 6000) return;   // never fight someone scrolling by hand
    const s = learn.getBoundingClientRect(), r = on[0].getBoundingClientRect();
    if (s.top < innerHeight && s.bottom > 0 && (r.top < 60 || r.bottom > innerHeight - 90)) on[0].scrollIntoView({block: 'center', behavior: 'smooth'});   // only while reading Learn; 90 = the dock
  };
  const tick = () => { mark(); if (!audio.paused) requestAnimationFrame(tick); };   // timeupdate is only ~4×/s: too late for a word under a second
  audio.addEventListener('play', tick);
  audio.ontimeupdate = () => {
    const t = audio.currentTime;
    mark();
    if (stopAt != null && t >= stopAt) { audio.pause(); stopAt = null; }
    seek.value = Math.floor(t); cur.textContent = mmss(t);
    ytl.href = `https://www.youtube.com/watch?v=${vid}&t=${Math.floor(t)}s`;
    const c = D.chapters.findLastIndex(c => c.start_time <= t);
    chs.forEach((b, i) => b.classList.toggle('now', i === c));
    document.getElementById('nowch').textContent = c >= 0 ? D.chapters[c].title : '';
    sls.forEach(b => { const s = D.slides[+b.dataset.i]; b.classList.toggle('now', s.t <= t && t < s.end); });
    const li = D.transcript.lines.findLastIndex(l => l.t <= t + 0.2);
    if (li !== lastLine) { lastLine = li; lines.forEach((x, i) => x.classList.toggle('now', i === li));
      if (li >= 0 && trl && !audio.paused) { const el = lines[li]; trl.scrollTop = el.offsetTop - trl.offsetTop - trl.clientHeight / 3; } }
  };
}

function slide(i){   // big slide + the words taught while it is on screen; ‹ › / arrow keys step, Esc closes
  document.querySelector('.yts .lb')?.remove();
  const S = D.slides, s = S[i]; if (!s) return;
  const all = (D.lesson?.sections || []).flatMap(x => x.items || []);
  const said = words(D.lesson).filter(w => w.t != null && w.t >= s.t - 1 && w.t < s.end);
  const lines = D.transcript.lines.filter(l => l.end > s.t && l.t < s.end);
  const lb = document.createElement('div'); lb.className = 'lb';
  lb.innerHTML = `<img src="${esc(s.src)}?big=1" alt="">
    <div class="lbw">${said.length ? said.map(w => `<div data-k="${all.indexOf(w)}"><b>${esc(w.de)}</b> — ${md(w.en)}${w.ml ? `<div lang="ml"><small>DE</small> ${esc(w.ml)}${w.men ? ` · <small>EN</small> ${esc(w.men)}` : ''}</div>` : ''}</div>`).join('') : lines.map(l => esc(l.text)).join(' ')}</div>
    <div class="lbbar"><button class="btn" id="lbp">‹</button><button class="btn p" id="lbh">▶ Hear it</button><span class="m" style="align-self:center">${i + 1} / ${S.length} · ${mmss(s.t)}</span>
      <button class="btn" id="lbn">›</button><button class="btn" id="lbx">✕ Close</button></div>`;
  root.appendChild(lb);
  const h = lb.querySelector('#lbh');
  const close = () => { if (playingBtn === h && !audio.paused) audio.pause(); lb.remove(); removeEventListener('keydown', key); };
  const key = e => { if (e.key === 'Escape') close(); else if (e.key === 'ArrowRight') { close(); slide(i + 1); } else if (e.key === 'ArrowLeft') { close(); slide(i - 1); } };
  addEventListener('keydown', key);
  lb.onclick = e => { if (e.target === lb) close(); };
  lb.querySelector('#lbx').onclick = close;
  lb.querySelector('#lbp').onclick = () => { close(); slide(Math.max(0, i - 1)); };
  lb.querySelector('#lbn').onclick = () => { close(); slide(Math.min(S.length - 1, i + 1)); };
  h.onclick = () => play(s.t, s.end, h);
  play(s.t, s.end, h);
}

// answers count like a book lesson's: Mistakes to fix, reviews, XP and the dashboard graphs (id = /practice item, key = question or German word)
let TRACK = null, trackStarted = false;
const practicePost = (path, body) => fetch('/practice/api/' + path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).catch(() => {});
function startTracking(){
  if (trackStarted) return; trackStarted = true;
  fetch(`/practice/api/video/${name}/${vid}`).then(r => r.ok ? r.json() : null).then(x => { if (x) TRACK = x; }).catch(() => {});
}
function track(key, correct, source){ const item = TRACK?.ids[key]; if (item) practicePost('answer', {item, correct, source}); }

function shuffle(a){ a = [...a]; for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; }
function quiz(Q){
  const box = document.getElementById('quiz'); let right = 0, answered = 0;
  box.innerHTML = Q.map((q, k) => `<div class="q" data-k="${k}"><div><b>${k + 1}.</b> ${md(q.q)}</div><div class="opts">${shuffle([q.a, ...(q.wrong || [])]).map(o => `<button class="opt" data-ok="${o === q.a ? 1 : 0}">${esc(o)}</button>`).join('')}</div><div class="why" hidden>${md(q.why || '')}</div></div>`).join('')
    + `<button class="btn" id="qagain">↻ Try again</button>`;
  box.querySelectorAll('.q').forEach(qd => qd.querySelectorAll('.opt').forEach(b => b.onclick = () => {
    if (qd.dataset.done) return; qd.dataset.done = 1; answered++;
    if (b.dataset.ok === '1') right++; else b.classList.add('wrong');
    track(Q[+qd.dataset.k].q, b.dataset.ok === '1');
    if (answered === Q.length && TRACK) practicePost('finish', {lesson: TRACK.lesson, correct: right, total: Q.length});   // a finished quiz = a session: score + check-in dates in Revisit
    qd.querySelector('[data-ok="1"]').classList.add('right'); const w = qd.querySelector('.why'); if (w.textContent.trim()) w.hidden = false;
    document.getElementById('qscore').textContent = `${right} / ${answered} right${answered === Q.length ? (right === Q.length ? ' — perfect! 🎉' : '') : ''}`;
  }));
  document.getElementById('qagain').onclick = () => { document.getElementById('qscore').textContent = ''; quiz(Q); };
}
function cards(W){
  const box = document.getElementById('fc'); let deck = shuffle(W), k = 0, again = [], dir = 'en';
  // the German is said only when a card is turned over, never when a new one appears: that would give the answer away; the choice is remembered on this device
  const soundOn = () => { try { return localStorage.getItem('german-card-sound') !== 'off'; } catch { return true; } };
  const setSound = v => { try { localStorage.setItem('german-card-sound', v ? 'on' : 'off'); } catch {} };
  const show = () => {
    if (k >= deck.length) { if (again.length) { deck = shuffle(again); again = []; k = 0; } else { if (TRACK) practicePost('revisit', {lesson: TRACK.lesson}); box.innerHTML = `<p style="text-align:center">All ${W.length} done ✓ <button class="btn" id="fcre">↻ Again</button></p>`; document.getElementById('fcre').onclick = () => { deck = shuffle(W); k = 0; show(); }; document.getElementById('fcn').textContent = ''; return; } }
    const c = deck[k], front = dir === 'en' ? `<div class="big">${md(c.en)}</div><div class="m">say it in German · tap to turn</div>` : `<div class="big de">${esc(c.de)}</div><div class="m">what does it mean? · tap to turn</div>`;
    const back = `<div class="big de">${esc(c.de)}</div>${c.ml ? `<div lang="ml"><small class="m">DE</small> ${esc(c.ml)}</div>` : ''}<div><small class="m">Meaning:</small> ${md(meaning(c.en))}</div>${c.men ? `<div lang="ml"><small class="m">EN</small> ${esc(c.men)}</div>` : ''}${c.note ? `<div class="m">${md(c.note)}</div>` : ''}`;
    document.getElementById('fcn').textContent = `${k + 1} / ${deck.length}${again.length ? ` · ${again.length} to repeat` : ''}`;
    box.innerHTML = `<div class="fc"><div class="fci" id="fci"><div class="fcf f">${front}</div><div class="fcf b">${back}</div></div></div>
      <div class="fcbar"><button class="btn" id="fcs" aria-pressed="${soundOn()}">${soundOn() ? '🔊 Sound on' : '🔇 Sound off'}</button><button class="btn" id="fcx">✗ Again</button><button class="btn p" id="fco">✓ Knew it</button>
      <button class="btn" id="fcd" title="Switch direction">${dir === 'en' ? 'EN → DE' : 'DE → EN'}</button></div>`;
    const say = () => { if (c.t == null || !soundOn()) return; const all = W.map(x => x.t).filter(x => x > c.t); play(c.t, c.end ?? (all.length ? Math.min(Math.min(...all), c.t + 8) : c.t + 5)); };
    document.getElementById('fci').onclick = e => { if (e.currentTarget.classList.toggle('flip')) say(); };
    document.getElementById('fcs').onclick = e => { setSound(!soundOn()); e.currentTarget.textContent = soundOn() ? '🔊 Sound on' : '🔇 Sound off'; e.currentTarget.setAttribute('aria-pressed', soundOn()); if (!soundOn() && audio && stopAt != null) audio.pause(); };
    document.getElementById('fcx').onclick = () => { track(c.de, false, 'flashcard'); again.push(c); k++; show(); };
    document.getElementById('fco').onclick = () => { track(c.de, true, 'flashcard'); k++; show(); };
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
