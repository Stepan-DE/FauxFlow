"""Загрузчик данных ядра: SQLite -> единый словарь-контекст"""

import json
import logging
import os
import time
import pathlib

import numpy as np

# Общий модуль подключения к БД
from include.transaction_generator.db import get_connection


# Настройки логирования
from include.config.log_config import setup_logging

setup_logging()

module_name = pathlib.Path(__file__).stem
logger = logging.getLogger(module_name)


def _fmt(n) -> str:
    """Форматирует число для читаемого вывода: 123456 -> 123_456."""
    return f"{int(n):,}".replace(",", "_")


def load_all() -> dict:
    """Загружает все данные ядра из БД и возвращает словарь-контекст."""

    total_start = time.perf_counter()
    logger.debug("старт загрузки контекста")

    conn = get_connection()
    context: dict = {}

    # Конфиги: значения хранятся как JSON-строки (числа, списки, словари)
    block_start = time.perf_counter()
    config: dict = {}
    cursor = conn.execute("SELECT key, value FROM generator_config")
    for key, value in cursor.fetchall():
        try:
            config[key] = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            logger.error(
                "битый JSON в generator_config, ключ %r: %s", key, exc
            )
            raise
    context["config"] = config
    logger.debug(
        "generator_config - %s записей, %.0f мс",
        _fmt(len(config)), (time.perf_counter() - block_start) * 1000,
    )

    # Категории: список id + name->id для конвертации колонок расписания
    block_start = time.perf_counter()
    name_to_id: dict = {}
    categories: list = []
    cursor = conn.execute("SELECT category_id, name FROM category")
    for category_id, name in cursor.fetchall():
        categories.append(category_id)
        name_to_id[name] = category_id
    context["categories"] = categories
    logger.debug(
        "category - %s записей, %.0f мс",
        _fmt(len(categories)), (time.perf_counter() - block_start) * 1000,
    )

    # Подкатегории: группируем по category_id, config_json парсим
    block_start = time.perf_counter()
    subcategories_by_category: dict = {}
    cursor = conn.execute(
        "SELECT subcategory_id, category_id, name, weight, type, config_json "
        "FROM subcategory"
    )
    subcategory_count = 0
    for subcategory_id, category_id, name, weight, type_, config_json in cursor.fetchall():
        try:
            parsed_config = json.loads(config_json)
        except (TypeError, json.JSONDecodeError) as exc:
            logger.error(
                "битый JSON в config_json у subcategory_id=%s: %s",
                subcategory_id, exc,
            )
            raise
        subcategories_by_category.setdefault(category_id, []).append(
            {
                "subcategory_id": subcategory_id,
                "name": name,
                "weight": weight,
                "type": type_,
                "config": parsed_config,
            }
        )
        subcategory_count += 1
    context["subcategories_by_category"] = subcategories_by_category
    logger.debug(
        "subcategory - %s записей, %.0f мс",
        _fmt(subcategory_count), (time.perf_counter() - block_start) * 1000,
    )

    # Расписание активности: (day, hour) -> {category_id: coefficient}
    # Колонки кроме day/hour - имена категорий; имя должно быть в category.
    block_start = time.perf_counter()
    activity: dict = {}
    cursor = conn.execute("SELECT * FROM activity_schedule")
    column_names = [description[0] for description in cursor.description]
    category_columns = [c for c in column_names if c not in ("day", "hour")]
    rows = cursor.fetchall()
    for row in rows:
        row_dict = dict(zip(column_names, row))
        day = row_dict["day"]
        hour = row_dict["hour"]
        coefficients: dict = {}
        for column_name in category_columns:
            if column_name not in name_to_id:
                logger.error(
                    "колонка %r в activity_schedule не найдена в "
                    "таблице category - баг схемы", column_name,
                )
                raise ValueError(
                    f"activity_schedule column {column_name!r} has no "
                    f"matching category"
                )
            coefficients[name_to_id[column_name]] = row_dict[column_name]
        activity[(day, hour)] = coefficients
    context["activity"] = activity
    logger.debug(
        "activity_schedule - %s записей, %.0f мс",
        _fmt(len(activity)), (time.perf_counter() - block_start) * 1000,
    )

    # Активные пользователи -> numpy.ndarray(dtype=int64)
    block_start = time.perf_counter()
    cursor = conn.execute("SELECT user_id FROM users WHERE is_active = 1")
    active_user_ids = np.array([row[0] for row in cursor.fetchall()], dtype=np.int64)
    context["active_user_ids"] = active_user_ids
    pool_size_users = len(active_user_ids)
    context["pool_size_users"] = pool_size_users
    if pool_size_users == 0:
        logger.error(
            "пустой пул активных пользователей - баг инициализации, "
            "пополните таблицу users (см. entity_manager)"
        )
        raise RuntimeError("pool_size_users == 0")
    logger.debug(
        "active_user_ids - %s, %.0f мс",
        _fmt(pool_size_users), (time.perf_counter() - block_start) * 1000,
    )

    # Активные мерчанты -> группировка по subcategory_id
    block_start = time.perf_counter()
    merchants_by_subcat: dict = {}
    cursor = conn.execute(
        "SELECT merchant_id, name, subcategory_id, address, weight "
        "FROM merchants WHERE is_active = 1"
    )
    for merchant_id, name, subcategory_id, address, weight in cursor.fetchall():
        merchants_by_subcat.setdefault(subcategory_id, []).append(
            {
                "merchant_id": merchant_id,
                "name": name,
                "address": address,
                "weight": weight,
            }
        )
    context["merchants_by_subcat"] = merchants_by_subcat
    pool_size_merchants = sum(len(v) for v in merchants_by_subcat.values())
    context["pool_size_merchants"] = pool_size_merchants
    if pool_size_merchants == 0:
        logger.error(
            "пустой пул активных мерчантов - мерчанты нужны для "
            "любой транзакции, пополните таблицу merchants (см. entity_manager)"
        )
        raise RuntimeError("pool_size_merchants == 0")
    logger.debug(
        "merchants - %s, %.0f мс",
        _fmt(pool_size_merchants), (time.perf_counter() - block_start) * 1000,
    )

    # Возврат
    logger.debug(
        "контекст готов, общее время %.0f мс",
        (time.perf_counter() - total_start) * 1000,
    )
    return context