"""
Управление пулами сущностей (пользователи и мерчанты) в SQLite.
Модуль отвечает за генерацию, деактивацию и активацию записей в пулах.
"""

import json
import logging
import os
import random
import time
import pathlib
from datetime import date, datetime, timedelta
from faker import Faker

fake = Faker()
Faker.seed()

# настройка работы с БД
from include.transaction_generator.db import close_connection, get_connection

# Настройки логирования
from include.config.log_config import setup_logging

setup_logging()

module_name = pathlib.Path(__file__).stem
logger = logging.getLogger(module_name)


_MODULE_START_TIME = time.monotonic()


def _fmt(n) -> str:
    """Форматирует число для читаемого вывода: 123456 -> 123_456."""
    return f"{int(n):,}".replace(",", "_")


def _log_elapsed_ms(start_time: float, prefix: str = "") -> None:
    """Логирует время выполнения операции (DEBUG, отдельной строкой в конце)."""
    elapsed_ms = (time.monotonic() - start_time) * 1000
    message = "время выполнения: %.0f мс" % elapsed_ms
    if prefix:
        message = f"{prefix}: {message}"
    logger.debug(message)


# Конфигурация сущностей

ENTITY_CONFIG = {
    "user": {
        "table": "users",
        "id_field": "user_id",
        "active_field": "is_active",
        "generate_one": None,  # заполняется после определения функций-генераторов
        "min_limit_key": "users_min",
        "max_limit_key": "users_max",
    },
    "merchant": {
        "table": "merchants",
        "id_field": "merchant_id",
        "active_field": "is_active",
        "generate_one": None,  # заполняется после определения функций-генераторов
        "min_limit_key": "merchants_min",
        "max_limit_key": "merchants_max",
    },
}

# Порядок колонок INSERT для каждой сущности
INSERT_COLUMNS = {
    "user": [
        "fio",
        "birth_date",
        "phone",
        "email",
        "register_date",
        "passport_number",
        "address",
        "account_number",
        "is_active",
    ],
    "merchant": [
        "name",
        "subcategory_id",
        "address",
        "phone",
        "email",
        "register_date",
        "weight",
        "is_active",
    ],
}


# Вспомогательные функции


def _random_digits(n: int) -> str:
    """Возвращает строку из n случайных цифр."""

    return "".join(str(random.randint(0, 9)) for _ in range(n))


def _random_past_date(start_days_ago: int, end_days_ago: int) -> date:
    """Случайная дата в диапазоне [today - start_days_ago, today - end_days_ago]."""

    days = random.randint(end_days_ago, start_days_ago)
    return date.today() - timedelta(days=days)


# Специфичные генераторы


def generate_one_user(register_date) -> dict:
    """Генерирует одну запись пользователя.
    Возвращает словарь со всеми полями, кроме user_id."""

    if isinstance(register_date, datetime):
        ref_date = register_date.date()
    elif isinstance(register_date, date):
        ref_date = register_date
    else:
        ref_date = datetime.strptime(str(register_date), "%Y-%m-%d").date()

    # Возраст от 18 до 80 лет относительно register_date.
    birth_date = ref_date - timedelta(days=random.randint(18 * 365, 80 * 365))

    return {
        "fio": fake.name(),
        "birth_date": birth_date.isoformat(),
        "phone": fake.phone_number(),
        "email": fake.email(),
        "register_date": register_date,
        "passport_number": f"{_random_digits(4)} {_random_digits(6)}",
        "address": fake.address().replace("\n", ", "),
        "account_number": _random_digits(20),
        "is_active": 1,
    }


def generate_one_merchant(register_date, context: dict) -> dict:
    """Генерирует одну запись мерчанта.
    Возвращает словарь со всеми полями, кроме merchant_id."""

    categories = context["categories"]
    subs_by_cat = context["subcategories_by_category"]

    category_id = random.choice(categories)
    subcategories = subs_by_cat[category_id]
    subcategory = random.choices(
        subcategories, weights=[s["weight"] for s in subcategories]
    )[0]

    # Длинный хвост: 10% получают вес 5.0-10.0, остальные 0.5-2.0.
    if random.random() < 0.10:
        weight = round(random.uniform(5.0, 10.0), 2)
    else:
        weight = round(random.uniform(0.5, 2.0), 2)

    return {
        "name": fake.company(),
        "subcategory_id": subcategory["subcategory_id"],
        "address": fake.address().replace("\n", ", "),
        "phone": fake.phone_number(),
        "email": fake.company_email(),
        "register_date": register_date,
        "weight": weight,
        "is_active": 1,
    }


