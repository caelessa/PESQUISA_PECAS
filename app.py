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
from typing import Optional

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from pydantic import BaseModel
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
    ITEMS = [
        {**item, "_catalog_id": catalog.id}
        for catalog in rows
        for item in json.loads(catalog.data)
    ]
    CATALOGS = [(catalog.id, catalog.manufacturer, catalog.edition) for catalog in rows]


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

STOPWORDS = {"A", "AS", "O", "OS", "DE", "DA", "DO", "DAS", "DOS", "EM", "PARA", "QUAL", "QUAIS", "UMA", "UM", "NO", "NA", "NOS", "NAS", "ONDE", "AONDE", "SERVE", "SERVEM", "APLICA", "APLICAM", "APLICACAO", "APLICACOES", "USADO", "USADA", "UTILIZADO", "UTILIZADA", "CARRO", "CARROS", "VEICULO", "VEICULOS", "PRECISO", "PECA", "AUTHOMIX", "NAKATA", "COFAP", "TRW", "AXIOS", "PDX"}
ALIASES = {"DIANTEIRO": "DIANTEIRA", "TRASEIRO": "TRASEIRA", "ESQUERDO": "ESQUERDA", "DIREITO": "DIREITA"}
SEARCH_SYNONYM_GROUPS = (
    ("CABO DE VELA", "CABOS DE VELA", "CABO DE IGNICAO", "CABOS DE IGNICAO", "JOGO DE CABOS", "JOGO DE CABO"),
    ("PALHETA", "LIMPADOR DE PARA-BRISA", "LIMPADOR DO PARA-BRISA", "PALHETA DO LIMPADOR"),
    ("FILTRO DE CABINE", "FILTRO DO AR-CONDICIONADO", "FILTRO DE AR-CONDICIONADO", "FILTRO ANTIPOLEN"),
    ("JUNTA HOMOCINETICA", "HOMOCINETICA"),
    ("SAPATA DE FREIO", "PATIM DE FREIO"),
    ("DISCO DE FREIO", "DISCO FREIO"),
    ("PASTILHA DE FREIO", "PASTILHA FREIO"),
)
YEAR_RE = re.compile(r"(?<!\d)(\d{2})/(\d{2}|\.\.\.)(?!\d)")
EXACT_YEAR_RE = re.compile(r"(?<![\d./])(\d{2})(?![\d./])")
FULL_YEAR_RANGE_RE = re.compile(
    r"(?<!\d)(?:\d{1,2}/)?(19\d{2}|20\d{2})\s+A\s+(?:\d{1,2}/)?(19\d{2}|20\d{2})(?!\d)"
)
SINCE_YEAR_RE = re.compile(r"\bDESDE\s+(?:\d{1,2}/)?(19\d{2}|20\d{2})(?!\d)")
UNTIL_YEAR_RE = re.compile(r"\bATE\s+(?:\d{1,2}/)?(19\d{2}|20\d{2})(?!\d)")
GENERIC_TERMS = {"BIELETA", "BOMBA", "AGUA", "CILINDRO", "CRUZETA", "CUBO", "FILTRO", "KIT", "BUCHA", "SUPORTE", "PINO", "PONTA", "POLIA", "GUIA", "TENSOR", "REPARO", "ROLAMENTO", "SAPATA", "SEMIEXO", "TERMINAL", "TRIZETA", "ADITIVO", "DIANTEIRA", "TRASEIRA", "DIREITA", "ESQUERDA"}


class CatalogQuestion(BaseModel):
    part_code: str
    part_type: str
    manufacturer: str
    vehicle_make: str
    vehicle_model: str
    year: Optional[int]
    engine: str
    position: str
    side: str
    normalized_query: str


class CatalogAnswer(BaseModel):
    answer: str
    supported: bool
    source_codes: list[str]


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9./-]+", " ", value).strip()


def normalize_search_language(value: str, active_canonicals: set[str] | None = None) -> str:
    """Normalize counter-sales synonyms, optionally only for requested groups.

    Restricting the catalog-side work to the synonym present in the query keeps
    searches fast even when several large catalogs are active.
    """
    text = normalize(value)
    for group in SEARCH_SYNONYM_GROUPS:
        canonical = normalize(group[0])
        if active_canonicals is not None and canonical not in active_canonicals:
            continue
        for synonym in sorted((normalize(term) for term in group), key=len, reverse=True):
            text = re.sub(
                rf"(?<![A-Z0-9]){re.escape(synonym)}(?![A-Z0-9])",
                canonical,
                text,
            )
    return text


