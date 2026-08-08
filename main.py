"""
MDMS Workstation Entry Point
Protects child worker process instantiation on Windows and initializes schema parameters.
"""

import sys
from config.settings import settings
from db.core import init_db_schema, close_thread_connection
from services.ingest import FastBootGuard
from utils.fpcalc import ensure_fpcalc

def main() -> None:
    init_db_schema()
    FastBootGuard.is_clean_boot()
    ensure_fpcalc()

    # Move the GUI import inside the main function.
    # This prevents child worker processes spawned by multiprocessing from loading PySide6 views.
    from PySide6.QtWidgets import QApplication
    from gui.app import MainWindow

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()

    exit_code = app.exec()
    close_thread_connection()
    sys.exit(exit_code)

if __name__ == "__main__":
    main()