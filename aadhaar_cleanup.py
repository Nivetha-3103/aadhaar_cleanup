import os
import re
import shutil
import calendar
import traceback
from datetime import datetime
import pyodbc

# ─────────────────────────────────────────────
#  CONNECTION CONSTANTS
# ─────────────────────────────────────────────
SERVER   = '10.21.42.17,1433'
DATABASE = 'abhi_mask'
USERNAME = 'ABHIMASK'
PASSWORD = 'abhiM@4312'

DOCUMENTS_TABLE          = "dbo.documents"
FILES_TABLE               = "dbo.files"
EXTRACTION_DETAILS_TABLE = "dbo.extractionDetails"

# ─────────────────────────────────────────────
#  OPERATIONAL CONSTANTS
# ─────────────────────────────────────────────
FREE_SPACE_THRESHOLD_GB = 200
DATA_ROOT_PATH          = "/data"

# Records with either of these ProcessingStatus values are eligible for cleanup.
TARGET_PROCESSING_STATUSES = ("Not Applicable", "Aadhaar not found")

# Accepted month-name spellings for free-text input, e.g. "January 2026"
MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ═══════════════════════════════════════════════════════════════
#  DISK UTILITIES
# ═══════════════════════════════════════════════════════════════

def bytes_to_gb(num_bytes):
    """Convert a byte count to GB (base-1024)."""
    return num_bytes / (1024 ** 3)


def get_disk_usage(path):
    """
    Returns disk usage statistics (in GB + used%/free%) for the given path.
    Keys: total_gb, used_gb, free_gb, used_pct, free_pct
    """
    usage = shutil.disk_usage(path)
    total = usage.total or 1          # guard against division by zero
    return {
        "total_gb":  bytes_to_gb(usage.total),
        "used_gb":   bytes_to_gb(usage.used),
        "free_gb":   bytes_to_gb(usage.free),
        "used_pct":  (usage.used / total) * 100,
        "free_pct":  (usage.free / total) * 100,
    }


def print_disk_usage(title, usage):
    """Pretty-prints a disk usage dict produced by get_disk_usage()."""
    print(f"\n{'=' * 52}")
    print(f"  {title}")
    print(f"{'=' * 52}")
    print(f"  Total Storage      : {usage['total_gb']:.2f} GB")
    print(f"  Used Storage       : {usage['used_gb']:.2f} GB")
    print(f"  Free Storage       : {usage['free_gb']:.2f} GB")
    print(f"  Used Percentage    : {usage['used_pct']:.2f}%")
    print(f"{'=' * 52}")


# ═══════════════════════════════════════════════════════════════
#  MONTH INPUT / PARSING
# ═══════════════════════════════════════════════════════════════

def parse_month_input(raw_input):
    """
    Parses a user-supplied month/year string into (label, start_date, end_date).

    Accepted formats (case-insensitive):
        "January 2026"   "Jan 2026"
        "2026-01"
        "01-2026"        "01/2026"

    start_date / end_date are returned as SQL Server compatible strings
    ('YYYY-MM-DD HH:MM:SS.fff') so the caller can use a half-open
    date-range predicate (>= start AND < end) that stays index-friendly
    (no YEAR()/MONTH() wrapping).

    Raises ValueError with a human-readable message on invalid input.
    """
    text = raw_input.strip().lower()

    year = None
    month = None

    # Format: YYYY-MM
    m = re.match(r'^(\d{4})-(\d{1,2})$', text)
    if m:
        year, month = int(m.group(1)), int(m.group(2))

    # Format: "<month name> YYYY"
    if year is None:
        m = re.match(r'^([a-zA-Z]+)\s+(\d{4})$', text)
        if m and m.group(1) in MONTH_NAMES:
            month = MONTH_NAMES[m.group(1)]
            year = int(m.group(2))

    # Format: MM-YYYY or MM/YYYY
    if year is None:
        m = re.match(r'^(\d{1,2})[-/](\d{4})$', text)
        if m:
            month, year = int(m.group(1)), int(m.group(2))

    if year is None or month is None:
        raise ValueError(
            "Could not understand the month/year. Try formats like "
            "'January 2026' or '2026-01'."
        )

    if not (1 <= month <= 12):
        raise ValueError(f"Month must be between 1 and 12 (got {month}).")

    if not (1900 <= year <= 2100):
        raise ValueError(f"Year looks out of range (got {year}).")

    start_dt = datetime(year, month, 1)
    if month == 12:
        end_dt = datetime(year + 1, 1, 1)
    else:
        end_dt = datetime(year, month + 1, 1)

    label = f"{calendar.month_name[month]} {year}"
    start_str = start_dt.strftime("%Y-%m-%d 00:00:00.000")
    end_str = end_dt.strftime("%Y-%m-%d 00:00:00.000")

    return label, start_str, end_str


