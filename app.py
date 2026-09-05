from __future__ import annotations

import json
import os
import re
import secrets
import unicodedata
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave-no-render")
DATA_DIR = Path(__file__).parent / "data"
ITEMS = []
for data_file in sorted(DATA_DIR.glob("*.json")):
    ITEMS.extend(json.loads(data_file.read_text(encoding="utf-8")))
CATALOGS = sorted({(item.get("manufacturer", "Catálogo"), item.get("edition", "")) for item in ITEMS})

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
                pieces.extend(haystack[m.start():m.start() + 70] for m in re.finditer(rf"\b{re.escape(anchor)}\b", haystack))
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
        if secrets.compare_digest(request.form.get("username", ""), expected_user) and secrets.compare_digest(request.form.get("password", ""), expected_password):
            session["logged_in"] = True
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
    return render_template("index.html", total=len(ITEMS), catalog_total=len(CATALOGS))


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
