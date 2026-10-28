"""Launch the JPEG Recovery desktop application."""

from __future__ import annotations

import sys
import traceback

from PyQt6.QtWidgets import QMessageBox

from jpegmedic import __app_name__
from jpegmedic.ui.main_window import MainWindow, create_application
from jpegmedic.ui.theme import apply_theme


def main(argv: list[str] | None = None) -> int:
    try:
        app = create_application(argv if argv is not None else sys.argv)
        apply_theme(app)
        window = MainWindow()
        window.show()
        return app.exec()
    except Exception:  # noqa: BLE001 - show a usable error instead of a stack
        detail = traceback.format_exc()
        try:
            QMessageBox.critical(
                None,
                f"{__app_name__} could not start",
                "An unexpected error occurred while starting the application."
                f"\n\n{detail}",
            )
        except Exception:
            print(detail, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