def prompt_for_month():
    """
    Repeatedly prompts the user for a target month/year until a valid
    value is entered. Returns (label, start_date, end_date).
    """
    print("\nEnter the target month and year (e.g. 'January 2026' or '2026-01'): ",
          end="", flush=True)
    while True:
        try:
            raw = input().strip()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\nInput interrupted. Exiting.")

        try:
            return parse_month_input(raw)
        except ValueError as e:
            print(f"  [INVALID] {e}")
            print("  Please re-enter (e.g. 'January 2026' or '2026-01'): ",
                  end="", flush=True)


# ═══════════════════════════════════════════════════════════════
#  DATABASE UTILITIES
# ═══════════════════════════════════════════════════════════════

def connect_to_db():
    """Establishes a connection to the SQL Server database."""
    try:
        driver = "{ODBC Driver 18 for SQL Server}"
        connection_string = (
            f"DRIVER={driver};"
            f"SERVER={SERVER};"
            f"DATABASE={DATABASE};"
            f"UID={USERNAME};"
            f"PWD={PASSWORD};"
            f"TrustServerCertificate=yes;"
        )
        conn = pyodbc.connect(connection_string)
        print("\n[DB] Successfully connected to the database.")
        return conn
    except pyodbc.Error as ex:
        print(f"[DB ERROR] Could not connect: {ex}")
        print("  Ensure the ODBC driver name is correct and the server is accessible.")
        return None


def _rows_to_dicts(cursor, rows):
    """Helper – convert pyodbc rows to a list of dicts."""
    if not rows:
        return []
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in rows]


def fetch_eligible_records_for_month(cursor, label, start_date, end_date):
    """
    Fetches records where d.UploadDate falls within [start_date, end_date)
    AND ed.processingStatus IN ('Not Applicable', 'Aadhaar not found').

    Uses a half-open date-range predicate (>= start / < end) so that SQL
    Server can perform an index seek on documents.UploadDate instead of a
    full table scan (avoids YEAR()/MONTH() function wrapping).

    Join chain: documents → files → extractionDetails
      documents.documentindex = files.documentindex
      files.id                = extractionDetails.fileId

    Returns a list of dicts with keys:
        documentindex, upload_date, file_id, file_name, processingStatus,
        extractedFilePath, pickleInputPath, pickleOutputPath,
        outputFilePrepration
    """
    try:
        query = f"""
            SELECT
                d.documentindex,
                d.UploadDate            AS upload_date,
                f.id                    AS file_id,
                f.file_name,
                ed.processingStatus,
                ed.extractedFilePath,
                ed.pickleInputPath,
                ed.pickleOutputPath,
                ed.outputFilePrepration
            FROM {DOCUMENTS_TABLE} d
            INNER JOIN {FILES_TABLE} f
                ON d.documentindex = f.documentindex
            INNER JOIN {EXTRACTION_DETAILS_TABLE} ed
                ON ed.fileId = f.id
            WHERE d.UploadDate >= ?
              AND d.UploadDate <  ?
              AND ed.processingStatus IN ('Not Applicable', 'Aadhaar not found')
        """
        cursor.execute(query, (start_date, end_date))
        rows = cursor.fetchall()
        result = _rows_to_dicts(cursor, rows)
        print(f"  [DB] {len(result)} record(s) found for {label} "
              f"(UploadDate >= '{start_date}' AND < '{end_date}', "
              f"processingStatus IN {TARGET_PROCESSING_STATUSES}).")
        return result
    except pyodbc.Error as ex:
        print(f"  [DB ERROR] fetch_eligible_records_for_month({label}): {ex}")
        return []


