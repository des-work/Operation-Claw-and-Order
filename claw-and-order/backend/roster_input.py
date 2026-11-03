"""Bounded CSV/XLSX roster readers. Cells are data, never executable instructions."""

import csv
import io
import re
import zipfile

from backend.enrollment_policy import EnrollmentError, identifier

FIELDS = ("student_id", "email", "display_name", "term_id", "class_id", "team_id", "role_key", "idea", "status")
REQUIRED = FIELDS[:5]
MAX_ROWS = 5000
MAX_BYTES = 10 * 1024 * 1024


def read_roster(content: bytes, filename: str, column_map=None):
    if len(content) > MAX_BYTES:
        raise EnrollmentError("Roster upload exceeds 10 MiB", 413)
    mapping = column_map or {}
    if not isinstance(mapping, dict) or any(v not in FIELDS and v is not None for v in mapping.values()):
        raise EnrollmentError("Column map must map source headers to roster fields or null to ignore")
    try:
        if filename.lower().endswith(".csv"):
            text = content.decode("utf-8-sig")
            if "\0" in text:
                raise EnrollmentError("CSV contains null bytes")
            matrix = []
            for values in csv.reader(io.StringIO(text)):
                if len(matrix) >= MAX_ROWS + 1 or len(values) > 40:
                    raise EnrollmentError("Roster exceeds 5,000 rows or 40 columns", 413)
                matrix.append(values)
        elif filename.lower().endswith(".xlsx"):
            from openpyxl import load_workbook
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if len(archive.infolist()) > 300 or sum(f.file_size for f in archive.infolist()) > 32 * 1024 * 1024:
                    raise EnrollmentError("XLSX expanded contents exceed the import limit", 413)
            book = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
            try:
                if "Roster" not in book.sheetnames:
                    raise EnrollmentError("XLSX must contain a sheet named Roster")
                sheet = book["Roster"]
                if sheet.max_row and sheet.max_row > MAX_ROWS + 1 or sheet.max_column and sheet.max_column > 40:
                    raise EnrollmentError("Roster exceeds 5,000 rows or 40 columns", 413)
                sheet.reset_dimensions()
                matrix = []
                for cells in sheet.iter_rows():
                    if len(matrix) >= MAX_ROWS + 1 or len(cells) > 40:
                        raise EnrollmentError("Roster exceeds 5,000 rows or 40 columns", 413)
                    if any(cell.data_type == "f" for cell in cells):
                        raise EnrollmentError("Roster formulas are not accepted; provide plain values")
                    matrix.append([cell.value for cell in cells])
            finally:
                book.close()
        else:
            raise EnrollmentError("Use UTF-8 CSV or .xlsx; convert legacy .xls files first")
    except EnrollmentError:
        raise
    except (ValueError, UnicodeError, csv.Error, zipfile.BadZipFile, OSError):
        raise EnrollmentError("Roster file could not be read; provide a valid CSV or XLSX") from None
    except Exception:
        raise EnrollmentError("Workbook XML could not be read safely; export a clean XLSX or CSV") from None
    if not matrix or len(matrix) > MAX_ROWS + 1 or len(matrix[0]) > 40:
        raise EnrollmentError("Roster must have a header and at most 5,000 student rows", 413)
    headers = []
    for original in matrix[0]:
        source = str(original or "").strip()
        field = mapping.get(source, source.lower().replace(" ", "_"))
        if field is not None and field not in FIELDS:
            raise EnrollmentError("Unknown roster column; use the template or an explicit column map")
        if field and field in headers:
            raise EnrollmentError("Duplicate roster column")
        headers.append(field)
    if not set(REQUIRED).issubset(headers):
        raise EnrollmentError("Required columns: " + ", ".join(REQUIRED))
    records = []
    for number, values in enumerate(matrix[1:], 2):
        if not any(v is not None and str(v).strip() for v in values):
            continue
        if len(values) > len(headers) and any(v is not None and str(v).strip() for v in values[len(headers):]):
            raise EnrollmentError(f"Row {number}: values exceed the header width")
        row = {field: values[i] if i < len(values) else "" for i, field in enumerate(headers) if field}
        row["_row"] = number
        records.append(row)
    return records


def normalize_rows(records, allowed_domains=()):
    if not isinstance(records, list) or not records or len(records) > MAX_ROWS:
        raise EnrollmentError("Provide 1–5,000 roster rows")
    result, errors, enrollments, identities, emails = [], [], set(), {}, {}
    for position, raw in enumerate(records, 2):
        number = raw.get("_row", position) if isinstance(raw, dict) else position
        try:
            if not isinstance(raw, dict) or set(raw) - set(FIELDS) - {"_row"}:
                raise EnrollmentError("Unknown roster fields")
            row = {}
            for field in FIELDS:
                value = raw.get(field, "")
                if value is None:
                    value = ""
                if not isinstance(value, str):
                    raise EnrollmentError(f"{field} must be stored as text, including identifiers")
                value = value.strip()
                if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
                    raise EnrollmentError(f"{field} contains control characters")
                row[field] = value
            for field in ("student_id", "term_id", "class_id"):
                if not identifier(row[field]):
                    raise EnrollmentError(f"Invalid {field}; use 1–64 letters, numbers, _ or -")
            for field in ("team_id", "role_key"):
                if row[field] and not identifier(row[field]):
                    raise EnrollmentError(f"Invalid {field}")
            row["email"] = row["email"].casefold()
            if len(row["email"]) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", row["email"]):
                raise EnrollmentError("Invalid email")
            if allowed_domains and row["email"].split("@", 1)[1] not in allowed_domains:
                raise EnrollmentError("Email domain is outside the configured college domains")
            if not row["display_name"] or len(row["display_name"]) > 120 or len(row["idea"]) > 2000:
                raise EnrollmentError("display_name is required (120 characters maximum); idea is limited to 2,000 characters")
            row["status"] = row["status"] or "active"
            if row["status"] not in ("active", "withdrawn"):
                raise EnrollmentError("status must be active or withdrawn")
            key = (row["student_id"], row["term_id"], row["class_id"])
            if key in enrollments:
                raise EnrollmentError("Duplicate student enrollment for this term/class")
            identity = (row["email"], row["display_name"])
            if row["student_id"] in identities and identities[row["student_id"]] != identity:
                raise EnrollmentError("Conflicting identity fields for the same student")
            if row["email"] in emails and emails[row["email"]] != row["student_id"]:
                raise EnrollmentError("The same email is assigned to different student IDs")
            enrollments.add(key)
            identities[row["student_id"]] = identity
            emails[row["email"]] = row["student_id"]
            result.append(row)
        except EnrollmentError as error:
            errors.append({"row": number, "error": str(error)})
    if errors:
        raise EnrollmentError("Roster validation failed: " + "; ".join(f"row {e['row']}: {e['error']}" for e in errors[:30]))
    return result


def safe_csv_cell(value):
    value = str(value or "")
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
