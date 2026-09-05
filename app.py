from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import unicodedata
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint

from scripts.import_catalog import extract

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave-no-render")
app.config["MAX_CONTENT_LENGTH"] = 60 * 1024 * 1024
database_url = os.environ.get("DATABASE_URL", "sqlite:///catalogs.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
elif database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)


class Catalog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    manufacturer = db.Column(db.String(120), nullable=False)
    edition = db.Column(db.String(120), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    item_count = db.Column(db.Integer, nullable=False, default=0)
    active = db.Column(db.Boolean, nullable=False, default=True)
    data = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (UniqueConstraint("manufacturer", "edition", name="uq_catalog_edition"),)


DATA_DIR = Path(__file__).parent / "data"
ITEMS = []
CATALOGS = []


def refresh_cache():
    global ITEMS, CATALOGS
    rows = Catalog.query.filter_by(active=True).order_by(Catalog.id).all()
    ITEMS = [item for catalog in rows for item in json.loads(catalog.data)]
    CATALOGS = [(catalog.manufacturer, catalog.edition) for catalog in rows]


def initialize_database():
    db.create_all()
    if Catalog.query.count() == 0:
        for data_file in sorted(DATA_DIR.glob("*.json")):
            items = json.loads(data_file.read_text(encoding="utf-8"))
            if not items:
                continue
            first = items[0]
            db.session.add(Catalog(
                manufacturer=first.get("manufacturer", data_file.stem),
                edition=first.get("edition", "Sem edição"),
                filename=data_file.name,
                item_count=len(items),
                data=json.dumps(items, ensure_ascii=False),
            ))
        db.session.commit()
    refresh_cache()


with app.app_context():
    initialize_database()

STOPWORDS = {"A", "AS", "O", "OS", "DE", "DA", "DO", "DAS", "DOS", "PARA", "QUAL", "QUAIS", "UMA", "UM", "NO", "NA", "NOS", "NAS", "SERVE", "APLICA", "APLICACAO", "PRECISO", "PECA", "AUTHOMIX", "NAKATA", "COFAP", "TRW", "AXIOS", "PDX"}
ALIASES = {"DIANTEIRO": "DIANTEIRA", "TRASEIRO": "TRASEIRA", "ESQUERDO": "ESQUERDA", "DIREITO": "DIREITA"}
YEAR_RE = re.compile(r"(?<!\d)(\d{2})/(\d{2}|\.\.\.)(?!\d)")
EXACT_YEAR_RE = re.compile(r"(?<![\d./])(\d{2})(?![\d./])")
GENERIC_TERMS = {"BIELETA", "BOMBA", "AGUA", "CILINDRO", "CRUZETA", "CUBO", "FILTRO", "KIT", "BUCHA", "SUPORTE", "PINO", "PONTA", "POLIA", "GUIA", "TENSOR", "REPARO", "ROLAMENTO", "SAPATA", "SEMIEXO", "TERMINAL", "TRIZETA", "ADITIVO", "DIANTEIRA", "TRASEIRA", "DIREITA", "ESQUERDA"}


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9./-]+", " ", value).strip()


