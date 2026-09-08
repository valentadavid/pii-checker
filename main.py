# main.py
# Main orchestrator for PII detection in replication packages. A library,
# invoked via interface.py — no __main__ entry point here.
#
# Two entry points:
#   1. Single package: run_package(folder_path, output_path)
#   2. Batch:          run_folder(folder_path) — auto-discovers package
#                      subfolders and maintains a resumable overview/status
#                      file (pii_results_overview.xlsx) in that folder
#
# Pipeline per package (run_package):
#   0. Record archive info (MD5, size) before unzipping
#   1. Unzip any archive files in the folder
#   2. Clean junk files
#   3. Build file list and find duplicates
#   4. First pass — process primary files only
#   5. Second pass — copy results for duplicate files
#   6. Save results to both package folder and central folder

import os
import sys
import logging
import importlib.util

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Core packages required for this project to run at all — kept in sync with requirements.txt.
_REQUIRED_PACKAGES = [
    "pandas", "numpy", "chardet", "pyreadstat", "pyreadr",
    "openpyxl", "xlrd", "odf", "rdata", "scipy", "requests", "py7zr",
]


def _check_core_packages():
    """Checks requirements.txt packages are installed before importing modules that need them."""
    missing = [pkg for pkg in _REQUIRED_PACKAGES if importlib.util.find_spec(pkg) is None]
    if missing:
        logger.error(
            "Missing required packages — install with:  pip install -r requirements.txt\n  Missing: %s",
            ", ".join(missing)
        )
        sys.exit(1)


_check_core_packages()

import shutil
import hashlib
import tempfile
import datetime
import time
import pandas as pd
from loader import load_data
from column_filter import is_candidate_column
from column_checker import check_column, sanitize_for_excel
from unzip_package import unzip_folder, _get_handler
from find_duplicities import find_duplicities
from clean_package import clean_folder
from llm_client import DEFAULT_PROVIDER, DEFAULT_MODEL


# --- Issue collector — captures all warnings and errors from all modules ---
class IssueCollector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.issues = []

    def emit(self, record):
        if record.levelno >= logging.WARNING:
            self.issues.append({
                'level'  : record.levelname,
                'message': record.getMessage(),
            })


def _check_dependencies(provider=None, model=None):
    """Checks the given LLM provider/model is actually usable before any processing
    begins (core packages are checked at import time). Defaults to the configured
    LLM_PROVIDER/LLM_MODEL if not given — but always pass the exact provider/model
    that will actually be used for the real run; validating a different one than
    what's actually invoked gives false confidence."""
    from llm_client import (
        check_ollama_reachable, check_model_available, check_model_works,
        check_anthropic_ready, check_openai_ready, OLLAMA_ENDPOINTS,
    )
    provider = provider or DEFAULT_PROVIDER
    model = model or DEFAULT_MODEL

    if provider == 'ollama':
        if not check_ollama_reachable():
            logger.error("Could not reach Ollama — check OLLAMA_ENDPOINTS/OLLAMA_API_KEY and try again")
            sys.exit(1)

        if not check_model_available(model):
            logger.error("Model '%s' is not available on Ollama — check LLM_MODEL and try again", model)
            sys.exit(1)

        if not check_model_works(model):
            logger.error("Model '%s' failed a test call — check Ollama logs and try again", model)
            sys.exit(1)

        logger.info("Ollama is reachable at %s with model '%s' (verified working)", OLLAMA_ENDPOINTS[0], model)

    elif provider == 'anthropic':
        if not check_anthropic_ready():
            sys.exit(1)
        logger.info("Anthropic provider ready (model '%s')", model)

    elif provider == 'openai':
        if not check_openai_ready():
            sys.exit(1)
        logger.info("OpenAI provider ready (model '%s')", model)

    else:
        logger.error("Unknown LLM_PROVIDER '%s' — expected ollama, anthropic, or openai", provider)
        sys.exit(1)

logging.getLogger("anthropic").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

issue_collector = IssueCollector()
logging.getLogger().addHandler(issue_collector)


# --- Throughput tracker — counts columns sent through check_column, bucketed by minute ---
class ThroughputTracker:
    def __init__(self, output_path):
        self.output_path = output_path
        self.counts = {}
        self._current_minute = None

    def record(self, n=1):
        now_minute = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
        self.counts[now_minute] = self.counts.get(now_minute, 0) + n
        if now_minute != self._current_minute:
            self._current_minute = now_minute
            self.flush()

    def flush(self):
        rows = [{'minute': m, 'columns_processed': c} for m, c in sorted(self.counts.items())]
        rows.append({'minute': 'TOTAL', 'columns_processed': sum(self.counts.values())})
        try:
            pd.DataFrame(rows).to_excel(self.output_path, sheet_name='Throughput', index=False)
        except PermissionError:
            logger.warning("Could not write throughput log (file open?) — will retry on next update")


