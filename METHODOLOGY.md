# Methodology

This describes how this tool works. See [README.md](README.md) for installation and usage.

**1. Data preparation**

Replication packages are unzipped (archive formats supported: `.zip`, `.7z`, `.tar`, `.tar.gz`, `.tgz`, `.tar.bz2`, and `.gz`), junk files (`.DS_Store`, `__MACOSX`, etc.) are removed, and duplicate files (by content hash) are detected so they're only evaluated once. Data files are then loaded if their format is supported: plain text tabular data (CSV/TSV/TXT/DAT — delimiter auto-detected), Excel (`.xls`/`.xlsx`), OpenDocument (`.ods`), Stata (`.dta`), SPSS (`.sav`/`.zsav`/`.por`), SAS (`.sas7bdat`/`.xpt`), MATLAB (`.mat`), and R (`.rds`/`.rdata`/`.rda`, data-frame-like objects only). Variable labels are used where the format carries them (Stata/SPSS/SAS). JSON and tabular data embedded in `.doc`/`.docx`/PDF are not yet supported.

**2. Column filtering**

Not every column is worth sending to an LLM. Variables that are unlikely to be PII are skipped to speed up the process. Each column is checked against these criteria, in order:

- **Empty columns** (no non-missing values) are skipped.
- **Boolean columns** are skipped — considered never PII.
- **Name/label always-include patterns**: a column is always sent, regardless of its data type or values, if its name or label matches a fixed set of sensitive-topic patterns — age, birth date/DOB, ethnicity, race, religion, disability, etc., in English and Spanish.
- **Platform participant ID patterns**: a column is always sent if its name or label suggests a crowdsourcing platform ID — an exact name of `workerid`, or a name/label containing "prolific" or "mturk".
- **Datetime columns** are always sent.
- **Possible GPS coordinates**: numeric columns are sent if every value is within ±180 with at least 3 decimal places.
- **Small numeric values** (maximum absolute value under 1,000) are skipped — covers Likert-scale items, ages, and simple counts.
- **Large floats with real decimals** (maximum ≥1,000 but not a whole number) are skipped.
- **Large integers** (maximum ≥1,000, no real decimals) are sent — possible phone numbers, IDs, or zip codes.
- **Short strings** (longest value under 4 characters) are skipped.
- **Categorical strings** are skipped: at most 20 unique values, each appearing at least 5 times, and unique values making up less than 10% of all values.
- **All other string columns** are sent as candidates.
- Any other, unrecognized data type is sent, to be safe.

**3. Data tabulation**

Filtered-in columns aren't sent to the LLM as raw values — a value tabulation is built instead. If a column has 50 or fewer unique values, all of them are sent. Above that, a sample is built instead: the 10 longest values, the 20 least frequent, 20 random values, plus up to 15 values for cells matched by a regular expression as possibly including one of three identifiers — email addresses, web addresses, and long digit sequences (phone numbers, account numbers, etc.). These are shown to the LLM under a generic heading so the model isn't primed.

**This means not every individual value in a high-cardinality column is necessarily reviewed** — the tabulation is an intentionally constructed sample, not an exhaustive one. In practice this is a trade-off between precision and speed, and it means the pipeline is a screening aid, not a value-by-value guarantee.

**4. LLM evaluation**

The tabulation of data, along with column name, label, data type, and row count, is sent to the configured LLM in a single fixed prompt, asking for a JSON response with `reasoning` and `evaluation`. Possible `evaluation` values:

- **direct_pii** — directly identifies individuals (names, emails, phone numbers, precise locations/coordinates, IP addresses)
- **internal_id** — unique internal identifier (respondent ID, household ID, etc.)
- **possible_indirect** — could help identify individuals in combination with other data (village names, detailed demographics, rare occupations)
- **not_pii** — categorical, coded, or clearly non-identifying data
- **consent** — the column records consent status/details rather than substantive data

Independently of the LLM, deterministic pattern checks (Prolific/MTurk ID formats, public IP addresses) run on the same column. If one of those matches triggers, the final result is forced to `direct_pii` regardless of what the LLM said — the model's own answer is kept in the reasoning text rather than discarded, so a disagreement is still visible.

**5. Output**

Every checked column gets a row in `pii_check.xlsx` with its evaluation, the model's one-sentence reasoning, and the exact tabulation it was shown — so a flagged (or cleared) column can be audited. Columns skipped are not reported in the output file.

Tip: use filtering in Excel to quickly see the columns labelled as PII.

## LLM model comparison

The pipeline is model-agnostic (any Ollama-served model) — accuracy and speed both depend on which model is used.

For our paper — [*On the Prevalence of Personally Identifiable Information (PII) in the Social Sciences*](https://www.econstor.eu/handle/10419/343373) — we used **Gemma 4 31B**. We separately ran an offline test pass with **Gemma 4 e4b** (a smaller model, viable on consumer-level hardware) and the results are promising. A proper systematic comparison of model performance is still in progress and will be reported separately.

## What this tool does and doesn't do

- It is designed to detect **direct identifiers**. Columns may also be flagged as possible indirect identifiers, but there is no holistic evaluation of overall reidentification risk.
- It only searches for PII in **tabular data** — documents (`.pdf`, `.doc`) and multimedia files are not evaluated.
- Supported file formats: Excel (`.xls`, `.xlsx`), plain text data (`.csv`, `.tsv`, `.txt`), Stata (`.dta`), R (`.rds`, `.rdata`), SPSS (`.sav`), SAS (`.sas7bdat`, `.xpt`), Matlab (`.mat`), and OpenDocument Spreadsheet (`.ods`).
- With the default local Ollama setup, no data ever leaves your computer. A third-party API (Anthropic/OpenAI) is only ever used if you deliberately opt into one, and that's only recommended for testing or benchmarking on synthetic data, not on data that may contain real PII.
