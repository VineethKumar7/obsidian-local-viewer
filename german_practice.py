"""Duolingo-style practice for the German course vault (only active when <vault>/Practice/exercises.json exists).

Routes (registered by app.py via register(app, vault_path)):
  GET  /practice/<lesson_id>      exercise session for one lesson (e.g. /practice/S1-1.1)
  GET  /practice/review           mistakes + spaced-review items that are due
  GET  /practice/api/data         exercises.json (built by ~/clawd/tools/german/build_course.py)
  GET  /practice/api/state        XP / goal / streak summary + due item ids
  POST /practice/api/answer       {item, correct}            -> XP, updated summary
  POST /practice/api/finish       {lesson, correct, total}   -> bonus XP, marks the lesson note Completed
  POST /practice/api/goal         {goal}                     -> change the daily XP goal
  GET  /practice/phrases/<id>     "🔊 Phrases only": every phrase + story line of a lesson, played one after another
  POST /practice/api/revisit      {lesson}                   -> a phrases-only replay counts as a revisit (Today check-ins)
The study dashboard (📊 in the viewer) also counts these answers: app.py calls study_activity().
Progress lives in <vault>/Practice/state.json (logic shared with the Today page: tools/german/practice_state.py).
"""
import json
import os
import subprocess
import sys
import threading
from html import escape
from pathlib import Path
from urllib.parse import quote

from flask import Blueprint, abort, jsonify, request, send_file

GERMAN_TOOLS = Path.home() / "clawd" / "tools" / "german"
sys.path.insert(0, str(GERMAN_TOOLS))
import practice_state  # noqa: E402

_lock = threading.Lock()
_vault = None
_app = None


def _profile():
	"""The viewer profile of this request (app.py VIEWER_PROFILE), or None when the vault has no profiles."""
	return _app.config.get("VIEWER_PROFILE", lambda: None)() if _app else None


def _state_file():
	pid = _profile()
	return practice_state.state_path(pid) if pid else _vault / "Practice" / "state.json"


def study_activity():
	"""Practice answers in the shape app.py's study dashboard merges in (EXTRA_STUDY_ACTIVITY)."""
	s = practice_state.load(_state_file())
	st = practice_state.summary(s)
	return {"days": practice_state.activity(s), "goal": st["goal"], "unit": "XP", "today": st["xp_today"],
		"streak": st["streak"], "due": st["due"], "mistakes": st["mistakes"], "reviewUrl": "/practice/review"}


def _short(iso):
	y, m, d = iso.split("-")
	return f"{int(d)} {['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][int(m) - 1]}"


def home_panel():
	"""HTML for the viewer's start page (app.py EXTRA_HOME_HTML): reviews due, today's lesson, lessons to revisit."""
	lessons = json.loads((_vault / "Practice" / "exercises.json").read_text(encoding="utf-8"))["lessons"]
	get_meta = _app.config["FILE_METADATA"][0]  # profile-aware "completed" (app.py)
	done = [l["id"] for l in lessons if get_meta(l["note"]).get("completed")]
	by_id = {l["id"]: l for l in lessons}
	s = practice_state.load(_state_file())
	st = practice_state.summary(s)
	seen = [x for x in practice_state.lessons_seen(s, done) if x["lesson"] in by_id]
	view = lambda note: "/view/" + quote(note)
	name = lambda l: escape(Path(l["note"]).stem)
	nxt = next((l for l in lessons if l["id"] not in done), None)

	top = []
	if st["due"]:
		top.append(f'<a class="gp-big due" href="/practice/review">🔁 Reviews due <b>{st["due"]}</b><small>about {max(1, st["due"] // 4)} min</small></a>')
	if nxt:
		top.append(f'<a class="gp-big" href="{view(nxt["note"])}">🎯 Today <b>{name(nxt)}</b><small>{escape(nxt["goal"])}</small></a>')
	top.append('<a class="gp-big" href="/view/00%20-%20Phrasebook.md">📒 Phrasebook<small>every phrase so far, with audio</small></a>')

	rows = []
	for x in sorted(seen, key=lambda x: not x["due"])[:8]:
		l = by_id[x["lesson"]]
		info = [f"last {_short(x['last'])}" if x["last"] else "ticked, not practised yet"]
		if x["score"]:
			info.append(f"{x['score'][0]}/{x['score'][1]}")
		badge = '<span class="gp-badge">check-in today</span>' if x["due"] else f'<span class="gp-next">back on {_short(x["checkin"])}</span>'
		rows.append(f'<div class="gp-row"><div class="gp-name"><b>{name(l)}</b> {badge}<small>{" · ".join(info)}</small></div>'
			f'<div class="gp-acts"><a href="{view(l["note"])}">📖 Reread</a><a href="/practice/{l["id"]}">🎯 Practise again</a>'
			f'<a href="/practice/phrases/{l["id"]}">🔊 Phrases only</a></div></div>')
	if not rows:
		rows.append('<div class="gp-empty">Lessons you finish show up here, so you can come back to them.</div>')
	return f"""<style>
.gp{{background:#1e1e1e;border:1px solid #3c3c3c;border-radius:16px;padding:24px;margin-bottom:30px}}
.gp h2{{margin:0 0 16px;font-size:1.4em}}.gp h3{{margin:22px 0 10px;font-size:1.05em;color:#cfcfcf}}
.gp-top{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}}
.gp a.gp-big{{display:flex;flex-direction:column;gap:4px;padding:14px 16px;border-radius:12px;background:#252526;border:1px solid #3c3c3c;color:#e0e0e0 !important;text-decoration:none}}
.gp-big b{{font-size:1.05em}}.gp-big small,.gp-name small{{color:#9a9a9a;font-size:.85em}}
.gp a.gp-big.due{{background:linear-gradient(135deg,#7c3aed,#5b21b6);border-color:#7c3aed;color:#fff !important}}.gp-big.due small{{color:#e9ddff}}
.gp-big:hover{{transform:translateY(-2px)}}
.gp-row{{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px;padding:12px 0;border-top:1px solid #333}}
.gp-name{{display:flex;flex-direction:column;gap:3px;min-width:0}}
.gp-acts{{display:flex;flex-wrap:wrap;gap:8px}}
.gp .gp-acts a{{padding:8px 12px;border-radius:10px;background:#252526;border:1px solid #3c3c3c;color:#e0e0e0 !important;text-decoration:none;font-size:.9em;white-space:nowrap}}
.gp-acts a:hover{{border-color:#7c3aed}}
.gp-badge{{align-self:flex-start;padding:1px 8px;border-radius:99px;background:#22c55e30;color:#4ade80;font-size:.75em;font-weight:600}}
.gp-next{{align-self:flex-start;color:#8a8a8a;font-size:.8em}}.gp-empty{{color:#9a9a9a}}
</style>
<div class="gp">
 <h2>🇩🇪 Pick up where you left off</h2>
 <div class="gp-top">{''.join(top)}</div>
 <h3>🔁 Revisit what you learned</h3>
 {''.join(rows)}
</div>"""


