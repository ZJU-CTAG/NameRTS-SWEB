import logging
import os
import time

from src.config import LOGGING


class InstanceLogger:
    _instance = None

    def __new__(cls, logger_id: str = "", logger_dir: str = ""):
        if cls._instance is None or (cls._instance.logger_id != logger_id and logger_id != ""):
            cls._instance = super().__new__(cls)
            if logger_id == "":
                logger_id = "logfile"

            time_tag = str(time.time())
            logger_file = f"{logger_id}_{time_tag}.log"
            if logger_dir:
                logger_path = os.path.join(LOGGING, logger_dir, logger_file)
            else:
                logger_path = os.path.join(LOGGING, logger_file)
            logger_parent = os.path.dirname(logger_path)
            if not os.path.exists(logger_parent):
                os.makedirs(logger_parent, exist_ok=True)
            cls._instance.logger = logging.getLogger(logger_file)
            cls._instance.logger.setLevel(logging.INFO)

            handler = logging.FileHandler(logger_path)
            handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
            cls._instance.logger.addHandler(handler)

            cls._instance.logger_id = logger_id

        return cls._instance

    @classmethod
    def get_logger(cls):
        return cls._instance.logger
