# main.py
# Main orchestrator for PII detection in replication packages. A library,
# invoked via interface.py — no __main__ entry point here.
#
# Two entry points:
#   1. Single package: run_package(folder_path, output_path)
#   2. Batch:          run_folder(folder_path) — auto-discovers package
#                      subfolders and maintains a resumable overview/status
#                      file (pii_checker_overview.xlsx) in that folder
#
# Pipeline per package (run_package):
#   0. Record archive info (SHA-256, size) before unzipping
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
    "openpyxl", "xlrd", "odf", "rdata", "scipy", "requests", "py7zr", "global_land_mask",
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
import tempfile
import datetime
import time
import pandas as pd
from copy import copy
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from loader import load_data
from column_filter import is_candidate_column, find_gps_candidates
from column_checker import check_column, sanitize_for_excel
from unzip_package import unzip_folder, _get_handler
from find_duplicities import find_duplicities, sha256_file
from version import __version__
from clean_package import clean_folder
from llm_client import DEFAULT_PROVIDER, DEFAULT_MODEL, OLLAMA_ENDPOINTS, reset_usage, get_usage, get_ollama_model_memory
import platform


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


def _get_archive_info(folder_path) -> dict:
    """Records SHA-256 and size of top-level archive files before extraction."""
    archives = [
        os.path.join(folder_path, f)
        for f in os.listdir(folder_path)
        if os.path.isfile(os.path.join(folder_path, f))
        and _get_handler(f) is not None
        and not f.startswith('._')
    ]

    names, sizes, digests = [], [], []
    for path in archives:
        name = os.path.basename(path)
        logger.info("Hashing archive: %s", name)
        names.append(name)
        sizes.append(round(os.path.getsize(path) / 1024 / 1024, 2))
        digests.append(sha256_file(path))

    return {
        'archive_files'    : '; '.join(names),
        'archive_sizes_mb' : '; '.join(str(s) for s in sizes),
        'archive_sha256s'  : '; '.join(digests),
    }


def _code_info() -> dict:
    """Returns the git remote URL (normalised to https) and current commit of
    this checkout, so a results file records which code produced it. Fields are
    empty when the code is not a git checkout or git is unavailable."""
    import subprocess
    code_dir = os.path.dirname(os.path.abspath(__file__))

    def _git(*args):
        try:
            return subprocess.run(['git', *args], cwd=code_dir, capture_output=True,
                                  text=True, timeout=5, check=True).stdout.strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            return ''

    url = _git('config', '--get', 'remote.origin.url')
    if url.startswith('git@'):                      # git@host:owner/repo.git -> https://host/owner/repo
        url = 'https://' + url[4:].replace(':', '/', 1)
    if url.endswith('.git'):
        url = url[:-4]

    commit = _git('rev-parse', '--short', 'HEAD')
    if commit and _git('status', '--porcelain'):
        commit += '+dirty'

    return {'code_url': url, 'code_commit': commit}


def _count_results(results, issues) -> dict:
    """Result counts shared by the Metadata/Overview sheets and the run_package() summary."""
    return {
        'n_columns_checked': sum(1 for r in results if r.get('duplicate_of') == ''),
        'n_direct_pii'     : sum(1 for r in results if r.get('evaluation') == 'direct_pii'),
        'n_indirect'       : sum(1 for r in results if r.get('evaluation') == 'possible_indirect'),
        'n_internal_id'    : sum(1 for r in results if r.get('evaluation') == 'internal_id'),
        'n_warnings'       : sum(1 for i in issues if i['level'] == 'WARNING'),
        'n_errors'         : sum(1 for i in issues if i['level'] == 'ERROR'),
    }


# Overview sheet: a short, readable selection of the metadata for reviewers (first tab).
# The Metadata sheet (last tab) holds everything, including these, under the raw keys.
_OVERVIEW_FIELDS = [
    ('package_name'      , 'Package'),
    ('n_files_total'     , 'Files found'),
    ('n_data_files'      , 'Data files loaded'),
    ('n_columns_checked' , 'Columns checked'),
    ('n_direct_pii'      , 'Flagged: direct PII'),
    ('n_indirect'        , 'Flagged: possible indirect PII'),
    ('n_internal_id'     , 'Flagged: internal ID'),
    ('n_warnings'        , 'Warnings'),
    ('n_errors'          , 'Errors'),
    ('model'             , 'LLM model'),
    ('ended'             , 'Run finished'),
]


def _build_overview(metadata) -> pd.DataFrame:
    rows = [{'Metric': label, 'Value': metadata.get(key, '')} for key, label in _OVERVIEW_FIELDS]
    for row in rows:
        if row['Metric'] == 'Run finished' and not row['Value']:
            row['Value'] = 'not finished (partial results)'
    return pd.DataFrame(rows)


