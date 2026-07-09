import os
import shutil
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import pyodbc

# ─────────────────────────────────────────────
#  CONNECTION CONSTANTS
# ─────────────────────────────────────────────
SERVER: str = '10.21.42.17,7865'
DATABASE: str = 'abhi_mask'
USERNAME: str = 'ABHIMASK'
PASSWORD: str = 'abhiM@4312'

DOCUMENTS_TABLE: str = "dbo.documents"
FILES_TABLE: str = "dbo.files"
EXTRACTION_DETAILS_TABLE: str = "dbo.extractionDetails"

# ─────────────────────────────────────────────
#  OPERATIONAL CONSTANTS
# ─────────────────────────────────────────────
DATA_ROOT_PATH: str = "/data"

# Records with either of these ProcessingStatus values are eligible for cleanup.
TARGET_PROCESSING_STATUSES: Tuple[str, str] = ("Not Applicable", "Aadhaar Not Found")

DATE_INPUT_FORMAT: str = "%Y-%m-%d"


# ═══════════════════════════════════════════════════════════════
#  DISK UTILITIES
# ═══════════════════════════════════════════════════════════════

def bytes_to_gb(num_bytes: float) -> float:
    """Convert a byte count to GB (base-1024)."""
    return num_bytes / (1024 ** 3)


def get_disk_usage(path: str) -> Dict[str, float]:
    """
    Returns disk usage statistics (in GB + used%/free%) for the given path.
    Keys: total_gb, used_gb, free_gb, used_pct, free_pct
    """
    usage = shutil.disk_usage(path)
    total = usage.total or 1  # guard against division by zero
    return {
        "total_gb": bytes_to_gb(usage.total),
        "used_gb": bytes_to_gb(usage.used),
        "free_gb": bytes_to_gb(usage.free),
        "used_pct": (usage.used / total) * 100,
        "free_pct": (usage.free / total) * 100,
    }


# ═══════════════════════════════════════════════════════════════
#  DATE INPUT / PARSING
# ═══════════════════════════════════════════════════════════════

def parse_date_input(raw_input: str) -> datetime:
    """
    Parses a user-supplied date string in strict 'YYYY-MM-DD' format.

    Raises ValueError with a human-readable message on invalid input.
    """
    text = raw_input.strip()
    try:
        return datetime.strptime(text, DATE_INPUT_FORMAT)
    except ValueError:
        raise ValueError(
            f"Invalid date '{raw_input}'. Please use the format YYYY-MM-DD "
            "(e.g. 2026-01-31)."
        )


def prompt_for_date(label: str) -> datetime:
    """
    Repeatedly prompts the user for a date until a valid YYYY-MM-DD value
    is entered. Returns the parsed datetime.
    """
    print(f"\nEnter the {label} date (format: YYYY-MM-DD): ", end="", flush=True)
    while True:
        try:
            raw = input().strip()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\nInput interrupted. Exiting.")

        try:
            return parse_date_input(raw)
        except ValueError as e:
            print(f"  [INVALID] {e}")
            print(f"  Please re-enter the {label} date (YYYY-MM-DD): ",
                  end="", flush=True)


def prompt_for_date_range() -> Tuple[str, str, str]:
    """
    Prompts for a start date and an end date, validates that start <= end,
    and returns (label, start_date_sql, end_date_sql) where the SQL strings
    form a half-open range: start 00:00:00.000  <=  UploadDate  <  (end + 1 day) 00:00:00.000

    Using a half-open range (>= start AND < end_exclusive) keeps the
    predicate sargable (index-seek friendly) instead of using a closed
    range with 23:59:59.999, which is functionally equivalent here since
    UploadDate has no sub-millisecond precision concerns.
    """
    while True:
        start_dt = prompt_for_date("start")
        end_dt = prompt_for_date("end")

        if start_dt > end_dt:
            print("  [INVALID] Start date cannot be greater than end date. "
                  "Please re-enter both dates.")
            continue

        break

    # Half-open upper bound: the day after the given end date at midnight.
    end_exclusive_dt = end_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end_exclusive_dt = datetime.fromordinal(end_exclusive_dt.toordinal() + 1)

    label = f"{start_dt.strftime(DATE_INPUT_FORMAT)} to {end_dt.strftime(DATE_INPUT_FORMAT)}"
    start_str = start_dt.strftime("%Y-%m-%d 00:00:00.000")
    end_str = end_exclusive_dt.strftime("%Y-%m-%d 00:00:00.000")

    return label, start_str, end_str


# ═══════════════════════════════════════════════════════════════
#  DATABASE UTILITIES
# ═══════════════════════════════════════════════════════════════

