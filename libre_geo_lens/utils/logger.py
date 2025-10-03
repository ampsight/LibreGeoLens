import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Optional

try:
    from qgis.core import Qgis, QgsMessageLog
    from qgis.PyQt.QtCore import QSettings
except ImportError:  # pragma: no cover - allows importing outside of QGIS for tooling
    Qgis = None
    QgsMessageLog = None

    class _FallbackSettings:
        """Lightweight stand-in so tooling outside QGIS can still import the module."""

        def __init__(self, *_args, **_kwargs):
            pass

        def value(self, _key, default_value=None, type=str):  # noqa: A003 - match QSettings signature
            return default_value

    QSettings = _FallbackSettings  # type: ignore

PLUGIN_LOG_CATEGORY = "LibreGeoLens"
BASE_LOGGER_NAME = "libre_geo_lens"
_LOGGER_CONFIGURED = False
_LOG_FILE_PATH: Optional[str] = None


class QgsMessageLogHandler(logging.Handler):
    """Logging handler that forwards records to the QGIS message log."""

    def emit(self, record):
        try:
            message = self.format(record)
            if QgsMessageLog is None or Qgis is None:
                # Running outside of QGIS (e.g., during static analysis); nothing to forward to.
                return
            if record.levelno >= logging.ERROR:
                qgis_level = Qgis.Critical
            elif record.levelno >= logging.WARNING:
                qgis_level = Qgis.Warning
            else:
                qgis_level = Qgis.Info
            QgsMessageLog.logMessage(message, PLUGIN_LOG_CATEGORY, level=qgis_level)
        except Exception:
            # Avoid logger-induced crashes; swallow any unexpected errors silently.
            pass


def _ensure_log_directory() -> str:
    """Resolve and create the log directory based on user settings or defaults."""
    settings = QSettings("Ampsight", "LibreGeoLens")
    logs_dir = settings.value("local_logs_directory", "", type=str)
    if not logs_dir:
        logs_dir = os.path.join(os.path.expanduser("~"), "LibreGeoLensLogs")
    os.makedirs(logs_dir, exist_ok=True)
    return logs_dir


def configure_logging() -> None:
    """Configure the shared LibreGeoLens logger (idempotent, updates on path change)."""
    global _LOGGER_CONFIGURED, _LOG_FILE_PATH

    logs_dir = _ensure_log_directory()
    log_file_path = os.path.join(logs_dir, "LibreGeoLens.log")

    base_logger = logging.getLogger(BASE_LOGGER_NAME)

    if _LOGGER_CONFIGURED and _LOG_FILE_PATH == log_file_path:
        return

    if _LOGGER_CONFIGURED:
        # Remove previous handlers so changes like a new directory take effect immediately.
        for handler in list(base_logger.handlers):
            base_logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

    formatter = logging.Formatter(
        fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_file_path,
        maxBytes=1_048_576,  # 1 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    qgis_handler = QgsMessageLogHandler()
    qgis_handler.setLevel(logging.INFO)
    qgis_handler.setFormatter(formatter)

    base_logger.setLevel(logging.INFO)
    base_logger.propagate = False
    base_logger.addHandler(file_handler)
    base_logger.addHandler(qgis_handler)

    _LOG_FILE_PATH = log_file_path
    _LOGGER_CONFIGURED = True


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a namespaced logger configured for LibreGeoLens."""
    configure_logging()
    if name and not name.startswith(BASE_LOGGER_NAME):
        full_name = f"{BASE_LOGGER_NAME}.{name}"
    elif name:
        full_name = name
    else:
        full_name = BASE_LOGGER_NAME
    return logging.getLogger(full_name)


def get_log_file_path() -> Optional[str]:
    """Expose the path of the active log file for other components if needed."""
    configure_logging()
    return _LOG_FILE_PATH
