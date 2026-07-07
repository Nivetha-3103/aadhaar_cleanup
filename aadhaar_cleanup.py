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
FILES_TABLE              = "dbo.files"
EXTRACTION_DETAILS_TABLE = "dbo.extractionDetails"

# ─────────────────────────────────────────────
#  OPERATIONAL CONSTANTS
# ─────────────────────────────────────────────
FREE_SPACE_THRESHOLD_GB = 200
DATA_ROOT_PATH          = "/data"

NOT_FOUND_STATUS_VALUE = "aadhar not found"

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
    Returns disk usage statistics (in GB + free%) for the given path.
    Keys: total_gb, used_gb, free_gb, free_pct
    """
    usage = shutil.disk_usage(path)
    total = usage.total or 1          # guard against division by zero
    return {
        "total_gb": bytes_to_gb(usage.total),
        "used_gb":  bytes_to_gb(usage.used),
        "free_gb":  bytes_to_gb(usage.free),
        "free_pct": (usage.free / total) * 100,
    }


def print_disk_usage(title, usage):
    """Pretty-prints a disk usage dict produced by get_disk_usage()."""
    print(f"\n{'=' * 52}")
    print(f"  {title}")
    print(f"{'=' * 52}")
    print(f"  Total Disk Size   : {usage['total_gb']:.2f} GB")
    print(f"  Used Space        : {usage['used_gb']:.2f} GB")
    print(f"  Free Space        : {usage['free_gb']:.2f} GB")
    print(f"  Free Space (%)    : {usage['free_pct']:.1f}%")
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


def fetch_records_for_month(cursor, label, start_date, end_date):
    """
    Fetches ALL records (both Aadhaar Found and Aadhaar Not Found) where
    d.UploadDate falls within [start_date, end_date).

    Uses a half-open date-range predicate (>= start / < end) so that SQL
    Server can perform an index seek on documents.UploadDate instead of a
    full table scan (avoids YEAR()/MONTH() function wrapping).

    Join chain: documents → files → extractionDetails
      documents.documentindex = files.documentindex
      files.id                = extractionDetails.fileId

    Returns a list of dicts with keys:
        documentindex, upload_date, file_id, file_name, maskingStatus,
        binaryFilePath, extractedFilePath, outputFilePath,
        pickleInputPath, pickleOutputPath
    """
    try:
        query = f"""
            SELECT
                d.documentindex,
                d.UploadDate            AS upload_date,
                f.id                    AS file_id,
                f.file_name,
                ed.maskingStatus,
                ed.binaryFilePath,
                ed.extractedFilePath,
                ed.outputFilePath,
                ed.pickleInputPath,
                ed.pickleOutputPath
            FROM {DOCUMENTS_TABLE} d
            INNER JOIN {FILES_TABLE} f
                ON d.documentindex = f.documentindex
            INNER JOIN {EXTRACTION_DETAILS_TABLE} ed
                ON ed.fileId = f.id
            WHERE d.UploadDate >= ?
              AND d.UploadDate <  ?
        """
        cursor.execute(query, (start_date, end_date))
        rows = cursor.fetchall()
        result = _rows_to_dicts(cursor, rows)
        print(f"  [DB] {len(result)} record(s) found for {label} "
              f"(UploadDate >= '{start_date}' AND < '{end_date}').")
        return result
    except pyodbc.Error as ex:
        print(f"  [DB ERROR] fetch_records_for_month({label}): {ex}")
        return []


def split_records_by_status(records):
    """
    Splits records into (found_records, not_found_records) based on
    maskingStatus. Only records whose maskingStatus is exactly
    'Aadhar not found' (case-insensitive, trimmed) go into the
    not-found bucket; everything else is treated as Aadhaar Found.
    """
    found_records = []
    not_found_records = []

    for record in records:
        status = (record.get("maskingStatus") or "").strip().lower()
        if status == NOT_FOUND_STATUS_VALUE:
            not_found_records.append(record)
        else:
            found_records.append(record)

    return found_records, not_found_records


# ═══════════════════════════════════════════════════════════════
#  FILE-PATH HELPERS
# ═══════════════════════════════════════════════════════════════

# Only the five extraction paths are targeted for deletion / size calc.
EXTRACTION_PATH_FIELDS = [
    ("binaryFilePath",    "binary file"),
    ("extractedFilePath", "extracted file"),
    ("outputFilePath",    "masked output file"),
    ("pickleInputPath",   "pickle input file"),
    ("pickleOutputPath",  "pickle output file"),
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

def print_pre_deletion_summary(month_label, disk_before, found_stats, not_found_stats):
    """
    Prints the combined pre-deletion summary: current server storage,
    selected month, and the Aadhaar Found / Aadhaar Not Found breakdown.

    found_stats / not_found_stats are dicts with keys:
        record_count, existing_paths, total_bytes, missing_count
    """
    print("\n" + "=" * 52)
    print("  CURRENT SERVER STORAGE")
    print("=" * 52)
    print(f"  Total Disk Size      : {disk_before['total_gb']:.2f} GB")
    print(f"  Used Space           : {disk_before['used_gb']:.2f} GB")
    print(f"  Free Space           : {disk_before['free_gb']:.2f} GB")
    print(f"  Free Space (%)       : {disk_before['free_pct']:.1f} %")

    print(f"\n  Selected Month       : {month_label}")

    print("\n  --- Aadhaar Found ---")
    print(f"  Record Count         : {found_stats['record_count']}")
    print(f"  Existing Files       : {len(found_stats['existing_paths'])}")
    print(f"  Missing Files        : {found_stats['missing_count']}")
    print(f"  Storage Occupied     : {bytes_to_gb(found_stats['total_bytes']):.4f} GB")

    print("\n  --- Aadhaar Not Found ---")
    print(f"  Record Count         : {not_found_stats['record_count']}")
    print(f"  Existing Files       : {len(not_found_stats['existing_paths'])}")
    print(f"  Missing Files        : {not_found_stats['missing_count']}")
    print(f"  Storage Occupied     : {bytes_to_gb(not_found_stats['total_bytes']):.4f} GB")
    print("=" * 52)


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
    Intended to be called ONLY with Aadhaar Not Found records.

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
          f"(Aadhaar Not Found only) ---")

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

def prompt_user_confirmation(month_label):
    """
    Asks the user whether to delete Aadhaar Not Found files for the
    selected month. Returns True only on explicit 'yes'.
    """
    print(f"\nDo you want to delete Aadhaar Not Found files for {month_label}? (yes/no): ",
          end="", flush=True)
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
    print(f"\n  Records Processed    : {records_processed}")
    print(f"  Files Deleted        : {stats['deleted']}")
    print(f"  Files Not Found      : {stats['not_found']}")
    print(f"  Deletion Failures    : {stats['errors']}")
    print(f"\n  Storage Deleted      : {deleted_gb:.4f} GB")

    print(f"\n  --- Disk Usage Before ---")
    print(f"  Total Space          : {disk_before['total_gb']:.2f} GB")
    print(f"  Used Space           : {disk_before['used_gb']:.2f} GB")
    print(f"  Free Space           : {disk_before['free_gb']:.2f} GB")
    print(f"  Free Percentage      : {disk_before['free_pct']:.1f} %")

    print(f"\n  --- Disk Usage After ---")
    print(f"  Total Space          : {disk_after['total_gb']:.2f} GB")
    print(f"  Used Space           : {disk_after['used_gb']:.2f} GB")
    print(f"  Free Space           : {disk_after['free_gb']:.2f} GB")
    print(f"  Free Percentage      : {disk_after['free_pct']:.1f} %")

    print(f"\n  Net Space Reclaimed  : {net_reclaimed_gb:.4f} GB")
    print("=" * 52)


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    """Main orchestration for the Aadhaar storage cleanup process."""

    # ── Step 1 : Disk usage snapshot ───────────────────────────
    print("\n[INFO] Gathering disk usage information...")
    disk_before = get_disk_usage(DATA_ROOT_PATH)
    print_disk_usage(f"Disk Usage for '{DATA_ROOT_PATH}'", disk_before)

    # ── Step 2 : Prompt for target month ───────────────────────
    month_label, start_date, end_date = prompt_for_month()

    # ── Step 3 : Connect to DB ──────────────────────────────────
    conn = connect_to_db()
    if not conn:
        return

    cursor = None
    try:
        cursor = conn.cursor()

        # ── Step 4 : Fetch & classify records for selected month ──
        print(f"\n[INFO] Querying database for records in {month_label}...")
        all_records = fetch_records_for_month(cursor, month_label, start_date, end_date)
        found_records, not_found_records = split_records_by_status(all_records)

        found_existing, found_bytes, found_missing = calculate_storage_for_records(found_records)
        not_found_existing, not_found_bytes, not_found_missing = calculate_storage_for_records(not_found_records)

        found_stats = {
            "record_count":   len(found_records),
            "existing_paths": found_existing,
            "total_bytes":    found_bytes,
            "missing_count":  found_missing,
        }
        not_found_stats = {
            "record_count":   len(not_found_records),
            "existing_paths": not_found_existing,
            "total_bytes":    not_found_bytes,
            "missing_count":  not_found_missing,
        }

        # ── Step 5 : Pre-deletion summary ──────────────────────
        print_pre_deletion_summary(month_label, disk_before, found_stats, not_found_stats)

        # ── Step 6 : Check if there are Aadhaar Not Found records ────────────────
        if not not_found_records:
            print("\n[INFO] No Aadhaar Not Found records found for this month. Exiting.")
            return
        
        print(f"\n[INFO] Aadhaar Not Found records identified for {month_label}.")
        print(f"Eligible records for cleanup : {len(not_found_records)}")
        print(f"Storage that can be reclaimed : {bytes_to_gb(not_found_bytes):.4f} GB")

        # ── Step 7 : User confirmation ─────────────────────────
        if not prompt_user_confirmation(month_label):
            print("\n[INFO] Deletion cancelled by user. No files were deleted.")
            return

        # ── Step 8 : Perform cleanup (Not Found only) ──────────
        stats = perform_cleanup(not_found_records)

        # ── Step 9 : Final report ──────────────────────────────
        disk_after = get_disk_usage(DATA_ROOT_PATH)
        print_final_report(
            month_label=month_label,
            records_processed=len(not_found_records),
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