def register(app, vault_path):
	vault = Path(vault_path)
	data_file = vault / "Practice" / "exercises.json"
	if not data_file.exists():
		return False
	global _vault, _app
	_vault, _app = vault, app
	bp = Blueprint("german_practice", __name__)

	def lesson_note(lesson_id):
		for les in json.loads(data_file.read_text(encoding="utf-8"))["lessons"]:
			if les["id"] == lesson_id:
				return les["note"]
		return None

	def state_reply(s):
		return {"summary": practice_state.summary(s), "due": practice_state.due_items(s)}

	@bp.route("/practice/api/data")
	def data():
		return send_file(data_file, mimetype="application/json", max_age=0)

	@bp.route("/practice/api/state")
	def state():
		with _lock:
			return jsonify(state_reply(practice_state.load(_state_file())))

	@bp.route("/practice/api/answer", methods=["POST"])
	def answer():
		body = request.get_json(force=True)
		with _lock:
			s = practice_state.load(_state_file())
			gained = practice_state.record_answer(s, str(body["item"]), bool(body["correct"]))
			practice_state.save(s, _state_file())
			return jsonify({"xp": gained, **state_reply(s)})

	@bp.route("/practice/api/finish", methods=["POST"])
	def finish():
		body = request.get_json(force=True)
		lesson = str(body.get("lesson", "review"))
		with _lock:
			s = practice_state.load(_state_file())
			bonus = practice_state.finish_session(s, lesson, int(body.get("correct", 0)), int(body.get("total", 0)))
			practice_state.save(s, _state_file())
			note = lesson_note(lesson) if lesson != "review" else None
			if note:  # finishing the practice = lesson done (same field as the viewer's ⓘ "Completed", per profile)
				get_meta, set_meta = app.config["FILE_METADATA"]
				set_meta(note, {**get_meta(note), "completed": True})
			reply = {"bonus": bonus, **state_reply(s)}
		# refresh "00 - Today" + course map right away instead of waiting for the timer
		subprocess.Popen([sys.executable, str(GERMAN_TOOLS / "build_course.py"), "--today"],
			stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
		return jsonify(reply)

	@bp.route("/practice/api/goal", methods=["POST"])
	def goal():
		g = int(request.get_json(force=True).get("goal", 10))
		if g not in (5, 10, 20, 30, 50):
			abort(400)
		with _lock:
			s = practice_state.load(_state_file())
			s["goal"] = g
			practice_state.save(s, _state_file())
			return jsonify(state_reply(s))

	@bp.route("/practice/api/revisit", methods=["POST"])
	def revisit():
		lesson = str(request.get_json(force=True).get("lesson", ""))
		if not lesson_note(lesson):
			abort(404)
		with _lock:
			s = practice_state.load(_state_file())
			practice_state.revisit(s, lesson)
			practice_state.save(s, _state_file())
		subprocess.Popen([sys.executable, str(GERMAN_TOOLS / "build_course.py"), "--today"],
			stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
		return jsonify({"ok": True})

	@bp.route("/practice/phrases/<lesson_id>")
	def phrases(lesson_id):
		if not lesson_note(lesson_id):
			abort(404)
		return PHRASES_PAGE.replace("__LESSON__", json.dumps(lesson_id)), 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}

	@bp.route("/practice/<lesson_id>")
	def page(lesson_id):
		if lesson_id != "review" and not lesson_note(lesson_id):
			abort(404)
		return PAGE.replace("__LESSON__", json.dumps(lesson_id)), 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}

	app.register_blueprint(bp)
	return True


