# interface.py
# Interactive wrapper for main.py.
#
# Asks the user whether they want to evaluate a single package or many
# packages inside a folder. A single package is run directly via
# run_package(); many packages are delegated to run_folder(), which
# auto-discovers package subfolders and maintains a resumable overview/
# status file (pii_results_overview.xlsx) in that folder.
# Each package gets its own pii_check.xlsx saved inside its own folder.

import os

from main import _check_dependencies, run_package, run_folder, logger, _rmtree_retrying


def _ask_mode():
    while True:
        answer = input("Evaluate (1) a single package or (2) many packages in a folder? [1/2]: ").strip()
        if answer in ('1', '2'):
            return answer
        print("Please enter 1 or 2.")


def _ask_folder():
    while True:
        folder = input("Folder path: ").strip().strip('"')
        if os.path.isdir(folder):
            return folder
        print(f"Not a valid folder: {folder}")


def main():
    _check_dependencies()

    mode = _ask_mode()
    folder = _ask_folder()

    if mode == '1':
        packages = [folder]
        for i, package_folder in enumerate(packages, start=1):
            name = os.path.basename(os.path.normpath(package_folder))
            output_path = os.path.join(package_folder, "pii_check.xlsx")
            temp_base = os.path.join(package_folder, "temp_pii_scan")
            os.makedirs(temp_base, exist_ok=True)

            logger.info("[%d/%d] Starting package: %s", i, len(packages), name)
            try:
                run_package(
                    package_folder,
                    output_path,
                    temp_base=temp_base,
                )
            except Exception as e:
                logger.error("Package failed: %s — %s", name, e, exc_info=True)
            finally:
                _rmtree_retrying(temp_base)  # run_package already cleans up its own subdir; this catches any leftovers
            logger.info("[%d/%d] Finished package: %s", i, len(packages), name)
    else:
        run_folder(folder)


if __name__ == "__main__":
    main()