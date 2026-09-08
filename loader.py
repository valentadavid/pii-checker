# This code detects file extension and opens data files as a data frame
# currently supported formats:
    # csv, tsv, tab, txt (if detectable structure) and dat (if csv like structure)
    # xlsx, xls
    # ods
    # dta (Stata)
    # sav/zsav/por (SPSS)
    # mat (Matlab)
    # rds, rdata, rda (R) - now only for data frame-like objects - tested for data.frame, tibble and data.table
    # sas7bdat/xpt (SAS)

# not implemented
    # json
    # parquet
    # tabular data in doc/docx and pdf



import csv
import chardet
import logging
import os
import pandas as pd
import pyreadstat
import pyreadr
import openpyxl
import xlrd
import rdata as rdatalib
import numpy as np
from scipy.io import loadmat

logger = logging.getLogger(__name__)

def _identity_labels(df):
    """For formats without meaningful labels, map each column name to itself."""
    return {col: col for col in df.columns}

def _pyreadstat_labels(df, meta):
    """For Stata/SPSS/SAS, use column label where available, fall back to column name."""
    return {col: label if label else col
            for col, label in zip(meta.column_names, meta.column_labels)}

# Function that reads data files in a way depending on the file format
# Returns a list of tuples: (file_path, sheet_name, df, labels, nrows)
#   file_path  - full path to the file
#   sheet_name - sheet/object name, None if not applicable
#   df         - the loaded dataframe
#   labels     - dict of {column_name: label} for all columns
#   nrows      - number of rows in the dataframe
def load_data(file_path, max_rows=None):
    _, ext = os.path.splitext(file_path.lower())
    stem = os.path.splitext(os.path.basename(file_path))[0]

    try:
        # --- CSV / text ---
        if ext in ['.csv', '.tsv', '.tab']:
            with open(file_path, 'rb') as f:
                raw = f.read(16384)

            detected = chardet.detect(raw)
            encoding = detected['encoding'] or 'utf-8'
            confidence = detected['confidence']

            if encoding.lower() not in ('utf-8', 'ascii') and confidence >= 0.5:
                logger.warning("File %s appears to be %s (confidence: %.0f%%), not UTF-8",
                               file_path, encoding, confidence * 100)

            if ext == '.csv':
                try:
                    sep = csv.Sniffer().sniff(raw.decode(encoding, errors='replace')).delimiter
                except csv.Error:
                    logger.warning("Could not detect delimiter for %s — defaulting to comma", file_path)
                    sep = ','
            else:
                sep = '\t'

            try:
                df = pd.read_csv(file_path, sep=sep, nrows=max_rows, encoding=encoding)
            except UnicodeDecodeError:
                logger.warning("Encoding error reading %s — retrying with UTF-8", file_path)
                df = pd.read_csv(file_path, sep=sep, nrows=max_rows, encoding='utf-8', encoding_errors='replace')
            except Exception:
                logger.warning("Could not read %s — retrying with UTF-8 and skipping bad lines", file_path)
                df = pd.read_csv(file_path, sep=sep, nrows=max_rows, encoding='utf-8',
                                 encoding_errors='replace', on_bad_lines='skip')
            return [(file_path, None, df, _identity_labels(df), len(df))]

        # -- investigate if txt file seems to be a data file + treat .dat (generic format) same way
        elif ext in ['.txt', '.dat']:
            with open(file_path, 'rb') as f:
                raw = f.read(16384)

            detected = chardet.detect(raw)
            encoding = detected['encoding'] or 'utf-8'
            confidence = detected['confidence']

            if encoding.lower() not in ('utf-8', 'ascii') and confidence >= 0.5:
                logger.warning("File %s appears to be %s (confidence: %.0f%%), not UTF-8",
                               file_path, encoding, confidence * 100)

            sample = raw.decode(encoding, errors='replace')

            try:
                dialect = csv.Sniffer().sniff(sample)

                VALID_DELIMITERS = {'\t', ',', ';', '|'}
                if dialect.delimiter not in VALID_DELIMITERS:
                    logger.info("Skipping %s — looks like plain text, not tabular", file_path)
                    return []

                lines = [l for l in sample.splitlines() if l.strip()]
                col_counts = [line.count(dialect.delimiter) for line in lines[:10]]
                is_consistent = len(set(col_counts)) == 1 and col_counts[0] > 0

                if is_consistent:
                    try:
                        df = pd.read_csv(file_path, sep=dialect.delimiter, nrows=max_rows, encoding=encoding)
                    except UnicodeDecodeError:
                        logger.warning("Encoding error reading %s — retrying with UTF-8", file_path)
                        df = pd.read_csv(file_path, sep=dialect.delimiter, nrows=max_rows, encoding='utf-8',
                                         encoding_errors='replace')
                    except Exception:
                        logger.warning("Could not read %s — retrying with UTF-8 and skipping bad lines", file_path)
                        df = pd.read_csv(file_path, sep=dialect.delimiter, nrows=max_rows, encoding='utf-8',
                                         encoding_errors='replace', on_bad_lines='skip')
                    return [(file_path, None, df, _identity_labels(df), len(df))]
                else:
                    logger.info("Skipping %s — looks like plain text, not tabular", file_path)
                    return []

            except csv.Error:
                logger.info("Skipping %s — could not detect structure", file_path)
                return []

        # --- Excel ---
        elif ext in ['.xls', '.xlsx']:
            engines = ['xlrd', 'openpyxl'] if ext == '.xls' else ['openpyxl', 'xlrd']
            sheets = None
            for engine in engines:
                try:
                    sheets = pd.read_excel(file_path, sheet_name=None, engine=engine)
                    break
                except Exception:
                    continue
            if sheets is None:
                logger.error("Could not read %s with any engine", file_path)
                return []
            if len(sheets) > 1:
                logger.info("Found %d sheets in %s: %s", len(sheets), file_path, list(sheets.keys()))
            return [
                (file_path, sheet_name, sheet_df.head(max_rows) if max_rows else sheet_df, _identity_labels(sheet_df),
                 len(sheet_df))
                for sheet_name, sheet_df in sheets.items()
            ]

        # --- ODS ---
        elif ext == '.ods':
            sheets = pd.read_excel(file_path, sheet_name=None, engine='odf')
            if len(sheets) > 1:
                logger.info("Found %d sheets in %s: %s", len(sheets), file_path, list(sheets.keys()))
            return [
                (file_path, sheet_name, sheet_df.head(max_rows) if max_rows else sheet_df, _identity_labels(sheet_df),
                 len(sheet_df))
                for sheet_name, sheet_df in sheets.items()
            ]

        # --- Stata ---
        # + handling for when file is too large > memory fail - limit number of rows
        # + handling encoding not utf error
        elif ext == '.dta':
            df, meta = None, None

            # --- try pyreadstat first (preserves labels) ---
            for enc in ['utf-8', 'latin-1', 'cp1252', 'cp1250', 'cp1251', 'cp1256']:
                try:
                    df, meta = pyreadstat.read_dta(file_path, apply_value_formats=True,
                                                   row_limit=max_rows or 0, encoding=enc)
                    break
                except Exception as e:
                    e_str = str(e)
                    if 'Unable to allocate' in e_str:
                        logger.warning("Memory error loading %s — retrying with 10000 rows", file_path)
                        df, meta = pyreadstat.read_dta(file_path, apply_value_formats=True,
                                                       row_limit=10000, encoding=enc)
                        break
                    elif 'codec can' in e_str or 'decode' in e_str or 'character set' in e_str or 'convert string' in e_str:
                        continue
                    else:
                        raise

            # --- fallback to pd.read_stata ---
            if df is None:
                logger.warning("pyreadstat failed for %s — falling back to pd.read_stata (no labels)", file_path)
                try:
                    df = pd.read_stata(file_path, convert_categoricals=True)
                except Exception:
                    try:
                        df = pd.read_stata(file_path, convert_categoricals=False)
                    except Exception as e:
                        logger.error("Could not load %s: %s", file_path, e)
                        return []
                return [(file_path, None, df, _identity_labels(df), len(df))]

            return [(file_path, None, df, _pyreadstat_labels(df, meta), len(df))]

        # --- SPSS ---
        elif ext in ['.sav', '.zsav', '.por']:
            reader = pyreadstat.read_por if ext == '.por' else pyreadstat.read_sav
            df, meta = reader(file_path, apply_value_formats=True, row_limit=max_rows or 0)
            return [(file_path, None, df, _pyreadstat_labels(df, meta), len(df))]

        # --- MATLAB ---
        # struct arrays (dtype.names set) are expanded into real named columns —
        # wrapping them as a single opaque structured-dtype column instead crashes
        # on nearly any pandas op downstream, and silently hides whatever the
        # struct's fields hold (e.g. a per-respondent struct with name/age fields)
        elif ext == '.mat':
            def _unwrap_mat_scalar(v):
                if isinstance(v, np.ndarray):
                    v = v.squeeze()
                    if v.ndim == 0:
                        return v.item()
                    if v.size == 1:
                        return v.reshape(-1)[0]
                    return v.tolist()
                return v

            mat = loadmat(file_path)
            outputs = []
            for name, obj in mat.items():
                if name.startswith('__'):
                    continue
                if not isinstance(obj, np.ndarray):
                    continue
                squeezed = obj.squeeze()

                if squeezed.dtype.names:
                    records = np.atleast_1d(squeezed).ravel()
                    field_data = {
                        field: [_unwrap_mat_scalar(records[field][i]) for i in range(records.shape[0])]
                        for field in squeezed.dtype.names
                    }
                    df = pd.DataFrame(field_data)
                    outputs.append((file_path, name, df, _identity_labels(df), len(df)))
                elif squeezed.ndim == 1:
                    df = pd.DataFrame({name: squeezed})
                    outputs.append((file_path, name, df, _identity_labels(df), len(df)))
                elif squeezed.ndim == 2:
                    df = pd.DataFrame(squeezed)
                    outputs.append((file_path, name, df, _identity_labels(df), len(df)))
                else:
                    logger.info("Skipping MATLAB variable '%s' in %s — unsupported shape (ndim=%d)",
                                name, file_path, squeezed.ndim)
            return outputs


        # # --- SAS --- NEEDS TO HANDLE DATA LABELS FIRST
        elif ext == '.sas7bdat':
             df, meta = pyreadstat.read_sas7bdat(file_path, row_limit=max_rows or 0)
             return [(file_path, None, df, _pyreadstat_labels(df, meta), len(df))]


        elif ext in ['.xpt', '.xpt5', '.xpt8']:
            df, meta = pyreadstat.read_xport(file_path, row_limit=max_rows or 0)
            return [(file_path, None, df, _pyreadstat_labels(df, meta), len(df))]


        # --- R formats ---
        # + with handling of oddly formatted RData
        # + SafeConverter to handle unknown/broken R classes (e.g. conjointDesign)

        elif ext in ['.rds', '.rdata', '.rda']:
            import gzip as _gzip
            import io as _io
            import warnings
            from rdata.conversion._conversion import DEFAULT_CLASS_MAP

            def safe_dataframe_constructor(obj, attrs):
                squeezed = {k: v.ravel() if isinstance(v, np.ndarray) and v.ndim == 2 and v.shape[1] == 1 else v
                            for k, v in obj.items()}
                columns = attrs.get('names', None)
                row_names = attrs.get('row.names', None)
                index = row_names if isinstance(row_names, np.ndarray) else None
                try:
                    return pd.DataFrame(squeezed, columns=columns, index=index)
                except Exception:
                    return pd.DataFrame(obj)

            class SafeConverter(rdatalib.conversion._conversion.SimpleConverter):
                def _convert_next(self, obj):
                    try:
                        return super()._convert_next(obj)
                    except Exception:
                        return None

            custom_map = dict(DEFAULT_CLASS_MAP)
            custom_map['data.frame'] = safe_dataframe_constructor

            def _parse_rdata(fileobj_or_path):
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    parsed = rdatalib.parser.parse_file(fileobj_or_path)
                    converter = SafeConverter(constructor_dict=custom_map)
                    return converter.convert(parsed)

            # detect gzip-compressed RDS saved with .RData extension
            with open(file_path, 'rb') as _f:
                _magic = _f.read(2)
            _is_gzip = _magic == b'\x1f\x8b'

            result = None
            try:
                result = pyreadr.read_r(file_path)
            except Exception as e:
                logger.warning("pyreadr failed for %s (%s) — trying rdata package", file_path, e)
                try:
                    if _is_gzip:
                        with _gzip.open(file_path, 'rb') as gf:
                            result = _parse_rdata(_io.BytesIO(gf.read()))
                    else:
                        result = _parse_rdata(file_path)
                except Exception as e2:
                    logger.error("Could not load R file %s: %s", file_path, e2)
                    return []

            if not result:
                return []

            outputs = []
            for name, obj in result.items():
                if isinstance(obj, pd.DataFrame):
                    outputs.append((file_path, name or stem, obj, _identity_labels(obj), len(obj)))
                else:
                    logger.info("Skipping R object '%s' in %s — not a dataframe (%s)",
                                name, file_path, type(obj).__name__)
            return outputs

        # # --- JSON ---
        # elif ext == '.json':
        #     with open(file_path) as f:
        #         raw = json.load(f)
        #     df = pd.json_normalize(raw) if isinstance(raw, (dict, list)) else pd.read_json(file_path)
        #     return [(file_path, None, df, _identity_labels(df), len(df))]
        #
        #
        # # --- Parquet ---
        # elif ext == '.parquet':
        #     df = pd.read_parquet(file_path)
        #     return [(file_path, None, df, _identity_labels(df), len(df))]
        #
        # else:
        #     logger.warning("Skipping unsupported format: %s (%s)", ext, file_path)
        #     return []

        else:
            if ext in {'.json', '.parquet'}: # known unsupported data formats
                logger.error("Skipping %s — format not yet implemented", file_path)
                return 'unsupported'
            else:
                logger.info("Skipping %s — likely not a data file", file_path)
                return 'skipped'

    except Exception as e:
        logger.error("Error loading %s: %s", file_path, e, exc_info=True)
        return []