# Results sheet order: checked columns by evaluation (unexpected labels after not_pii),
# then columns not checked (one row per file/sheet), then files not checked.
_RESULTS_ORDER = ['error', 'direct_pii', 'possible_indirect', 'internal_id', 'not_pii']
_RESULTS_FIELDS = ['file', 'sheet', 'col_name', 'evaluation', 'reasoning']

# load_data() return value -> why the file was not checked
_NOT_LOADED_REASONS = {
    'skipped'    : 'not a data file',
    'unsupported': 'data format not supported',
}


def _with_dup_note(reason, duplicate_of):
    return f"{reason} (NOTE: duplicate of {duplicate_of})" if duplicate_of else reason


def _build_results(results, skipped_cols, skipped_files) -> pd.DataFrame:
    """One row per checked column, per file/sheet with unchecked columns, and per unchecked file."""
    checked = sorted(results, key=lambda r: _RESULTS_ORDER.index(r['evaluation'])
                     if r['evaluation'] in _RESULTS_ORDER else len(_RESULTS_ORDER))
    rows = [{'file': r.get('file', ''), 'sheet': r.get('sheet', '—'), 'col_name': r.get('col_name', ''),
             'evaluation': r['evaluation'],
             'reasoning': _with_dup_note(r.get('reasoning', ''), r.get('duplicate_of', ''))}
            for r in checked]
    rows += [{'file': s['file'], 'sheet': s['sheet'], 'col_name': ', '.join(map(str, s['columns'])),
              'evaluation': 'columns not checked',
              'reasoning': _with_dup_note(f"{len(s['columns'])} column(s) filtered out as unlikely PII",
                                          s['duplicate_of'])} for s in skipped_cols]
    rows += [{'file': f['file'], 'sheet': '—', 'col_name': '', 'evaluation': 'file not checked',
              'reasoning': _with_dup_note(f['reason'], f['duplicate_of'])} for f in skipped_files]
    return pd.DataFrame(rows, columns=_RESULTS_FIELDS)


# Row fill (and font colour) per evaluation on the Results/Detail sheets
_EVALUATION_STYLES = {
    'error'              : ('C00000', 'FFFFFF'),
    'direct_pii'         : ('FFD9D9', None),
    'possible_indirect'  : ('FFEFC2', None),
    'internal_id'        : ('DDF2DD', None),
    'not_pii'            : ('DCE8F7', None),
    'columns not checked': ('EEEEEE', None),
    'file not checked'   : ('D4D4D4', None),
}


def _set_font(cell, **attrs):
    """Changes only the given font attributes, keeping the cell's font name and size."""
    font = copy(cell.font)
    for key, value in attrs.items():
        setattr(font, key, value)
    cell.font = font


def _style_sheet(ws, evaluation_header):
    """Bold header row; colour the evaluation column by its value.
    Colours use conditional formatting rather than cell styles: styled cells lose
    LibreOffice's automatic full-height display of multi-line values (e.g. tabulation)."""
    header = [c.value for c in ws[1]]
    for cell in ws[1]:
        _set_font(cell, bold=True)
    if evaluation_header not in header or ws.max_row < 2:
        return
    evaluation_col = get_column_letter(header.index(evaluation_header) + 1)
    cell_range = f"{evaluation_col}2:{evaluation_col}{ws.max_row}"
    for evaluation, (fill, font_color) in _EVALUATION_STYLES.items():
        ws.conditional_formatting.add(cell_range, FormulaRule(
            formula=[f'${evaluation_col}2="{evaluation}"'],
            fill=PatternFill('solid', start_color=fill, end_color=fill, bgColor=fill),
            font=Font(color=font_color) if font_color else None))


def _fit_columns_before(ws, stop_header, max_width=40):
    """Widens every column left of stop_header to its longest entry, capped at max_width."""
    for cells in ws.iter_cols():
        if cells[0].value == stop_header:
            break
        width = max(len(str(c.value)) for c in cells if c.value is not None)
        ws.column_dimensions[cells[0].column_letter].width = min(width, max_width) + 2


def _format_key_column(ws):
    """Bold first column, widened to fit its longest entry."""
    width = 0
    for (cell,) in ws.iter_rows(max_col=1):
        _set_font(cell, bold=True)
        width = max(width, len(str(cell.value or '')))
    ws.column_dimensions['A'].width = width + 3  # bold text runs a little wider


