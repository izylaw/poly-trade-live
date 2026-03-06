import logging
import sys
from pathlib import Path
from datetime import datetime


def setup_logger(name: str = "poly-trade", level: str = "INFO", log_dir: Path | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        fh = logging.FileHandler(log_dir / f"{today}.log")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger
