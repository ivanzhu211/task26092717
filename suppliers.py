"""Supplier directory: CSV import/export backed by SQLite (stdlib only)."""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass, field

HEADERS = [
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


# Characters that must never begin a cell in the exported file (formula
# injection guard).  Values starting with one of these, or with a leading
# apostrophe, get a single "'" prepended on export; import removes one
# leading "'" when it is followed by one of these characters or another "'".
_DANGEROUS_START = ("=", "+", "-", "@", "\t", "\r")


def _escape_cell(value: str) -> str:
    if value[:1] in _DANGEROUS_START or value[:1] == "'":
        return "'" + value
    return value


def _unescape_cell(value: str) -> str:
    if (
        len(value) >= 2
        and value[0] == "'"
        and (value[1] in _DANGEROUS_START or value[1] == "'")
    ):
        return value[1:]
    return value


def name_key(name: str) -> str:
    """Uniqueness key: NFKC, case-folded, all whitespace removed."""
    return "".join(unicodedata.normalize("NFKC", name).casefold().split())


def normalize_phone(raw: str) -> str | None:
    """Return '(NXX) NXX-XXXX' or None if raw is not a valid US number."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    if digits[0] not in "23456789" or digits[3] not in "23456789":
        return None
    return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"


def encode_categories(categories: list[str]) -> str:
    """Serialize categories into one cell as an embedded CSV row."""
    buf = io.StringIO(newline="")
    csv.writer(buf, lineterminator="").writerow(categories)
    return buf.getvalue()


def decode_categories(cell: str) -> list[str] | None:
    """Parse a categories cell; None if the embedded CSV is malformed."""
    if not cell.strip():
        return []
    try:
        rows = list(csv.reader(io.StringIO(cell, newline="")))
    except csv.Error:
        return None
    if not rows:
        return []
    return [item.strip() for item in rows[0]]


_INT_RE = re.compile(r"[+-]?\d+")


def _parse_int(text: str, lo: int, hi: int) -> int | None:
    if not _INT_RE.fullmatch(text):
        return None
    value = int(text)
    return value if lo <= value <= hi else None


@dataclass
class _ParsedRow:
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


def _parse_csv(data: bytes) -> tuple[list[_ParsedRow], list[CsvImportError]]:
    """Decode and validate every record; collect ALL errors."""
    errors: list[CsvImportError] = []
    rows: list[_ParsedRow] = []
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], [CsvImportError(1, "", "file is not valid UTF-8")]
    reader = csv.reader(io.StringIO(text, newline=""))

    header = next(reader, None)
    if header is None:
        return [], [CsvImportError(1, "", "empty file: missing header row")]
    col_index: dict[str, int] = {}
    for i, cell in enumerate(header):
        key = cell.strip().casefold()
        if key in HEADERS and key not in col_index:
            col_index[key] = i
    for name in HEADERS:
        if name != "id" and name not in col_index:
            errors.append(
                CsvImportError(1, name, f"missing required column '{name}'")
            )
    if errors:
        return [], errors

    def cell_of(record: list[str], name: str) -> str:
        idx = col_index.get(name)
        if idx is None or idx >= len(record):
            return ""
        return record[idx]

    data_rows = 0
    while True:
        start_line = reader.line_num + 1
        record = next(reader, None)
        if record is None:
            break
        if not any(cell.strip() for cell in record):
            continue  # blank line: not counted, not a record
        data_rows += 1
        if data_rows > MAX_ROWS:
            errors.append(
                CsvImportError(
                    start_line, "", f"too many data rows (limit {MAX_ROWS})"
                )
            )
            break

        row_errors_before = len(errors)

        id_text = cell_of(record, "id").strip()
        row_id: int | None = None
        if id_text:
            if _INT_RE.fullmatch(id_text):
                row_id = int(id_text)
            else:
                errors.append(
                    CsvImportError(start_line, "id", f"invalid id '{id_text}'")
                )

        company_name = _unescape_cell(cell_of(record, "company_name")).strip()
        if not company_name:
            errors.append(
                CsvImportError(start_line, "company_name", "company_name is required")
            )

        contact_person = _unescape_cell(cell_of(record, "contact_person")).strip()

        phone_text = _unescape_cell(cell_of(record, "phone")).strip()
        phone = normalize_phone(phone_text)
        if phone is None:
            errors.append(
                CsvImportError(
                    start_line, "phone", f"invalid US phone number '{phone_text}'"
                )
            )
            phone = ""

        street_address = _unescape_cell(
            cell_of(record, "street_address")
        ).strip()

        lead_text = cell_of(record, "lead_time_days").strip()
        lead_time = _parse_int(lead_text, 1, 90)
        if lead_time is None:
            errors.append(
                CsvImportError(
                    start_line,
                    "lead_time_days",
                    f"lead_time_days must be an integer in 1..90, got '{lead_text}'",
                )
            )
            lead_time = 0

        moq_text = cell_of(record, "moq").strip()
        moq = _parse_int(moq_text, 1, 2**63 - 1)
        if moq is None:
            errors.append(
                CsvImportError(
                    start_line, "moq", f"moq must be a positive integer, got '{moq_text}'"
                )
            )
            moq = 0

        rel_text = cell_of(record, "reliability").strip()
        reliability = _parse_int(rel_text, 1, 5)
        if reliability is None:
            errors.append(
                CsvImportError(
                    start_line,
                    "reliability",
                    f"reliability must be an integer in 1..5, got '{rel_text}'",
                )
            )
            reliability = 0

        cat_cell = _unescape_cell(cell_of(record, "categories"))
        decoded = decode_categories(cat_cell)
        if decoded is None:
            errors.append(
                CsvImportError(start_line, "categories", "malformed categories cell")
            )
            categories: list[str] = []
        else:
            categories = []
            seen: set[str] = set()
            for item in decoded:
                if not item:
                    continue
                key = item.casefold()
                if key not in seen:
                    seen.add(key)
                    categories.append(item)
            if len(categories) > MAX_CATEGORIES:
                errors.append(
                    CsvImportError(
                        start_line,
                        "categories",
                        f"too many categories (limit {MAX_CATEGORIES})",
                    )
                )

        if len(errors) == row_errors_before:
            rows.append(
                _ParsedRow(
                    line=start_line,
                    id=row_id,
                    company_name=company_name,
                    contact_person=contact_person,
                    phone=phone,
                    street_address=street_address,
                    lead_time_days=lead_time,
                    moq=moq,
                    reliability=reliability,
                    categories=categories,
                )
            )
    return rows, errors


@dataclass
class Supplier:
    id: int
    owner_id: str
    company_name: str
    contact_person: str
    phone: str
    street_address: str
    lead_time_days: int
    moq: int
    reliability: int
    categories: list[str] = field(default_factory=list)


@dataclass
class CsvImportError:
    line: int  # physical line where the record starts (header = line 1)
    column: str
    message: str


@dataclass
class Report:
    ok: bool
    errors: list[CsvImportError] = field(default_factory=list)
    created: int = 0
    updated: int = 0


class SupplierDirectory:
    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS suppliers (
                id INTEGER PRIMARY KEY,
                owner_id TEXT NOT NULL,
                company_name TEXT NOT NULL,
                name_key TEXT NOT NULL,
                contact_person TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL,
                street_address TEXT NOT NULL DEFAULT '',
                lead_time_days INTEGER NOT NULL,
                moq INTEGER NOT NULL,
                reliability INTEGER NOT NULL,
                categories TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_suppliers_owner "
            "ON suppliers (owner_id, name_key)"
        )
        self._conn.commit()
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row_to_supplier(row: sqlite3.Row | tuple) -> Supplier:
        return Supplier(
            id=row[0],
            owner_id=row[1],
            company_name=row[2],
            contact_person=row[3],
            phone=row[4],
            street_address=row[5],
            lead_time_days=row[6],
            moq=row[7],
            reliability=row[8],
            categories=json.loads(row[9]),
        )

    def list_suppliers(self, owner_id: str) -> list[Supplier]:
        cur = self._conn.execute(
            "SELECT id, owner_id, company_name, contact_person, phone,"
            " street_address, lead_time_days, moq, reliability, categories"
            " FROM suppliers WHERE owner_id = ? ORDER BY id",
            (owner_id,),
        )
        return [self._row_to_supplier(row) for row in cur.fetchall()]

    def import_csv(self, owner_id: str, data: bytes) -> Report:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                report = self._import_locked(owner_id, data)
                if report.ok:
                    self._conn.commit()
                else:
                    self._conn.rollback()
                return report
            except Exception:
                self._conn.rollback()
                raise

    def _import_locked(self, owner_id: str, data: bytes) -> Report:
        rows, errors = _parse_csv(data)

        existing = self.list_suppliers(owner_id)
        by_id = {s.id: s for s in existing}
        by_key = {name_key(s.company_name): s for s in existing}

        # Resolve each parsed row to its target supplier (or a create).
        targets: list[int | None] = []  # supplier id per row, None = create
        unresolved: set[int] = set()  # row indexes with a bad id
        for row in rows:
            target: int | None = None
            if row.id is not None:
                if row.id not in by_id:
                    errors.append(
                        CsvImportError(
                            row.line,
                            "id",
                            f"id {row.id} does not belong to this owner",
                        )
                    )
                    targets.append(None)
                    unresolved.add(len(targets) - 1)
                    continue
                target = row.id
            else:
                hit = by_key.get(name_key(row.company_name))
                if hit is not None:
                    target = hit.id
            targets.append(target)

        # Two rows pointing at the same supplier: both are errors.
        target_lines: dict[int, list[int]] = {}
        for row, target in zip(rows, targets):
            if target is not None:
                target_lines.setdefault(target, []).append(row.line)
        for lines in target_lines.values():
            if len(lines) > 1:
                for line in lines:
                    errors.append(
                        CsvImportError(
                            line,
                            "company_name",
                            "multiple rows in this file target the same supplier",
                        )
                    )

        # Uniqueness of company_name is judged on the final state.
        final_key_by_id = {s.id: name_key(s.company_name) for s in existing}
        for i, (row, target) in enumerate(zip(rows, targets)):
            if i in unresolved:
                continue
            if target is not None and target_lines.get(target, []) == [row.line]:
                final_key_by_id[target] = name_key(row.company_name)
        key_owners: dict[str, list[int]] = {}
        for sid, key in final_key_by_id.items():
            key_owners.setdefault(key, []).append(sid)
        for i, (row, target) in enumerate(zip(rows, targets)):
            if i in unresolved:
                continue
            key = name_key(row.company_name)
            holders = key_owners.get(key, [])
            if target is None:
                # A create must not collide with any final name.
                if holders:
                    errors.append(
                        CsvImportError(
                            row.line,
                            "company_name",
                            f"duplicate company_name '{row.company_name}'",
                        )
                    )
            elif len(holders) > 1 or (len(holders) == 1 and holders[0] != target):
                errors.append(
                    CsvImportError(
                        row.line,
                        "company_name",
                        f"duplicate company_name '{row.company_name}'",
                    )
                )
        # Two creates with the same final name collide with each other.
        create_key_lines: dict[str, list[int]] = {}
        for i, (row, target) in enumerate(zip(rows, targets)):
            if i in unresolved:
                continue
            if target is None:
                create_key_lines.setdefault(name_key(row.company_name), []).append(
                    row.line
                )
        for lines in create_key_lines.values():
            if len(lines) > 1:
                for line in lines:
                    errors.append(
                        CsvImportError(
                            line,
                            "company_name",
                            "multiple rows in this file create the same company_name",
                        )
                    )

        if errors:
            errors.sort(
                key=lambda e: (
                    e.line,
                    HEADERS.index(e.column) if e.column in HEADERS else -1,
                )
            )
            return Report(ok=False, errors=errors)

        created = 0
        updated = 0
        for row, target in zip(rows, targets):
            if target is None:
                self._conn.execute(
                    "INSERT INTO suppliers (owner_id, company_name, name_key,"
                    " contact_person, phone, street_address, lead_time_days,"
                    " moq, reliability, categories)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        owner_id,
                        row.company_name,
                        name_key(row.company_name),
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
                current = by_id[target]
                changed = (
                    current.company_name != row.company_name
                    or current.contact_person != row.contact_person
                    or current.phone != row.phone
                    or current.street_address != row.street_address
                    or current.lead_time_days != row.lead_time_days
                    or current.moq != row.moq
                    or current.reliability != row.reliability
                    or current.categories != row.categories
                )
                if changed:
                    self._conn.execute(
                        "UPDATE suppliers SET company_name = ?, name_key = ?,"
                        " contact_person = ?, phone = ?, street_address = ?,"
                        " lead_time_days = ?, moq = ?, reliability = ?,"
                        " categories = ? WHERE id = ?",
                        (
                            row.company_name,
                            name_key(row.company_name),
                            row.contact_person,
                            row.phone,
                            row.street_address,
                            row.lead_time_days,
                            row.moq,
                            row.reliability,
                            json.dumps(row.categories, ensure_ascii=False),
                            target,
                        ),
                    )
                    updated += 1
        return Report(ok=True, created=created, updated=updated)

    def export_csv(self, owner_id: str) -> bytes:
        buf = io.StringIO(newline="")
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow(HEADERS)
        for s in self.list_suppliers(owner_id):
            writer.writerow(
                [
                    s.id,
                    _escape_cell(s.company_name),
                    _escape_cell(s.contact_person),
                    _escape_cell(s.phone),
                    _escape_cell(s.street_address),
                    s.lead_time_days,
                    s.moq,
                    s.reliability,
                    _escape_cell(encode_categories(s.categories)),
                ]
            )
        return buf.getvalue().encode("utf-8-sig")
