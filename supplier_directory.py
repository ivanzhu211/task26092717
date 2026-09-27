"""Supplier directory CSV import/export backed by SQLite (stdlib only)."""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field

COLUMNS = [
    "id",
    "company_name",
    "contact_person",
    "phone",
    "street_address",
    "lead_time_days",
    "moq",
    "reliability",
    "categories",
]

MAX_ROWS = 500
MAX_CATEGORIES = 20


@dataclass
class Supplier:
    id: int
    owner_id: object
    company_name: str
    contact_person: str
    phone: str
    street_address: str
    lead_time_days: int
    moq: int
    reliability: int
    categories: list[str]


@dataclass
class ImportError:
    line: int
    column: str
    message: str


@dataclass
class Report:
    created: int = 0
    updated: int = 0
    errors: list[ImportError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# Normalization / validation helpers
# ---------------------------------------------------------------------------

_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def name_key(name: str) -> str:
    """Uniqueness key: Unicode-compat normalized, casefolded, whitespace-collapsed."""
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def clean_text(value: str) -> str:
    """Strip outer whitespace and normalize internal line endings to LF."""
    return value.strip().replace("\r\n", "\n").replace("\r", "\n")


def normalize_phone(raw: str):
    """Return (NXX) NXX-XXXX or raise ValueError."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError("phone must have 10 digits (optional +1 prefix)")
    if digits[0] not in "23456789" or digits[3] not in "23456789":
        raise ValueError("area code and exchange must start with 2-9")
    return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"


def escape_cell(value: str) -> str:
    """Minimal formula-injection escape for export."""
    if value and (value[0] in _DANGEROUS_PREFIXES or value[0] == "'"):
        return "'" + value
    return value


def unescape_cell(value: str) -> str:
    """Inverse of escape_cell, applied on import."""
    if (
        len(value) >= 2
        and value[0] == "'"
        and (value[1] in _DANGEROUS_PREFIXES or value[1] == "'")
    ):
        return value[1:]
    return value


def encode_categories(categories) -> str:
    """Join categories into one editable cell; backslash-escapes delimiter/quotes/newlines."""
    parts = []
    for cat in categories:
        cat = (
            cat.replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
        )
        parts.append(cat)
    return "; ".join(parts)


def decode_categories(cell: str):
    """Split a categories cell back into a list, honoring backslash escapes."""
    cats, buf = [], []
    it = iter(range(len(cell)))
    i = 0
    while i < len(cell):
        ch = cell[i]
        if ch == "\\" and i + 1 < len(cell):
            nxt = cell[i + 1]
            if nxt == "n":
                buf.append("\n")
            elif nxt == "r":
                buf.append("\r")
            else:
                buf.append(nxt)
            i += 2
        elif ch == ";":
            cats.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    cats.append("".join(buf))
    return cats


@dataclass
class _Row:
    line: int
    id: int | None
    company_name: str
    contact_person: str
    phone: str
    street_address: str
    lead_time_days: int
    moq: int
    reliability: int
    categories: list[str]
    target: int | None = None  # resolved existing supplier id, None = create


def _parse_int(raw: str, lo: int | None, hi: int | None, label: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{label} must be an integer")
    if lo is not None and value < lo or hi is not None and value > hi:
        if hi is None:
            raise ValueError(f"{label} must be at least {lo}")
        raise ValueError(f"{label} must be between {lo} and {hi}")
    return value


def _parse_categories(cell: str) -> list[str]:
    cats = []
    seen = set()
    for cat in decode_categories(clean_text(cell)):
        cat = cat.strip()
        if not cat:
            continue
        key = cat.casefold()
        if key in seen:
            continue
        seen.add(key)
        cats.append(cat)
    if len(cats) > MAX_CATEGORIES:
        raise ValueError(f"at most {MAX_CATEGORIES} categories allowed")
    return cats


def _parse_rows(text: str):
    """Parse CSV text into validated rows. Returns (rows, errors)."""
    errors: list[ImportError] = []
    rows: list[_Row] = []
    reader = csv.reader(io.StringIO(text, newline=""))
    prev_end = 0
    header = None
    col_index: dict[str, int] = {}
    data_count = 0
    for raw in reader:
        start = prev_end + 1
        prev_end = reader.line_num
        if header is None:
            header = raw
            for idx, name in enumerate(raw):
                key = name.strip().casefold()
                if key and key not in col_index:
                    col_index[key] = idx
            for required in COLUMNS:
                if required == "id":
                    continue
                if required not in col_index:
                    errors.append(
                        ImportError(1, required, f"missing required column '{required}'")
                    )
            if errors:
                return [], errors
            continue
        if not raw or all(not cell.strip() for cell in raw):
            continue  # blank line: not a record, not counted
        data_count += 1
        if data_count > MAX_ROWS:
            errors.append(
                ImportError(start, "", f"too many data rows (max {MAX_ROWS})")
            )
            break

        def cell(col: str) -> str:
            idx = col_index.get(col)
            if idx is None or idx >= len(raw):
                return ""
            return raw[idx]

        def text_field(col: str) -> str:
            return clean_text(unescape_cell(cell(col)))

        row_errors = 0

        def fail(col: str, message: str) -> None:
            nonlocal row_errors
            row_errors += 1
            errors.append(ImportError(start, col, message))

        rec_id: int | None = None
        id_raw = cell("id").strip()
        if id_raw:
            try:
                rec_id = _parse_int(id_raw, 1, None, "id")
            except ValueError as exc:
                fail("id", str(exc))

        company_name = text_field("company_name")
        if not company_name:
            fail("company_name", "company_name must not be empty")

        phone = ""
        try:
            phone = normalize_phone(unescape_cell(cell("phone")).strip())
        except ValueError as exc:
            fail("phone", str(exc))

        numbers: dict[str, int] = {}
        for col, lo, hi in (
            ("lead_time_days", 1, 90),
            ("moq", 1, None),
            ("reliability", 1, 5),
        ):
            try:
                numbers[col] = _parse_int(cell(col).strip(), lo, hi, col)
            except ValueError as exc:
                fail(col, str(exc))
                numbers[col] = 0

        categories: list[str] = []
        try:
            categories = _parse_categories(unescape_cell(cell("categories")))
        except ValueError as exc:
            fail("categories", str(exc))

        if row_errors:
            continue
        rows.append(
            _Row(
                line=start,
                id=rec_id,
                company_name=company_name,
                contact_person=text_field("contact_person"),
                phone=phone,
                street_address=text_field("street_address"),
                lead_time_days=numbers["lead_time_days"],
                moq=numbers["moq"],
                reliability=numbers["reliability"],
                categories=categories,
                    )
                )
    if header is None:
        errors.append(ImportError(1, "", "file is empty"))
    return rows, errors


class SupplierDirectory:
    def __init__(self, db_path: str = "suppliers.db"):
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS suppliers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                company_name TEXT NOT NULL,
                contact_person TEXT NOT NULL,
                phone TEXT NOT NULL,
                street_address TEXT NOT NULL,
                lead_time_days INTEGER NOT NULL,
                moq INTEGER NOT NULL,
                reliability INTEGER NOT NULL,
                categories TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def import_csv(self, owner_id, data: bytes) -> Report:
        owner_key = str(owner_id)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            return Report(errors=[ImportError(1, "", f"file is not valid UTF-8: {exc}")])
        rows, errors = _parse_rows(text)
        errors.extend(self._resolve(owner_key, rows))
        if errors:
            errors.sort(key=lambda e: (e.line, e.column))
            return Report(errors=errors)
        created = updated = 0
        with self._conn:  # single transaction: all-or-nothing
            for row in rows:
                if row.target is None:
                    self._conn.execute(
                        "INSERT INTO suppliers (owner_id, company_name,"
                        " contact_person, phone, street_address, lead_time_days,"
                        " moq, reliability, categories)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            owner_key,
                            row.company_name,
                            row.contact_person,
                            row.phone,
                            row.street_address,
                            row.lead_time_days,
                            row.moq,
                            row.reliability,
                            json.dumps(row.categories, ensure_ascii=False),
                        ),
                    )
                    created += 1
                else:
                    current = self._fetch_one(owner_key, row.target)
                    new_values = (
                        row.company_name,
                        row.contact_person,
                        row.phone,
                        row.street_address,
                        row.lead_time_days,
                        row.moq,
                        row.reliability,
                        json.dumps(row.categories, ensure_ascii=False),
                    )
                    old_values = (
                        current.company_name,
                        current.contact_person,
                        current.phone,
                        current.street_address,
                        current.lead_time_days,
                        current.moq,
                        current.reliability,
                        json.dumps(current.categories, ensure_ascii=False),
                    )
                    if new_values != old_values:
                        self._conn.execute(
                            "UPDATE suppliers SET company_name = ?,"
                            " contact_person = ?, phone = ?, street_address = ?,"
                            " lead_time_days = ?, moq = ?, reliability = ?,"
                            " categories = ? WHERE id = ? AND owner_id = ?",
                            (*new_values, row.target, owner_key),
                        )
                        updated += 1
        return Report(created=created, updated=updated)

    def _fetch_one(self, owner_key: str, supplier_id: int) -> Supplier:
        row = self._conn.execute(
            "SELECT id, owner_id, company_name, contact_person, phone,"
            " street_address, lead_time_days, moq, reliability, categories"
            " FROM suppliers WHERE owner_id = ? AND id = ?",
            (owner_key, supplier_id),
        ).fetchone()
        return Supplier(
            id=row[0],
            owner_id=owner_key,
            company_name=row[2],
            contact_person=row[3],
            phone=row[4],
            street_address=row[5],
            lead_time_days=row[6],
            moq=row[7],
            reliability=row[8],
            categories=json.loads(row[9]),
        )

    def _resolve(self, owner_key: str, rows: list[_Row]) -> list[ImportError]:
        """Match rows to existing suppliers and check final-state uniqueness."""
        errors: list[ImportError] = []
        current = self.list_suppliers(owner_key)
        by_id = {s.id: s for s in current}
        by_key: dict[str, Supplier] = {}
        for s in current:
            by_key.setdefault(name_key(s.company_name), s)

        for row in rows:
            if row.id is not None:
                if row.id not in by_id:
                    errors.append(
                        ImportError(
                            row.line, "id", f"no supplier with id {row.id} for this owner"
                        )
                    )
                else:
                    row.target = row.id
            else:
                hit = by_key.get(name_key(row.company_name))
                row.target = hit.id if hit else None

        targets: dict[int, list[_Row]] = {}
        for row in rows:
            if row.target is not None:
                targets.setdefault(row.target, []).append(row)
        for group in targets.values():
            if len(group) > 1:
                for row in group:
                    col = "id" if row.id is not None else "company_name"
                    errors.append(
                        ImportError(
                            row.line, col, "multiple rows target the same supplier"
                        )
                    )

        # Uniqueness is judged on the final state, so name swaps succeed.
        final_keys: dict[str, list[tuple[str, _Row | None]]] = {}
        touched = {row.target for row in rows if row.target is not None}
        for s in current:
            if s.id not in touched:
                final_keys.setdefault(name_key(s.company_name), []).append(
                    (s.company_name, None)
                )
        for row in rows:
            if row.target is not None and len(targets.get(row.target, [])) > 1:
                continue  # already reported as conflicting target
            final_keys.setdefault(name_key(row.company_name), []).append(
                (row.company_name, row)
            )
        for key, sources in final_keys.items():
            if len(sources) > 1:
                for _, row in sources:
                    if row is not None:
                        errors.append(
                            ImportError(
                                row.line,
                                "company_name",
                                "duplicate company_name in final state",
                            )
                        )
        return errors

    def export_csv(self, owner_id) -> bytes:
        buf = io.StringIO(newline="")
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow(COLUMNS)
        for s in self.list_suppliers(owner_id):
            writer.writerow(
                [
                    s.id,
                    escape_cell(s.company_name),
                    escape_cell(s.contact_person),
                    escape_cell(s.phone),
                    escape_cell(s.street_address),
                    s.lead_time_days,
                    s.moq,
                    s.reliability,
                    escape_cell(encode_categories(s.categories)),
                ]
            )
        return buf.getvalue().encode("utf-8-sig")

    def list_suppliers(self, owner_id) -> list[Supplier]:
        rows = self._conn.execute(
            "SELECT id, owner_id, company_name, contact_person, phone,"
            " street_address, lead_time_days, moq, reliability, categories"
            " FROM suppliers WHERE owner_id = ? ORDER BY id",
            (str(owner_id),),
        ).fetchall()
        return [
            Supplier(
                id=row[0],
                owner_id=owner_id,
                company_name=row[2],
                contact_person=row[3],
                phone=row[4],
                street_address=row[5],
                lead_time_days=row[6],
                moq=row[7],
                reliability=row[8],
                categories=json.loads(row[9]),
            )
            for row in rows
        ]