def validate_prepared_catalog(payload, manufacturer: str, edition: str) -> list[dict]:
    """Validate and normalize a prepared JSON catalog before saving it."""
    if not isinstance(payload, list) or not payload:
        raise ValueError("O JSON deve conter uma lista de produtos.")
    if len(payload) > 50000:
        raise ValueError("O catálogo ultrapassa o limite de 50.000 produtos.")

    products = []
    seen_codes = set()
    for position, raw in enumerate(payload, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"Produto {position} inválido.")
        code = str(raw.get("code", "")).strip().upper()
        text = str(raw.get("text", "")).strip()
        category = str(raw.get("category", "Autopeças")).strip() or "Autopeças"
        if not code or len(code) > 80 or not re.fullmatch(r"[A-Z0-9][A-Z0-9./_-]*", code):
            raise ValueError(f"Código inválido no produto {position}.")
        if code in seen_codes:
            raise ValueError(f"Código duplicado no JSON: {code}.")
        if not text or len(text) > 100000:
            raise ValueError(f"Descrição inválida para o código {code}.")
        raw_pages = raw.get("pages", raw.get("page", []))
        if isinstance(raw_pages, int):
            raw_pages = [raw_pages]
        if not isinstance(raw_pages, list):
            raw_pages = []
        pages = sorted({int(page) for page in raw_pages if str(page).isdigit() and int(page) > 0})
        product = {
            "code": code,
            "manufacturer": manufacturer,
            "edition": edition,
            "category": category[:160],
            "pages": pages,
            "text": text,
        }
        product["search"] = normalize(" ".join([
            code, manufacturer, edition, category, text, str(raw.get("search", ""))
        ]))
        products.append(product)
        seen_codes.add(code)
    return products


def contains_token(text: str, token: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(token)}(?![A-Z0-9])", text) is not None


def year_spans(text: str) -> list[tuple[int, int]]:
    """Read catalog year formats such as 09/14, 2009 a 2014 and 2009 a 07/2014."""
    spans = [(int(start), int(end)) for start, end in FULL_YEAR_RANGE_RE.findall(text)]
    spans.extend((int(start), 9999) for start in SINCE_YEAR_RE.findall(text))
    spans.extend((1950, int(end)) for end in UNTIL_YEAR_RE.findall(text))
    for start, end in YEAR_RE.findall(text):
        start_year = 1900 + int(start) if int(start) >= 40 else 2000 + int(start)
        end_year = 9999 if end == "..." else (1900 + int(end) if int(end) >= 40 else 2000 + int(end))
        spans.append((start_year, end_year))
    return spans


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
    qnorm = normalize_search_language(query)
    active_synonyms = {
        normalize(group[0])
        for group in SEARCH_SYNONYM_GROUPS
        if contains_token(qnorm, normalize(group[0]).split()[0])
        and normalize(group[0]) in qnorm
    }
    haystack = normalize_search_language(
        item.get("search", normalize(item.get("text", ""))),
        active_synonyms,
    )
    category = normalize(item.get("category", ""))
    requested_part = detect_part_intent(qnorm)
    if requested_part:
        item_kind = normalize(f"{item.get('category', '')} {item.get('text', '')[:180]}")
        if not any(contains_token(item_kind, term) for term in PART_INTENTS[requested_part]):
            return 0, []
    category_intents = {
        "FILTRO DO AR": "FILTRO DO AR",
        "FILTRO DE AR": "FILTRO DO AR",
        "FILTRO DO OLEO": "FILTRO DO OLEO",
        "FILTRO DE OLEO": "FILTRO DO OLEO",
        "FILTRO DO COMBUSTIVEL": "FILTRO DO COMBUSTIVEL",
        "FILTRO DE COMBUSTIVEL": "FILTRO DO COMBUSTIVEL",
        "FILTRO DE CABINE": "FILTRO DE CABINE",
        "FILTRO DE CAMBIO": "FILTRO DE CAMBIO",
        "FILTRO DO CAMBIO": "FILTRO DE CAMBIO",
    }
    requested_category = next((target for phrase, target in category_intents.items() if phrase in qnorm), None)
    if requested_category and requested_category not in category:
        return 0, []
    raw_tokens = [ALIASES.get(t, t) for t in qnorm.split() if t not in STOPWORDS and len(t) > 1]
    years = [int(t) for t in raw_tokens if t.isdigit() and len(t) == 4 and 1950 <= int(t) <= 2035]
    tokens = [t for t in raw_tokens if not (t.isdigit() and len(t) == 4)]
    if not tokens:
        return 0, []
    score, matches = 0, []
    code = normalize(item.get("code", ""))
    if qnorm == code:
        return 1000, ["código exato"]
    if code in raw_tokens:
        return 1000, ["código exato"]
    if code and code in qnorm:
        score += 300
        matches.append("código")
    missing = []
    for token in tokens:
        if contains_token(haystack, token):
            score += 18 if any(ch.isdigit() for ch in token) else 10
            matches.append(token)
        else:
            score -= 6
            missing.append(token)
    if missing:
        return 0, []
    if years:
        anchors = [t for t in tokens if t not in GENERIC_TERMS and not t.isdigit()]
        anchors = anchors[-1:]
        year_text = haystack
        if anchors:
            pieces = []
            for anchor in anchors:
                for match in re.finditer(rf"\b{re.escape(anchor)}\b", haystack):
                    if "|" in haystack:
                        left = haystack.rfind("|", 0, match.start()) + 1
                        right = haystack.find("|", match.end())
                        pieces.append(haystack[left:right if right >= 0 else len(haystack)])
                    else:
                        # NGK descriptions can be long before the year column (engine, power and fuel).
                        segment = haystack[match.start():match.start() + 260]
                        pieces.append(re.split(r"[,;]", segment, maxsplit=1)[0])
            if pieces:
                year_text = " ".join(pieces)
        spans = year_spans(year_text)
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
            return 0, []
    if tokens and all(contains_token(haystack, token) for token in tokens):
        score += 45
    return score, matches


