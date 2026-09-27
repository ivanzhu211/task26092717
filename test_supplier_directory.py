import csv
import io
import random
import unittest

from supplier_directory import (
    COLUMNS,
    SupplierDirectory,
    escape_cell,
    name_key,
)

DANGEROUS = ("=", "+", "-", "@", "\t", "\r")

NASTY_NAMES = [
    "'Quoted",
    "'=cmd",
    "'",
    "=SUM(A1:A2)",
    "+1-800-HACK",
    "-2-3",
    "@mention",
    "  Padded  Name ",
    "Ｆｕｌｌｗｉｄｔｈ　Ｃｏ",
    "Café Société",
    "Multi\nLine\nName",
]

NASTY_CATEGORIES = [
    "semi;colon",
    'quo"te',
    "new\nline",
    "back\\slash",
    "'apostrophe",
    "=formula",
    "trailing\\",
    "semi\\;escaped",
    "CR\rlf",
]


def make_supplier_rows(rng, n, used_keys):
    rows = []
    while len(rows) < n:
        name = rng.choice(
            NASTY_NAMES
            + [
                f"Co {rng.randrange(10**6)}",
                f"  Spaced   {rng.randrange(10**6)}  ",
                f"'{rng.randrange(10**6)}",
                f"={rng.randrange(10**6)}",
            ]
        )
        if not name.strip() or name_key(name.strip()) in used_keys:
            continue
        used_keys.add(name_key(name.strip()))
        area = rng.randrange(200, 1000)
        exch = rng.randrange(200, 1000)
        line = rng.randrange(10000)
        cats = []
        seen = set()
        for _ in range(rng.randrange(0, 8)):
            c = rng.choice(NASTY_CATEGORIES + [f"cat{rng.randrange(10**4)}"])
            if c.strip() and c.strip().casefold() not in seen:
                seen.add(c.strip().casefold())
                cats.append(c.strip())
        rows.append(
            {
                "company_name": name.strip(),
                "contact_person": rng.choice(["Jane", "'O'Brien", "=Bob", ""]),
                "phone": f"({area}) {exch}-{line:04d}",
                "street_address": rng.choice(
                    ["1 Main St", "10 Oak Ave\nSuite 3", "'5 Quote Rd", "=7 Eq Blvd"]
                ),
                "lead_time_days": rng.randrange(1, 91),
                "moq": rng.randrange(1, 10000),
                "reliability": rng.randrange(1, 6),
                "categories": cats,
            }
        )
    return rows


def rows_to_csv(rows, with_id=False, ids=None):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    cols = [c for c in COLUMNS if with_id or c != "id"]
    writer.writerow(cols)
    for i, r in enumerate(rows):
        from supplier_directory import encode_categories

        row = []
        for c in cols:
            if c == "id":
                row.append(ids[i] if ids else "")
            elif c == "categories":
                row.append(escape_cell(encode_categories(r["categories"])))
            elif c in ("lead_time_days", "moq", "reliability"):
                row.append(r[c])
            else:
                row.append(escape_cell(r[c]))
        writer.writerow(row)
    return buf.getvalue().encode("utf-8-sig")


def strip_id_column(data: bytes) -> bytes:
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"), newline="")))
    idx = rows[0].index("id")
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    for row in rows:
        writer.writerow([c for i, c in enumerate(row) if i != idx])
    return buf.getvalue().encode("utf-8-sig")


