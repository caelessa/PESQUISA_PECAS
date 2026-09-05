#!/usr/bin/env python3
"""Extrai blocos de produtos de catálogos PDF com layout em colunas."""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

import fitz

CODE_RE = re.compile(r"^[A-Z]{2,6}[A-Z0-9.-]*\d[A-Z0-9.-]*$")
EXCLUDED = {"AUTHOMIX", "PAGINAS", "COFAP", "NAKATA", "AXIOS", "TRW", "PDX"}
COLS = [(25, 159), (159, 292), (292, 424), (424, 568)]
WEGA_FILTER_COLS = [
    (425, 460, "Filtro do Ar"),
    (460, 499, "Filtro do Óleo"),
    (499, 558, "Filtro do Óleo Opcional"),
    (558, 616, "Filtro do Combustível"),
    (616, 693, "Filtro do Combustível Opcional"),
    (693, 737, "Filtro de Cabine"),
    (737, 805, "Filtro de Cabine com Carvão"),
]


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" |\n")


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return clean(text.upper())


def page_category(page: fitz.Page) -> str:
    words = page.get_text("words")
    top = [w[4] for w in words if w[1] < 45 and 25 < w[0] < 560]
    return clean(" ".join(top)).title()


def words_in(words, x0, x1):
    return clean(" ".join(w[4] for w in sorted(words, key=lambda item: item[0]) if x0 <= w[0] < x1))


def normalize_table_year(value: str) -> str:
    value = value.replace("‐", "-").replace("–", "-").replace("—", "-")
    numbers = re.findall(r"\d{2,4}", value)
    if "até" in value.lower() and numbers:
        return f".../{int(numbers[-1]) % 100:02d}"
    if len(numbers) >= 2:
        return f"{int(numbers[0]) % 100:02d}/{int(numbers[1]) % 100:02d}"
    if numbers and (">" in value or value.count("-") >= 2):
        return f"{int(numbers[0]) % 100:02d}/..."
    if numbers:
        return f"{int(numbers[0]) % 100:02d}"
    return clean(value)


def extract_application_table(doc: fitz.Document, manufacturer: str, edition: str) -> list[dict]:
    """Extrai catálogos tabulares no padrão Montadora/Modelo/Ano/Tipos de filtro."""
    products: dict[str, dict] = {}
    for page_number, page in enumerate(doc, 1):
        words = page.get_text("words")
        page_text = page.get_text("text")
        if "Montadora" not in page_text or "Filtro" not in page_text:
            continue
        lines = defaultdict(list)
        for word in words:
            if 66 < word[1] < 565:
                lines[round(word[1], 1)].append(word)
        for line_words in lines.values():
            maker = words_in(line_words, 10, 69)
            model = words_in(line_words, 69, 133)
            details = words_in(line_words, 133, 390)
            raw_year = words_in(line_words, 390, 425)
            if not maker or not model or not re.search(r"\d", raw_year):
                continue
            year = normalize_table_year(raw_year)
            application = clean(f"{maker} {model} {details} {year}")
            for x0, x1, category in WEGA_FILTER_COLS:
                cell = words_in(line_words, x0, x1)
                if not cell:
                    continue
                # Uma célula pode conter mais de um código separado por espaço.
                codes = [token.strip(" ,;") for token in cell.split() if re.search(r"[A-Z]", token, re.I)]
                for code in codes:
                    code = code.upper()
                    if len(code) < 3:
                        continue
                    record = products.setdefault(code, {
                        "code": code,
                        "manufacturer": manufacturer,
                        "edition": edition,
                        "category": category,
                        "pages": [],
                        "text": f"{category}. Aplicações:",
                    })
                    record["pages"].append(page_number)
                    if norm(application) not in norm(record["text"]):
                        record["text"] = clean(record["text"] + " | " + application)
    rows = list(products.values())
    for row in rows:
        row["pages"] = sorted(set(row["pages"]))
        row["search"] = norm(" ".join([row["code"], row["manufacturer"], row["category"], row["text"]]))
    return sorted(rows, key=lambda row: row["code"])


def candidates(page: fitz.Page):
    words = page.get_text("words")
    result = []
    for w in words:
        x0, y0, x1, y1, token = w[:5]
        token = token.strip().upper()
        if token in EXCLUDED or not CODE_RE.match(token) or y0 < 45 or y0 > 780:
            continue
        col = next((i for i, (a, b) in enumerate(COLS) if a <= x0 < b and abs(x0 - a) < 25), None)
        if col is None:
            continue
        below = [v[4].lower() for v in words if COLS[col][0] <= v[0] < COLS[col][1] and y0 < v[1] < y0 + 45]
        if not any(t.startswith(("orig", "bieleta", "bomba", "cilindro", "cruzeta", "cubo", "filtro", "kit", "bucha", "suporte", "pino", "ponta", "polia", "guia", "tensor", "reparo", "rolamento", "sapata", "semieixo", "terminal", "trizeta", "aditivo")) for t in below):
            continue
        result.append((token, col, y0))
    return result


def extract(pdf_path: Path, manufacturer: str, edition: str) -> list[dict]:
    doc = fitz.open(pdf_path)
    if any("Montadora" in page.get_text("text") and "Filtro" in page.get_text("text") for page in doc):
        return extract_application_table(doc, manufacturer, edition)
    collected: dict[str, dict] = {}
    for page_number, page in enumerate(doc, 1):
        found = candidates(page)
        if not found:
            continue
        category = page_category(page)
        by_col = defaultdict(list)
        for code, col, y in found:
            by_col[col].append((y, code))
        for col, entries in by_col.items():
            entries.sort()
            x0, x1 = COLS[col]
            for idx, (y0, code) in enumerate(entries):
                y1 = entries[idx + 1][0] - 3 if idx + 1 < len(entries) else 795
                text = clean(page.get_text("text", clip=fitz.Rect(x0, y0, x1, y1), sort=True))
                if len(text) < len(code) + 8:
                    continue
                record = collected.setdefault(code, {
                    "code": code,
                    "manufacturer": manufacturer,
                    "edition": edition,
                    "category": category,
                    "pages": [],
                    "text": "",
                })
                record["pages"].append(page_number)
                if norm(text) not in norm(record["text"]):
                    record["text"] = clean(record["text"] + " " + text)
    # Enriquece os produtos com linhas das tabelas de equivalência do catálogo.
    for page_number, page in enumerate(doc, 1):
        words = page.get_text("words")
        for word in words:
            code = word[4].strip().upper()
            if code not in collected:
                continue
            y = word[1]
            same_line = sorted((w for w in words if abs(w[1] - y) < 2.2), key=lambda w: w[0])
            line = clean(" ".join(w[4] for w in same_line))
            if len(line.split()) >= 3 and norm(line) not in norm(collected[code]["text"]):
                collected[code]["text"] = clean(collected[code]["text"] + " Equivalências: " + line)
                collected[code]["pages"].append(page_number)

    rows = list(collected.values())
    for row in rows:
        row["pages"] = sorted(set(row["pages"]))
        row["search"] = norm(" ".join([row["code"], row["manufacturer"], row["category"], row["text"]]))
    return sorted(rows, key=lambda r: r["code"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--manufacturer", default="AuthoMix")
    parser.add_argument("--edition", default="Abril/2022")
    args = parser.parse_args()
    rows = extract(args.pdf, args.manufacturer, args.edition)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(rows)} produtos extraídos para {args.output}")


if __name__ == "__main__":
    main()