def search_catalog(query: str, limit: int = 12, catalog_id: int | None = None):
    catalog_items = ITEMS if catalog_id is None else [
        item for item in ITEMS if item.get("_catalog_id") == catalog_id
    ]
    specification_results = search_product_specifications(query, limit, catalog_items)
    if specification_results is not None:
        return specification_results
    ranked = []
    for item in catalog_items:
        score, matches = token_score(query, item)
        if score > 0:
            ranked.append((score, item, matches))
    ranked.sort(key=lambda row: (-row[0], row[1]["code"]))
    if ranked and ranked[0][0] >= 1000:
        ranked = [ranked[0]]
    return [{**item, "score": score, "matches": matches[:6]} for score, item, matches in ranked[:limit]]


APPLICATION_SECTION_RE = re.compile(
    r"\bAPLICA[CÇ][OÕ]ES(?:\s+DETALHADAS)?\s*:\s*(.*?)(?=\s+EQUIVAL[EÊ]NCIAS(?:\s+E\s+REFER[EÊ]NCIAS(?:\s+ORIGINAIS)?)?\s*:|$)",
    re.I,
)


def application_focus_tokens(query: str, item: dict, product_text: str = "") -> tuple[list[str], list[int]]:
    """Return vehicle/application terms and explicit years from a search query."""
    qnorm = normalize_search_language(query)
    ignored = set(STOPWORDS) | set(GENERIC_TERMS) | set(SPEC_QUERY_WORDS)
    ignored.update({
        "AUTOPECA", "AUTOPECAS", "ANO", "ANOS", "MOTOR", "MODELO", "MARCA",
        "FRENTE", "EIXO", "SISTEMA", "DIANTEIRA", "TRASEIRA", "DIREITA", "ESQUERDA",
    })
    for terms in PART_INTENTS.values():
        ignored.update(terms)
    # Tudo que identifica a própria peça deve ser retirado. O que sobra na
    # pergunta são, em regra, marca/modelo/motor do veículo e o ano desejado.
    product_identity = normalize_search_language(" ".join([
        str(item.get("code", "")),
        str(item.get("manufacturer", "")),
        str(item.get("category", "")),
        product_text,
    ]))
    ignored.update(product_identity.split())
    years = [int(token) for token in qnorm.split() if token.isdigit() and len(token) == 4 and 1950 <= int(token) <= 2035]
    tokens = []
    for token in qnorm.split():
        if token in ignored or (token.isdigit() and len(token) == 4):
            continue
        if len(token) < 2:
            continue
        tokens.append(token)
    return list(dict.fromkeys(tokens)), years


def focused_application_text(item: dict, query: str) -> str | None:
    """Create a short display excerpt containing only matching application rows."""
    text = str(item.get("text", ""))
    section_match = APPLICATION_SECTION_RE.search(text)
    if section_match:
        application_text = section_match.group(1)
        product_text = text[:section_match.start()]
    else:
        # Catálogos antigos nem sempre possuem o título "Aplicações". Nesses
        # casos pesquisamos o texto, mas retornamos apenas pequenos trechos.
        application_text = text
        product_text = " ".join([
            str(item.get("code", "")),
            str(item.get("manufacturer", "")),
            str(item.get("category", "")),
        ])
    focus_tokens, years = application_focus_tokens(query, item, product_text)
    if not focus_tokens:
        return None
    matching = []
    segments = [piece.strip(" .") for piece in application_text.split("|")]
    for segment in segments:
        if not segment:
            continue
        normalized_segment = normalize_search_language(segment)
        if not all(contains_token(normalized_segment, token) for token in focus_tokens):
            continue
        if years:
            spans = year_spans(normalized_segment)
            if not spans or not all(any(start <= year <= end for start, end in spans) for year in years):
                continue
        # Evita que um catálogo sem separadores volte a produzir uma página
        # inteira. Mantém somente o contexto próximo ao veículo encontrado.
        if len(segment) > 520:
            positions = [
                normalized_segment.find(token)
                for token in focus_tokens
                if normalized_segment.find(token) >= 0
            ]
            if positions:
                normalized_position = min(positions)
                ratio = normalized_position / max(len(normalized_segment), 1)
                center = int(ratio * len(segment))
                start = max(0, center - 170)
                end = min(len(segment), center + 350)
                segment = ("…" if start else "") + segment[start:end].strip(" .") + ("…" if end < len(segment) else "")
        matching.append(segment)
        if len(matching) >= 8:
            break
    if not matching:
        return None
    return "Aplicações correspondentes à pesquisa: " + " | ".join(matching)