def split_records_by_status(records):
    """
    Splits a combined list of records into a dict keyed by processingStatus,
    e.g. {"Not Applicable": [...], "Aadhaar not found": [...]}.
    Any status outside TARGET_PROCESSING_STATUSES is ignored (should not
    occur given the query filter, but guards against unexpected data).
    """
    grouped = {status: [] for status in TARGET_PROCESSING_STATUSES}
    for record in records:
        status = record.get("processingStatus")
        if status in grouped:
            grouped[status].append(record)
    return grouped


# ═══════════════════════════════════════════════════════════════
#  FILE-PATH HELPERS
# ═══════════════════════════════════════════════════════════════

# Only these four extraction paths are targeted for deletion / size calc.
EXTRACTION_PATH_FIELDS = [
    ("extractedFilePath",    "extracted file"),
    ("pickleInputPath",      "pickle input file"),
    ("pickleOutputPath",     "pickle output file"),
    ("outputFilePrepration", "output file prepration"),
]


def get_extraction_paths(record):
    """Returns [(abs_path, description), …] for a single record."""
    results = []
    for field, desc in EXTRACTION_PATH_FIELDS:
        raw = record.get(field)
        if raw and raw.strip():
            results.append((os.path.abspath(os.path.normpath(raw.strip())), desc))
    return results


# ═══════════════════════════════════════════════════════════════
#  STORAGE CALCULATION
# ═══════════════════════════════════════════════════════════════

def calculate_storage_for_records(records):
    """
    Scans all extraction file paths in `records`, de-duplicating physical
    paths so the same file is never counted twice.

    Returns: (unique_existing_paths: set, total_bytes: int, missing_count: int)
    """
    seen_paths    = set()
    total_bytes   = 0
    missing_count = 0

    for record in records:
        for abs_path, _ in get_extraction_paths(record):
            if abs_path in seen_paths:
                continue
            seen_paths.add(abs_path)

            if os.path.isfile(abs_path):
                try:
                    total_bytes += os.path.getsize(abs_path)
                except OSError:
                    pass
            else:
                missing_count += 1

    existing_paths = {p for p in seen_paths if os.path.isfile(p)}
    return existing_paths, total_bytes, missing_count


# ═══════════════════════════════════════════════════════════════
#  REPORTING
# ═══════════════════════════════════════════════════════════════

def print_pre_deletion_summary(month_label, disk_before, na_stats, anf_stats, overall_stats):
    """
    Prints the combined pre-deletion summary ONCE: current server storage,
    selected month, per-status breakdown (Not Applicable / Aadhaar not found),
    and an overall total.

    na_stats / anf_stats / overall_stats are dicts with keys:
        record_count, existing_paths, total_bytes, missing_count
    """
    print("\n" + "=" * 50)
    print("Current Server Storage")
    print("-" * 50)
    print(f"Total Storage      : {disk_before['total_gb']:.2f} GB")
    print(f"Used Storage       : {disk_before['used_gb']:.2f} GB")
    print(f"Free Storage       : {disk_before['free_gb']:.2f} GB")
    print(f"Used Percentage    : {disk_before['used_pct']:.2f} %")

    print(f"\nSelected Month     : {month_label}")

    print("\nNot Applicable")
    print("-" * 26)
    print(f"Record Count       : {na_stats['record_count']}")
    print(f"Storage Occupied   : {bytes_to_gb(na_stats['total_bytes']):.2f} GB")

    print("\nAadhaar not found")
    print("-" * 26)
    print(f"Record Count       : {anf_stats['record_count']}")
    print(f"Storage Occupied   : {bytes_to_gb(anf_stats['total_bytes']):.2f} GB")

    print("\nOverall")
    print("-" * 26)
    print(f"Total Records      : {overall_stats['record_count']}")
    print(f"Total Storage      : {bytes_to_gb(overall_stats['total_bytes']):.2f} GB")
    print("=" * 50)


# ═══════════════════════════════════════════════════════════════
#  FILE DELETION
# ═══════════════════════════════════════════════════════════════

