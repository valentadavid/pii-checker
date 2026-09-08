# clean_package.py
# Removes junk files and folders after archive extraction.
# Called after unzip_folder in main.py.
#
# Removes:
#   - Any file or folder starting with '.'  (.DS_Store, ._file, etc.)
#   - __MACOSX/ folders
#   - Thumbs.db
#   - desktop.ini

import os
import shutil
import logging

logger = logging.getLogger(__name__)

JUNK_FILES = {'thumbs.db', 'desktop.ini'}
JUNK_FOLDERS = {'__macosx'}


def clean_folder(folder_path):
    """
    Removes junk files and folders from folder_path.
    """
    n_files = 0
    n_folders = 0

    for root, dirs, files in os.walk(folder_path, topdown=False):

        # remove junk files
        for f in files:
            if f.startswith('.') or f.lower() in JUNK_FILES:
                file_path = os.path.join(root, f)
                try:
                    os.remove(file_path)
                    logger.info("Deleted junk file: %s", os.path.relpath(file_path, folder_path))
                    n_files += 1
                except Exception as e:
                    logger.warning("Could not delete %s: %s", file_path, e)

        # remove junk folders
        for d in dirs:
            if d.startswith('.') or d.lower() in JUNK_FOLDERS:
                dir_path = os.path.join(root, d)
                try:
                    shutil.rmtree(dir_path)
                    logger.info("Deleted junk folder: %s", os.path.relpath(dir_path, folder_path))
                    n_folders += 1
                except Exception as e:
                    logger.warning("Could not delete %s: %s", dir_path, e)

    if n_files + n_folders > 0:
        logger.info("Cleanup complete — removed %d file(s) and %d folder(s)", n_files, n_folders)
    else:
        logger.info("Cleanup complete — nothing to remove")


if __name__ == "__main__":
    import sys
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s - %(message)s",
        stream=sys.stdout
    )

    folder = r"C:\Test_folder"
    clean_folder(folder)