def focus_search_results(results: list[dict], query: str) -> list[dict]:
    focused = []
    for item in results:
        display_text = focused_application_text(item, query)
        focused.append({**item, **({"display_text": display_text} if display_text else {})})
    return focused


SPEC_PATTERNS = {
    "dentes": [r"(?<!\d)(\d{1,3})\s+DENTES?\b"],
    "elo": [r"\bELO\s+(\d+(?:[.,]\d+)?)\s*MM\b", r"(\d+(?:[.,]\d+)?)\s*MM\s+DE\s+ELO\b"],
    "pressao": [r"\bPRESSAO\s+(?:DE\s+)?(\d+(?:[.,]\d+)?)\s*BAR\b", r"(?<!\d)(\d+(?:[.,]\d+)?)\s*BAR\b"],
    "folga": [r"\bFOLGA\s+(?:DE\s+)?(\d+(?:[.,]\d+)?)\s*MM\b", r"(\d+(?:[.,]\d+)?)\s*MM\s+DE\s+FOLGA\b"],
    "altura": [r"\bALTURA\s+(?:DE\s+)?(\d+(?:[.,]\d+)?)\s*MM\b", r"(\d+(?:[.,]\d+)?)\s*MM\s+DE\s+ALTURA\b"],
    "diametro": [r"\bDIAMETRO\s+(?:DE\s+)?(\d+(?:[.,]\d+)?)\s*MM\b", r"(\d+(?:[.,]\d+)?)\s*MM\s+DE\s+DIAMETRO\b"],
    "comprimento": [r"\bCOMPRIMENTO\s+(?:DE\s+)?(\d+(?:[.,]\d+)?)\s*MM\b", r"(\d+(?:[.,]\d+)?)\s*MM\s+DE\s+COMPRIMENTO\b"],
    "largura": [r"\bLARGURA\s+(?:DE\s+)?(\d+(?:[.,]\d+)?)\s*MM\b", r"(\d+(?:[.,]\d+)?)\s*MM\s+DE\s+LARGURA\b"],
    "rosca": [r"\bROSCA\s+(?:DE\s+)?([A-Z]?\d+(?:[.,X/-]\d+)*)\b"],
}
SPEC_LABELS = {
    "dentes": lambda value: f"{value} dentes",
    "elo": lambda value: f"elo de {value} mm",
    "pressao": lambda value: f"pressão de {value} bar",
    "folga": lambda value: f"folga de {value} mm",
    "altura": lambda value: f"altura de {value} mm",
    "diametro": lambda value: f"diâmetro de {value} mm",
    "comprimento": lambda value: f"comprimento de {value} mm",
    "largura": lambda value: f"largura de {value} mm",
    "rosca": lambda value: f"rosca {value}",
}
PART_INTENTS = {
    "TRIZETA": ("TRIZETA", "TRIZETAS"),
    "VELA": ("VELA", "VELAS"),
    "FILTRO": ("FILTRO", "FILTROS"),
    "BOBINA": ("BOBINA", "BOBINAS"),
    "CABO": ("CABO", "CABOS"),
    "REGULADOR": ("REGULADOR", "REGULADORES"),
    "ROLAMENTO": ("ROLAMENTO", "ROLAMENTOS"),
    "JUNTA": ("JUNTA", "JUNTAS"),
    "PALHETA": ("PALHETA", "PALHETAS"),
    "PASTILHA": ("PASTILHA", "PASTILHAS"),
    "SAPATA": ("SAPATA", "SAPATAS"),
}
PART_RESPONSE_NAMES = {
    "TRIZETA": ("trizeta", "trizetas"),
    "VELA": ("vela", "velas"),
    "FILTRO": ("filtro", "filtros"),
    "BOBINA": ("bobina", "bobinas"),
    "CABO": ("cabo", "cabos"),
    "REGULADOR": ("regulador", "reguladores"),
    "ROLAMENTO": ("rolamento", "rolamentos"),
    "JUNTA": ("junta", "juntas"),
    "PALHETA": ("palheta", "palhetas"),
    "PASTILHA": ("pastilha", "pastilhas"),
    "SAPATA": ("sapata", "sapatas"),
}