def connect_to_db() -> Optional[pyodbc.Connection]:
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


def _rows_to_dicts(cursor: "pyodbc.Cursor", rows: List[Any]) -> List[Dict[str, Any]]:
    """Helper – convert pyodbc rows to a list of dicts."""
    if not rows:
        return []
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in rows]


def fetch_eligible_records_for_range(
    cursor: "pyodbc.Cursor", label: str, start_date: str, end_date: str
) -> List[Dict[str, Any]]:
    """
    Fetches records where d.UploadDate falls within [start_date, end_date)
    AND ed.processingStatus IN ('Not Applicable', 'Aadhaar Not Found').

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
                d.UploadDate                AS upload_date,
                ed.fileId                   AS file_id,
                ed.orginalFileName           AS file_name,
                ed.processingStatus,
                ed.extractedFilePath,
                ed.pickleInputPath,
                ed.pickleOutputPath,
                ed.outputFilePrepration
            FROM {DOCUMENTS_TABLE} d
            INNER JOIN {FILES_TABLE} f
                ON d.documentindex = f.documentindex
            INNER JOIN {EXTRACTION_DETAILS_TABLE} ed
                ON f.id = ed.fileId
            WHERE d.UploadDate >= ?
              AND d.UploadDate <  ?
              AND ed.processingStatus IN ('Not Applicable', 'Aadhaar Not Found')
        """
        cursor.execute(query, (start_date, end_date))
        rows = cursor.fetchall()
        result = _rows_to_dicts(cursor, rows)
        print(f"  [DB] {len(result)} record(s) found for {label} "
              f"(UploadDate >= '{start_date}' AND < '{end_date}', "
              f"processingStatus IN {TARGET_PROCESSING_STATUSES}, "
              f"joined via documents -> files -> extractionDetails).")
        return result
    except pyodbc.Error as ex:
        print(f"  [DB ERROR] fetch_eligible_records_for_range({label}): {ex}")
        return []