PAGE = r"""<!doctype html>
<html lang="de"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Practice</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1f2328;--mut:#6b7280;--line:#e5e7eb;--acc:#58a700;--acc2:#89e219;--bad:#e5484d;--badbg:#ffe4e4;--okbg:#e7f9d7;--tile:#fff;--tileb:#d1d5db;--pri:#1cb0f6}
@media (prefers-color-scheme:dark){:root{--bg:#131f24;--card:#1b2a31;--ink:#e8eef1;--mut:#9aa7ad;--line:#2b3b43;--okbg:#1f3b17;--badbg:#3d1d1f;--tile:#22333b;--tileb:#3a4d56}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--ink);font:18px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:720px;margin:0 auto;min-height:100vh;display:flex;flex-direction:column;padding:12px 16px calc(16px + env(safe-area-inset-bottom))}
.top{display:flex;align-items:center;gap:12px}
.x{font-size:26px;color:var(--mut);text-decoration:none;padding:4px 8px}
.bar{flex:1;height:16px;background:var(--line);border-radius:10px;overflow:hidden}
.bar>i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--acc),var(--acc2));border-radius:10px;transition:width .35s}
.xp{font-weight:700;color:#ffb020;white-space:nowrap}
main{flex:1;padding:22px 0 12px}
h2{font-size:22px;margin:4px 0 18px}
.prompt{font-size:20px;margin:0 0 16px;padding:14px 16px;background:var(--card);border:2px solid var(--line);border-radius:16px}
.who{color:var(--mut);font-size:14px}
.snd{display:flex;gap:12px;margin:8px 0 20px}
.snd button{font-size:30px;width:84px;height:72px;border-radius:18px;border:none;background:var(--pri);color:#fff;box-shadow:0 4px 0 #1899d6}
.snd button.slow{background:#8b5cf6;box-shadow:0 4px 0 #6d28d9;font-size:26px}
.answer{min-height:64px;border-bottom:2px solid var(--line);display:flex;flex-wrap:wrap;gap:8px;padding:8px 0;margin-bottom:18px}
.bank{display:flex;flex-wrap:wrap;gap:10px}
.tile{font-size:19px;padding:10px 14px;border-radius:12px;border:2px solid var(--tileb);background:var(--tile);color:var(--ink);box-shadow:0 3px 0 var(--tileb)}
.tile.used{visibility:hidden}
.opts{display:grid;gap:12px}
.opt{text-align:left;font-size:19px;padding:14px 16px;border-radius:14px;border:2px solid var(--tileb);background:var(--tile);color:var(--ink);box-shadow:0 3px 0 var(--tileb)}
.opt.sel{border-color:var(--pri);background:rgba(28,176,246,.12)}
.pairs{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.pairs .opt{text-align:center}
.opt.done{opacity:.35;pointer-events:none}
.opt.flash{border-color:var(--bad);background:var(--badbg)}
input.type{width:100%;font-size:20px;padding:14px;border-radius:14px;border:2px solid var(--tileb);background:var(--tile);color:var(--ink)}
.gap{display:inline-block;min-width:70px;border-bottom:3px solid var(--pri);text-align:center;color:var(--pri);font-weight:700}
footer{position:sticky;bottom:0;padding-top:10px;background:var(--bg)}
.fb{display:none;padding:14px 16px;border-radius:16px;margin-bottom:12px;font-size:18px}
.fb.ok{display:block;background:var(--okbg);color:var(--acc)}
.fb.bad{display:block;background:var(--badbg);color:var(--bad)}
.fb b{display:block;font-size:20px;margin-bottom:4px}
.fb .sol{color:var(--ink)}
.fb .play{border:none;background:none;font-size:22px;vertical-align:middle;padding:0 6px}
.go{width:100%;font-size:19px;font-weight:700;letter-spacing:.5px;padding:16px;border-radius:16px;border:none;background:var(--acc);color:#fff;box-shadow:0 4px 0 #46a302;text-transform:uppercase}
.go:disabled{background:var(--line);color:var(--mut);box-shadow:none}
.go.bad{background:var(--bad);box-shadow:0 4px 0 #b4282d}
.end{text-align:center;padding-top:30px}
.ring{width:150px;height:150px;border-radius:50%;margin:18px auto;display:grid;place-items:center;font-weight:800;font-size:22px}
.ring span{background:var(--bg);width:118px;height:118px;border-radius:50%;display:grid;place-items:center}
.stats{display:flex;gap:12px;justify-content:center;margin:12px 0 24px;flex-wrap:wrap}
.stat{background:var(--card);border:2px solid var(--line);border-radius:16px;padding:12px 16px;min-width:120px}
.stat b{display:block;font-size:24px}
.links{display:grid;gap:12px}
.links a{display:block;text-decoration:none;text-align:center}
.links a.sec{background:var(--card);color:var(--pri);border:2px solid var(--line);box-shadow:0 3px 0 var(--line)}
.goalsel{margin-top:22px;color:var(--mut);font-size:15px}
.goalsel button{font-size:15px;margin:4px;padding:6px 12px;border-radius:10px;border:2px solid var(--line);background:var(--card);color:var(--ink)}
.goalsel button.on{border-color:var(--acc);color:var(--acc);font-weight:700}
.hint{color:var(--mut);font-size:15px;margin-top:10px}
</style></head>
<body><div class="wrap">
<div class="top"><a class="x" id="exit" href="/view/00%20-%20Today.md" aria-label="Exit">✕</a><div class="bar"><i id="bar"></i></div><div class="xp" id="xp">⭐ 0</div></div>
<main id="main"><h2>Loading…</h2></main>
<footer id="foot"><div class="fb" id="fb"></div><button class="go" id="go" disabled>Check</button></footer>
</div>
<script>
const LESSON = __LESSON__;
const $ = s => document.querySelector(s);
const shuffle = a => { a = a.slice(); for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; };
const pick = (a, n) => shuffle(a).slice(0, n);
let DATA, LESS = {}, ITEM = {}, ALL = [], queue = [], idx = 0, correctCount = 0, answered = 0, current = null, summary = null, sessionXP = 0;

// ---------- sound (WebAudio, unlocked on the first tap — iPad needs a user gesture) ----------
let ctx;
function tone(freqs, type = 'sine', dur = 0.12) {
  try {
    ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
    if (ctx.state === 'suspended') ctx.resume();
    freqs.forEach((f, i) => {
      const o = ctx.createOscillator(), g = ctx.createGain(), t = ctx.currentTime + i * dur;
      o.type = type; o.frequency.value = f; g.gain.setValueAtTime(0.18, t); g.gain.exponentialRampToValueAtTime(0.001, t + dur * 1.6);
      o.connect(g).connect(ctx.destination); o.start(t); o.stop(t + dur * 1.7);
    });
  } catch (e) {}
}
const ding = () => tone([660, 880]), buzz = () => tone([180, 140], 'square', 0.14), fanfare = () => tone([523, 659, 784, 1047], 'triangle', 0.13);
let player;
function play(src) { if (!src) return; if (player) player.pause(); player = new Audio('/raw/' + src.split('/').map(encodeURIComponent).join('/')); player.play().catch(() => {}); }

// ---------- answer checking ----------
const norm = s => s.toLowerCase().replace(/[’‘`]/g, "'").replace(/[.,!?;:„“"()«»–—\-…]/g, ' ').replace(/\s+/g, ' ').trim();
const fold = s => s.replace(/ä/g, 'ae').replace(/ö/g, 'oe').replace(/ü/g, 'ue').replace(/ß/g, 'ss');
function lev(a, b) { const m = []; for (let i = 0; i <= a.length; i++) { m[i] = [i]; for (let j = 1; j <= b.length; j++) m[i][j] = i ? Math.min(m[i-1][j] + 1, m[i][j-1] + 1, m[i-1][j-1] + (a[i-1] === b[j-1] ? 0 : 1)) : j; } return m[a.length][b.length]; }
function checkTyped(given, target) {
  const g = norm(given), t = norm(target);
  if (!g) return { ok: false };
  if (g === t) return { ok: true };
  if (fold(g) === fold(t)) return { ok: true, note: 'Watch the special letters (ä ö ü ß).' };
  if (lev(g, t) <= (t.length > 14 ? 2 : 1)) return { ok: true, note: 'Small typo — check the spelling.' };
  return { ok: false };
}
const words = de => de.split(/\s+/).map(w => w.replace(/^[„"(«]+|[.,!?;:“"»)]+$/g, '')).filter(Boolean);
const usable = de => !/[\/=…]|\s–\s/.test(de) && words(de).length >= 2 && words(de).length <= 10;

// ---------- distractors ----------
const GROUPS = [
  ['der','die','das','den','dem','des'], ['ein','eine','einen','einem','einer'], ['kein','keine','keinen','keinem','keiner'],
  ['mein','meine','meinen','meinem','meiner'], ['dein','deine','deinen'], ['sein','seine','seinen','ihr','ihre','ihren'],
  ['bin','bist','ist','sind','seid'], ['habe','hast','hat','haben','habt'], ['war','warst','waren','wart','hatte','hatten'],
  ['am','um','im','in','an','auf'], ['wenn','wann','als','dass','ob'], ['weil','obwohl','deshalb','trotzdem','aber','oder','und'],
  ['mir','mich','dir','dich','ihm','ihn','uns','euch'], ['nicht','kein','keine','nie'], ['zum','zur','ins','im'],
  ['kann','kannst','können','muss','musst','darf','will','soll'], ['würde','würdest','würden','wurde','werde'],
];
function distractors(answer, pool, n = 3) {
  const low = answer.toLowerCase(), cap = answer[0] === answer[0].toUpperCase() && answer[0] !== answer[0].toLowerCase();
  const fit = w => cap ? w[0].toUpperCase() + w.slice(1) : w;
  let out = [];
  const g = GROUPS.find(gr => gr.includes(low));
  if (g) out = shuffle(g.filter(w => w !== low)).map(fit);
  const more = shuffle(pool.filter(w => w.toLowerCase() !== low && !out.includes(w)));
  return [...new Set([...out, ...more])].filter(w => w.toLowerCase() !== low).slice(0, n);
}

// ---------- exercise builders ----------
function seenPool(it) {  // words from this lesson and earlier ones only (no spoilers from later lessons)
  const lid = it.id.split(':')[0], pos = DATA.lessons.findIndex(l => l.id === lid);
  const same = LESS[lid].items, earlier = DATA.lessons.slice(Math.max(0, pos - 6), pos).flatMap(l => l.items);
  return { same, earlier };
}
function exTiles(it) {
  const target = words(it.de), { same, earlier } = seenPool(it);
  const wordsOf = xs => xs.filter(x => x.de && x.id !== it.id).flatMap(x => words(x.de)).filter(w => !target.includes(w));
  let others = [...new Set(wordsOf(same))];
  if (others.length < 3) others = [...new Set([...others, ...wordsOf(earlier)])];
  return { type: 'tiles', it, target, bank: shuffle([...target, ...pick(others, Math.min(3, Math.max(2, 9 - target.length)))]) };
}
const exType = it => ({ type: 'type', it });
function exListen(it, lessonItems) {
  const pool = lessonItems.filter(x => x.en && x.id !== it.id && x.en !== it.en);
  const extra = ALL.filter(x => x.en && x.en !== it.en);
  const wrong = [...new Set([...pick(pool, 3).map(x => x.en), ...pick(extra, 3).map(x => x.en)])].slice(0, 3);
  return { type: 'listen', it, options: shuffle([it.en, ...wrong]) };
}
function exGap(it, gapIndex) {
  const pool = ALL.filter(x => x.kind === 'cloze').flatMap(x => x.answers);
  const ans = it.answers[gapIndex];
  return { type: 'gap', it, gapIndex, options: shuffle([ans, ...distractors(ans, pool)]) };
}
const exMcq = it => ({ type: 'mcq', it, options: shuffle([it.correct, ...it.wrong]) });
function exMatch(items) { return { type: 'match', items, left: shuffle(items), right: shuffle(items) }; }

function buildLessonQueue(les) {
  const items = les.items;
  const sayable = items.filter(x => (x.kind === 'phrase' || x.kind === 'line') && usable(x.de));
  const short = sayable.filter(x => words(x.de).length <= 6);
  const listen = pick(items.filter(x => x.kind === 'phrase' || x.kind === 'line'), 3).map(x => exListen(x, items));
  const tiles = pick(sayable, 5).map(exTiles);
  const typed = pick(short.length ? short : sayable, 3).map(exType);
  const gaps = items.filter(x => x.kind === 'cloze').flatMap(x => x.answers.map((_, i) => exGap(x, i)));
  const mcqs = items.filter(x => x.kind === 'mcq').map(exMcq);
  const pairSrc = shuffle([...items.filter(x => x.kind === 'phrase' && x.de.length <= 28 && x.en.length <= 32), ...items.filter(x => x.kind === 'card' && x.a.length <= 28 && x.q.length <= 32).map(c => ({ id: c.id, de: c.a, en: c.q }))]);
  const match = pairSrc.length >= 4 ? [exMatch(pairSrc.slice(0, 4))] : [];
  // easy → hard, mixed: listen, tiles, gap, type, mcq, tiles, match, listen, type, tiles, gap, mcq, type, tiles, listen, gap
  const order = ['listen','tiles','gap','type','mcq','tiles','match','listen','type','tiles','gap','mcq','type','tiles','listen','gap'];
  const pools = { listen, tiles, gap: gaps, type: typed, mcq: mcqs, match };
  const q = [];
  for (const k of order) if (pools[k].length) q.push(pools[k].shift());
  for (const k of ['tiles','gap','listen','type']) while (q.length < 15 && pools[k].length) q.push(pools[k].shift());
  return q.slice(0, 16);
}
function buildReviewQueue(due) {
  const q = [];
  due.forEach((id, n) => {
    const it = ITEM[id]; if (!it) return;
    if (it.kind === 'cloze') q.push(exGap(it, 0));
    else if (it.kind === 'mcq') q.push(exMcq(it));
    else if (it.kind === 'phrase' || it.kind === 'line') q.push(usable(it.de) ? (n % 3 === 0 ? exType(it) : n % 3 === 1 ? exTiles(it) : exListen(it, ALL)) : exListen(it, ALL));
  });
  return q.slice(0, 15);
}

// ---------- rendering ----------
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
function sndButtons(it, auto) {
  if (auto) setTimeout(() => play(it.a), 0);
  return `<div class="snd"><button onclick="play('${it.a}')" aria-label="Play">🔊</button><button class="slow" onclick="play('${it.s}')" aria-label="Slow">🐢</button></div>`;
}
let sel = null, built = [], matchLeft = null, matchErr = 0, matchDone = 0;
function render(fromGesture) {
  const go = $('#go'), fb = $('#fb');
  fb.className = 'fb'; fb.innerHTML = ''; go.className = 'go'; go.textContent = 'Check'; go.disabled = true; sel = null; built = [];
  $('#bar').style.width = (100 * idx / queue.length) + '%';
  if (idx >= queue.length) return renderEnd();
  current = queue[idx];
  const e = current, m = $('#main');
  if (e.type === 'tiles') {
    m.innerHTML = `<h2>Write this in German</h2><div class="prompt">${esc(e.it.en)}</div><div class="answer" id="ans"></div><div class="bank">${e.bank.map((w, i) => `<button class="tile" data-i="${i}">${esc(w)}</button>`).join('')}</div>`;
    m.querySelectorAll('.bank .tile').forEach(b => b.onclick = () => { b.classList.add('used'); built.push(+b.dataset.i); drawAns(); });
  } else if (e.type === 'type') {
    m.innerHTML = `<h2>Type what you hear</h2>${sndButtons(e.it, fromGesture)}<input class="type" id="inp" lang="de" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="Auf Deutsch tippen…"><div class="hint">Tip: ä ö ü ß — or type ae oe ue ss.</div>`;
    const inp = $('#inp'); inp.oninput = () => go.disabled = !inp.value.trim(); inp.onkeydown = ev => { if (ev.key === 'Enter' && !go.disabled) go.click(); };
  } else if (e.type === 'listen') {
    m.innerHTML = `<h2>What does it mean?</h2>${sndButtons(e.it, fromGesture)}<div class="opts">${e.options.map((o, i) => `<button class="opt" data-i="${i}">${esc(o)}</button>`).join('')}</div>`;
    bindOpts(m);
  } else if (e.type === 'mcq') {
    m.innerHTML = `<h2>Choose the right answer</h2><div class="prompt">${esc(e.it.q)}</div><div class="opts">${e.options.map((o, i) => `<button class="opt" data-i="${i}">${esc(o)}</button>`).join('')}</div>`;
    bindOpts(m);
  } else if (e.type === 'gap') {
    let k = 0;
    const html = esc(e.it.text).replace(/\{\{(.+?)\}\}/g, (_, a) => (k++ === e.gapIndex ? '<span class="gap" id="gap">?</span>' : `<b>${a}</b>`));
    m.innerHTML = `<h2>Fill the gap</h2><div class="prompt">${html}</div><div class="opts">${e.options.map((o, i) => `<button class="opt" data-i="${i}">${esc(o)}</button>`).join('')}</div>`;
    bindOpts(m, o => $('#gap').textContent = o);
  } else if (e.type === 'match') {
    matchLeft = null; matchErr = 0; matchDone = 0;
    m.innerHTML = `<h2>Tap the matching pairs</h2><div class="pairs"><div class="opts">${e.left.map(x => `<button class="opt L" data-id="${x.id}">${esc(x.de)}</button>`).join('')}</div><div class="opts">${e.right.map(x => `<button class="opt R" data-id="${x.id}">${esc(x.en)}</button>`).join('')}</div></div>`;
    m.querySelectorAll('.L').forEach(b => b.onclick = () => { m.querySelectorAll('.L').forEach(x => x.classList.remove('sel')); b.classList.add('sel'); matchLeft = b; const it = e.items.find(x => x.id === b.dataset.id); play(it.a); });
    m.querySelectorAll('.R').forEach(b => b.onclick = () => {
      if (!matchLeft) return;
      if (b.dataset.id === matchLeft.dataset.id) { ding(); matchLeft.classList.add('done'); b.classList.add('done'); matchLeft = null; if (++matchDone === e.items.length) { go.disabled = false; go.click(); } }
      else { buzz(); matchErr++; recordItem(matchLeft.dataset.id, false); const l = matchLeft; [l, b].forEach(x => x.classList.add('flash')); setTimeout(() => [l, b].forEach(x => x.classList.remove('flash')), 450); }
    });
  }
}
function bindOpts(m, onSel) {
  m.querySelectorAll('.opts .opt').forEach(b => b.onclick = () => { m.querySelectorAll('.opt').forEach(x => x.classList.remove('sel')); b.classList.add('sel'); sel = +b.dataset.i; $('#go').disabled = false; if (onSel) onSel(current.options[sel]); });
}
function drawAns() {
  const e = current, a = $('#ans');
  a.innerHTML = built.map((bi, k) => `<button class="tile" data-k="${k}">${esc(e.bank[bi])}</button>`).join('');
  a.querySelectorAll('.tile').forEach(t => t.onclick = () => { const bi = built.splice(+t.dataset.k, 1)[0]; document.querySelector(`.bank .tile[data-i="${bi}"]`).classList.remove('used'); drawAns(); });
  $('#go').disabled = !built.length;
}

// ---------- check / continue ----------
let checked = false;
$('#go').onclick = () => {
  if (checked) { checked = false; idx++; return render(true); }
  const e = current; let ok = false, sol = '', note = '', audio = null;
  if (e.type === 'tiles') { ok = built.map(i => e.bank[i]).join(' ') === e.target.join(' '); sol = e.it.de; audio = e.it.a; }
  else if (e.type === 'type') { const r = checkTyped($('#inp').value, e.it.de); ok = r.ok; note = r.note || ''; sol = e.it.de; audio = e.it.a; }
  else if (e.type === 'listen') { ok = e.options[sel] === e.it.en; sol = `${e.it.de} = ${e.it.en}`; audio = e.it.a; }
  else if (e.type === 'mcq') { ok = e.options[sel] === e.it.correct; sol = e.it.correct; }
  else if (e.type === 'gap') { ok = e.options[sel] === e.it.answers[e.gapIndex]; sol = e.it.text.replace(/\{\{(.+?)\}\}/g, '$1'); }
  else if (e.type === 'match') { ok = matchErr === 0; sol = matchErr ? 'You got there — the mixed-up pairs come back in your reviews.' : ''; }
  checked = true;
  if (!e.retry) { answered++; if (ok) correctCount++; }
  if (e.type !== 'match') recordItem(e.it.id, ok, e.retry);
  else if (!e.retry) e.items.forEach(x => recordItem(x.id, true, true));
  if (!ok && !e.retry && e.type !== 'match') queue.push({ ...e, retry: true });
  const fb = $('#fb'), go = $('#go');
  if (ok) { ding(); fb.className = 'fb ok'; fb.innerHTML = `<b>${['Richtig!','Super!','Genau!','Toll!','Prima!'][Math.floor(Math.random()*5)]}</b>${note ? `<div class="sol">${esc(note)} ${esc(sol)}</div>` : ''}`; go.className = 'go'; }
  else { buzz(); fb.className = 'fb bad'; fb.innerHTML = `<b>Not quite</b><div class="sol">Correct: ${esc(sol)} ${audio ? `<button class="play" onclick="play('${audio}')">🔊</button>` : ''}</div>${e.retry ? '' : '<div class="hint">It comes back at the end.</div>'}`; go.className = 'go bad'; }
  if (audio && e.type !== 'listen') play(audio);
  go.textContent = 'Continue'; go.disabled = false;
};
function recordItem(id, correct, silent) {
  if (silent && correct === true && current && current.type !== 'match') return; // retries don't count twice
  fetch('/practice/api/answer', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ item: id, correct }) })
    .then(r => r.json()).then(r => { sessionXP += r.xp; summary = r.summary; showXP(); }).catch(() => {});
}
const showXP = () => { if (summary) $('#xp').textContent = `⭐ ${summary.xp_today}/${summary.goal}`; };

async function renderEnd() {
  $('#bar').style.width = '100%'; $('#foot').style.display = 'none';
  const r = await fetch('/practice/api/finish', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ lesson: LESSON, correct: correctCount, total: answered }) }).then(r => r.json()).catch(() => null);
  if (r) { summary = r.summary; sessionXP += r.bonus; }
  fanfare(); showXP();
  const s = summary || { xp_today: 0, goal: 10, streak: 0, due: 0 };
  const pct = Math.min(100, Math.round(100 * s.xp_today / s.goal));
  const les = LESSON === 'review' ? null : LESS[LESSON];
  const list = DATA.lessons, pos = les ? list.indexOf(les) : -1, next = pos >= 0 ? list[pos + 1] : null;
  const viewUrl = n => '/view/' + n.split('/').map(encodeURIComponent).join('/');
  $('#main').innerHTML = `<div class="end"><h2>${LESSON === 'review' ? 'Review done!' : 'Lektion geschafft! 🎉'}</h2>
    <div class="ring" style="background:conic-gradient(var(--acc) ${pct}%, var(--line) 0)"><span>${s.xp_today}/${s.goal} XP</span></div>
    <div>${s.met_today ? '✅ Daily goal reached' : `${s.goal - s.xp_today} XP to today's goal`}</div>
    <div class="stats"><div class="stat"><b>+${sessionXP}</b>XP this round</div><div class="stat"><b>${correctCount}/${answered}</b>right first time</div><div class="stat"><b>🔥 ${s.streak}</b>day streak${s.freeze_used ? ' 🧊' : ''}</div></div>
    <div class="links">
      ${next ? `<a class="go" href="${viewUrl(next.note)}">Next lesson: ${esc(next.id)} · ${esc(next.title)}</a>` : ''}
      ${s.due ? `<a class="go sec" href="/practice/review">🔁 Practise mistakes & reviews (${s.due})</a>` : ''}
      ${les ? `<a class="go sec" href="${viewUrl(les.note)}">Back to the lesson</a>` : ''}
      <a class="go sec" href="/view/00%20-%20Today.md">Today page</a>
    </div>
    <div class="goalsel">Daily goal: ${[5,10,20,30,50].map(g => `<button class="${g === s.goal ? 'on' : ''}" onclick="setGoal(${g})">${g} XP</button>`).join('')}</div></div>`;
}
async function setGoal(g) { const r = await fetch('/practice/api/goal', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ goal: g }) }).then(r => r.json()); summary = r.summary; renderEnd(); }

// ---------- start ----------
(async () => {
  DATA = await fetch('/practice/api/data').then(r => r.json());
  DATA.lessons.forEach(l => { LESS[l.id] = l; l.items.forEach(it => { ITEM[it.id] = it; ALL.push(it); }); });
  const st = await fetch('/practice/api/state').then(r => r.json());
  summary = st.summary; showXP();
  if (LESSON === 'review') {
    queue = buildReviewQueue(st.due);
    if (!queue.length) { $('#foot').style.display = 'none'; $('#main').innerHTML = '<div class="end"><h2>Nothing to review right now 🎉</h2><p>Practise a lesson and the words you miss come back here at the right time.</p><div class="links"><a class="go" href="/view/00%20-%20Today.md">Today page</a></div></div>'; return; }
  } else {
    const les = LESS[LESSON];
    $('#exit').href = '/view/' + les.note.split('/').map(encodeURIComponent).join('/');
    queue = buildLessonQueue(les);
  }
  $('#main').innerHTML = `<div class="end"><h2>${LESSON === 'review' ? '🔁 Review' : esc(LESSON + ' · ' + LESS[LESSON].title)}</h2><p>${queue.length} quick exercises · about 5 minutes</p><p class="hint">Sound on 🔊 — you'll hear every sentence.</p></div>`;
  const go = $('#go'); go.disabled = false; go.textContent = 'Start'; checked = true; idx = -1;
})();
</script></body></html>
"""