def delete_file_from_system(abs_path, description="file"):
    """
    Deletes a single file.  abs_path must already be an absolute path.

    Returns: (success: bool, status: str, size_bytes: int)
    status ∈ {"deleted", "not_found", "not_a_file", "error"}
    """
    if os.path.exists(abs_path):
        if os.path.isfile(abs_path):
            try:
                size_bytes = os.path.getsize(abs_path)
                os.remove(abs_path)
                print(f"    [DELETED] {description}: {abs_path}  "
                      f"({bytes_to_gb(size_bytes):.6f} GB)")
                return True, "deleted", size_bytes
            except OSError as e:
                print(f"    [ERROR] Cannot delete {abs_path}: {e}")
                return False, "error", 0
        else:
            print(f"    [SKIP] Not a file (directory?): {abs_path}")
            return False, "not_a_file", 0
    else:
        print(f"    [NOT FOUND] {abs_path}")
        return False, "not_found", 0


def perform_cleanup(records):
    """
    Deletes every extraction file referenced across `records`,
    de-duplicating so the same physical path is never touched twice.
    Intended to be called ONLY with records whose ProcessingStatus is
    'Not Applicable' or 'Aadhaar not found'.

    Returns a stats dict.
    """
    stats = {
        "deleted":       0,
        "not_found":     0,
        "errors":        0,
        "deleted_bytes": 0,
    }
    deleted_paths = set()

    print(f"\n--- Starting Deletion: {len(records)} eligible record(s) "
          f"(processingStatus IN {TARGET_PROCESSING_STATUSES}) ---")

    for record in records:
        file_id   = record.get("file_id")
        file_name = record.get("file_name")
        print(f"\n  FileID: {file_id}  |  {file_name}")

        for abs_path, description in get_extraction_paths(record):
            if abs_path in deleted_paths:
                continue   # already deleted in this run

            success, status, size_bytes = delete_file_from_system(abs_path, description)

            if success:
                stats["deleted"]       += 1
                stats["deleted_bytes"] += size_bytes
                deleted_paths.add(abs_path)
            elif status == "not_found":
                stats["not_found"] += 1
            elif status == "error":
                stats["errors"] += 1
            # "not_a_file" – skip silently (already printed)

    return stats


# ═══════════════════════════════════════════════════════════════
#  USER CONFIRMATION
# ═══════════════════════════════════════════════════════════════

def prompt_user_confirmation():
    """
    Asks the user whether to delete files belonging to both eligible
    ProcessingStatus values. Returns True only on explicit 'yes'.
    """
    print("\nDo you want to delete these files? (yes/no): ", end="", flush=True)
    while True:
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nInput interrupted. Exiting without deleting.")
            return False

        if answer == "yes":
            return True
        elif answer == "no":
            return False
        else:
            print("  Invalid input – please type 'yes' or 'no': ", end="", flush=True)


# ═══════════════════════════════════════════════════════════════
#  FINAL REPORT
# ═══════════════════════════════════════════════════════════════

