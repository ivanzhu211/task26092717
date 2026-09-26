import csv
import io
import random
import string
import unittest

from suppliers import (
    HEADERS,
    Supplier,
    SupplierDirectory,
    _DANGEROUS_START,
    name_key,
)


def make_csv(rows, headers=HEADERS, bom=False, eol="\r\n"):
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator=eol)
    w.writerow(headers)
    for row in rows:
        w.writerow(row)
    data = buf.getvalue().encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if bom else data


def supplier_row(s, headers=HEADERS):
    values = {
        "id": s.id,
        "company_name": s.company_name,
        "contact_person": s.contact_person,
        "phone": s.phone,
        "street_address": s.street_address,
        "lead_time_days": s.lead_time_days,
        "moq": s.moq,
        "reliability": s.reliability,
        "categories": None,  # filled by export normally; tests set explicitly
    }
    return [values[h] for h in headers]


def suppliers_equal_ignore_id(a, b):
    return (
        a.company_name == b.company_name
        and a.contact_person == b.contact_person
        and a.phone == b.phone
        and a.street_address == b.street_address
        and a.lead_time_days == b.lead_time_days
        and a.moq == b.moq
        and a.reliability == b.reliability
        and a.categories == b.categories
    )


NASTY_NAMES = [
    "=SUM(A1:A2)",
    "+1-800-FLOWERS",
    "-leading-dash",
    "@mention",
    "'apostrophe",
    "'=cmd|'/2",
    "''double",
    "  padded  ",
    "In\tterior\ttab",
    "Café",
    "Ｃａｆé Ｆｕｌｌｗｉｄｔｈ",
    "Comma, Co",
    'Quote "Co"',
    "Multi\nLine\nCo",
]

NASTY_CATEGORIES = [
    ["books, used", 'say "hi"', "line\nbreak", "semi;colon", " pipe|cat "],
    ["=", "+", "-", "@", "'"],
    ["tab\there", "cr\rhere"],
]


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.dir = SupplierDirectory()

    def tearDown(self):
        self.dir.close()

    def seed(self, owner, rows):
        report = self.dir.import_csv(owner, make_csv(rows))
        self.assertTrue(report.ok, report.errors)
        return report

    def test_roundtrip_zero_change_nasty(self):
        simple = []
        for i, name in enumerate(NASTY_NAMES):
            simple.append(
                ["", name, "C", f"212555{1000 + i}", "addr", 7, 100, 3, "cat" + str(i)]
            )
        self.seed("o", simple)
        before = self.dir.list_suppliers("o")
        exported = self.dir.export_csv("o")
        report = self.dir.import_csv("o", exported)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.created, 0)
        self.assertEqual(report.updated, 0)
        after = self.dir.list_suppliers("o")
        self.assertEqual(before, after)

    def test_roundtrip_without_id_column(self):
        rows = [
            ["", "Alpha", "Ann", "415-555-1000", "1 A St", 5, 10, 1, "a,b"],
            ["", "Beta", "", "+1 650 555 2000", "2 B St\nFl 2", 90, 99999, 5, ""],
        ]
        self.seed("src", rows)
        exported = self.dir.export_csv("src")
        # drop the id column
        table = list(csv.reader(io.StringIO(exported.decode("utf-8-sig"), newline="")))
        id_idx = table[0].index("id")
        stripped = [[c for j, c in enumerate(r) if j != id_idx] for r in table]
        buf = io.StringIO(newline="")
        csv.writer(buf, lineterminator="\r\n").writerows(stripped)
        report = self.dir.import_csv("dst", buf.getvalue().encode("utf-8"))
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.created, 2)
        src = self.dir.list_suppliers("src")
        dst = self.dir.list_suppliers("dst")
        self.assertEqual(len(src), len(dst))
        for a, b in zip(src, dst):
            self.assertTrue(suppliers_equal_ignore_id(a, b), (a, b))

    def test_export_has_no_dangerous_cells(self):
        rows = [
            ["", n, "=x", "2125551000", "- 1 St", 1, 1, 1, "c" + str(i)]
            for i, n in enumerate(NASTY_NAMES)
        ]
        self.seed("o", rows)
        exported = self.dir.export_csv("o")
        table = list(csv.reader(io.StringIO(exported.decode("utf-8-sig"), newline="")))
        for r in table[1:]:
            for cell in r:
                self.assertNotIn(cell[:1], _DANGEROUS_START, repr(cell))
        # and property 1 still holds with the escaping in place
        report = self.dir.import_csv("o", exported)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual((report.created, report.updated), (0, 0))

    def test_categories_with_separators_quotes_newlines(self):
        for i, cats in enumerate(NASTY_CATEGORIES):
            owner = f"cat{i}"
            cell = io.StringIO(newline="")
            csv.writer(cell, lineterminator="").writerow(cats)
            rows = [["", f"Co{i}", "", "2125551000", "a", 1, 1, 1, cell.getvalue()]]
            self.seed(owner, rows)
            got = self.dir.list_suppliers(owner)[0]
            want = [c.strip() for c in cats]
            self.assertEqual(got.categories, want)
            # round-trip through export keeps them intact
            report = self.dir.import_csv(owner, self.dir.export_csv(owner))
            self.assertTrue(report.ok, report.errors)
            self.assertEqual((report.created, report.updated), (0, 0))
            self.assertEqual(self.dir.list_suppliers(owner)[0].categories, want)


