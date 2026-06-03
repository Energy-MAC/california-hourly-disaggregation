"""
Shared utilities for all data scrapers in this project.

Provides chunked CSV writing with progress-file resume support,
and the filename/chunk helpers used by every scrape_* function.
All public — import freely from any per-source scraper module.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Generator, Optional


DEFAULT_MAX_FILE_MB = 100.0


# ── Filename helpers ──────────────────────────────────────────────────────────

def build_filename(prefix: str, start: Optional[str], end: Optional[str], chunk: int) -> str:
    """
    Build a CSV filename from a descriptive prefix, optional date range, and chunk index.

    Convention:
        {prefix}_{start}_{end}_part{chunk:03d}.csv

    start / end may be None for open-ended scrapes; "earliest" / "latest" are used instead.
    """
    s = start.replace("-", "") if start else "earliest"
    e = end.replace("-", "") if end else "latest"
    return f"{prefix}_{s}_{e}_part{chunk:03d}.csv"


# ── Progress file helpers ─────────────────────────────────────────────────────

def progress_path(output_dir: Path, prefix: str) -> Path:
    return output_dir / f"{prefix}_progress.json"


def load_progress(output_dir: Path, prefix: str) -> Optional[dict]:
    p = progress_path(output_dir, prefix)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def save_progress(output_dir: Path, prefix: str, state: dict) -> None:
    progress_path(output_dir, prefix).write_text(
        json.dumps(state, indent=2), encoding="utf-8"
    )


def delete_progress(output_dir: Path, prefix: str) -> None:
    p = progress_path(output_dir, prefix)
    if p.exists():
        p.unlink()


# ── Coordinate injection ──────────────────────────────────────────────────────

def inject_coords(
    page_iter: Generator,
    lookup: dict,
    key_field: str,
) -> Generator:
    """
    Wrap a page iterator to add 'longitude' and 'latitude' columns to every row.

    Matching is case-insensitive. Rows whose key_field value has no match in the
    lookup receive empty strings for both columns.

    Parameters
    ----------
    page_iter : Generator
        Yields (list[dict], total_int) — the standard scraper page iterator.
    lookup : dict
        {name: (longitude, latitude)} — built by ArcGISClient.build_coordinate_lookup().
    key_field : str
        Field in each row to look up (e.g. "subname", "SUBSTATION").
    """
    ci_lookup = {k.upper(): v for k, v in lookup.items()}
    for rows, total in page_iter:
        for row in rows:
            name = str(row.get(key_field) or "").strip().upper()
            coords = ci_lookup.get(name)
            row["longitude"] = coords[0] if coords else ""
            row["latitude"] = coords[1] if coords else ""
        yield rows, total


# ── Generic CSV writer ────────────────────────────────────────────────────────

def pages_to_csv(
    page_iter: Generator,
    output_dir: Path,
    prefix: str,
    start: Optional[str],
    end: Optional[str],
    max_file_mb: float,
    resume: Optional[dict] = None,
) -> list[Path]:
    """
    Consume a paginated iterator of (rows, total) tuples and write to chunked CSVs.

    Rotates to a new file whenever the current CSV reaches max_file_mb.
    Each chunk gets its own header row so every file is independently readable.
    Saves a progress file after each page so an interrupted run can be resumed.
    Catches KeyboardInterrupt cleanly — flushes and closes the current file,
    prints a resume hint, and returns the files written so far.

    Parameters
    ----------
    page_iter : Generator
        Yields (list[dict], total_int). Rows must be flat dicts (no nesting).
    output_dir : Path
        Directory to write files. Created if absent.
    prefix : str
        Filename prefix — passed to build_filename().
    start, end : str | None
        Date strings (YYYY-MM-DD) or None for open-ended pulls.
    max_file_mb : float
        File size ceiling in megabytes.
    resume : dict | None
        Progress state from a previous interrupted run. When provided the
        current chunk file is opened in append mode and counters are restored.

    Returns
    -------
    list[Path]
        Paths to all CSV files written (or appended to) in this session.
    """
    max_bytes = max_file_mb * 1024 * 1024
    output_dir.mkdir(parents=True, exist_ok=True)

    written_files: list[Path] = []
    fieldnames: Optional[list[str]] = None
    chunk = 1
    current_path: Optional[Path] = None
    current_file = None
    current_writer: Optional[csv.DictWriter] = None
    rows_in_chunk = 0
    total_rows = 0
    total: Optional[int] = None

    def _open_chunk() -> None:
        nonlocal current_path, current_file, current_writer, rows_in_chunk, chunk
        if current_file and not current_file.closed:
            current_file.close()
            print(f"\n  Closed: {current_path.name}  ({rows_in_chunk:,} rows)")
        fname = build_filename(prefix, start, end, chunk)
        current_path = output_dir / fname
        current_file = open(current_path, "w", newline="", encoding="utf-8")
        written_files.append(current_path)
        current_writer = None
        rows_in_chunk = 0
        chunk += 1

    if resume:
        total_rows = resume["offset"]
        chunk = resume["next_chunk"]
        rows_in_chunk = resume["rows_in_chunk"]
        fieldnames = resume.get("fieldnames")
        total = resume.get("total")

        fname = build_filename(prefix, start, end, resume["current_chunk"])
        current_path = output_dir / fname
        if current_path.exists():
            current_file = open(current_path, "a", newline="", encoding="utf-8")
        else:
            print(f"  Warning: {fname} not found — restarting that chunk.")
            current_file = open(current_path, "w", newline="", encoding="utf-8")
            fieldnames = None
        written_files.append(current_path)
        if fieldnames:
            current_writer = csv.DictWriter(current_file, fieldnames=fieldnames)
    else:
        _open_chunk()

    interrupted = False
    try:
        for page_rows, page_total in page_iter:
            if not page_rows:
                break

            if total is None:
                total = page_total

            if fieldnames is None:
                fieldnames = list(page_rows[0].keys())

            if current_writer is None:
                current_writer = csv.DictWriter(current_file, fieldnames=fieldnames)
                current_writer.writeheader()

            current_writer.writerows(page_rows)
            rows_in_chunk += len(page_rows)
            total_rows += len(page_rows)
            current_file.flush()

            save_progress(output_dir, prefix, {
                "offset": total_rows,
                "current_chunk": chunk - 1,
                "next_chunk": chunk,
                "rows_in_chunk": rows_in_chunk,
                "total": total,
                "fieldnames": fieldnames,
            })

            pct = total_rows / total * 100 if total else 0.0
            size_mb = current_path.stat().st_size / 1024 / 1024
            print(
                f"  {total_rows:,}/{total:,} rows  ({pct:.1f}%)  |  "
                f"chunk {chunk - 1}: {size_mb:.1f} / {max_file_mb} MB",
                end="\r",
            )

            if current_path.stat().st_size >= max_bytes:
                _open_chunk()

    except KeyboardInterrupt:
        interrupted = True

    finally:
        if current_file and not current_file.closed:
            current_file.close()
            print(f"\n  Closed: {current_path.name}  ({rows_in_chunk:,} rows)")

    if interrupted:
        print(f"\nStopped at {total_rows:,}/{total or '?'} rows.")
        print("Re-run the same command to resume from where it left off.")
        return written_files

    delete_progress(output_dir, prefix)
    print(f"Done.  {total_rows:,} total rows across {len(written_files)} file(s).")
    return written_files