def print_final_report(month_label, records_processed, stats, disk_before, disk_after):
    """Prints the post-cleanup summary, including net space reclaimed."""
    deleted_gb = bytes_to_gb(stats["deleted_bytes"])
    net_reclaimed_gb = disk_after["free_gb"] - disk_before["free_gb"]

    print("\n" + "=" * 52)
    print("  CLEANUP COMPLETED")
    print("=" * 52)
    print(f"  Selected Month       : {month_label}")
    print(f"  Processing Statuses  : {', '.join(TARGET_PROCESSING_STATUSES)}")
    print(f"\n  Records Processed    : {records_processed}")
    print(f"  Files Deleted        : {stats['deleted']}")
    print(f"  Files Not Found      : {stats['not_found']}")
    print(f"  Errors               : {stats['errors']}")
    print(f"\n  Total Storage Deleted: {deleted_gb:.4f} GB")

    print(f"\n  --- Disk Usage Before ---")
    print(f"  Total Storage        : {disk_before['total_gb']:.2f} GB")
    print(f"  Used Storage         : {disk_before['used_gb']:.2f} GB")
    print(f"  Free Storage         : {disk_before['free_gb']:.2f} GB")
    print(f"  Used Percentage      : {disk_before['used_pct']:.2f}%")
    print(f"  Free Percentage      : {disk_before['free_pct']:.2f}%")

    print(f"\n  --- Disk Usage After ---")
    print(f"  Total Storage        : {disk_after['total_gb']:.2f} GB")
    print(f"  Used Storage         : {disk_after['used_gb']:.2f} GB")
    print(f"  Free Storage         : {disk_after['free_gb']:.2f} GB")
    print(f"  Used Percentage      : {disk_after['used_pct']:.2f}%")
    print(f"  Free Percentage      : {disk_after['free_pct']:.2f}%")

    print(f"\n  Net Space Reclaimed  : {net_reclaimed_gb:.4f} GB")
    print("=" * 52)


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    """Main orchestration for the Aadhaar storage cleanup process."""

    # ── Step 1 : Disk usage snapshot (not printed here — it is shown
    #            once as part of the pre-deletion summary below) ────
    disk_before = get_disk_usage(DATA_ROOT_PATH)

    # ── Step 2 : Prompt for target month ───────────────────────
    month_label, start_date, end_date = prompt_for_month()

    # ── Step 3 : Connect to DB ──────────────────────────────────
    conn = connect_to_db()
    if not conn:
        return

    cursor = None
    try:
        cursor = conn.cursor()

        # ── Step 4 : Fetch eligible records for selected month ──
        print(f"\n[INFO] Querying database for records in {month_label} "
              f"with processingStatus IN {TARGET_PROCESSING_STATUSES}...")
        eligible_records = fetch_eligible_records_for_month(
            cursor, month_label, start_date, end_date
        )

        # ── Step 5 : Split by status & calculate storage per status ──
        records_by_status = split_records_by_status(eligible_records)
        na_records  = records_by_status["Not Applicable"]
        anf_records = records_by_status["Aadhaar not found"]

        na_existing, na_bytes, na_missing = calculate_storage_for_records(na_records)
        anf_existing, anf_bytes, anf_missing = calculate_storage_for_records(anf_records)
        overall_existing, overall_bytes, overall_missing = calculate_storage_for_records(
            eligible_records
        )

        na_stats = {
            "record_count":   len(na_records),
            "existing_paths": na_existing,
            "total_bytes":    na_bytes,
            "missing_count":  na_missing,
        }
        anf_stats = {
            "record_count":   len(anf_records),
            "existing_paths": anf_existing,
            "total_bytes":    anf_bytes,
            "missing_count":  anf_missing,
        }
        overall_stats = {
            "record_count":   len(eligible_records),
            "existing_paths": overall_existing,
            "total_bytes":    overall_bytes,
            "missing_count":  overall_missing,
        }

        # ── Step 6 : Pre-deletion summary (shown only once) ───
        print_pre_deletion_summary(month_label, disk_before, na_stats, anf_stats, overall_stats)

        # ── Step 7 : Check if there are eligible records ───────
        if not eligible_records:
            print(f"\n[INFO] No records with processingStatus IN "
                  f"{TARGET_PROCESSING_STATUSES} found for this month. Exiting.")
            return

        # ── Step 8 : User confirmation ─────────────────────────
        if not prompt_user_confirmation():
            print("\n[INFO] Deletion cancelled by user. No files were deleted.")
            return

        # ── Step 9 : Perform cleanup ────────────────────────────
        stats = perform_cleanup(eligible_records)

        # ── Step 10 : Final report ──────────────────────────────
        disk_after = get_disk_usage(DATA_ROOT_PATH)
        print_final_report(
            month_label=month_label,
            records_processed=len(eligible_records),
            stats=stats,
            disk_before=disk_before,
            disk_after=disk_after,
        )

    except Exception as e:
        print(f"\n[FATAL] Unexpected error: {e}")
        print("Traceback:")
        traceback.print_exc()
    finally:
        if cursor:
            cursor.close()
            print("\n[DB] Cursor closed.")
        if conn:
            conn.close()
            print("[DB] Connection closed.")


if __name__ == "__main__":
    main()
