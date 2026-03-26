import logging
import logging.handlers
import sys
import queue
import atexit
from pathlib import Path
from datetime import datetime

_listener = None


def setup_logger(name: str = "poly-trade", level: str = "INFO", log_dir: Path | None = None) -> logging.Logger:
    global _listener
    logger = logging.getLogger(name)
    if logger.handlers or _listener is not None:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler (runs on listener thread via QueueHandler)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)

    handlers = [console]

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        fh = logging.FileHandler(log_dir / f"{today}.log")
        fh.setFormatter(fmt)
        handlers.append(fh)

    # Queue-based async logging for all handlers
    log_queue = queue.Queue(maxsize=10_000)

    class _DropOnFullQueueHandler(logging.handlers.QueueHandler):
        def enqueue(self, record):
            try:
                self.queue.put_nowait(record)
            except queue.Full:
                if record.levelno < logging.WARNING:
                    return  # drop DEBUG/INFO under backpressure
                try:
                    self.queue.put(record, timeout=0.5)
                except queue.Full:
                    fallback = logging.StreamHandler(sys.stderr)
                    try:
                        fallback.emit(record)
                    finally:
                        fallback.close()

    queue_handler = _DropOnFullQueueHandler(log_queue)
    logger.addHandler(queue_handler)

    _listener = logging.handlers.QueueListener(log_queue, *handlers, respect_handler_level=True)
    _listener.start()
    atexit.register(_listener.stop)

    return logger