def _md5_file(file_path, chunk_size=8192) -> str:
    h = hashlib.md5()
    with open(file_path, 'rb') as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def _get_archive_info(folder_path) -> dict:
    """Records MD5 and size of top-level archive files before extraction."""
    archives = [
        os.path.join(folder_path, f)
        for f in os.listdir(folder_path)
        if os.path.isfile(os.path.join(folder_path, f))
        and _get_handler(f) is not None
        and not f.startswith('._')
    ]

    names, sizes, md5s = [], [], []
    for path in archives:
        name = os.path.basename(path)
        logger.info("Hashing archive: %s", name)
        names.append(name)
        sizes.append(round(os.path.getsize(path) / 1024 / 1024, 2))
        md5s.append(_md5_file(path))

    return {
        'archive_files'    : '; '.join(names),
        'archive_sizes_mb' : '; '.join(str(s) for s in sizes),
        'archive_md5s'     : '; '.join(md5s),
    }


def save_results(results, issues, output_path):
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        if results:
            pd.DataFrame(results).to_excel(writer, sheet_name='Results', index=False)
        else:
            pd.DataFrame([{'message': 'No variables were evaluated'}]).to_excel(
                writer, sheet_name='Results', index=False)
        if issues:
            pd.DataFrame(issues).to_excel(writer, sheet_name='Issues', index=False)
    logger.info("Results saved to %s", output_path)


def run_package(folder_path, output_path, central_output_path=None, temp_base=None,
                provider=DEFAULT_PROVIDER, model=DEFAULT_MODEL, tracker=None, track_throughput=False):
    """
    Processes a single package folder without modifying it.
    Archives are extracted directly into a temp dir; non-archive files are copied there.
    Temp dir is deleted when done. Results saved to output_path / central_output_path.
    track_throughput: if True (and no tracker is passed in), logs columns/minute to an
                       Excel file next to output_path. Off by default.
    tracker: optional ThroughputTracker to log columns/minute into (used by run_folder to
             share one tracker across packages); overrides track_throughput.
    Returns a summary dict with counts.
    """

    # --- Step 0: record archive info from original folder (read-only) ---
    issue_collector.issues.clear()
    archive_info = _get_archive_info(folder_path)

    if tracker is None and track_throughput:
        throughput_path = os.path.join(os.path.dirname(output_path) or '.', 'throughput_log.xlsx')
        tracker = ThroughputTracker(throughput_path)

    # --- Step 1: set up temp working dir ---
    temp_dir = tempfile.mkdtemp(dir=temp_base)
    logger.info("Working in temp dir: %s", temp_dir)

    # paths that may live inside folder_path itself and must not be copied into
    # temp (temp_base can be a subfolder of folder_path, and copying it would
    # copy temp_dir into itself, recursing forever)
    _skip_abs = {os.path.abspath(output_path)}
    if central_output_path:
        _skip_abs.add(os.path.abspath(central_output_path))
    if temp_base:
        _skip_abs.add(os.path.abspath(temp_base))

    try:
        # --- Step 2: populate temp dir ---
        # Archives: extract directly into temp (no copy needed)
        # Everything else: copy into temp
        for item in os.listdir(folder_path):
            src = os.path.join(folder_path, item)
            if item.startswith('._') or '__MACOSX' in item:
                continue
            if os.path.abspath(src) in _skip_abs:
                continue
            handler = _get_handler(item)
            if os.path.isfile(src) and handler is not None:
                logger.info("Extracting to temp: %s", item)
                try:
                    handler(src, dest_base=temp_dir)
                except Exception as e:
                    logger.error("Failed to extract %s: %s", item, e)
            elif os.path.isdir(src):
                shutil.copytree(src, os.path.join(temp_dir, item))
            else:
                shutil.copy2(src, temp_dir)

        # --- Step 3: clean junk files ---
        clean_folder(temp_dir)

        # --- Step 4: unzip any nested archives ---
        unzip_folder(temp_dir)

        # --- Step 5: build file list ---
        files = [
            os.path.join(root, f)
            for root, _, filenames in os.walk(temp_dir)
            for f in filenames
            if not f.startswith('._') and '__MACOSX' not in root
        ]

        if not files:
            logger.error("No files found in %s", folder_path)
            return {**archive_info, 'n_files_total': 0}

        logger.info("Found %d file(s) in %s", len(files), folder_path)

        # --- Step 6: find duplicates ---
        dup_lookup = find_duplicities(files)
        n_duplicates = sum(1 for p, pri in dup_lookup.items() if p != pri)

        results = []
        primary_results = {}
        n_data_files = 0

        # --- Step 7: first pass — process primaries ---
        for file_path in files:
            if dup_lookup.get(file_path) != file_path:
                continue

            rel_path = os.path.relpath(file_path, temp_dir)
            loaded = load_data(file_path)

            if not loaded or isinstance(loaded, str):
                continue

            n_data_files += 1
            file_results = []

            for fp, sheet_name, df, labels, nrows in loaded:

                candidates = []
                for col in df.columns:
                    try:
                        label = labels.get(col, col) if labels else col
                        is_candidate, filter_reason = is_candidate_column(df[col], label=label)
                        if is_candidate:
                            candidates.append((col, label, filter_reason))
                    except Exception as e:
                        logger.error("Skipping column '%s' in %s — filter error: %s", col, rel_path, e)
                        label = labels.get(col, col) if labels else col
                        err_row = {
                            'file': rel_path, 'sheet': sheet_name or '—',
                            'col_name': col, 'label': sanitize_for_excel(str(label)), 'n_rows': nrows,
                            'filter_reason': 'error', 'evaluation': 'error',
                            'reasoning': f"Filter error: {e}", 'tabulation': '', 'duplicate_of': '',
                        }
                        file_results.append(err_row)
                        results.append(err_row)

                n_candidates = len(candidates)
                if n_candidates == 0:
                    continue

                logger.info("Processing: %s — 0/%d candidate columns", rel_path, n_candidates)

                for i, (col, label, filter_reason) in enumerate(candidates):
                    try:
                        if i > 0 and i % 10 == 0:
                            logger.info("Processing: %s — %d/%d", rel_path, i, n_candidates)

                        evaluation = check_column(df[col], col, label, nrows,
                                                  file_name=os.path.basename(file_path),
                                                  provider=provider, model=model,
                                                  test_mode=False)
                        result_row = {
                            'file'          : rel_path,
                            'sheet'         : sheet_name or '—',
                            'col_name'      : col,
                            'label'         : sanitize_for_excel(str(label)),
                            'n_rows'        : nrows,
                            'filter_reason' : filter_reason,
                            'evaluation'    : evaluation['evaluation'],
                            'reasoning'     : evaluation['reasoning'],
                            'tabulation'    : evaluation.get('tabulation', ''),
                            'duplicate_of'  : '',
                        }
                        file_results.append(result_row)
                        results.append(result_row)
                    except Exception as e:
                        logger.error("Skipping column '%s' in %s — evaluation error: %s", col, rel_path, e)
                        err_row = {
                            'file': rel_path, 'sheet': sheet_name or '—',
                            'col_name': col, 'label': sanitize_for_excel(str(label)), 'n_rows': nrows,
                            'filter_reason': filter_reason, 'evaluation': 'error',
                            'reasoning': f"Evaluation error: {e}", 'tabulation': '', 'duplicate_of': '',
                        }
                        file_results.append(err_row)
                        results.append(err_row)
                    finally:
                        if tracker:
                            tracker.record()

            primary_results[file_path] = file_results

            if results:
                save_results(results, issue_collector.issues, output_path)

        # --- Step 8: second pass — copy results for duplicates ---
        for file_path in files:
            primary = dup_lookup.get(file_path)
            if primary == file_path:
                continue

            rel_path = os.path.relpath(file_path, temp_dir)
            rel_primary = os.path.relpath(primary, temp_dir)

            if primary in primary_results:
                logger.info("Copying results for duplicate: %s (same as %s)", rel_path, rel_primary)
                for result_row in primary_results[primary]:
                    dup_row = result_row.copy()
                    dup_row['file']         = rel_path
                    dup_row['duplicate_of'] = rel_primary
                    results.append(dup_row)

        # --- Step 9: final save ---
        save_results(results, issue_collector.issues, output_path)
        if central_output_path:
            save_results(results, issue_collector.issues, central_output_path)

        # --- build summary ---
        results_df = pd.DataFrame(results) if results else pd.DataFrame()
        summary = {
            **archive_info,
            'n_files_total' : len(files),
            'n_data_files'  : n_data_files,
            'n_duplicates'  : n_duplicates,
            'n_checked'     : len(results_df[results_df['duplicate_of'] == '']) if not results_df.empty else 0,
            'n_direct_pii'  : (results_df['evaluation'] == 'direct_pii').sum() if not results_df.empty else 0,
            'n_indirect'    : (results_df['evaluation'] == 'possible_indirect').sum() if not results_df.empty else 0,
            'n_internal_id' : (results_df['evaluation'] == 'internal_id').sum() if not results_df.empty else 0,
            'n_warnings'    : sum(1 for i in issue_collector.issues if i['level'] == 'WARNING'),
            'n_errors'      : sum(1 for i in issue_collector.issues if i['level'] == 'ERROR'),
            'model'         : f"{provider}/{model}",
            'run_date'      : datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
            'output_path'   : output_path,
        }

        logger.info("Done. Total candidate columns checked: %d", summary['n_checked'])
        return summary

    finally:
        if tracker:
            tracker.flush()
        if _rmtree_retrying(temp_dir):
            logger.info("Temp dir deleted: %s", temp_dir)


