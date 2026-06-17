import logging
import sys
from datetime import datetime
import pytz

ET = pytz.timezone("America/New_York")


class ETFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ct = datetime.fromtimestamp(record.created, ET)
        return ct.strftime("%Y-%m-%d %H:%M:%S %Z")


def setup_logger(name: str = "meic", debug: bool = False) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    fmt = ETFormatter("[%(asctime)s] %(levelname)-8s %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if debug else logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    fh = logging.FileHandler("meic_trader.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


def set_debug(enabled: bool) -> None:
    """Flip the console handler level at runtime (called after arg parsing)."""
    logger = logging.getLogger("meic")
    for h in logger.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.setLevel(logging.DEBUG if enabled else logging.INFO)


log = setup_logger()
