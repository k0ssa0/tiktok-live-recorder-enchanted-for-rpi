import logging
import os
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler


# Log rotation settings
MAX_LOG_SIZE = 10 * 1024 * 1024  # 10 MB per log file
MAX_LOG_BACKUPS = 5  # Keep 5 backup files


class MaxLevelFilter(logging.Filter):
    """
    Filter that only allows log records up to a specified maximum level.
    """

    def __init__(self, max_level):
        super().__init__()
        self.max_level = max_level

    def filter(self, record):
        # Only accept records whose level number is <= self.max_level
        return record.levelno <= self.max_level


class LoggerManager:
    _instance = None  # Singleton instance
    _verbose = False
    _file_handler = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(LoggerManager, cls).__new__(cls)
            cls._instance.logger = None
            cls._instance.setup_logger()
        return cls._instance

    def setup_logger(self):
        if self.logger is None:
            self.logger = logging.getLogger("logger")
            self.logger.setLevel(logging.DEBUG)  # Allow all levels, handlers will filter

            # 1) INFO handler (console)
            info_handler = logging.StreamHandler()
            info_handler.setLevel(logging.INFO)
            info_format = "[*] %(asctime)s - %(message)s"
            info_datefmt = "%Y-%m-%d %H:%M:%S"
            info_formatter = logging.Formatter(info_format, info_datefmt)
            info_handler.setFormatter(info_formatter)

            # Add a filter to exclude ERROR level (and above) messages
            info_handler.addFilter(MaxLevelFilter(logging.INFO))

            self.logger.addHandler(info_handler)

            # 2) WARNING handler (console)
            warning_handler = logging.StreamHandler()
            warning_handler.setLevel(logging.WARNING)
            warning_handler.addFilter(MaxLevelFilter(logging.WARNING))
            warning_format = "[!] %(asctime)s - %(message)s"
            warning_datefmt = "%Y-%m-%d %H:%M:%S"
            warning_formatter = logging.Formatter(warning_format, warning_datefmt)
            warning_handler.setFormatter(warning_formatter)

            self.logger.addHandler(warning_handler)

            # 3) ERROR handler (console)
            error_handler = logging.StreamHandler()
            error_handler.setLevel(logging.ERROR)
            error_format = "[!] %(asctime)s - %(message)s"
            error_datefmt = "%Y-%m-%d %H:%M:%S"
            error_formatter = logging.Formatter(error_format, error_datefmt)
            error_handler.setFormatter(error_formatter)

            self.logger.addHandler(error_handler)

    @classmethod
    def enable_verbose(cls, enabled: bool = True):
        """
        Enable or disable verbose mode with file logging.
        When enabled, all logs (DEBUG and above) are saved to ~/tiktok-recorder-logs/
        """
        instance = cls()
        cls._verbose = enabled
        
        if enabled:
            # Create logs directory in home
            log_dir = Path.home() / "tiktok-recorder-logs"
            log_dir.mkdir(exist_ok=True)
            
            # Create log file with timestamp
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            log_file = log_dir / f"tiktok_recorder_{timestamp}.log"
            
            # Add rotating file handler for verbose logging (prevents disk fill)
            cls._file_handler = RotatingFileHandler(
                log_file, 
                maxBytes=MAX_LOG_SIZE,
                backupCount=MAX_LOG_BACKUPS,
                encoding='utf-8'
            )
            cls._file_handler.setLevel(logging.DEBUG)
            
            verbose_format = "[%(levelname)s] %(asctime)s - %(filename)s:%(lineno)d - %(message)s"
            verbose_datefmt = "%Y-%m-%d %H:%M:%S"
            verbose_formatter = logging.Formatter(verbose_format, verbose_datefmt)
            cls._file_handler.setFormatter(verbose_formatter)
            
            instance.logger.addHandler(cls._file_handler)
            
            # Also add DEBUG handler to console in verbose mode
            debug_handler = logging.StreamHandler()
            debug_handler.setLevel(logging.DEBUG)
            debug_handler.addFilter(MaxLevelFilter(logging.DEBUG))
            debug_format = "[DEBUG] %(asctime)s - %(message)s"
            debug_formatter = logging.Formatter(debug_format, verbose_datefmt)
            debug_handler.setFormatter(debug_formatter)
            debug_handler.name = "verbose_debug"
            instance.logger.addHandler(debug_handler)
            
            instance.logger.info(f"Verbose mode enabled. Logs saved to: {log_file}")
        else:
            # Remove file handler if it exists
            if cls._file_handler:
                instance.logger.removeHandler(cls._file_handler)
                cls._file_handler.close()
                cls._file_handler = None
            
            # Remove verbose debug handler
            for handler in instance.logger.handlers[:]:
                if getattr(handler, 'name', None) == "verbose_debug":
                    instance.logger.removeHandler(handler)

    @classmethod
    def is_verbose(cls) -> bool:
        """Check if verbose mode is enabled."""
        return cls._verbose

    def debug(self, message):
        """
        Log a DEBUG-level message (only visible in verbose mode).
        """
        self.logger.debug(message)

    def warning(self, message):
        """
        Log a WARNING-level message.
        """
        self.logger.warning(message)

    def info(self, message):
        """
        Log an INFO-level message.
        """
        self.logger.info(message)

    def error(self, message):
        """
        Log an ERROR-level message.
        """
        self.logger.error(message)

    def critical(self, message):
        """
        Log a CRITICAL-level message.
        """
        self.logger.critical(message)


logger = LoggerManager()