def _rmtree_retrying(path, attempts=5, delay=0.5):
    """
    Deletes path, retrying on PermissionError/OSError (e.g. Windows still
    holding a file handle open — antivirus scan, a just-closed workbook).
    A plain shutil.rmtree(ignore_errors=True) gives up silently on the first
    failure and leaves the temp dir behind; this retries briefly instead and
    logs a warning if it still can't be removed.
    """
    for attempt in range(1, attempts + 1):
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if attempt < attempts:
                time.sleep(delay)
            else:
                logger.warning("Could not fully delete temp dir %s after %d attempts — "
                                "it may still contain locked files", path, attempts)
                return False


def _list_packages(folder):
    """Direct subfolders of folder, excluding __MACOSX."""
    return [
        os.path.join(folder, name)
        for name in sorted(os.listdir(folder))
        if os.path.isdir(os.path.join(folder, name)) and name != '__MACOSX'
    ]


def _save_overview(df, primary_path, max_fallbacks=5):
    """
    Saves df to primary_path (always tried first, so a later save naturally
    migrates back once the file is no longer locked). If primary_path is
    locked (PermissionError — e.g. open in Excel), falls back to numbered
    sibling files (<primary>.working.xlsx, <primary>.working2.xlsx, ...)
    until one write succeeds — a batch's progress is never lost to a locked
    overview file, just possibly saved under a different path until it's
    freed up. Once any save succeeds, other stale working-fallback files are
    superseded by it and get cleaned up; one that can't be deleted (e.g.
    also still open) is left alone and retried on a later save.
    """
    base, ext = os.path.splitext(primary_path)
    candidates = [primary_path] + [
        f"{base}.working{'' if i == 1 else i}{ext}" for i in range(1, max_fallbacks + 1)
    ]

    saved_path = None
    for path in candidates:
        try:
            df.to_excel(path, index=False)
            saved_path = path
            break
        except PermissionError:
            continue

    if saved_path is None:
        logger.warning("Could not save overview to %s or any fallback (all locked?) — will retry on next save", primary_path)
        return

    if saved_path != primary_path:
        logger.warning("Overview file locked — saved progress to %s instead", saved_path)

    for path in candidates:
        if path == saved_path or not os.path.exists(path):
            continue
        try:
            os.remove(path)
        except OSError:
            pass  # still locked/in use — leave it, cleaned up on a later successful save


_OVERVIEW_SUMMARY_COLUMNS = [
    'archive_files', 'archive_sizes_mb', 'archive_md5s',
    'n_files_total', 'n_data_files', 'n_duplicates', 'n_checked',
    'n_direct_pii', 'n_indirect', 'n_internal_id', 'n_warnings', 'n_errors',
    'model', 'run_date', 'output_path',
]
_OVERVIEW_COLUMNS = ['package_path', 'status', 'notes'] + _OVERVIEW_SUMMARY_COLUMNS


