import os
import logging
import logging.config

logging.addLevelName(logging.DEBUG, "DEBUG]")
logging.addLevelName(logging.INFO, "INFO]")
logging.addLevelName(logging.WARNING, "WARNING]")
logging.addLevelName(logging.ERROR, "ERROR]")
logging.addLevelName(logging.CRITICAL, "CRITICAL]")

# Читаем уровень из переменной окружения
_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

# Описываем конфиг
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "aligned": {
            "format": "[%(levelname)-7s %(name)s: %(message)s"
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "aligned",
            "level": _log_level,
        },
    },
    "loggers": {
        "": {
            "handlers": ["console"],
            "level": _log_level,
        },
        # блокируем логи от faker
        "faker": {
            "level": "WARNING",  # Игнорируем DEBUG/INFO от Faker
            "handlers": ["console"],
            "propagate": False,  # Не дублируем логи в корневой логгер
        },
    },
}


def setup_logging():
    """Функция для инициализации настроек"""
    logging.config.dictConfig(LOGGING_CONFIG)