# Связываем конфигурацию сущностей с функциями-генераторами.
ENTITY_CONFIG["user"]["generate_one"] = generate_one_user
ENTITY_CONFIG["merchant"]["generate_one"] = generate_one_merchant


# Чтение лимитов и справочников


def load_limits(entity: str) -> tuple[int, int]:
    """Читает лимиты (min, max) пула из таблицы generator_config.

    Ключи берутся из ENTITY_CONFIG (min_limit_key / max_limit_key).
    Значения возвращаются как int."""

    config = ENTITY_CONFIG[entity]
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT key, value FROM generator_config " "WHERE key IN (?, ?)",
        (config["min_limit_key"], config["max_limit_key"]),
    )
    rows = dict(cursor.fetchall())

    missing = [
        k for k in (config["min_limit_key"], config["max_limit_key"]) if k not in rows
    ]
    if missing:
        logger.error("entity_manager: в generator_config отсутствуют ключи %s", missing)
        raise LookupError(f"generator_config: missing keys {missing}")

    min_limit = int(rows[config["min_limit_key"]])
    max_limit = int(rows[config["max_limit_key"]])
    logger.debug(
        "(%s): лимиты из generator_config (min=%s, max=%s)",
        entity,
        _fmt(min_limit),
        _fmt(max_limit),
    )
    return min_limit, max_limit


def _load_reference_data() -> dict:
    """Загружает справочники категорий/подкатегорий из БД."""

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT category_id FROM category ORDER BY category_id")
    categories = [row[0] for row in cursor.fetchall()]

    cursor.execute(
        "SELECT subcategory_id, category_id, weight, type, config_json "
        "FROM subcategory ORDER BY subcategory_id"
    )
    subcategories_by_category: dict = {}
    for subcategory_id, category_id, weight, type_, config_json in cursor.fetchall():
        parsed_config = json.loads(config_json) if config_json else {}
        subcategories_by_category.setdefault(category_id, []).append(
            {
                "subcategory_id": subcategory_id,
                "weight": weight,
                "type": type_,
                "config_json": parsed_config,
            }
        )

    logger.debug(
        "Справочники загружены: категорий %s, подкатегорий %s",
        _fmt(len(categories)),
        _fmt(sum(len(v) for v in subcategories_by_category.values())),
    )
    return {
        "categories": categories,
        "subcategories_by_category": subcategories_by_category,
    }


# Основные функции (п. 5 ТЗ)


def _validate_entity(entity: str) -> dict:
    """Проверяет, что entity есть в ENTITY_CONFIG."""

    if entity not in ENTITY_CONFIG:
        logger.error(
            "entity_manager: неизвестная сущность '%s' (ожидалось одно из %s)",
            entity,
            sorted(ENTITY_CONFIG),
        )
        raise ValueError(f"Unknown entity: {entity!r}")
    return ENTITY_CONFIG[entity]


def _validate_count(count: int) -> None:
    """Проверяет, что count > 0."""

    if count <= 0:
        logger.error("entity_manager: count должен быть > 0, получено %s", count)
        raise ValueError(f"count must be > 0, got {count}")


def generate_entities(entity: str, count: int, context: dict | None = None) -> int:
    """Создаёт count новых сущностей с учётом лимитов пула."""

    start_time = time.monotonic()
    config = _validate_entity(entity)
    _validate_count(count)

    table = config["table"]
    generate_one = config["generate_one"]
    columns = INSERT_COLUMNS[entity]

    conn = get_connection()
    cursor = conn.cursor()

    # Если контекст не передан - загружаем справочники сами.
    if entity == "merchant" and context is None:
        context = _load_reference_data()

    cursor.execute(f"SELECT COUNT(*) FROM {table}")
    total_before = cursor.fetchone()[0]

    # Текущий размер активного пула.
    cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE is_active = 1")
    current = cursor.fetchone()[0]

    min_limit, max_limit = load_limits(entity)

    if current >= max_limit:
        logger.debug(
            "%s: лимит достигнут (max=%s), операция пропущена",
            entity,
            _fmt(max_limit),
        )
        logger.debug("(%s) сгенерированно: 0", entity)
        _log_elapsed_ms(start_time, prefix=f"{entity} generate_entities")
        return 0

    to_add = min(count, max_limit - current)
    if to_add < count:
        logger.debug(
            "%s: count %s уменьшен до %s из-за лимита max=%s",
            entity,
            _fmt(count),
            _fmt(to_add),
            _fmt(max_limit),
        )

    # Режим register_date: первичное заполнение / потоковая генерация.
    if current == 0:
        register_date = _random_past_date(3 * 365, 30).isoformat()
    else:
        register_date = date.today().isoformat()

    # Генерируем словари, затем превращаем в кортежи в порядке колонок INSERT.
    records = []
    for _ in range(to_add):
        if entity == "merchant":
            record = generate_one(register_date, context)
        else:
            record = generate_one(register_date)
        records.append(tuple(record[col] for col in columns))

    placeholders = ", ".join("?" for _ in columns)
    insert_sql = (
        f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) "
        f"VALUES ({placeholders})"
    )
    cursor.executemany(insert_sql, records)

    # Фактически вставленное количество записей.
    batch_rowcount = cursor.rowcount
    count_cursor = conn.cursor()
    count_cursor.execute(f"SELECT COUNT(*) FROM {table}")
    total_after = count_cursor.fetchone()[0]
    inserted = min(batch_rowcount, total_after - total_before)
    conn.commit()

    skipped = to_add - inserted

    # Пул после операции.
    cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE is_active = 1")
    pool_after = cursor.fetchone()[0]

    logger.debug("(%s) сгенерированно: %s", entity, _fmt(inserted))
    logger.debug("(%s): сгенерированно %s", entity, _fmt(to_add))
    logger.debug("(%s): вставлено %s", entity, _fmt(inserted))
    logger.debug("(%s): пропущено %s (коллизии UNIQUE)", entity, _fmt(skipped))

    _log_elapsed_ms(start_time, prefix=f"{entity} generate_entities")
    return inserted