def field_tuple(s):
    return (
        s.company_name,
        s.contact_person,
        s.phone,
        s.street_address,
        s.lead_time_days,
        s.moq,
        s.reliability,
        list(s.categories),
    )


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self.dir = SupplierDirectory(":memory:")
        self.rng = random.Random(20260927)

    def tearDown(self):
        self.dir.close()

    def test_roundtrip_zero_change_random(self):
        rows = make_supplier_rows(self.rng, 60, set())
        report = self.dir.import_csv("o", rows_to_csv(rows))
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.created, 60)
        exported = self.dir.export_csv("o")
        again = self.dir.import_csv("o", exported)
        self.assertTrue(again.ok, again.errors)
        self.assertEqual((again.created, again.updated), (0, 0))
        self.assertEqual(self.dir.export_csv("o"), exported)

    def test_reimport_without_id_into_empty_owner(self):
        rows = make_supplier_rows(self.rng, 40, set())
        self.assertTrue(self.dir.import_csv("o1", rows_to_csv(rows)).ok)
        stripped = strip_id_column(self.dir.export_csv("o1"))
        report = self.dir.import_csv("o2", stripped)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.created, 40)
        left = self.dir.list_suppliers("o1")
        right = self.dir.list_suppliers("o2")
        self.assertEqual(len(left), len(right))
        for a, b in zip(left, right):
            self.assertEqual(field_tuple(a), field_tuple(b))

    def test_export_has_no_dangerous_cells(self):
        rows = make_supplier_rows(self.rng, 60, set())
        self.assertTrue(self.dir.import_csv("o", rows_to_csv(rows)).ok)
        exported = self.dir.export_csv("o")
        parsed = list(csv.reader(io.StringIO(exported.decode("utf-8-sig"))))
        for line_no, row in enumerate(parsed[1:], start=2):
            for cell in row:
                self.assertFalse(
                    cell.startswith(DANGEROUS),
                    f"line {line_no}: dangerous cell {cell!r}",
                )
        # requirement 1 still holds with escaping in play
        again = self.dir.import_csv("o", exported)
        self.assertTrue(again.ok, again.errors)
        self.assertEqual((again.created, again.updated), (0, 0))

    def test_dangerous_and_quote_prefixed_names_roundtrip(self):
        rows = []
        used = set()
        for name in NASTY_NAMES:
            if not name.strip() or name_key(name.strip()) in used:
                continue
            used.add(name_key(name.strip()))
            rows.append(
                {
                    "company_name": name.strip(),
                    "contact_person": "x",
                    "phone": "(415) 555-0132",
                    "street_address": "1 Main St",
                    "lead_time_days": 5,
                    "moq": 10,
                    "reliability": 3,
                    "categories": NASTY_CATEGORIES[:5],
                }
            )
        report = self.dir.import_csv("o", rows_to_csv(rows))
        self.assertTrue(report.ok, report.errors)
        exported = self.dir.export_csv("o")
        again = self.dir.import_csv("o", exported)
        self.assertTrue(again.ok, again.errors)
        self.assertEqual((again.created, again.updated), (0, 0))
        stored = {s.company_name: s for s in self.dir.list_suppliers("o")}
        for r in rows:
            self.assertIn(r["company_name"], stored)
            self.assertEqual(stored[r["company_name"]].categories, r["categories"])

    def test_multi_cycle_fuzz(self):
        rows = make_supplier_rows(self.rng, 120, set())
        self.assertTrue(self.dir.import_csv("o", rows_to_csv(rows)).ok)
        snapshot = [field_tuple(s) for s in self.dir.list_suppliers("o")]
        exported = self.dir.export_csv("o")
        for _ in range(5):
            report = self.dir.import_csv("o", exported)
            self.assertTrue(report.ok, report.errors)
            self.assertEqual((report.created, report.updated), (0, 0))
            self.assertEqual(self.dir.export_csv("o"), exported)
        self.assertEqual(
            [field_tuple(s) for s in self.dir.list_suppliers("o")], snapshot
        )