SPEC_QUERY_WORDS = {
    "DENTE", "DENTES", "ELO", "PRESSAO", "BAR", "FOLGA", "ALTURA", "DIAMETRO",
    "COMPRIMENTO", "LARGURA", "ROSCA", "MEDIDA", "MEDIDAS", "MILIMETRO",
    "MILIMETROS", "MM", "TEM", "TENHA", "COM", "E",
}


def detect_part_intent(value: str) -> str | None:
    """Identify the requested part while prioritizing compound part names."""
    qnorm = normalize_search_language(value)
    tokens = set(qnorm.split())
    priority = (
        "CABO", "TRIZETA", "PASTILHA", "SAPATA", "PALHETA", "FILTRO",
        "BOBINA", "VELA", "REGULADOR", "ROLAMENTO", "JUNTA",
    )
    for name in priority:
        if any(term in tokens for term in PART_INTENTS.get(name, ())):
            return name
    return None

# Campos técnicos presentes em catálogos estruturados, como o da Fremax.
# As frases mais específicas vêm primeiro para que "espessura mínima" não seja
# confundida com o campo genérico "espessura".
TECHNICAL_FIELD_QUERIES = (
    ("ESPESSURA MINIMA", "ESPESSURA MINIMA", "espessura mínima"),
    ("DIAMETRO DO FURO CENTRAL", "DIAMETRO DO FURO CENTRAL", "diâmetro do furo central"),
    ("DIAMETRO DO FURO DE FIXACAO", "DIAMETRO DO FURO DE FIXACAO", "diâmetro do furo de fixação"),
    ("DIAMETRO POSICAO DOS FUROS", "DIAMETRO POSICAO DOS FUROS", "diâmetro da posição dos furos"),
    ("DIAMETRO DE ASSENTAMENTO", "DIAMETRO DE ASSENTAMENTO", "diâmetro de assentamento"),
    ("DIAMETRO EXTERNO", "DIAMETRO EXTERNO", "diâmetro externo"),
    ("DIAMETRO INTERNO", "DIAMETRO INTERNO", "diâmetro interno"),
    ("DIAMETRO MAXIMO", "DIAMETRO MAXIMO", "diâmetro máximo"),
    ("ALTURA TOTAL", "ALTURA TOTAL", "altura total"),
    ("QUANTIDADE DE FUROS GUIA", "QUANTIDADE DE FUROS GUIA", "quantidade de furos guia"),
    ("QUANTIDADE DE FUROS", "QUANTIDADE DE FUROS", "quantidade de furos"),
    ("QUANTIDADE DE ESTRIAS", "QUANTIDADE DE ESTRIAS", "quantidade de estrias"),
    ("INCLUI ROLAMENTO", "INCLUI ROLAMENTO", "inclusão de rolamento"),
    ("INCLUI CUBO", "INCLUI CUBO", "inclusão de cubo"),
    ("SISTEMA DE VENTILACAO", "SISTEMA DE VENTILACAO", "sistema de ventilação"),
    ("DISCO PERFURADO", "DISCO PERFURADO", "disco perfurado"),
    ("DISCO SLOTADO", "DISCO SLOTADO", "disco slotado"),
    ("ESPESSURA", "ESPESSURA", "espessura"),
    ("POSICAO", "POSICAO", "posição"),
    ("MODELO", "MODELO", "modelo"),
    ("LADO", "LADO", "lado"),
    ("ABS", "ABS", "ABS"),
)


def extract_specifications(value: str) -> dict[str, str]:
    text = normalize(value.replace(",", "."))
    found = {}
    for name, patterns in SPEC_PATTERNS.items():
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                found[name] = match.group(1).replace(".", ",")
                break
    return found


def technical_fields(value: str) -> dict[str, str]:
    """Extract ``label: value`` pairs from a product's technical specification section."""
    text = str(value or "")
    marker = re.search(r"ESPECIFICA[CÇ][OÕ]ES T[EÉ]CNICAS:\s*", text, re.I)
    if not marker:
        return {}
    section = text[marker.end():]
    section = re.split(r"\.\s+(?:APLICA[CÇ][OÕ]ES|EQUIVAL[EÊ]NCIAS)\s*:", section, maxsplit=1, flags=re.I)[0]
    fields = {}
    for part in section.split(";"):
        if ":" not in part:
            continue
        label, value = part.split(":", 1)
        fields[normalize(label)] = value.strip().rstrip(".")
    return fields


def requested_technical_field(query: str) -> tuple[str, str] | None:
    qnorm = normalize(query)
    for query_phrase, catalog_label, response_label in TECHNICAL_FIELD_QUERIES:
        if query_phrase in qnorm:
            return catalog_label, response_label
    return None


