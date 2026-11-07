import logging
logger = logging.getLogger("astrbot")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)
