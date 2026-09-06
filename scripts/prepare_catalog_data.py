#!/usr/bin/env python3
"""Prepare catalog PDFs as normalized JSON files for private database import."""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path

import fitz

TSA_CODE_RE = re.compile(r"^(?:T-\d{6}|\d{6}[A-Z])$")
TSA_FAMILY_RE = re.compile(r"^(T-\d{2,3})\s*-\s*(.+)$")


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" |\n")


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9./>-]+", " ", value).strip()


def tsa_search_years(value: str) -> str:
    value = re.sub(
        r"(?<!\d)(\d{4})\s*>\s*(\d{4})(?!\d)",
        lambda match: f"{match.group(0)} {int(match.group(1)) % 100:02d}/{int(match.group(2)) % 100:02d}",
        value,
    )
    return re.sub(
        r"(?<!\d)(\d{4})\s*>(?!\s*\d)",
        lambda match: f"{match.group(0)} {int(match.group(1)) % 100:02d}/...",
        value,
    )


def page_spans(page: fitz.Page) -> list[dict]:
    spans = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                text = clean(span["text"])
                if text:
                    spans.append({**span, "text": text})
    return spans


def tsa_family_categories(document: fitz.Document) -> dict[str, str]:
    categories = {}
    for page in document:
        for span in page_spans(page):
            match = TSA_FAMILY_RE.match(span["text"].upper())
            if match and round(span["size"], 1) == 10.0:
                categories[match.group(1)] = clean(match.group(2)).title()
    return categories


def initial_brands(page: fitz.Page) -> tuple[str, str]:
    header = clean(" ".join(
        span["text"] for span in page_spans(page)
        if span["bbox"][1] < 48 and round(span["size"], 1) >= 11.5
    ))
    brands = [clean(part).title() for part in header.split("|") if clean(part)]
    if not brands:
        return "", ""
    return brands[0], brands[1] if len(brands) > 1 else brands[0]


def is_brand_span(span: dict) -> bool:
    text = span["text"]
    return (
        round(span["size"], 1) == 10.0
        and span["color"] == 16777215
        and "Bold" in span["font"]
        and not re.search(r"\d", text)
        and "|" not in text
        and len(text) <= 45
    )


def category_from_span(span: dict) -> str:
    text = clean(span["text"])
    if not (
        round(span["size"], 1) == 10.0
        and span["color"] == 0
        and "Bold" in span["font"]
        and text.upper() not in {"VEÍCULO", "MODELO", "MOTOR", "ANO", "COMBUSTÍVEL", "CÓDIGOS", "OBSERVAÇÕES"}
    ):
        return ""
    match = TSA_FAMILY_RE.match(text.upper())
    return clean(match.group(2)).title() if match else text.title()


def prepare_tsa(pdf_path: Path, manufacturer: str, edition: str) -> list[dict]:
    document = fitz.open(pdf_path)
    family_categories = tsa_family_categories(document)
    products: dict[str, dict] = {}

    for page_number, page in enumerate(document, 1):
        if page_number < 23:
            continue
        spans = page_spans(page)
        left_brand, right_brand = initial_brands(page)
        for x0, x1, default_brand in ((20, 302, left_brand), (302, 585, right_brand)):
            column_spans = [span for span in spans if x0 <= span["bbox"][0] < x1 and span["bbox"][1] > 48]
            codes = sorted([
                span for span in column_spans
                if TSA_CODE_RE.match(span["text"].upper())
                and round(span["size"], 1) == 8.0
                and span["color"] == 16777215
                and "BoldItalic" in span["font"]
            ], key=lambda span: span["bbox"][1])
            for index, code_span in enumerate(codes):
                code = code_span["text"].upper()
                y0 = code_span["bbox"][1] - 2
                y1 = codes[index + 1]["bbox"][1] - 3 if index + 1 < len(codes) else 755
                prior = [span for span in column_spans if span["bbox"][1] < y0]
                brands = [span for span in prior if is_brand_span(span)]
                brand = clean(brands[-1]["text"]).title() if brands else default_brand
                nearby_categories = [category_from_span(span) for span in prior]
                nearby_categories = [value for value in nearby_categories if value]
                category = nearby_categories[-1] if nearby_categories else ""
                family = max(
                    (prefix for prefix in family_categories if code.startswith(prefix)),
                    key=len,
                    default=None,
                )
                if family:
                    category = family_categories[family]
                if not category:
                    category = "Autopeças TSA"

                segment = clean(page.get_text("text", clip=fitz.Rect(x0, y0, x1, y1), sort=True))
                if not segment or code not in segment.upper():
                    continue
                application_text = clean(f"{brand}. {category}. {segment}")
                record = products.setdefault(code, {
                    "code": code,
                    "manufacturer": manufacturer,
                    "edition": edition,
                    "category": category,
                    "pages": [],
                    "text": "",
                })
                record["pages"].append(page_number)
                if normalize(application_text) not in normalize(record["text"]):
                    record["text"] = clean(f"{record['text']} | {application_text}")

    rows = sorted(products.values(), key=lambda item: item["code"])
    for row in rows:
        row["pages"] = sorted(set(row["pages"]))
        searchable_text = tsa_search_years(row["text"])
        row["search"] = normalize(" ".join([
            row["code"], row["manufacturer"], row["category"], searchable_text
        ]))
    return rows


def validate(rows: list[dict]) -> None:
    if len(rows) < 100:
        raise ValueError(f"Extração incompleta: somente {len(rows)} produtos.")
    codes = [row["code"] for row in rows]
    if len(codes) != len(set(codes)):
        raise ValueError("Há códigos duplicados no catálogo preparado.")
    invalid = [row["code"] for row in rows if not row["text"] or not row["pages"]]
    if invalid:
        raise ValueError(f"Produtos incompletos: {', '.join(invalid[:10])}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--profile", choices=["tsa"], required=True)
    parser.add_argument("--manufacturer", required=True)
    parser.add_argument("--edition", required=True)
    args = parser.parse_args()

    rows = prepare_tsa(args.pdf, args.manufacturer, args.edition)
    validate(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(rows)} produtos preparados em {args.output}")


if __name__ == "__main__":
    main()