PHRASES_PAGE = r"""<!doctype html>
<html lang="de"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Phrases</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1f2328;--mut:#6b7280;--line:#e5e7eb;--pri:#1cb0f6;--hl:rgba(28,176,246,.14)}
@media (prefers-color-scheme:dark){:root{--bg:#131f24;--card:#1b2a31;--ink:#e8eef1;--mut:#9aa7ad;--line:#2b3b43;--hl:rgba(28,176,246,.22)}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--ink);font:18px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:12px 16px calc(24px + env(safe-area-inset-bottom))}
.top{display:flex;align-items:center;gap:12px}
.x{font-size:26px;color:var(--mut);text-decoration:none;padding:4px 8px}
h1{font-size:21px;margin:0;flex:1}
.ctl{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}
.ctl button{font-size:17px;padding:10px 16px;border-radius:14px;border:none;background:var(--pri);color:#fff;box-shadow:0 3px 0 #1899d6}
.ctl button.alt{background:var(--card);color:var(--ink);border:2px solid var(--line);box-shadow:none}
.ctl button.on{border-color:var(--pri)}
.row{display:flex;gap:10px;align-items:flex-start;padding:12px 14px;margin:8px 0;background:var(--card);border:2px solid var(--line);border-radius:14px}
.row.now{border-color:var(--pri);background:var(--hl)}
.row button{font-size:20px;min-width:44px;height:44px;border-radius:12px;border:none;background:var(--line);color:var(--ink)}
.de{font-weight:600}.en{color:var(--mut);font-size:16px}.who{color:var(--mut);font-size:14px}
h2{font-size:15px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin:22px 0 4px}
.done{margin:20px 0;text-align:center;color:var(--mut)}
</style></head><body><div class="wrap">
<div class="top"><a class="x" href="/view/00%20-%20Today.md" aria-label="Close">✕</a><h1 id="title">Phrases</h1></div>
<div class="ctl">
 <button id="playall">▶ Play all</button>
 <button class="alt" id="slow">🐢 Slow</button>
 <button class="alt" id="hide">🙈 Hide English</button>
</div>
<div id="list"></div>
<div class="done" id="done"></div>
</div>
<script>
const LESSON = __LESSON__;
let items = [], player = null, idx = -1, running = false, slow = false, counted = false;
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const url = src => '/raw/' + src.split('/').map(encodeURIComponent).join('/');
function mark(i) { document.querySelectorAll('.row').forEach((r, j) => r.classList.toggle('now', j === i)); if (i >= 0) document.querySelectorAll('.row')[i].scrollIntoView({block: 'center', behavior: 'smooth'}); }
function play(i, chain) {
  if (player) { player.onended = null; player.pause(); }
  idx = i; mark(i);
  player = new Audio(url(slow ? items[i].s : items[i].a));
  player.onended = () => { if (!chain) return; if (i + 1 < items.length) setTimeout(() => play(i + 1, true), 900); else finish(); };
  player.play().catch(() => {});
}
function finish() {
  running = false; $('#playall').textContent = '▶ Play all again'; mark(-1);
  if (counted) return;
  counted = true;
  fetch('/practice/api/revisit', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({lesson: LESSON})})
    .then(() => { $('#done').textContent = '✅ Counted as a revisit — Today will bring this lesson back later.'; }).catch(() => {});
}
$('#playall').onclick = () => {
  if (running) { running = false; if (player) { player.onended = null; player.pause(); } $('#playall').textContent = '▶ Play all'; return; }
  running = true; $('#playall').textContent = '⏸ Stop'; play(0, true);
};
$('#slow').onclick = () => { slow = !slow; $('#slow').classList.toggle('on', slow); };
$('#hide').onclick = () => { const h = $('#hide').classList.toggle('on'); document.querySelectorAll('.en').forEach(e => e.style.visibility = h ? 'hidden' : ''); };
fetch('/practice/api/data').then(r => r.json()).then(d => {
  const les = d.lessons.find(l => l.id === LESSON);
  document.title = $('#title').textContent = `🔊 ${les.id} · ${les.title}`;
  items = les.items.filter(x => (x.kind === 'phrase' || x.kind === 'line') && x.a);
  let html = '', last = '';
  items.forEach((it, i) => {
    const sec = it.kind === 'phrase' ? 'Phrases' : 'Story';
    if (sec !== last) { html += `<h2>${sec}</h2>`; last = sec; }
    html += `<div class="row" data-i="${i}"><button aria-label="Play">▶</button><div>${it.who ? `<div class="who">${esc(it.who)}</div>` : ''}<div class="de">${esc(it.de)}</div><div class="en">${esc(it.en)}</div></div></div>`;
  });
  $('#list').innerHTML = html;
  document.querySelectorAll('.row button').forEach(b => b.onclick = () => { running = false; $('#playall').textContent = '▶ Play all'; play(+b.closest('.row').dataset.i, false); });
});
</script></body></html>
"""
