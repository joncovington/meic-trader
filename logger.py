import logging
import sys
from datetime import datetime
import pytz

ET = pytz.timezone("America/New_York")


class ETFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ct = datetime.fromtimestamp(record.created, ET)
        return ct.strftime("%Y-%m-%d %H:%M:%S %Z")


def setup_logger(name: str = "meic") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    fmt = ETFormatter("[%(asctime)s] %(levelname)-8s %(message)s")
    handler.setFormatter(fmt)
    logger.addHandler(handler)

    fh = logging.FileHandler("meic_trader.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


log = setup_logger()