def specification_context_query(query: str, requested: dict[str, str], requested_part: str) -> str:
    """Keep application constraints left beside a technical specification.

    Example: ``trizeta 22 dentes para Palio 2012`` becomes ``PALIO 2012``.
    The returned terms are later evaluated together against the same catalog product.
    """
    part_words = {word for words in PART_INTENTS.values() for word in words}
    spec_values = {normalize(value).replace(",", ".") for value in requested.values()}
    context = []
    for token in normalize_search_language(query).split():
        canonical = ALIASES.get(token, token)
        numeric_unit = re.fullmatch(r"(\d+(?:[.,]\d+)?)(?:MM|BAR)?", canonical)
        if canonical in STOPWORDS or canonical in SPEC_QUERY_WORDS or canonical in part_words:
            continue
        if numeric_unit and numeric_unit.group(1).replace(",", ".") in spec_values:
            continue
        context.append(canonical)
    return " ".join(context)


def own_product_blocks(item: dict, known_codes: set[str]) -> list[str]:
    """Return only text blocks headed by this item's code, avoiding neighboring PDF products."""
    text = str(item.get("text", ""))
    own_code = str(item.get("code", "")).upper()
    boundaries = []
    for match in re.finditer(r"(?<![A-Z0-9])([A-Z][A-Z0-9./_-]*\d[A-Z0-9./_-]*)(?![A-Z0-9])", text, re.I):
        token = match.group(1).upper()
        if token in known_codes:
            boundaries.append((match.start(), token))
    blocks = []
    for index, (start, code) in enumerate(boundaries):
        if code != own_code:
            continue
        end = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(text)
        blocks.append(text[start:end])
    return blocks or [text]


def search_product_specifications(query: str, limit: int = 12, items: list[dict] | None = None) -> list[dict] | None:
    """Reverse-search catalog products using one or more explicit technical specifications."""
    qnorm = normalize_search_language(query)
    requested = extract_specifications(query)
    requested_part = detect_part_intent(qnorm)
    if not requested or not requested_part:
        return None
    context_query = specification_context_query(query, requested, requested_part)
    source_items = ITEMS if items is None else items
    canonical = {str(item.get("code", "")).upper(): item for item in source_items}
    known_codes = set(canonical)
    results = []
    for code, item in canonical.items():
        item_kind = normalize(f"{item.get('category', '')} {item.get('text', '')[:160]}")
        if requested_part not in item_kind:
            continue
        matched = False
        for block in own_product_blocks(item, known_codes):
            available = extract_specifications(block[:1200])
            specifications_match = all(available.get(name) == value for name, value in requested.items())
            context_matches = True
            if context_query:
                context_item = {
                    "code": item.get("code", ""),
                    "category": item.get("category", ""),
                    "text": block,
                    "search": normalize(f"{item.get('category', '')} {block}"),
                }
                context_matches = token_score(context_query, context_item)[0] > 0
            if specifications_match and context_matches:
                matched = True
                break
        if matched:
            results.append({
                **item,
                "score": 900,
                "matches": [SPEC_LABELS[name](value) for name, value in requested.items()]
                           + ([f"aplicação: {context_query}"] if context_query else []),
            })
    results.sort(key=lambda item: item["code"])
    return results[:limit]


def is_catalog_question(query: str) -> bool:
    qnorm = normalize(query)
    question_terms = {
        "QUANTO", "QUANTOS", "QUANTA", "QUANTAS", "QUAL", "QUAIS", "ONDE", "COMO",
        "DENTE", "DENTES", "MEDIDA", "MEDIDAS", "ROSCA", "DIAMETRO", "COMPRIMENTO",
        "ALTURA", "LARGURA", "PRESSAO", "BAR", "FOLGA", "ESPESSURA", "MINIMA",
        "FURO", "FUROS", "ESTRIA", "ESTRIAS", "CUBO", "ABS", "VENTILACAO",
        "ELO", "APLICA", "APLICACAO", "SERVE", "MOTOR", "COMBUSTIVEL", "POSICAO",
        "LADO", "EQUIVALENTE", "EQUIVALENTES", "ORIGINAL", "ORIGINAIS",
    }
    return "?" in query or any(token in question_terms for token in qnorm.split())


