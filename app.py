import ast
import builtins
import difflib
import json
import os
import re
import secrets
import socket
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from itertools import combinations

from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("PRUEBAS_DB") or os.path.join(BASE, "pruebas.db")
DATABASE_URL = next(
    (os.environ[k] for k in ("DATABASE_URL", "POSTGRES_URL", "STORAGE_DATABASE_URL", "STORAGE_URL") if os.environ.get(k)),
    None,
)
PG = bool(DATABASE_URL)
IS_VERCEL = bool(os.environ.get("VERCEL"))
# En Vercel la clave debe venir de la variable de entorno; en local se genera una al arrancar.
ADMIN_PASS = os.environ.get("ADMIN_PASS") or (None if IS_VERCEL else secrets.token_urlsafe(6))
COLOMBIA = timezone(timedelta(hours=-5))

if PG:
    import psycopg
    from psycopg.rows import dict_row


def load_questions():
    raw = os.environ.get("QUESTIONS_JSON")
    if raw:
        return json.loads(raw)
    for name in ("questions.json", "questions.example.json"):
        path = os.path.join(BASE, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    raise RuntimeError("No hay preguntas: define QUESTIONS_JSON o crea questions.json")


CONFIG = load_questions()

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# ---------- base de datos (Postgres si hay DATABASE_URL, SQLite si no) ----------

def connect():
    if PG:
        return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row, prepare_threshold=None)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


def run(sql, params=(), con=None):
    con = con or db()
    cur = con.execute(sql.replace("?", "%s") if PG else sql, params)
    if not PG:
        con.commit()
    return cur


def run_many(sql, rows):
    con = db()
    if PG:
        con.cursor().executemany(sql.replace("?", "%s"), rows)
    else:
        con.executemany(sql, rows)
        con.commit()


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()


_ready = False


def ensure_ready():
    """Crea las tablas y los candidatos de CANDIDATOS ("Nombre1;Nombre2") la primera vez."""
    global _ready
    if _ready:
        return
    con = connect()
    try:
        run("CREATE TABLE IF NOT EXISTS candidates(token TEXT PRIMARY KEY, name TEXT, variant INTEGER, "
            "created DOUBLE PRECISION, started DOUBLE PRECISION)", con=con)
        # pausa del reloj (también migra bases creadas antes de esta función)
        for col in ("paused_at", "paused_total", "extra"):
            if PG:
                run(f"ALTER TABLE candidates ADD COLUMN IF NOT EXISTS {col} DOUBLE PRECISION", con=con)
            elif col not in [r["name"] for r in run("PRAGMA table_info(candidates)", con=con).fetchall()]:
                run(f"ALTER TABLE candidates ADD COLUMN {col} DOUBLE PRECISION", con=con)
        pk = "BIGSERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
        run(f"CREATE TABLE IF NOT EXISTS events(id {pk}, token TEXT, q TEXT, type TEXT, data TEXT, "
            "t DOUBLE PRECISION)", con=con)
        for name in (n.strip() for n in os.environ.get("CANDIDATOS", "").split(";")):
            if name and not run("SELECT 1 FROM candidates WHERE name=?", (name,), con).fetchone():
                n = run("SELECT COUNT(*) AS n FROM candidates", con=con).fetchone()["n"]
                run("INSERT INTO candidates(token,name,variant,created) VALUES(?,?,?,?)",
                    (secrets.token_urlsafe(8), name, n, time.time()), con)
    finally:
        con.close()
    _ready = True


@app.before_request
def _init():
    try:
        if IS_VERCEL and not PG:
            names = sorted(k for k in os.environ if any(w in k for w in ("URL", "POSTGRES", "STORAGE", "DATABASE")))
            raise RuntimeError("No llegó la cadena de la base de datos. Variables vistas (solo nombres): " + ", ".join(names))
        ensure_ready()
    except Exception as e:
        return Response(f"Error de inicio: {type(e).__name__}: {str(e)[:400]}", 500, mimetype="text/plain")


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not ADMIN_PASS:
            return Response("Configura la variable de entorno ADMIN_PASS", 503)
        auth = request.authorization
        if not auth or not secrets.compare_digest(auth.password or "", ADMIN_PASS):
            return Response("Acceso restringido", 401, {"WWW-Authenticate": 'Basic realm="admin"'})
        return fn(*a, **kw)

    return wrapper


