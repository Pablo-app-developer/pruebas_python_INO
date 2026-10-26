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


# ---------- candidato ----------

@app.route("/")
def index():
    return Response("Plataforma de pruebas. Usa el enlace personal que te enviaron.", mimetype="text/plain")


@app.route("/t/<token>")
def test_page(token):
    get_candidate(token)
    return render_template("test.html")


@app.route("/api/test/<token>")
def api_test(token):
    c = get_candidate(token)
    now = time.time()
    if not c["started"]:
        return jsonify(name=c["name"], started=False, minutes=CONFIG["minutes"])
    started = c["started"]
    questions = []
    for q in CONFIG["questions"]:
        v = q["variants"][c["variant"] % len(q["variants"])]
        questions.append({"id": q["id"], "title": q["title"], **v})
    saved = {}
    for r in run("SELECT q, data FROM events WHERE token=? AND type IN ('snap','submit') ORDER BY id", (token,)):
        saved[r["q"]] = json.loads(r["data"]).get("code", "")
    remaining = max(0, CONFIG["minutes"] * 60 - (now - started))
    return jsonify(name=c["name"], started=True, questions=questions, saved=saved, remaining=remaining)


@app.route("/api/start/<token>", methods=["POST"])
def api_start(token):
    get_candidate(token)
    run("UPDATE candidates SET started=? WHERE token=? AND started IS NULL", (time.time(), token))
    return jsonify(ok=True)


@app.route("/api/events/<token>", methods=["POST"])
def api_events(token):
    c = get_candidate(token)
    if c["started"] and time.time() > c["started"] + CONFIG["minutes"] * 60 + 30:
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
        s = dict(pastes=0, pasted_chars=0, blurs=0, copies=0, submits=0, passed=0, total=0, evs=len(evs))
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
        info[c["token"]] = dict(c=c, s=s, flags=[], score=0)

    for t, i in info.items():
        s = i["s"]
        big = s["pasted_chars"]
        if s["pastes"]:
            i["score"] += min(40, 10 * s["pastes"] + big // 50)
            i["flags"].append(f"{s['pastes']} pegado(s), {big} caracteres pegados")
        if s["blurs"] >= 3:
            i["score"] += min(25, 5 * s["blurs"])
            i["flags"].append(f"salió de la pestaña/ventana {s['blurs']} veces")

    pairs = []
    for q, codes in finals.items():
        for (a, ca), (b, cb) in combinations(codes.items(), 2):
            # los ejercicios cortos se parecen de forma natural: solo se comparan programas largos
            if len(ca.strip()) < 100 or len(cb.strip()) < 100:
                continue
            r = difflib.SequenceMatcher(None, normalize(ca), normalize(cb)).ratio()
            if r >= 0.9:
                pairs.append((q, a, b, round(r, 2)))
                for t, o in ((a, b), (b, a)):
                    info[t]["score"] += 40
                    info[t]["flags"].append(f"{q}: {int(r * 100)}% similar a {info[o]['c']['name']}")
    return info, pairs


@app.template_filter("ts")
def ts(v):
    return datetime.fromtimestamp(v, COLOMBIA).strftime("%H:%M:%S") if v else "-"


# ---------- admin ----------

@app.route("/admin")
@admin_required
def admin():
    info, pairs = analyze()
    return render_template("admin.html", info=info, pairs=pairs, base=request.url_root.rstrip("/"))


@app.route("/admin/new", methods=["POST"])
@admin_required
def admin_new():
    n = run("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    run("INSERT INTO candidates(token,name,variant,created) VALUES(?,?,?,?)",
        (secrets.token_urlsafe(8), request.form["name"].strip(), n, time.time()))
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
