"""
Общий модуль подключения к SQLite.
"""

import logging
import os
import sqlite3
import pathlib

# Настройки логирования
from include.config.log_config import setup_logging

setup_logging()

module_name = pathlib.Path(__file__).stem
logger = logging.getLogger(module_name)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generator.db")

# Единственное состояние модуля: одно соединение на процесс.
_connection: sqlite3.Connection | None = None


def get_connection() -> sqlite3.Connection:
    """
    Возвращает соединение с БД.
    """

    global _connection
    if _connection is None:
        logger.debug("подключение к %s", DB_PATH)
        try:
            _connection = sqlite3.connect(DB_PATH)
            _connection.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error as exc:
            logger.error("ошибка подключения к %s: %s", DB_PATH, exc)
            raise
    return _connection


def close_connection() -> None:
    """
    Закрывает соединение, если оно открыто, и обнуляет глобальную переменную.
    """

    global _connection
    if _connection is not None:
        _connection.close()
        _connection = None
        logger.debug("соединение закрыто")
