import logging
import logging.handlers
import sys
import queue
import atexit
from pathlib import Path
from datetime import datetime

_listener = None


class _BackpressureQueueHandler(logging.handlers.QueueHandler):
    """QueueHandler that drops DEBUG/INFO when the queue is full instead of blocking."""

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            if record.levelno >= logging.WARNING:
                # Block for critical messages so they aren't lost
                self.queue.put(record)
            # else: silently drop low-priority messages


def setup_logger(name: str = "poly-trade", level: str = "INFO", log_dir: Path | None = None) -> logging.Logger:
    global _listener
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler — runs on QueueListener thread, not trading thread
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)

    handlers = [console]

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        fh = logging.FileHandler(log_dir / f"{today}.log")
        fh.setFormatter(fmt)
        handlers.append(fh)

    # Queue-based async logging — bounded to prevent OOM under backpressure
    log_queue = queue.Queue(maxsize=10_000)
    queue_handler = _BackpressureQueueHandler(log_queue)
    logger.addHandler(queue_handler)

    _listener = logging.handlers.QueueListener(log_queue, *handlers, respect_handler_level=True)
    _listener.start()
    atexit.register(_listener.stop)

    return logger