def answer_catalog_question(query: str, results: list[dict]) -> dict | None:
    """Answer only from catalog evidence already found by the deterministic search."""
    if not results or not is_catalog_question(query):
        return None
    all_result_items = results[:12]
    evidence_items = all_result_items[:5]

    def sources_for(codes: list[str]) -> list[dict]:
        selected = {code.upper() for code in codes}
        return [
            {
                "code": item.get("code"),
                "manufacturer": item.get("manufacturer"),
                "edition": item.get("edition"),
                "pages": item.get("pages", []),
            }
            for item in all_result_items
            if str(item.get("code", "")).upper() in selected
        ]

    # Especificações objetivas são extraídas diretamente do catálogo.
    # Assim, continuam funcionando mesmo se a API estiver temporariamente indisponível.
    qnorm = normalize(query)
    primary = evidence_items[0]
    primary_text = normalize(str(primary.get("text", "")))
    primary_code = str(primary.get("code", "")).upper()
    requested_specs = extract_specifications(query)
    requested_part = detect_part_intent(qnorm)
    requested_code = any(contains_token(qnorm, str(item.get("code", "")).upper()) for item in all_result_items)
    technical_request = requested_technical_field(query)
    if technical_request and requested_code:
        catalog_label, response_label = technical_request
        fields = technical_fields(primary.get("text", ""))
        value = fields.get(catalog_label)
        if value:
            return {
                "text": f"A {response_label} da peça {primary_code} é {value}.",
                "supported": True,
                "sources": sources_for([primary_code]),
            }
    if requested_specs and requested_part and not requested_code:
        codes = [str(item.get("code", "")).upper() for item in all_result_items]
        if codes:
            singular, plural = PART_RESPONSE_NAMES[requested_part]
            if len(codes) == 1:
                subject = f"O código {codes[0]} corresponde à peça {singular} com"
            else:
                article = "As" if requested_part in {"TRIZETA", "VELA", "BOBINA", "JUNTA"} else "Os"
                subject = f"{article} {plural} {', '.join(codes[:-1])} e {codes[-1]} possuem"
            characteristics = [SPEC_LABELS[name](value) for name, value in requested_specs.items()]
            description = " e ".join(characteristics)
            return {
                "text": f"{subject} {description}.",
                "supported": True,
                "sources": sources_for(codes),
            }
    if "DENTE" in qnorm or "DENTES" in qnorm:
        teeth = re.search(r"(?<!\d)(\d{1,3})\s+DENTES?\b", primary_text)
        if teeth:
            detail = f"A peça {primary_code} possui {teeth.group(1)} dentes"
            link = re.search(r"\bELO\s+(\d+(?:[.,]\d+)?)\s*MM\b", primary_text)
            if link:
                detail += f" e elo de {link.group(1).replace('.', ',')} mm"
            return {
                "text": detail + ".",
                "supported": True,
                "sources": sources_for([primary_code]),
            }

    if not os.environ.get("OPENAI_API_KEY"):
        return None
    evidence = []
    allowed_codes = set()
    for item in evidence_items:
        code = str(item.get("code", "")).upper()
        allowed_codes.add(code)
        evidence.append(
            f"CÓDIGO: {code}\n"
            f"FABRICANTE: {item.get('manufacturer', '')}\n"
            f"EDIÇÃO: {item.get('edition', '')}\n"
            f"PÁGINAS: {', '.join(map(str, item.get('pages', [])))}\n"
            f"TEXTO DO CATÁLOGO: {str(item.get('text', ''))[:3500]}"
        )
    try:
        from openai import OpenAI

        client = OpenAI(timeout=15.0, max_retries=1)
        response = client.responses.parse(
            model=os.environ.get("OPENAI_MODEL", "gpt-5-mini"),
            store=False,
            input=[
                {
                    "role": "system",
                    "content": (
                        "Responda como assistente de balcão de autopeças usando EXCLUSIVAMENTE as evidências "
                        "dos catálogos fornecidas. Nunca use conhecimento externo, nunca deduza especificações "
                        "e nunca invente compatibilidade. Dê uma resposta direta e curta em português do Brasil. "
                        "Se a informação pedida não estiver escrita explicitamente nas evidências, responda que "
                        "ela não consta nos catálogos consultados e marque supported como false. Em source_codes, "
                        "inclua somente códigos presentes nas evidências que sustentam a resposta."
                    ),
                },
                {
                    "role": "user",
                    "content": f"PERGUNTA: {query[:500]}\n\nEVIDÊNCIAS:\n\n" + "\n\n---\n\n".join(evidence),
                },
            ],
            text_format=CatalogAnswer,
        )
        parsed = response.output_parsed
        if not parsed or not parsed.answer.strip():
            return None
        source_codes = [code.upper() for code in parsed.source_codes if code.upper() in allowed_codes]
        sources = sources_for(source_codes)
        return {
            "text": parsed.answer.strip()[:1000],
            "supported": bool(parsed.supported and sources),
            "sources": sources,
        }
    except Exception:
        app.logger.exception("Falha ao responder pergunta com os dados do catálogo")
        return None