class SwapAndConflictTest(unittest.TestCase):
    def setUp(self):
        self.dir = SupplierDirectory(":memory:")
        csv_data = (
            b"id,company_name,contact_person,phone,street_address,"
            b"lead_time_days,moq,reliability,categories\n"
            b",Alpha,A,415-555-0132,1 A St,7,100,4,Tools\n"
            b",Beta,B,212-555-0147,2 B St,14,50,5,Parts\n"
        )
        report = self.dir.import_csv("o", csv_data)
        self.assertTrue(report.ok, report.errors)
        self.ids = {s.company_name: s.id for s in self.dir.list_suppliers("o")}

    def tearDown(self):
        self.dir.close()

    def _csv(self, lines):
        header = (
            "id,company_name,contact_person,phone,street_address,"
            "lead_time_days,moq,reliability,categories\n"
        )
        return (header + "\n".join(lines) + "\n").encode()

    def test_name_swap_succeeds(self):
        a, b = self.ids["Alpha"], self.ids["Beta"]
        report = self.dir.import_csv(
            "o",
            self._csv(
                [
                    f"{a},Beta,A,415-555-0132,1 A St,7,100,4,Tools",
                    f"{b},Alpha,B,212-555-0147,2 B St,14,50,5,Parts",
                ]
            ),
        )
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.updated, 2)
        names = {s.id: s.company_name for s in self.dir.list_suppliers("o")}
        self.assertEqual(names[a], "Beta")
        self.assertEqual(names[b], "Alpha")

    def test_final_duplicate_names_all_rows_error(self):
        a, b = self.ids["Alpha"], self.ids["Beta"]
        report = self.dir.import_csv(
            "o",
            self._csv(
                [
                    f"{a},Same,A,415-555-0132,1 A St,7,100,4,Tools",
                    f"{b}, SAME ,B,212-555-0147,2 B St,14,50,5,Parts",
                ]
            ),
        )
        self.assertFalse(report.ok)
        self.assertEqual(len(report.errors), 2)
        self.assertTrue(all(e.column == "company_name" for e in report.errors))
        names = sorted(s.company_name for s in self.dir.list_suppliers("o"))
        self.assertEqual(names, ["Alpha", "Beta"])

    def test_two_rows_same_target_both_error(self):
        a = self.ids["Alpha"]
        report = self.dir.import_csv(
            "o",
            self._csv(
                [
                    f"{a},X,A,415-555-0132,1 A St,7,100,4,Tools",
                    f"{a},Y,B,212-555-0147,2 B St,14,50,5,Parts",
                ]
            ),
        )
        self.assertFalse(report.ok)
        self.assertEqual(len(report.errors), 2)
        self.assertTrue(all(e.column == "id" for e in report.errors))

    def test_two_new_rows_same_name_both_error(self):
        report = self.dir.import_csv(
            "o",
            self._csv(
                [
                    ",Gamma,A,415-555-0132,1 A St,7,100,4,Tools",
                    ", GAMMA,B,212-555-0147,2 B St,14,50,5,Parts",
                ]
            ),
        )
        self.assertFalse(report.ok)
        self.assertEqual(len(report.errors), 2)
        self.assertEqual(len(self.dir.list_suppliers("o")), 2)

    def test_id_of_other_owner_rejected(self):
        a = self.ids["Alpha"]
        report = self.dir.import_csv(
            "other",
            self._csv([f"{a},X,A,415-555-0132,1 A St,7,100,4,Tools"]),
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.errors[0].column, "id")
        self.assertEqual(self.dir.list_suppliers("other"), [])

    def test_rename_into_untouched_supplier_name_rejected(self):
        a = self.ids["Alpha"]
        report = self.dir.import_csv(
            "o",
            self._csv([f"{a}, beta ,A,415-555-0132,1 A St,7,100,4,Tools"]),
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.errors[0].column, "company_name")
        names = sorted(s.company_name for s in self.dir.list_suppliers("o"))
        self.assertEqual(names, ["Alpha", "Beta"])