def get_candidate(token):
    c = run("SELECT * FROM candidates WHERE token=?", (token,)).fetchone()
    if not c:
        abort(404)
    return c


def time_state(c, now=None):
    """Estado del reloj de un candidato: el tiempo en pausa no cuenta."""
    now = now or time.time()
    total = CONFIG["minutes"] * 60 + (c["extra"] or 0)
    if not c["started"]:
        return dict(state="sin_iniciar", remaining=total, raw=total)
    ref = c["paused_at"] or now
    raw = total - (ref - c["started"] - (c["paused_total"] or 0))
    state = "pausada" if c["paused_at"] else ("terminada" if raw <= 0 else "corriendo")
    return dict(state=state, remaining=max(0, raw), raw=raw)


def log_event(token, type_, data=None):
    run("INSERT INTO events(token,q,type,data,t) VALUES(?,?,?,?,?)",
        (token, None, type_, json.dumps(data or {}), time.time()))


# ---------- candidato ----------

@app.route("/")
def index():
    cands = run("SELECT token, name FROM candidates ORDER BY created").fetchall()
    return render_template("index.html", cands=cands)


@app.route("/t/<token>")
def test_page(token):
    get_candidate(token)
    return render_template("test.html")


@app.route("/api/test/<token>")
def api_test(token):
    c = get_candidate(token)
    if not c["started"]:
        return jsonify(name=c["name"], started=False, minutes=CONFIG["minutes"])
    questions = []
    for q in CONFIG["questions"]:
        v = q["variants"][c["variant"] % len(q["variants"])]
        questions.append({"id": q["id"], "title": q["title"], **v})
    saved = {}
    for r in run("SELECT q, data FROM events WHERE token=? AND type IN ('snap','submit') ORDER BY id", (token,)):
        saved[r["q"]] = json.loads(r["data"]).get("code", "")
    ts_ = time_state(c)
    return jsonify(name=c["name"], started=True, questions=questions, saved=saved,
                   remaining=ts_["remaining"], paused=ts_["state"] == "pausada")


@app.route("/api/state/<token>")
def api_state(token):
    ts_ = time_state(get_candidate(token))
    return jsonify(remaining=ts_["remaining"], paused=ts_["state"] == "pausada")


@app.route("/api/start/<token>", methods=["POST"])
def api_start(token):
    get_candidate(token)
    run("UPDATE candidates SET started=? WHERE token=? AND started IS NULL", (time.time(), token))
    return jsonify(ok=True)


@app.route("/api/events/<token>", methods=["POST"])
def api_events(token):
    c = get_candidate(token)
    if c["started"] and time_state(c)["raw"] < -30:
        return jsonify(ok=False), 403
    rows = [
        (token, e.get("q"), e.get("type"), json.dumps(e.get("data") or {}), e.get("t") or time.time())
        for e in request.get_json(force=True)
    ]
    run_many("INSERT INTO events(token,q,type,data,t) VALUES(?,?,?,?,?)", rows)
    return jsonify(ok=True)


# ---------- análisis ----------

class _Norm(ast.NodeTransformer):
    """Ignora nombres de variables/funciones para comparar la estructura del código."""

    def visit_Name(self, n):
        if n.id in dir(builtins):
            return n
        return ast.copy_location(ast.Name(id="v", ctx=n.ctx), n)

    def visit_arg(self, n):
        n.arg, n.annotation = "v", None
        return n

    def visit_FunctionDef(self, n):
        self.generic_visit(n)
        n.name = "f"
        return n


def normalize(code):
    try:
        return ast.dump(_Norm().visit(ast.parse(code)))
    except SyntaxError:
        return re.sub(r"\s+", "", code)


