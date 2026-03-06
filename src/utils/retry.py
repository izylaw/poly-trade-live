import time
import logging
from functools import wraps

logger = logging.getLogger("poly-trade")


def retry(max_attempts: int = 3, base_delay: float = 1.0, max_delay: float = 30.0):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    if attempt < max_attempts - 1:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        logger.warning(f"{func.__name__} attempt {attempt+1} failed: {e}. Retrying in {delay}s")
                        time.sleep(delay)
            logger.error(f"{func.__name__} failed after {max_attempts} attempts: {last_exc}")
            raise last_exc
        return wrapper
    return decorator