def save_results(results, issues, output_path, metadata=None, skipped_cols=(), skipped_files=()):
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        if metadata:
            _build_overview(metadata).to_excel(writer, sheet_name='Overview', index=False, header=False)
            _format_key_column(writer.sheets['Overview'])
        _build_results(results, skipped_cols, skipped_files).to_excel(
            writer, sheet_name='Results', index=False)
        if results:
            pd.DataFrame(results).to_excel(writer, sheet_name='Detail', index=False)
        else:
            pd.DataFrame([{'message': 'No variables were evaluated'}]).to_excel(
                writer, sheet_name='Detail', index=False)
        _style_sheet(writer.sheets['Results'], 'evaluation')
        _fit_columns_before(writer.sheets['Results'], 'reasoning')
        _style_sheet(writer.sheets['Detail'], 'evaluation')
        _fit_columns_before(writer.sheets['Detail'], 'reasoning')
        if issues:
            pd.DataFrame(issues).to_excel(writer, sheet_name='Issues', index=False)
        if metadata:
            pd.DataFrame({'key': list(metadata), 'value': list(metadata.values())}).to_excel(
                writer, sheet_name='Metadata', index=False, header=False)
            _format_key_column(writer.sheets['Metadata'])
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

    # --- Step 0: run metadata (written to the Metadata sheet of pii_checker_results.xlsx, and a
    # selection of it to the Overview sheet) ---
    reset_usage()
    started_at = datetime.datetime.now()
    metadata = {
        'package_name'   : os.path.basename(os.path.abspath(folder_path)),
        'package_path'   : os.path.abspath(folder_path),
        **archive_info,
        'version'        : __version__,
        **_code_info(),
        'python_version' : platform.python_version(),
        'provider'       : provider,
        'model'          : model,
        'ollama_endpoint': OLLAMA_ENDPOINTS[0] if provider == 'ollama' and OLLAMA_ENDPOINTS else '',
        'started'        : started_at.strftime('%Y-%m-%d %H:%M:%S'),
        'ended'          : '',
        'duration_s'     : '',
    }
    n_columns_total = 0
    n_columns_candidate = 0

    def _update_metadata(files, n_data_files, n_duplicates, results):
        """Refreshes the counts — called before every save, so a partial-results save
        mid-run shows the numbers so far."""
        metadata.update({
            'n_files_total'      : len(files),
            'n_data_files'       : n_data_files,
            'n_duplicates'       : n_duplicates,
            # every file is a duplicate (results copied from its primary), a loaded data
            # file, or neither — the last group is never checked for PII
            'n_files_not_loaded' : len(files) - n_duplicates - n_data_files,
            'n_columns_total'    : n_columns_total,
            'n_columns_candidate': n_columns_candidate,
            **_count_results(results, issue_collector.issues),
        })

    def _finalize_metadata(files, n_data_files, n_duplicates, results):
        ended_at = datetime.datetime.now()
        metadata['ended'] = ended_at.strftime('%Y-%m-%d %H:%M:%S')
        metadata['duration_s'] = round((ended_at - started_at).total_seconds(), 1)
        _update_metadata(files, n_data_files, n_duplicates, results)
        metadata.update(get_usage())
        if provider == 'ollama':
            metadata.update(get_ollama_model_memory(model))

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
        skipped_cols = []    # per file/sheet: columns the filter left out
        skipped_files = []   # files never loaded as data
        primary_skipped_cols = {}
        primary_not_loaded = {}
        n_data_files = 0

        # --- Step 7: first pass — process primaries ---
        for file_path in files:
            if dup_lookup.get(file_path) != file_path:
                continue

            rel_path = os.path.relpath(file_path, temp_dir)
            loaded = load_data(file_path)

            if not loaded or isinstance(loaded, str):
                reason = _NOT_LOADED_REASONS.get(loaded, 'failed to load or empty')
                primary_not_loaded[file_path] = reason
                skipped_files.append({'file': rel_path, 'reason': reason, 'duplicate_of': ''})
                continue

            n_data_files += 1
            file_results = []
            file_skipped_cols = []

            for fp, sheet_name, df, labels, nrows in loaded:

                try:
                    gps_candidates = find_gps_candidates(df)
                except Exception as e:
                    logger.error("GPS pair check failed in %s: %s", rel_path, e)
                    gps_candidates = {}

                candidates = []
                filter_errors = set()
                n_columns_total += len(df.columns)
                for col in df.columns:
                    try:
                        label = labels.get(col, col) if labels else col
                        if col in gps_candidates:
                            candidates.append((col, label, gps_candidates[col], True))
                            continue
                        is_candidate, filter_reason = is_candidate_column(df[col], label=label)
                        if is_candidate:
                            candidates.append((col, label, filter_reason, False))
                    except Exception as e:
                        logger.error("Skipping column '%s' in %s — filter error: %s", col, rel_path, e)
                        filter_errors.add(col)
                        label = labels.get(col, col) if labels else col
                        err_row = {
                            'file': rel_path, 'sheet': sheet_name or '—',
                            'col_name': col, 'label': sanitize_for_excel(str(label)), 'n_rows': nrows,
                            'filter_reason': 'error', 'evaluation': 'error',
                            'reasoning': f"Filter error: {e}", 'tabulation': '', 'duplicate_of': '',
                        }
                        file_results.append(err_row)
                        results.append(err_row)

                candidate_cols = {c[0] for c in candidates}
                filtered_out = [c for c in df.columns
                                if c not in candidate_cols and c not in filter_errors]
                if filtered_out:
                    row = {'file': rel_path, 'sheet': sheet_name or '—',
                           'columns': filtered_out, 'duplicate_of': ''}
                    file_skipped_cols.append(row)
                    skipped_cols.append(row)

                n_candidates = len(candidates)
                n_columns_candidate += n_candidates
                if n_candidates == 0:
                    continue

                logger.info("Processing: %s — 0/%d candidate columns", rel_path, n_candidates)

                for i, (col, label, filter_reason, is_gps) in enumerate(candidates):
                    try:
                        if i > 0 and i % 10 == 0:
                            logger.info("Processing: %s — %d/%d", rel_path, i, n_candidates)

                        evaluation = check_column(df[col], col, label, nrows,
                                                  file_name=os.path.basename(file_path),
                                                  provider=provider, model=model,
                                                  test_mode=False, gps_candidate=is_gps)
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
            primary_skipped_cols[file_path] = file_skipped_cols

            if results:
                _update_metadata(files, n_data_files, n_duplicates, results)
                save_results(results, issue_collector.issues, output_path, metadata=metadata,
                             skipped_cols=skipped_cols, skipped_files=skipped_files)

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
                for skipped in primary_skipped_cols[primary]:
                    skipped_cols.append({**skipped, 'file': rel_path, 'duplicate_of': rel_primary})
            elif primary in primary_not_loaded:
                skipped_files.append({'file': rel_path, 'reason': primary_not_loaded[primary],
                                      'duplicate_of': rel_primary})

        # --- Step 9: final save ---
        _finalize_metadata(files, n_data_files, n_duplicates, results)
        save_results(results, issue_collector.issues, output_path, metadata=metadata,
                     skipped_cols=skipped_cols, skipped_files=skipped_files)
        if central_output_path:
            save_results(results, issue_collector.issues, central_output_path, metadata=metadata,
                         skipped_cols=skipped_cols, skipped_files=skipped_files)

        # --- build summary ---
        counts = _count_results(results, issue_collector.issues)
        summary = {
            **archive_info,
            'n_files_total' : len(files),
            'n_data_files'  : n_data_files,
            'n_duplicates'  : n_duplicates,
            'n_checked'     : counts['n_columns_checked'],
            'n_direct_pii'  : counts['n_direct_pii'],
            'n_indirect'    : counts['n_indirect'],
            'n_internal_id' : counts['n_internal_id'],
            'n_warnings'    : counts['n_warnings'],
            'n_errors'      : counts['n_errors'],
            'model'         : f"{provider}/{model}",
            'version'       : __version__,
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
    'archive_files', 'archive_sizes_mb', 'archive_sha256s',
    'n_files_total', 'n_data_files', 'n_duplicates', 'n_checked',
    'n_direct_pii', 'n_indirect', 'n_internal_id', 'n_warnings', 'n_errors',
    'model', 'version', 'run_date', 'output_path',
]
_OVERVIEW_COLUMNS = ['package_path', 'status', 'notes'] + _OVERVIEW_SUMMARY_COLUMNS


def run_folder(folder_path, provider=DEFAULT_PROVIDER, model=DEFAULT_MODEL,
                track_throughput=False):
    """
    Discovers all package subfolders in folder_path, processes any row
    currently 'pending' (including one just reset from 'running'), and
    maintains a resumable overview/status file at
    folder_path/pii_checker_overview.xlsx. 'done', 'skip', and the terminal
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
    returned summary dict (archive_files, archive_sizes_mb, archive_sha256s,
    n_files_total, n_data_files, n_duplicates, n_checked, n_direct_pii,
    n_indirect, n_internal_id, n_warnings, n_errors, model, version, run_date,
    output_path).

    'notes' is a free-text column, never written by this function — purely
    for a human to record why a package was marked 'skip'.
    """
    overview_path = os.path.join(folder_path, "pii_checker_overview.xlsx")
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
        output_path = os.path.join(package_path, "pii_checker_results.xlsx")
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

