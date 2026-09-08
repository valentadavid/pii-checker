# find_duplicities.py
# Finds duplicate files by comparing MD5 hashes.
# Takes a list of file paths, returns a lookup dict mapping each file to its primary (first seen) file.

import hashlib
import logging
import os
from collections import defaultdict

logger = logging.getLogger(__name__)


def _md5(file_path, chunk_size=8192) -> str:
    """Computes MD5 hash of a file."""
    h = hashlib.md5()
    with open(file_path, 'rb') as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def find_duplicities(files: list) -> dict:
    """
    Takes a list of file paths and computes MD5 for each.
    Returns a dict: {file_path: primary_path}, with exactly one entry per
    input file path:
        - If file is unique, is the primary, or couldn't be hashed:
          primary_path == file_path
        - If file is a duplicate: primary_path == path of the first seen identical file

    A file that can't be hashed (permission error, locked, missing, etc.) is mapped to itself

    Also logs all duplicate groups found.
    """
    logger.info("Computing MD5 hashes for %d file(s)...", len(files))

    hash_to_files = defaultdict(list)
    lookup = {}
    for file_path in files:
        try:
            md5 = _md5(file_path)
            hash_to_files[md5].append(file_path)
        except Exception as e:
            logger.warning("Could not hash %s: %s", file_path, e)
            lookup[file_path] = file_path

    # build lookup dict
    n_duplicates = 0

    for md5, paths in hash_to_files.items():
        primary = paths[0]
        for path in paths:
            lookup[path] = primary

        if len(paths) > 1:
            n_duplicates += len(paths) - 1
            logger.info("Duplicate group: %s is primary, duplicates: %s",
                        paths[0], paths[1:])

    if n_duplicates > 0:
        logger.info("Found %d duplicate file(s) across %d group(s)",
                    n_duplicates,
                    sum(1 for p in hash_to_files.values() if len(p) > 1))
    else:
        logger.info("No duplicate files found")

    return lookup


if __name__ == "__main__":
    import sys
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s - %(message)s",
        stream=sys.stdout
    )

    folder = r"C:\Test_package"

    files = [
        os.path.join(root, f)
        for root, _, filenames in os.walk(folder)
        for f in filenames
        if not f.startswith('._') and '__MACOSX' not in root
    ]

    lookup = find_duplicities(files)

    print("\nLookup table:")
    for path, primary in lookup.items():
        is_dup = path != primary
        print(f"  {'DUP' if is_dup else 'OK ':3} | {os.path.relpath(path, folder)} -> {os.path.relpath(primary, folder)}")