class ErrorReportTest(unittest.TestCase):
    def setUp(self):
        self.dir = SupplierDirectory(":memory:")

    def tearDown(self):
        self.dir.close()

    def test_physical_line_numbers_and_columns(self):
        data = (
            "id,company_name,contact_person,phone,street_address,"
            "lead_time_days,moq,reliability,categories\r\n"
            ',Ok,Jo,415-555-0132,"10 Pine Rd\r\nSuite 9",7,100,4,Tools\r\n'
            "5,Bad1,Jo,123,Addr,0,100,4,Tools\r\n"
            ",Bad2,Jo,415-555-0132,Addr,91,-3,9,Tools\r\n"
            "abc,Bad3,Jo,415-555-0132,Addr,7,100,4,Tools\r\n"
        ).encode()
        report = self.dir.import_csv("o", data)
        self.assertFalse(report.ok)
        got = [(e.line, e.column) for e in report.errors]
        self.assertEqual(
            got,
            [
                (4, "lead_time_days"),
                (4, "phone"),
                (5, "lead_time_days"),
                (5, "moq"),
                (5, "reliability"),
                (6, "id"),
            ],
        )
        # atomic: nothing was written
        self.assertEqual(self.dir.list_suppliers("o"), [])

    def test_too_many_categories(self):
        cats = "; ".join(f"c{i}" for i in range(21))
        data = (
            "id,company_name,contact_person,phone,street_address,"
            "lead_time_days,moq,reliability,categories\n"
            f",A,Jo,415-555-0132,Addr,7,100,4,{cats}\n"
        ).encode()
        report = self.dir.import_csv("o", data)
        self.assertFalse(report.ok)
        self.assertEqual([(e.line, e.column) for e in report.errors], [(2, "categories")])

    def test_row_limit_ignores_blank_lines(self):
        header = (
            "id,company_name,contact_person,phone,street_address,"
            "lead_time_days,moq,reliability,categories\n"
        )
        lines = [
            f",Co{i},Jo,415-555-0132,Addr,7,100,4,Tools\n\n" for i in range(500)
        ]
        ok = self.dir.import_csv("o", (header + "".join(lines)).encode())
        self.assertTrue(ok.ok, ok.errors)
        over = self.dir.import_csv(
            "o2", (header + "".join(lines) + ",Co500,Jo,415-555-0132,Addr,7,100,4,T\n").encode()
        )
        self.assertFalse(over.ok)
        self.assertEqual(over.errors[0].column, "")
        self.assertEqual(self.dir.list_suppliers("o2"), [])


class HeaderToleranceTest(unittest.TestCase):
    def setUp(self):
        self.dir = SupplierDirectory(":memory:")

    def tearDown(self):
        self.dir.close()

    def test_bom_crlf_shuffled_messy_header(self):
        text = (
            "\ufeff CATEGORIES , Phone ,MOQ,company_NAME,reliability,"
            "street_address,lead_time_days,contact_person\r\n"
            '"Tools; Parts","+1 (415) 555-0132",100,"Acme",4,'
            '"1 Main St",7,Jane\r\n'
        )
        report = self.dir.import_csv("o", text.encode("utf-8"))
        self.assertTrue(report.ok, report.errors)
        (s,) = self.dir.list_suppliers("o")
        self.assertEqual(s.company_name, "Acme")
        self.assertEqual(s.phone, "(415) 555-0132")
        self.assertEqual(s.categories, ["Tools", "Parts"])

    def test_update_only_counts_real_changes(self):
        base = (
            b"id,company_name,contact_person,phone,street_address,"
            b"lead_time_days,moq,reliability,categories\n"
            b",Acme,Jo,415-555-0132,Addr,7,100,4,Tools\n"
        )
        self.assertTrue(self.dir.import_csv("o", base).ok)
        same = self.dir.import_csv("o", base)
        self.assertTrue(same.ok)
        self.assertEqual((same.created, same.updated), (0, 0))
        changed = self.dir.import_csv(
            "o",
            base.replace(b"Jo", b"Joanna").replace(b",Acme,", b",acme,"),
        )
        self.assertTrue(changed.ok, changed.errors)
        self.assertEqual((changed.created, changed.updated), (0, 1))


if __name__ == "__main__":
    unittest.main()