def analyze():
    cands = run("SELECT * FROM candidates ORDER BY created").fetchall()
    info, finals = {}, {}
    for c in cands:
        evs = run("SELECT * FROM events WHERE token=? ORDER BY id", (c["token"],)).fetchall()
        s = dict(pastes=0, pasted_chars=0, blurs=0,copies=0, submits=0, passed=0, total=0, evs=len(evs))
        last = {}
        for e in evs:
            d = json.loads(e["data"])
            if e["type"] == "paste":
                s["pastes"] += 1
                s["pasted_chars"] += d.get("len", 0)
            elif e["type"] == "blur":
                s["blurs"] += 1
            elif e["type"] in ("copy", "cut"):
                s["copies"] += 1
            elif e["type"] == "submit":
                s["submits"] += 1
                last[e["q"]] = (d.get("passed", 0), d.get("total", 0))
            if e["type"] in ("snap", "submit"):
                finals.setdefault(e["q"], {})[c["token"]] = d.get("code", "")
        s["passed"] = sum(p for p, _ in last.values())
        s["total"] = sum(t for _, t in last.values())
        info[c["token"]] = dict(c=c, s=s, flags=[], score=0, time=time_state(c))

    for t, i in info.items():
        s = i["s"]
        big = s["pasted_chars"]
        # sospecha = pegados (10 por pegado + 1 por cada 50 caracteres) + salidas de pantalla (3 por salida, tope 30)
        if s["pastes"]:
            i["score"] += 10 * s["pastes"] + big // 50
            i["flags"].append(f"{s['pastes']} pegado(s), {big} caracteres pegados")
        if s["blurs"]:
            i["score"] += min(30, 3 * s["blurs"])
            i["flags"].append(f"{s['blurs']} salida(s) de pantalla")
        i["score"] = min(100, i["score"])
    return info, []


@app.template_filter("ts")
def ts(v):
    return datetime.fromtimestamp(v, COLOMBIA).strftime("%H:%M:%S") if v else "-"


# ---------- admin ----------

@app.route("/admin")
@admin_required
def admin():
    info, pairs = analyze()
    return render_template("admin.html", info=info, pairs=pairs, base=request.url_root.rstrip("/"))


PLACEHOLDER = "# Escribe tu código aquí"


@app.route("/admin/resultados")
@admin_required
def admin_resultados():
    """Resultado final: todas las variables medidas, por candidato y por ejercicio."""
    info, _ = analyze()
    orden = [(q["id"], q["title"]) for q in CONFIG["questions"]]
    detalle = {}
    for token in info:
        rows = {qid: dict(codigo=False, entregada=False, passed=0, total=0, pegados=0, chars=0, salidas=0, ejecuciones=0)
                for qid, _ in orden}
        for e in run("SELECT q, type, data FROM events WHERE token=? ORDER BY id", (token,)).fetchall():
            r = rows.get(e["q"])
            if r is None:
                continue
            d = json.loads(e["data"])
            if e["type"] in ("snap", "submit"):
                r["codigo"] = bool((d.get("code") or "").replace(PLACEHOLDER, "").strip())
            if e["type"] == "submit":
                r["entregada"], r["passed"], r["total"] = True, d.get("passed", 0), d.get("total", 0)
            elif e["type"] == "paste":
                r["pegados"] += 1
                r["chars"] += d.get("len", 0)
            elif e["type"] == "blur":
                r["salidas"] += 1
            elif e["type"] == "run":
                r["ejecuciones"] += 1
        detalle[token] = rows

    # calificación: pruebas aprobadas sobre el total de pruebas del examen, en escala 0 a 5.
    # El total por ejercicio es el mayor visto en las entregas (un ejercicio sin entregar cuenta como 0).
    ref = {qid: max([detalle[t][qid]["total"] for t in detalle] or [0]) for qid, _ in orden}
    total_ref = sum(ref.values())
    for token, i in info.items():
        ok = sum(detalle[token][qid]["passed"] for qid, _ in orden)
        entregados = sum(1 for qid, _ in orden if detalle[token][qid]["entregada"])
        i["nota"] = round(5 * ok / total_ref, 1) if total_ref else 0.0
        i["ok"], i["entregados"] = ok, entregados
        i["nivel"] = "alta" if i["score"] >= 60 else "media" if i["score"] >= 30 else "baja"
        i["informe"] = (
            f"Aprobó {ok} de {total_ref} pruebas y entregó {entregados} de {len(orden)} ejercicios, "
            f"con nota {i['nota']:.1f}/5. Sospecha {i['nivel']} ({i['score']}/100): "
            + (" · ".join(i["flags"]) if i["flags"] else "sin pegados ni salidas de pantalla")
            + "."
        )
    ranking = sorted(info.items(), key=lambda kv: (-kv[1]["nota"], kv[1]["score"]))
    notas = [i["nota"] for i in info.values()]
    resumen = dict(
        candidatos=len(info),
        promedio=round(sum(notas) / len(notas), 1) if notas else 0.0,
        mejor=ranking[0][1]["c"]["name"] if ranking else "-",
        alta=sum(1 for i in info.values() if i["nivel"] == "alta"),
        media=sum(1 for i in info.values() if i["nivel"] == "media"),
    )
    return render_template("resultados.html", info=info, detalle=detalle, orden=orden,
                           ranking=ranking, resumen=resumen, total_ref=total_ref)