NAME_ALPHABET = (
    string.ascii_letters
    + string.digits
    + "     \t\n\r\"',;=+-@()&é中ｶﾌｪ"
)


def random_text(rng, max_len=24, alphabet=NAME_ALPHABET):
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, max_len)))


def random_phone(rng):
    area = rng.choice("23456789") + "".join(rng.choices(string.digits, k=2))
    exch = rng.choice("23456789") + "".join(rng.choices(string.digits, k=2))
    line = "".join(rng.choices(string.digits, k=4))
    core = area + exch + line
    fmt = rng.randrange(6)
    if fmt == 0:
        return core
    if fmt == 1:
        return f"({area}) {exch}-{line}"
    if fmt == 2:
        return f"{area}-{exch}-{line}"
    if fmt == 3:
        return f"+1 {area} {exch} {line}"
    if fmt == 4:
        return f"1-{area}-{exch}-{line}"
    return f"{area}.{exch}.{line}"


def random_categories(rng):
    count = rng.randint(0, 8)
    seen = set()
    cats = []
    for _ in range(count):
        entry = random_text(rng, max_len=12).strip()
        if not entry or entry.casefold() in seen:
            continue
        seen.add(entry.casefold())
        cats.append(entry)
    return cats


class FuzzRoundTripTests(unittest.TestCase):
    def test_fuzz_roundtrip(self):
        rng = random.Random(20260927)
        for trial in range(15):
            dir_ = SupplierDirectory()
            try:
                owner = f"fuzz{trial}"
                names = set()
                rows = []
                expected = []
                for _ in range(rng.randint(1, 40)):
                    name = random_text(rng).strip()
                    while not name or name_key(name) in names:
                        name = random_text(rng).strip()
                    names.add(name_key(name))
                    cats = random_categories(rng)
                    rows.append(
                        [
                            "",
                            name,
                            random_text(rng).strip(),
                            random_phone(rng),
                            random_text(rng, max_len=40).strip(),
                            rng.randint(1, 90),
                            rng.randint(1, 10**6),
                            rng.randint(1, 5),
                            None,  # categories filled below via export path
                        ]
                    )
                    expected.append(cats)
                # categories cells are embedded CSV rows
                for row, cats in zip(rows, expected):
                    cell = io.StringIO(newline="")
                    csv.writer(cell, lineterminator="").writerow(cats)
                    row[-1] = cell.getvalue()
                report = dir_.import_csv(owner, make_csv(rows))
                self.assertTrue(report.ok, report.errors)

                # property 1: import(export) is a zero-change no-op
                before = dir_.list_suppliers(owner)
                exported = dir_.export_csv(owner)
                report = dir_.import_csv(owner, exported)
                self.assertTrue(report.ok, report.errors)
                self.assertEqual((report.created, report.updated), (0, 0))
                self.assertEqual(dir_.list_suppliers(owner), before)

                # property 2: no exported cell starts with a dangerous char
                table = list(
                    csv.reader(io.StringIO(exported.decode("utf-8-sig"), newline=""))
                )
                for r in table[1:]:
                    for cell in r:
                        self.assertNotIn(cell[:1], _DANGEROUS_START, repr(cell))

                # dropping the id column and importing into an empty owner
                # yields identical records except for id
                id_idx = table[0].index("id")
                stripped = [
                    [c for j, c in enumerate(r) if j != id_idx] for r in table
                ]
                buf = io.StringIO(newline="")
                csv.writer(buf, lineterminator="\r\n").writerows(stripped)
                clone_owner = owner + "-clone"
                report = dir_.import_csv(clone_owner, buf.getvalue().encode())
                self.assertTrue(report.ok, report.errors)
                clones = dir_.list_suppliers(clone_owner)
                self.assertEqual(len(clones), len(before))
                for a, b in zip(before, clones):
                    self.assertTrue(suppliers_equal_ignore_id(a, b), (a, b))
            finally:
                dir_.close()


class ImportSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.dir = SupplierDirectory()

    def tearDown(self):
        self.dir.close()

    def seed(self, owner, rows):
        report = self.dir.import_csv(owner, make_csv(rows))
        self.assertTrue(report.ok, report.errors)
        return report

    def test_swap_names_in_one_file(self):
        self.seed(
            "o",
            [
                ["", "Alpha", "", "2125551000", "a", 1, 1, 1, "x"],
                ["", "Beta", "", "2125551001", "b", 2, 2, 2, "y"],
            ],
        )
        a, b = self.dir.list_suppliers("o")
        rows = [
            [a.id, "Beta", "", "2125551000", "a", 1, 1, 1, "x"],
            [b.id, "Alpha", "", "2125551001", "b", 2, 2, 2, "y"],
        ]
        report = self.dir.import_csv("o", make_csv(rows))
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.updated, 2)
        names = [s.company_name for s in self.dir.list_suppliers("o")]
        self.assertEqual(names, ["Beta", "Alpha"])

    def test_two_rows_same_id_both_error(self):
        self.seed("o", [["", "Alpha", "", "2125551000", "a", 1, 1, 1, "x"]])
        (a,) = self.dir.list_suppliers("o")
        rows = [
            [a.id, "One", "", "2125551000", "a", 1, 1, 1, "x"],
            [a.id, "Two", "", "2125551000", "a", 1, 1, 1, "x"],
        ]
        report = self.dir.import_csv("o", make_csv(rows))
        self.assertFalse(report.ok)
        self.assertEqual(len(report.errors), 2)
        self.assertEqual([e.line for e in report.errors], [2, 3])
        self.assertTrue(all(e.column == "company_name" for e in report.errors))
        # atomic: nothing changed
        self.assertEqual(self.dir.list_suppliers("o")[0].company_name, "Alpha")

    def test_id_row_and_name_row_same_supplier_both_error(self):
        self.seed("o", [["", "Alpha", "", "2125551000", "a", 1, 1, 1, "x"]])
        (a,) = self.dir.list_suppliers("o")
        rows = [
            [a.id, "Renamed", "", "2125551000", "a", 1, 1, 1, "x"],
            ["", "Alpha", "", "2125551000", "a", 1, 1, 1, "x"],
        ]
        report = self.dir.import_csv("o", make_csv(rows))
        self.assertFalse(report.ok)
        self.assertEqual(len(report.errors), 2)
        self.assertEqual([e.line for e in report.errors], [2, 3])

    def test_two_creates_same_name_both_error(self):
        rows = [
            ["", "Acme", "", "2125551000", "a", 1, 1, 1, "x"],
            ["", "  ACME ", "", "2125551001", "b", 2, 2, 2, "y"],
        ]
        report = self.dir.import_csv("o", make_csv(rows))
        self.assertFalse(report.ok)
        self.assertEqual(len(report.errors), 2)
        self.assertEqual([e.line for e in report.errors], [2, 3])
        self.assertEqual(self.dir.list_suppliers("o"), [])

    def test_rename_collides_with_untouched_supplier(self):
        self.seed(
            "o",
            [
                ["", "Alpha", "", "2125551000", "a", 1, 1, 1, "x"],
                ["", "Beta", "", "2125551001", "b", 2, 2, 2, "y"],
            ],
        )
        a, b = self.dir.list_suppliers("o")
        rows = [[a.id, " beta ", "", "2125551000", "a", 1, 1, 1, "x"]]
        report = self.dir.import_csv("o", make_csv(rows))
        self.assertFalse(report.ok)
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(report.errors[0].line, 2)
        self.assertEqual(report.errors[0].column, "company_name")

    def test_id_of_other_owner_rejected(self):
        self.seed("o1", [["", "Alpha", "", "2125551000", "a", 1, 1, 1, "x"]])
        (a,) = self.dir.list_suppliers("o1")
        rows = [[a.id, "Hijack", "", "2125551000", "a", 1, 1, 1, "x"]]
        report = self.dir.import_csv("o2", make_csv(rows))
        self.assertFalse(report.ok)
        self.assertEqual(report.errors[0].column, "id")
        self.assertEqual(self.dir.list_suppliers("o2"), [])

    def test_atomic_all_or_nothing(self):
        self.seed("o", [["", "Alpha", "", "2125551000", "a", 1, 1, 1, "x"]])
        before = self.dir.list_suppliers("o")
        rows = [
            ["", "NewCo", "", "2125551000", "a", 1, 1, 1, "x"],
            ["", "BadCo", "", "not-a-phone", "a", 1, 1, 1, "x"],
        ]
        report = self.dir.import_csv("o", make_csv(rows))
        self.assertFalse(report.ok)
        self.assertEqual(self.dir.list_suppliers("o"), before)

    def test_no_change_rows_not_counted_as_updated(self):
        self.seed("o", [["", "Alpha", "", "2125551000", "a", 1, 1, 1, "x"]])
        (a,) = self.dir.list_suppliers("o")
        rows = [
            [a.id, "Alpha", "", "(212) 555-1000", "a", 1, 1, 1, "x"],
            ["", "Brand New", "", "2125551001", "b", 2, 2, 2, "y"],
        ]
        report = self.dir.import_csv("o", make_csv(rows))
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.created, 1)
        self.assertEqual(report.updated, 0)

    def test_multiline_record_error_lines_and_columns(self):
        # header = physical line 1; record 1 spans lines 2-4 via a quoted
        # multi-line address; record 2 starts at line 5.
        data = (
            "id,company_name,contact_person,phone,street_address,"
            "lead_time_days,moq,reliability,categories\r\n"
            ',Good Co,Joe,212-555-1000,"10 Main St\r\n'
            "Suite 5\r\n"
            'Springfield",7,100,4,books\r\n'
            ",Bad Co,,garbage,1 Way,0,notanumber,9,,\r\n"
            ',Also Bad,,2125551002,"2 Ave\r\n'
            'Unit 3",91,5,6,x\r\n'
        ).encode()
        report = self.dir.import_csv("o", data)
        self.assertFalse(report.ok)
        got = [(e.line, e.column) for e in report.errors]
        self.assertEqual(
            got,
            [
                (5, "phone"),
                (5, "lead_time_days"),
                (5, "moq"),
                (5, "reliability"),
                (6, "lead_time_days"),
                (6, "reliability"),
            ],
        )
        self.assertEqual(self.dir.list_suppliers("o"), [])

    def test_blank_lines_not_counted_toward_limit(self):
        rows = [["", f"Co{i}", "", "2125551000", "a", 1, 1, 1, "x"] for i in range(500)]
        data = make_csv(rows)
        # sprinkle blank lines (also whitespace-only lines) throughout
        lines = data.decode().split("\r\n")
        with_blanks = []
        for i, line in enumerate(lines):
            with_blanks.append(line)
            if i % 7 == 0:
                with_blanks.append("")
                with_blanks.append("   ")
        data = "\r\n".join(with_blanks).encode()
        report = self.dir.import_csv("o", data)
        self.assertTrue(report.ok, report.errors[:3])
        self.assertEqual(report.created, 500)
        # 501st data row is rejected
        rows.append(["", "Co500", "", "2125551000", "a", 1, 1, 1, "x"])
        report = self.dir.import_csv("o2", make_csv(rows))
        self.assertFalse(report.ok)
        self.assertTrue(any("limit" in e.message for e in report.errors))

    def test_header_variants(self):
        # shuffled order, odd case, padded header names, BOM, LF endings
        headers = [
            " CATEGORIES ",
            "Reliability",
            "MOQ",
            "lead_time_days",
            "STREET_ADDRESS",
            "phone",
            "Contact_Person",
            "company_name ",
            "ID",
        ]
        raw = [["x", 1, 1, 1, "a", "2125551000", "Ann", "Alpha", ""]]
        data = make_csv(raw, headers=headers, bom=True, eol="\n")
        report = self.dir.import_csv("o", data)
        self.assertTrue(report.ok, report.errors)
        (s,) = self.dir.list_suppliers("o")
        self.assertEqual(s.company_name, "Alpha")
        self.assertEqual(s.categories, ["x"])
        # missing required column -> error naming the column at line 1
        bad = make_csv([["", "Alpha"]], headers=["id", "company_name"])
        report = self.dir.import_csv("o2", bad)
        self.assertFalse(report.ok)
        self.assertTrue(all(e.line == 1 for e in report.errors))
        self.assertIn("phone", {e.column for e in report.errors})

    def test_phone_formats(self):
        cases = {
            "(415) 555-1234": "(415) 555-1234",
            "415-555-1234": "(415) 555-1234",
            "415.555.1234": "(415) 555-1234",
            "415 555 1234": "(415) 555-1234",
            "+1 415 555 1234": "(415) 555-1234",
            "+1 (415) 555-1234": "(415) 555-1234",
            "1-415-555-1234": "(415) 555-1234",
            "4155551234": "(415) 555-1234",
        }
        rows = [
            ["", f"Co{i}", "", raw, "a", 1, 1, 1, "x"]
            for i, raw in enumerate(cases)
        ]
        self.seed("o", rows)
        for s, want in zip(self.dir.list_suppliers("o"), cases.values()):
            self.assertEqual(s.phone, want)
        for bad in ["123", "555-1234", "(115) 555-1234", "212-155-1234", "+2 415 555 1234", ""]:
            rows = [["", "Co", "", bad, "a", 1, 1, 1, "x"]]
            report = self.dir.import_csv("bad", make_csv(rows))
            self.assertFalse(report.ok, bad)
            self.assertEqual(report.errors[0].column, "phone")

    def test_name_uniqueness_normalization(self):
        self.seed(
            "o",
            [
                ["", "Café  Co", "", "2125551000", "a", 1, 1, 1, "x"],
                ["", "Other", "", "2125551001", "b", 2, 2, 2, "y"],
            ],
        )
        a, b = self.dir.list_suppliers("o")
        # NFC vs NFD, case, and whitespace differences all collide
        for variant in ["café co", "CAFÉCO", "Café Co", " café   co "]:
            rows = [[b.id, variant, "", "2125551001", "b", 2, 2, 2, "y"]]
            report = self.dir.import_csv("o", make_csv(rows))
            self.assertFalse(report.ok, variant)
        # a no-id row whose name matches after normalization is an update
        rows = [["", " café   co ", "", "2125551000", "a", 9, 1, 1, "x"]]
        report = self.dir.import_csv("o", make_csv(rows))
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.updated, 1)
        self.assertEqual(self.dir.list_suppliers("o")[0].company_name, "café   co")

    def test_categories_limit_and_dedup(self):
        cats = [f"cat{i}" for i in range(20)]
        cell = io.StringIO(newline="")
        csv.writer(cell, lineterminator="").writerow(cats)
        rows = [["", "Co", "", "2125551000", "a", 1, 1, 1, cell.getvalue()]]
        self.seed("o", rows)
        self.assertEqual(len(self.dir.list_suppliers("o")[0].categories), 20)
        # 21 distinct -> error
        cats21 = cats + ["cat20"]
        cell = io.StringIO(newline="")
        csv.writer(cell, lineterminator="").writerow(cats21)
        rows = [["", "Co2", "", "2125551000", "a", 1, 1, 1, cell.getvalue()]]
        report = self.dir.import_csv("o", make_csv(rows))
        self.assertFalse(report.ok)
        self.assertEqual(report.errors[0].column, "categories")
        # case-insensitive duplicates collapse, empties dropped
        cell = io.StringIO(newline="")
        csv.writer(cell, lineterminator="").writerow(["Books", "books", "BOOKS ", "", "toys"])
        rows = [["", "Co3", "", "2125551000", "a", 1, 1, 1, cell.getvalue()]]
        self.seed("o", rows)
        self.assertEqual(self.dir.list_suppliers("o")[1].categories, ["Books", "toys"])


if __name__ == "__main__":
    unittest.main()
