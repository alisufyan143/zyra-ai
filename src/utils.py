"""
Shared utilities: logging setup, URL helpers.
"""

import logging
import os
import sys
from datetime import datetime


def setup_logging(log_file: str = None, level: int = logging.INFO) -> logging.Logger:
    """
    Configure root logger with both console and file output.
    
    Args:
        log_file: Path to log file. If None, console only.
        level: Logging level.
        
    Returns:
        Configured root logger.
    """
    root = logging.getLogger()
    root.setLevel(level)

    # Clear existing handlers
    root.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler (always)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    # File handler (if log_file provided)
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    return root


def domain_to_filename(url: str) -> str:
    """Convert a URL to a safe filename slug."""
    slug = url.replace("https://", "").replace("http://", "")
    slug = slug.strip("/").replace(".", "_").replace("/", "_")
    return slug
