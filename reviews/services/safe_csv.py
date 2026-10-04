"""
CSV export without formula injection.

Spreadsheet apps run any cell that starts with = + - @ (or tab / carriage
return) as a formula. Review text is written by the public, so a review like
=HYPERLINK("http://evil.example","Click") would turn into a live formula when
the owner opens the export in Excel or Google Sheets. Such cells get a leading
apostrophe, which spreadsheets show as plain text. Numbers are left as numbers.
"""
import csv

FORMULA_START = ('=', '+', '-', '@', '\t', '\r', '＝', '＋', '－', '＠')


def clean_cell(value):
    if isinstance(value, str) and value.lstrip(' ').startswith(FORMULA_START):
        return "'" + value
    return value


class SafeWriter:
    def __init__(self, stream):
        self._writer = csv.writer(stream)

    def writerow(self, row):
        return self._writer.writerow([clean_cell(v) for v in row])

    def writerows(self, rows):
        for row in rows:
            self.writerow(row)


def safe_csv_writer(stream):
    return SafeWriter(stream)