def logged_in(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def token_score(query: str, item: dict) -> tuple[int, list[str]]:
    qnorm = normalize(query)
    haystack = item.get("search", normalize(item.get("text", "")))
    raw_tokens = [ALIASES.get(t, t) for t in qnorm.split() if t not in STOPWORDS and len(t) > 1]
    years = [int(t) for t in raw_tokens if t.isdigit() and len(t) == 4 and 1950 <= int(t) <= 2035]
    tokens = [t for t in raw_tokens if not (t.isdigit() and len(t) == 4)]
    if not tokens:
        return 0, []
    score, matches = 0, []
    code = normalize(item.get("code", ""))
    if qnorm == code:
        return 1000, ["código exato"]
    if code and code in qnorm:
        score += 300
        matches.append("código")
    missing = []
    for token in tokens:
        if token in haystack:
            score += 18 if any(ch.isdigit() for ch in token) else 10
            matches.append(token)
        else:
            score -= 6
            missing.append(token)
    if missing:
        return 0, []
    if years:
        spans = []
        anchors = [t for t in tokens if t not in GENERIC_TERMS and not any(ch.isdigit() for ch in t)]
        year_text = haystack
        if anchors:
            pieces = []
            for anchor in anchors:
                for match in re.finditer(rf"\b{re.escape(anchor)}\b", haystack):
                    segment = haystack[match.start():match.start() + 70]
                    pieces.append(re.split(r"[,;]", segment, maxsplit=1)[0])
            if pieces:
                year_text = " ".join(pieces)
        for start, end in YEAR_RE.findall(year_text):
            start_year = 1900 + int(start) if int(start) >= 40 else 2000 + int(start)
            end_year = 9999 if end == "..." else (1900 + int(end) if int(end) >= 40 else 2000 + int(end))
            spans.append((start_year, end_year))
        if not spans and anchors:
            for exact in EXACT_YEAR_RE.findall(year_text):
                exact_year = 1900 + int(exact) if int(exact) >= 40 else 2000 + int(exact)
                spans.append((exact_year, exact_year))
        if spans and all(any(a <= year <= b for a, b in spans) for year in years):
            score += 28
            matches.extend(str(year) for year in years)
        elif spans:
            return 0, []
        else:
            score -= 35
    if tokens and all(token in haystack for token in tokens):
        score += 45
    return score, matches


def search_catalog(query: str, limit: int = 12):
    ranked = []
    for item in ITEMS:
        score, matches = token_score(query, item)
        if score > 0:
            ranked.append((score, item, matches))
    ranked.sort(key=lambda row: (-row[0], row[1]["code"]))
    if ranked and ranked[0][0] >= 1000:
        ranked = [ranked[0]]
    return [{**item, "score": score, "matches": matches[:6]} for score, item, matches in ranked[:limit]]


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        expected_user = os.environ.get("BALCAO_USER", "vendedor")
        expected_password = os.environ.get("BALCAO_PASSWORD", "authomix")
        admin_user = os.environ.get("ADMIN_USER", "admin")
        admin_password = os.environ.get("ADMIN_PASSWORD", "admin-authomix")
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if secrets.compare_digest(username, admin_user) and secrets.compare_digest(password, admin_password):
            session["logged_in"] = True
            session["role"] = "admin"
            return redirect(url_for("admin_catalogs"))
        if secrets.compare_digest(username, expected_user) and secrets.compare_digest(password, expected_password):
            session["logged_in"] = True
            session["role"] = "seller"
            return redirect(url_for("index"))
        error = "Usuário ou senha incorretos."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@logged_in
def index():
    return render_template("index.html", total=len(ITEMS), catalog_total=len(CATALOGS), is_admin=session.get("role") == "admin")


@app.route("/admin/catalogs", methods=["GET", "POST"])
@admin_required
def admin_catalogs():
    if request.method == "POST":
        uploaded = request.files.get("catalog")
        manufacturer = request.form.get("manufacturer", "").strip()
        edition = request.form.get("edition", "").strip()
        if not uploaded or not uploaded.filename.lower().endswith(".pdf") or not manufacturer or not edition:
            flash("Informe fabricante, edição e um arquivo PDF válido.", "error")
            return redirect(url_for("admin_catalogs"))
        if Catalog.query.filter_by(manufacturer=manufacturer, edition=edition).first():
            flash("Já existe um catálogo desse fabricante com essa edição.", "error")
            return redirect(url_for("admin_catalogs"))
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp:
                uploaded.save(temp)
                temp_path = Path(temp.name)
            items = extract(temp_path, manufacturer, edition)
            if not items:
                flash("Nenhum produto pôde ser extraído desse PDF.", "error")
                return redirect(url_for("admin_catalogs"))
            db.session.add(Catalog(manufacturer=manufacturer, edition=edition, filename=uploaded.filename, item_count=len(items), data=json.dumps(items, ensure_ascii=False)))
            db.session.commit()
            refresh_cache()
            flash(f"Catálogo incluído com {len(items)} produtos.", "success")
        except Exception:
            db.session.rollback()
            app.logger.exception("Falha ao importar catálogo")
            flash("Não foi possível processar o catálogo. Confira o PDF e tente novamente.", "error")
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)
        return redirect(url_for("admin_catalogs"))
    return render_template("admin_catalogs.html", catalogs=Catalog.query.order_by(Catalog.created_at.desc()).all())


@app.post("/admin/catalogs/<int:catalog_id>/toggle")
@admin_required
def toggle_catalog(catalog_id):
    catalog = db.get_or_404(Catalog, catalog_id)
    catalog.active = not catalog.active
    db.session.commit()
    refresh_cache()
    flash("Status do catálogo atualizado.", "success")
    return redirect(url_for("admin_catalogs"))


@app.post("/api/search")
@logged_in
def api_search():
    query = (request.get_json(silent=True) or {}).get("query", "").strip()
    if len(query) < 2:
        return jsonify({"error": "Digite pelo menos dois caracteres."}), 400
    return jsonify({"query": query, "results": search_catalog(query), "catalogs": len(CATALOGS)})


@app.get("/health")
def health():
    return jsonify({"status": "ok", "items": len(ITEMS)})


if __name__ == "__main__":
    app.run(debug=True)