@app.route("/admin/new", methods=["POST"])
@admin_required
def admin_new():
    n = run("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    run("INSERT INTO candidates(token,name,variant,created) VALUES(?,?,?,?)",
        (secrets.token_urlsafe(8), request.form["name"].strip(), n, time.time()))
    return redirect("/admin")


def _pause(token):
    now = time.time()
    n = run("UPDATE candidates SET paused_at=? WHERE token=? AND started IS NOT NULL AND paused_at IS NULL",
            (now, token)).rowcount
    if n:
        log_event(token, "admin_pause")


def _resume(token):
    now = time.time()
    n = run("UPDATE candidates SET paused_total = COALESCE(paused_total, 0) + (? - paused_at), paused_at = NULL "
            "WHERE token=? AND paused_at IS NOT NULL", (now, token)).rowcount
    if n:
        log_event(token, "admin_resume")


@app.route("/admin/pause/<token>", methods=["POST"])
@admin_required
def admin_pause(token):
    _pause(token)
    return redirect("/admin")


@app.route("/admin/resume/<token>", methods=["POST"])
@admin_required
def admin_resume(token):
    _resume(token)
    return redirect("/admin")


@app.route("/admin/extra/<token>", methods=["POST"])
@admin_required
def admin_extra(token):
    """Suma (o resta, si es negativo) minutos al tiempo de un candidato."""
    try:
        mins = max(-60, min(60, int(request.form["min"])))
    except (KeyError, ValueError):
        abort(400)
    n = run("UPDATE candidates SET extra = COALESCE(extra, 0) + ? WHERE token=?", (mins * 60, token)).rowcount
    if n:
        log_event(token, "admin_extra", {"min": mins})
    return redirect("/admin")


@app.route("/admin/reopen/<token>", methods=["POST"])
@admin_required
def admin_reopen(token):
    """Reabre una prueba terminada dejándole exactamente `min` minutos."""
    mins = max(1, min(60, int(request.form.get("min", 5))))
    c = get_candidate(token)
    if c["started"]:
        extra = (c["extra"] or 0) + mins * 60 - time_state(c)["raw"]
        run("UPDATE candidates SET extra=? WHERE token=?", (extra, token))
        log_event(token, "admin_reopen", {"min": mins})
    return redirect("/admin")


@app.route("/admin/pause_all", methods=["POST"])
@admin_required
def admin_pause_all():
    for c in run("SELECT token FROM candidates WHERE started IS NOT NULL AND paused_at IS NULL").fetchall():
        _pause(c["token"])
    return redirect("/admin")


@app.route("/admin/resume_all", methods=["POST"])
@admin_required
def admin_resume_all():
    for c in run("SELECT token FROM candidates WHERE paused_at IS NOT NULL").fetchall():
        _resume(c["token"])
    return redirect("/admin")


@app.route("/admin/delete/<token>", methods=["POST"])
@admin_required
def admin_delete(token):
    run("DELETE FROM events WHERE token=?", (token,))
    run("DELETE FROM candidates WHERE token=?", (token,))
    return redirect("/admin")


@app.route("/admin/c/<token>")
@admin_required
def admin_detail(token):
    c = get_candidate(token)
    evs = run("SELECT * FROM events WHERE token=? ORDER BY id", (token,)).fetchall()
    snaps, timeline = {}, []
    for e in evs:
        d = json.loads(e["data"])
        if e["type"] in ("snap", "submit"):
            snaps.setdefault(e["q"], []).append({"t": e["t"], "code": d.get("code", ""), "type": e["type"]})
        if e["type"] not in ("snap", "key"):
            timeline.append({"t": e["t"], "q": e["q"], "type": e["type"], "data": {k: v for k, v in d.items() if k != "code"}})
    return render_template("detail.html", c=c, snaps=snaps, timeline=timeline)


if __name__ == "__main__":
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    print(f"\n  Admin:  http://{ip}:5000/admin   (usuario: cualquiera, clave: {ADMIN_PASS})\n")
    app.run(host="0.0.0.0", port=5000, threaded=True)