def run_folder(folder_path, provider=DEFAULT_PROVIDER, model=DEFAULT_MODEL,
                track_throughput=False):
    """
    Discovers all package subfolders in folder_path, processes any row
    currently 'pending' (including one just reset from 'running'), and
    maintains a resumable overview/status file at
    folder_path/pii_results_overview.xlsx. 'done', 'skip', and the terminal
    error states below are left untouched unless a human resets them back
    to 'pending' manually.

    Status values:
      pending  - not yet processed (default for newly discovered folders,
                 and the value a human sets manually to force a re-run of
                 a package that already has another terminal status)
      running  - currently being processed; reset to 'pending' on the next
                 run if found in this state (crash recovery)
      done     - finished successfully
      skip     - human-set only, never assigned by this function; excluded
                 from processing same as 'done', left untouched by
                 reconciliation
      '.rar archive present — extract manually or change archive format'
      'error — no files found'
      'error'
                 - terminal error states

    Columns: package_path, status, notes, plus every key in run_package's
    returned summary dict (archive_files, archive_sizes_mb, archive_md5s,
    n_files_total, n_data_files, n_duplicates, n_checked, n_direct_pii,
    n_indirect, n_internal_id, n_warnings, n_errors, model, run_date,
    output_path).

    'notes' is a free-text column, never written by this function — purely
    for a human to record why a package was marked 'skip'.
    """
    overview_path = os.path.join(folder_path, "pii_results_overview.xlsx")
    discovered = _list_packages(folder_path)

    def _blank_row(package_path):
        row = {col: None for col in _OVERVIEW_COLUMNS}
        row['package_path'] = package_path
        row['status'] = 'pending'
        return row

    if not os.path.exists(overview_path):
        overview = pd.DataFrame([_blank_row(p) for p in discovered], columns=_OVERVIEW_COLUMNS)
    else:
        overview = pd.read_excel(overview_path)
        for col in _OVERVIEW_COLUMNS:
            if col not in overview.columns:
                overview[col] = None
        # keep any extra column a human added (e.g. their own 'priority' column) —
        # standard columns first in a stable order, anything else preserved after
        extra_cols = [c for c in overview.columns if c not in _OVERVIEW_COLUMNS]
        overview = overview[_OVERVIEW_COLUMNS + extra_cols]

        # crash recovery: a package left 'running' by an interrupted run gets retried
        overview.loc[overview['status'] == 'running', 'status'] = 'pending'

        existing_paths = set(overview['package_path'])
        new_rows = [_blank_row(p) for p in discovered if p not in existing_paths]
        if new_rows:
            overview = pd.concat([overview, pd.DataFrame(new_rows, columns=_OVERVIEW_COLUMNS)], ignore_index=True)

    # object dtype on every column: avoids a crash writing mixed types (str/int/float)
    # into a column that round-tripped through Excel as all-empty float64
    for col in _OVERVIEW_COLUMNS:
        overview[col] = overview[col].astype(object)

    _save_overview(overview, overview_path)

    n_pending = int((overview['status'] == 'pending').sum())
    n_done = int((overview['status'] == 'done').sum())
    n_skipped = int((overview['status'] == 'skip').sum())
    logger.info("Overview: %d pending, %d done, %d skipped", n_pending, n_done, n_skipped)

    tracker = None
    if track_throughput:
        tracker = ThroughputTracker(os.path.join(folder_path, 'throughput_log.xlsx'))
        logger.info("Throughput log: %s", tracker.output_path)

    pending_indices = overview.index[overview['status'] == 'pending'].tolist()
    n_pending_total = len(pending_indices)

    for i, idx in enumerate(pending_indices, start=1):
        package_path = overview.at[idx, 'package_path']
        name = os.path.basename(os.path.normpath(package_path))
        output_path = os.path.join(package_path, "pii_check.xlsx")
        temp_base = os.path.join(package_path, "temp_pii_scan")
        os.makedirs(temp_base, exist_ok=True)

        overview.at[idx, 'status'] = 'running'
        _save_overview(overview, overview_path)

        logger.info("[%d/%d] Starting package: %s", i, n_pending_total, name)
        try:
            summary = run_package(package_path, output_path, temp_base=temp_base,
                                  provider=provider, model=model, tracker=tracker)

            if summary:
                for key, val in summary.items():
                    overview.at[idx, key] = val
                rar_files = [f for f in summary.get('archive_files', '').split('; ') if f.lower().endswith('.rar')]
                if rar_files:
                    overview.at[idx, 'status'] = '.rar archive present — extract manually or change archive format'
                elif summary.get('n_files_total', 0) == 0:
                    overview.at[idx, 'status'] = 'error — no files found'
                else:
                    overview.at[idx, 'status'] = 'done'
            else:
                overview.at[idx, 'status'] = 'error — no files found'

        except Exception as e:
            logger.error("Package failed: %s — %s", name, e, exc_info=True)
            overview.at[idx, 'status'] = 'error'

        finally:
            _rmtree_retrying(temp_base)  # run_package already cleans up its own subdir; this catches any leftovers
            logger.info("[%d/%d] Finished package: %s", i, n_pending_total, name)

        _save_overview(overview, overview_path)

    if tracker:
        tracker.flush()

