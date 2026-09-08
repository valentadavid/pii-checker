# unzip_package.py
# Extracts all archive files in a folder
# Each archive is extracted into a subfolder named after the archive (e.g. data-zip, data-7z).
# Supported formats: .zip, .tar, .tar.gz, .tar.bz2, .tgz, .gz, .7z
# .rar is NOT supported

# Called by main.py (run_package, Step 4) after the temp working dir is populated —
# not meant to be run as a separate pre-step.

import os
import sys
import gzip
import logging
import shutil
import tarfile
import zipfile
import py7zr

logger = logging.getLogger(__name__)

MAX_ROUNDS = 10  # safety limit to prevent infinite loops - caps nesting depth (archive-within-archive
                 # levels), not the total number of archives extracted: a single round already extracts
                 # every archive found at that level, however many there are


def _out_dir(file_path, suffix, dest_base=None):
    """Creates and returns a subfolder named after the archive with a format suffix.
    If dest_base is given, the subfolder is created there instead of alongside the archive."""
    base = os.path.basename(file_path)
    for ext in ['.tar.gz', '.tar.bz2', '.tgz', '.tar', '.zip', '.gz', '.rar', '.7z']:
        if base.lower().endswith(ext):
            base = base[:-len(ext)]
            break
    parent = dest_base if dest_base else os.path.dirname(file_path)
    out = os.path.join(parent, f"{base}-{suffix}")
    os.makedirs(out, exist_ok=True)
    return out


def _is_valid_filename(name):
    """
    Returns False if filename contains CR, LF, or NUL — characters that can
    break Windows path handling and sometimes appear in crafted/corrupt
    archive entries. This is a pre-check for a clean warning message only;
    it's not exhaustive (other Windows-illegal characters like : < > | ? *
    aren't checked here) — those are still caught by the try/except around
    each member's extraction, just with a less specific log message.
    """
    return '\r' not in name and '\n' not in name and '\x00' not in name


def _extract_zip(file_path, dest_base=None):
    out = _out_dir(file_path, 'zip', dest_base)
    with zipfile.ZipFile(file_path, 'r') as z:
        for member in z.infolist():
            if not _is_valid_filename(member.filename):
                logger.warning("Skipping file with invalid name in zip: %r", member.filename)
                continue
            try:
                z.extract(member, out)
            except Exception as e:
                logger.error("Could not extract %r: %s", member.filename, e)


def _extract_tar(file_path, dest_base=None):
    out = _out_dir(file_path, 'tar', dest_base)
    with tarfile.open(file_path, 'r:*') as t:
        for member in t.getmembers():
            if not _is_valid_filename(member.name):
                logger.warning("Skipping file with invalid name in tar: %r", member.name)
                continue
            try:
                t.extract(member, out, filter='data')
            except Exception as e:
                logger.error("Could not extract %r: %s", member.name, e)


def _extract_gz(file_path, dest_base=None):
    out = _out_dir(file_path, 'gz', dest_base)
    out_path = os.path.join(out, os.path.basename(file_path).removesuffix('.gz'))
    with gzip.open(file_path, 'rb') as f_in:
        with open(out_path, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)


def _extract_rar(file_path, dest_base=None):
    raise NotImplementedError("RAR extraction not supported — extract manually or convert to zip/7z")


def _extract_7z(file_path, dest_base=None):
    out = _out_dir(file_path, '7z', dest_base)
    with py7zr.SevenZipFile(file_path, mode='r') as z:
        z.extractall(path=out)


def _get_handler(filename):
    name = filename.lower()
    if name.endswith('.tar.gz') or name.endswith('.tar.bz2') or name.endswith('.tgz'):
        return _extract_tar
    if name.endswith('.gz'):
        return _extract_gz
    ext = os.path.splitext(name)[1]
    return {
        '.zip': _extract_zip,
        '.tar': _extract_tar,
        '.rar': _extract_rar,
        '.7z' : _extract_7z,
    }.get(ext)


def unzip_folder(folder_path):
    """
    Recursively finds and extracts all archive files in folder_path.
    Deletes each archive after successful extraction.
    Repeats until no archives remain or MAX_ROUNDS is reached.
    """
    logger.info("Checking for archive files in %s", folder_path)
    round_num = 0
    failed = set()

    while True:
        round_num += 1

        if round_num > MAX_ROUNDS:
            logger.error("Max extraction rounds (%d) reached — stopping to prevent infinite loop", MAX_ROUNDS)
            break

        archives = [
            os.path.join(root, f)
            for root, _, filenames in os.walk(folder_path)
            for f in filenames
            if _get_handler(f) is not None
            and not f.startswith('._')
            and '__MACOSX' not in root
            and os.path.join(root, f) not in failed
        ]

        if not archives:
            logger.info("No archive files found — extraction complete after %d round(s)", round_num - 1)
            break

        logger.info("Round %d: found %d archive(s) to extract", round_num, len(archives))

        for file_path in archives:
            handler = _get_handler(os.path.basename(file_path))
            try:
                logger.info("Extracting: %s", os.path.relpath(file_path, folder_path))
                handler(file_path)
                os.remove(file_path)
                logger.info("Deleted: %s", os.path.relpath(file_path, folder_path))
            except Exception as e:
                logger.error("Failed to extract %s: %s", file_path, e)
                failed.add(file_path)


if __name__ == "__main__":
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s - %(message)s",
        stream=sys.stdout
    )

    folder = r"C:\Test_folder\Test_package"
    unzip_folder(folder)