def interpret_catalog_question(query: str) -> CatalogQuestion | None:
    """Use AI only to convert a natural question into catalog search terms."""
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    try:
        from openai import OpenAI

        client = OpenAI(timeout=12.0, max_retries=1)
        response = client.responses.parse(
            model=os.environ.get("OPENAI_MODEL", "gpt-5-mini"),
            store=False,
            input=[
                {
                    "role": "system",
                    "content": (
                        "Você interpreta perguntas de balcão de autopeças em português do Brasil. "
                        "Extraia somente os dados informados pelo usuário. Não indique compatibilidade, "
                        "não invente veículos, anos, motores, marcas ou códigos. Remova palavras de pergunta "
                        "como 'onde aplica', 'qual serve' e 'preciso de'. Em normalized_query, devolva apenas "
                        "os termos úteis para pesquisar literalmente em um catálogo: código, tipo da peça, "
                        "fabricante da peça, marca/modelo do veículo, ano, motor, posição e lado. Preserve códigos "
                        "com hífen. Exemplos: 'qual filtro vai no City 2012?' vira 'FILTRO CITY 2012'; "
                        "'onde aplica WEOC-004?' vira 'WEOC-004'. Use string vazia para dados ausentes."
                    ),
                },
                {"role": "user", "content": query[:500]},
            ],
            text_format=CatalogQuestion,
        )
        return response.output_parsed
    except Exception:
        app.logger.exception("Falha ao interpretar pergunta com OpenAI")
        return None


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
    return render_template(
        "index.html",
        total=len(ITEMS),
        catalog_total=len(CATALOGS),
        catalogs=CATALOGS,
        is_admin=session.get("role") == "admin",
    )


@app.route("/admin/catalogs", methods=["GET", "POST"])
@admin_required
def admin_catalogs():
    if request.method == "POST":
        uploaded = request.files.get("catalog")
        manufacturer = request.form.get("manufacturer", "").strip()
        edition = request.form.get("edition", "").strip()
        suffix = Path(uploaded.filename).suffix.lower() if uploaded else ""
        if not uploaded or suffix not in {".pdf", ".json"} or not manufacturer or not edition:
            flash("Informe fabricante, edição e um arquivo PDF ou JSON válido.", "error")
            return redirect(url_for("admin_catalogs"))
        if Catalog.query.filter_by(manufacturer=manufacturer, edition=edition).first():
            flash("Já existe um catálogo desse fabricante com essa edição.", "error")
            return redirect(url_for("admin_catalogs"))
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp:
                uploaded.save(temp)
                temp_path = Path(temp.name)
            if suffix == ".json":
                with temp_path.open("r", encoding="utf-8-sig") as stream:
                    items = validate_prepared_catalog(json.load(stream), manufacturer, edition)
            else:
                items = extract(temp_path, manufacturer, edition)
            if not items:
                flash("Nenhum produto pôde ser extraído desse arquivo.", "error")
                return redirect(url_for("admin_catalogs"))
            db.session.add(Catalog(manufacturer=manufacturer, edition=edition, filename=uploaded.filename, item_count=len(items), data=json.dumps(items, ensure_ascii=False)))
            db.session.commit()
            refresh_cache()
            flash(f"Catálogo incluído com {len(items)} produtos.", "success")
        except (ValueError, json.JSONDecodeError) as error:
            db.session.rollback()
            flash(f"Catálogo preparado inválido: {error}", "error")
        except Exception:
            db.session.rollback()
            app.logger.exception("Falha ao importar catálogo")
            flash("Não foi possível processar o catálogo. Confira o arquivo e tente novamente.", "error")
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


@app.post("/admin/catalogs/<int:catalog_id>/delete")
@admin_required
def delete_catalog(catalog_id):
    catalog = db.get_or_404(Catalog, catalog_id)
    name = f"{catalog.manufacturer} {catalog.edition}"
    db.session.delete(catalog)
    db.session.commit()
    refresh_cache()
    flash(f"Catálogo {name} excluído. Você já pode importá-lo novamente.", "success")
    return redirect(url_for("admin_catalogs"))


@app.post("/api/search")
@logged_in
def api_search():
    payload = request.get_json(silent=True) or {}
    query = str(payload.get("query", "")).strip()
    if len(query) < 2:
        return jsonify({"error": "Digite pelo menos dois caracteres."}), 400
    raw_catalog_id = payload.get("catalog_id")
    catalog_id = None
    if raw_catalog_id not in (None, ""):
        try:
            catalog_id = int(raw_catalog_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Catálogo selecionado inválido."}), 400
        if not any(active_id == catalog_id for active_id, _, _ in CATALOGS):
            return jsonify({"error": "O catálogo selecionado não está ativo."}), 400
    results = search_catalog(query, catalog_id=catalog_id)
    effective_query = query
    interpreted = None
    if not results:
        interpreted = interpret_catalog_question(query)
        ai_query = interpreted.normalized_query.strip() if interpreted else ""
        if ai_query and normalize(ai_query) != normalize(query):
            results = search_catalog(ai_query, catalog_id=catalog_id)
            effective_query = ai_query
    direct_answer = answer_catalog_question(query, results)
    results = focus_search_results(results, effective_query)
    return jsonify({
        "query": query,
        "results": results,
        "catalogs": len(CATALOGS),
        "catalog_id": catalog_id,
        "ai_used": interpreted is not None,
        "interpreted_query": interpreted.normalized_query if interpreted else None,
        "answer": direct_answer,
    })


@app.get("/health")
def health():
    return jsonify({"status": "ok", "items": len(ITEMS)})


if __name__ == "__main__":
    app.run(debug=True)
