"""Обновление пулов сущностей в конце окна генерации."""

import logging
import os
import random
import time
import pathlib

# Подключние менеджера сущностей
from include.transaction_generator.account_management import entity_manager

# Настройки логирования
from include.config.log_config import setup_logging

setup_logging()

module_name = pathlib.Path(__file__).stem
logger = logging.getLogger(module_name)


def _fmt(n) -> str:
    """Форматирует число для читаемого вывода: 123456 -> 123_456."""
    return f"{int(n):,}".replace(",", "_")


# Порядок вызовов entity_manager
_OPERATIONS = [
    ("user", "generate"),
    ("user", "deactivate"),
    ("user", "activate"),
    ("merchant", "generate"),
    ("merchant", "deactivate"),
    ("merchant", "activate"),
]

# Имя ключа статистики для каждой пары (entity, operation).
_STATS_KEYS = {
    ("user", "generate"): "users_generated",
    ("user", "deactivate"): "users_deactivated",
    ("user", "activate"): "users_activated",
    ("merchant", "generate"): "merchants_generated",
    ("merchant", "deactivate"): "merchants_deactivated",
    ("merchant", "activate"): "merchants_activated",
}


def _calculate_actual(pool_size: float, rate_per_tick: float, num_ticks: int) -> tuple[float, int]:
    """Считает фактическое число изменений по формуле."""

    expected = pool_size * rate_per_tick * num_ticks
    integer_part = int(expected)
    fractional_part = expected - integer_part
    actual = integer_part + (1 if random.random() < fractional_part else 0)
    return expected, actual


def update_pools(context: dict, num_ticks: int) -> dict:
    """Обновляет пулы пользователей и мерчантов за окно генерации."""

    t_start = time.perf_counter()

    # Подготовка
    config = context["config"]
    pool_size_users = context["pool_size_users"]
    pool_size_merchants = context["pool_size_merchants"]

    logger.debug(
        "старт, num_ticks=%s, pool_users=%s, pool_merchants=%s",
        _fmt(num_ticks), _fmt(pool_size_users), _fmt(pool_size_merchants),
    )

    stats = {
        "users_generated": 0,
        "users_deactivated": 0,
        "users_activated": 0,
        "merchants_generated": 0,
        "merchants_deactivated": 0,
        "merchants_activated": 0,
    }

    # Расчёт actual и вызовы entity_manager
    for entity, operation in _OPERATIONS:
        rate_key = f"{operation}_{entity}s_rate_per_tick"

        if rate_key not in config:
            logger.error(
                "отсутствует ключ %s в generator_config", rate_key
            )
            raise KeyError(rate_key)

        rate_per_tick = config[rate_key]
        pool_size = pool_size_users if entity == "user" else pool_size_merchants

        expected, actual = _calculate_actual(pool_size, rate_per_tick, num_ticks)

        logger.debug(
            "%s %s: expected=%.2f, actual=%d",
            entity, operation, expected, actual,
        )

        # обработка actual == 0. бывает при очень маленьком пуле
        if actual == 0:
            logger.debug("%s %s: actual=0, вызов пропущен", entity, operation)
            stats[_STATS_KEYS[(entity, operation)]] = 0
            continue

        # Для мерчантов передаём context, чтобы не грузить повторно
        if operation == "generate":
            if entity == "merchant":
                done = entity_manager.generate_entities(entity, actual, context=context)
            else:
                done = entity_manager.generate_entities(entity, actual)
        elif operation == "deactivate":
            done = entity_manager.deactivate_entities(entity, actual)
        else:  # activate
            done = entity_manager.activate_entities(entity, actual)

        logger.debug(
            "%s %s: фактически %d", entity, operation, done
        )

        stats[_STATS_KEYS[(entity, operation)]] = done

    # Возврат
    time_ms = (time.perf_counter() - t_start) * 1000
    stats["time_ms"] = time_ms

    logger.debug("готово, время %d мс", int(time_ms))
    logger.debug(
        "users +%s/-%s/↺%s, merchants +%s/-%s/↺%s, время %d мс",
        _fmt(stats["users_generated"]),
        _fmt(stats["users_deactivated"]),
        _fmt(stats["users_activated"]),
        _fmt(stats["merchants_generated"]),
        _fmt(stats["merchants_deactivated"]),
        _fmt(stats["merchants_activated"]),
        int(time_ms),
    )

    return stats