def split_records_by_status(
    records: List[Dict[str, Any]]
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Splits a combined list of records into a dict keyed by processingStatus,
    e.g. {"Not Applicable": [...], "Aadhaar Not Found": [...]}.
    Any status outside TARGET_PROCESSING_STATUSES is ignored (should not
    occur given the query filter, but guards against unexpected data).
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {
        status: [] for status in TARGET_PROCESSING_STATUSES
    }
    for record in records:
        status = record.get("processingStatus")
        if status in grouped:
            grouped[status].append(record)
    return grouped


# ═══════════════════════════════════════════════════════════════
#  FILE-PATH HELPERS
# ═══════════════════════════════════════════════════════════════

# Only these four extraction paths are targeted for deletion / size calc.
EXTRACTION_PATH_FIELDS: List[Tuple[str, str]] = [
    ("extractedFilePath", "extracted file"),
    ("pickleInputPath", "pickle input file"),
    ("pickleOutputPath", "pickle output file"),
    ("outputFilePrepration", "output file prepration"),
]


def get_extraction_paths(record: Dict[str, Any]) -> List[Tuple[str, str]]:
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

def calculate_storage_for_records(
    records: List[Dict[str, Any]]
) -> Tuple[Set[str], int, int]:
    """
    Scans all extraction file paths in `records`, de-duplicating physical
    paths so the same file is never counted twice.

    Returns: (unique_existing_paths: set, total_bytes: int, missing_count: int)
    """
    seen_paths: Set[str] = set()
    total_bytes = 0
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

def print_pre_deletion_summary(
    range_label: str,
    disk_before: Dict[str, float],
    na_stats: Dict[str, Any],
    anf_stats: Dict[str, Any],
    overall_stats: Dict[str, Any],
) -> None:
    """
    Prints the combined pre-deletion summary ONCE: current server storage,
    selected date range, per-status breakdown (Not Applicable / Aadhaar Not
    Found), and an overall total.

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

    print(f"\nSelected Range     : {range_label}")

    print("\nNot Applicable")
    print("-" * 26)
    print(f"Record Count       : {na_stats['record_count']}")
    print(f"Storage Occupied   : {bytes_to_gb(na_stats['total_bytes']):.2f} GB")

    print("\nAadhaar Not Found")
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

def delete_file_from_system(
    abs_path: str, description: str = "file"
) -> Tuple[bool, str, int]:
    """
    Deletes a single file.  abs_path must already be an absolute path.

    Returns: (success: bool, status: str, size_bytes: int)
    status ∈ {"deleted", "not_found", "not_a_file", "error"}
    """
    if not os.path.exists(abs_path):
        print(f"    [NOT FOUND] {abs_path}")
        return False, "not_found", 0

    if not os.path.isfile(abs_path):
        print(f"    [SKIP] Not a file (directory?): {abs_path}")
        return False, "not_a_file", 0

    try:
        size_bytes = os.path.getsize(abs_path)
        os.remove(abs_path)
        print(f"    [DELETED] {description}: {abs_path}  "
              f"({bytes_to_gb(size_bytes):.6f} GB)")
        return True, "deleted", size_bytes
    except OSError as e:
        print(f"    [ERROR] Cannot delete {abs_path}: {e}")
        return False, "error", 0


def perform_cleanup(records: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Deletes every extraction file referenced across `records`,
    de-duplicating so the same physical path is never touched twice.
    Intended to be called ONLY with records whose ProcessingStatus is
    'Not Applicable' or 'Aadhaar Not Found'.

    Returns a stats dict.
    """
    stats = {
        "deleted": 0,
        "not_found": 0,
        "errors": 0,
        "deleted_bytes": 0,
    }
    deleted_paths: Set[str] = set()

    print(f"\n--- Starting Deletion: {len(records)} eligible record(s) "
          f"(processingStatus IN {TARGET_PROCESSING_STATUSES}) ---")

    for record in records:
        file_id = record.get("file_id")
        file_name = record.get("file_name")
        print(f"\n  FileID: {file_id}  |  {file_name}")

        for abs_path, description in get_extraction_paths(record):
            if abs_path in deleted_paths:
                continue  # already deleted in this run

            success, status, size_bytes = delete_file_from_system(abs_path, description)

            if success:
                stats["deleted"] += 1
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

def prompt_user_confirmation() -> bool:
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

def print_final_report(
    range_label: str,
    records_processed: int,
    stats: Dict[str, int],
    disk_before: Dict[str, float],
    disk_after: Dict[str, float],
) -> None:
    """Prints the post-cleanup summary, including net space reclaimed."""
    deleted_gb = bytes_to_gb(stats["deleted_bytes"])
    net_reclaimed_gb = disk_after["free_gb"] - disk_before["free_gb"]

    print("\n" + "=" * 52)
    print("  CLEANUP COMPLETED")
    print("=" * 52)
    print(f"  Selected Range       : {range_label}")
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

def main() -> None:
    """Main orchestration for the Aadhaar storage cleanup process."""

    # ── Step 1 : Disk usage snapshot (not printed here — it is shown
    #            once as part of the pre-deletion summary below) ────
    disk_before = get_disk_usage(DATA_ROOT_PATH)

    # ── Step 2 : Prompt for target date range ──────────────────
    range_label, start_date, end_date = prompt_for_date_range()

    # ── Step 3 : Connect to DB ──────────────────────────────────
    print(f"\n[INFO] Fetching from DB: {DATABASE}")
    conn = connect_to_db()
    if not conn:
        return

    cursor: Optional["pyodbc.Cursor"] = None
    try:
        cursor = conn.cursor()

        # ── Step 4 : Fetch eligible records for selected range ──
        print(f"\n[INFO] Querying database for records in {range_label} "
              f"with processingStatus IN {TARGET_PROCESSING_STATUSES}...")
        eligible_records = fetch_eligible_records_for_range(
            cursor, range_label, start_date, end_date
        )

        # ── Step 5 : Split by status & calculate storage per status ──
        records_by_status = split_records_by_status(eligible_records)
        na_records = records_by_status["Not Applicable"]
        anf_records = records_by_status["Aadhaar Not Found"]

        na_existing, na_bytes, na_missing = calculate_storage_for_records(na_records)
        anf_existing, anf_bytes, anf_missing = calculate_storage_for_records(anf_records)
        overall_existing, overall_bytes, overall_missing = calculate_storage_for_records(
            eligible_records
        )

        na_stats = {
            "record_count": len(na_records),
            "existing_paths": na_existing,
            "total_bytes": na_bytes,
            "missing_count": na_missing,
        }
        anf_stats = {
            "record_count": len(anf_records),
            "existing_paths": anf_existing,
            "total_bytes": anf_bytes,
            "missing_count": anf_missing,
        }
        overall_stats = {
            "record_count": len(eligible_records),
            "existing_paths": overall_existing,
            "total_bytes": overall_bytes,
            "missing_count": overall_missing,
        }

        # ── Step 6 : Pre-deletion summary (shown only once) ───
        print_pre_deletion_summary(range_label, disk_before, na_stats, anf_stats, overall_stats)

        # ── Step 7 : Check if there are eligible records ───────
        if not eligible_records:
            print(f"\n[INFO] No records with processingStatus IN "
                  f"{TARGET_PROCESSING_STATUSES} found for this range. Exiting.")
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
            range_label=range_label,
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