def deactivate_entities(entity: str, count: int) -> int:
    """Деактивирует count случайных активных сущностей."""

    start_time = time.monotonic()
    config = _validate_entity(entity)
    _validate_count(count)

    table = config["table"]
    id_field = config["id_field"]

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE is_active = 1")
    active_count = cursor.fetchone()[0]

    min_limit, _max_limit = load_limits(entity)

    if active_count < min_limit:
        logger.debug(
            "%s: лимит достигнут (min=%s), операция пропущена",
            entity,
            _fmt(min_limit),
        )
        logger.debug("%s деактивированно: 0", entity)
        _log_elapsed_ms(start_time, prefix=f"{entity} deactivate_entities")
        return 0

    if active_count - count < min_limit:
        count = active_count - min_limit
        logger.debug(
            "(%s): деактивированно %s, нарушен лимит (min=%s)",
            entity,
            _fmt(count),
            _fmt(min_limit),
        )
        if count <= 0:
            logger.debug("%s деактивированно: 0", entity)
            _log_elapsed_ms(start_time, prefix=f"{entity} deactivate_entities")
            return 0

    cursor.execute(
        f"SELECT {id_field} FROM {table} WHERE is_active = 1 "
        f"ORDER BY RANDOM() LIMIT {int(count)}"
    )
    ids = [row[0] for row in cursor.fetchall()]

    if not ids:
        logger.debug("(%s) deactivate_entities: нет активных сущностей", entity)
        logger.debug("(%s) деактивированно: 0", entity)
        _log_elapsed_ms(start_time, prefix=f"{entity} deactivate_entities")
        return 0

    cursor.executemany(
        f"UPDATE {table} SET is_active = 0 WHERE {id_field} = ?",
        [(id_,) for id_ in ids],
    )
    conn.commit()

    deactivated = len(ids)

    logger.debug("(%s) деактивированно: %s", entity, _fmt(deactivated))

    _log_elapsed_ms(start_time, prefix=f"{entity} deactivate_entities")
    return deactivated


def activate_entities(entity: str, count: int) -> int:
    """Активирует count случайных неактивных сущностей."""

    start_time = time.monotonic()
    config = _validate_entity(entity)
    _validate_count(count)

    table = config["table"]
    id_field = config["id_field"]

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        f"SELECT {id_field} FROM {table} WHERE is_active = 0 "
        f"ORDER BY RANDOM() LIMIT {int(count)}"
    )
    ids = [row[0] for row in cursor.fetchall()]

    if not ids:
        logger.debug("(%s) activate_entities: нет неактивных сущностей", entity)
        logger.debug("(%s) активированно: 0", entity)
        _log_elapsed_ms(start_time, prefix=f"{entity} activate_entities")
        return 0

    cursor.executemany(
        f"UPDATE {table} SET is_active = 1 WHERE {id_field} = ?",
        [(id_,) for id_ in ids],
    )
    conn.commit()

    activated = len(ids)

    logger.debug("(%s) активированно: %s", entity, _fmt(activated))
    _log_elapsed_ms(start_time, prefix=f"{entity} activate_entities")
    return activated


if __name__ == "__main__":
    deactivate_entities("user", 10)
    deactivate_entities("merchant", 10)

    close_connection()
