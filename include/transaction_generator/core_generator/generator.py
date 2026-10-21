"""
Ядро генерации транзакций: цикл по тикам и запись событий в JSONL.
"""

import logging
import os
import random
import time
import uuid
import pathlib
from datetime import datetime, timedelta
from typing import Iterator
from zoneinfo import ZoneInfo

import numpy as np
import orjson

# настройка работы с БД
from include.transaction_generator.db import close_connection, get_connection

# Настройки логирования
from include.config.log_config import setup_logging

setup_logging()

module_name = pathlib.Path(__file__).stem
logger = logging.getLogger(module_name)

# Генераторы сумм
from include.transaction_generator.core_generator.amount_generator import generate_continuous_sum, generate_discrete_sum


# Таймзона всех timestamp - Europe/Moscow.
MSK = ZoneInfo("Europe/Moscow")

# Дни недели по порядку: 0 = monday ... 6 = sunday. В БД activity_schedule
# день хранится числом (0..6), generator получает из тика имя дня
DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# Поля пользователя в транзакции.
USER_FIELDS = (
    "fio",
    "birth_date",
    "phone",
    "email",
    "register_date",
    "passport_number",
    "address",
    "account_number",
)

# Причины сгорания событий.
BURN_REASONS = (
    "empty_subcategory",
    "zero_weight",
    "amount_none",
    "user_data_missing",
)


def _fmt(n) -> str:
    """Форматирует число для читаемого вывода: 123456 -> 123_456."""
    return f"{int(n):,}".replace(",", "_")


def _get_config_value(config: dict, key: str, default):
    """Достаёт значение из config 
    при отсутствии ключа - default с логом."""

    if key in config:
        return config[key]
    logger.debug("ключ %r отсутствует в config, используется %s", key, default)
    return default


# итератор тиков

def _iter_ticks(start: datetime, end: datetime, num_ticks: int) -> Iterator[tuple]:
    """Генерирует тики окна."""

    for tick_index in range(num_ticks):
        tick_start = start + timedelta(minutes=tick_index)
        tick_end = tick_start + timedelta(minutes=1)
        yield tick_index, tick_start, tick_end, tick_start.strftime("%A").lower(), tick_start.hour


def _activity_key(context: dict, day_name: str, hour: int) -> tuple:
    """Возвращает ключ расписания (day, hour) в том виде, в каком он лежит в context."""

    activity = context["activity"]
    if (day_name, hour) in activity:
        return (day_name, hour)
    return (DAY_NAMES.index(day_name), hour)


# интенсивность тика

def _calculate_intensity(context: dict, day_name: str, hour: int) -> float:
    """Считает ожидаемое число событий за тик."""

    coefficients = context["activity"][_activity_key(context, day_name, hour)]
    hour_coefficient = sum(coefficients.values()) / len(coefficients)

    config = context["config"]
    jitter_min = _get_config_value(config, "jitter_min", 0.8)
    jitter_max = _get_config_value(config, "jitter_max", 1.2)
    base_user_rate = _get_config_value(config, "base_user_rate", 0.001)
    hour_coefficient_reference = _get_config_value(config, "hour_coefficient_reference", 1.0)

    jitter = random.uniform(jitter_min, jitter_max)
    intensity = (
        base_user_rate
        * (hour_coefficient / hour_coefficient_reference)
        * context["pool_size_users"]
        * jitter
    )
    if intensity <= 0:
        return 0.0
    return intensity


# заготовки тика

def _prepare_tick(context: dict, coefficients: dict, n: int) -> list:
    """Цикл заготовок: список из n пар (category_id, subcategory_id)."""

    category_ids = list(coefficients.keys())
    category_weights = list(coefficients.values())
    prepared: list = []
    while len(prepared) < n:
        category_id = random.choices(category_ids, weights=category_weights, k=1)[0]
        subcategories = context["subcategories_by_category"].get(category_id)
        if not subcategories:
            # Дырка в справочнике: категория без подкатегорий - пропускаем,
            # повторяем выбор, чтобы набрать ровно n заготовок.
            continue
        weights = [s["weight"] for s in subcategories]
        if sum(weights) <= 0:
            # Все веса подкатегорий нулевые - повторяем выбор категории,
            # чтобы набрать ровно n заготовок.
            continue
        sub = random.choices(subcategories, weights=weights, k=1)[0]
        prepared.append((category_id, sub["subcategory_id"]))
    return prepared


# Пакетная загрузка пользователей

def _fetch_users(context: dict, n: int) -> tuple:
    """Выбирает n пользователей и загружает их данные одним запросом."""

    user_ids = np.random.choice(context["active_user_ids"], size=n, replace=True)
    unique_ids = list(set(user_ids.tolist()))

    conn = get_connection()
    users_data: dict = {}
    query_template = (
        "SELECT user_id, fio, birth_date, phone, email, register_date, "
        "passport_number, address, account_number "
        "FROM users WHERE user_id IN ({placeholders})"
    )
    # Чанки по 900 параметров - обход лимита SQLite.
    for i in range(0, len(unique_ids), 900):
        chunk = unique_ids[i:i + 900]
        chunk_query = query_template.format(placeholders=",".join("?" * len(chunk)))
        try:
            cursor = conn.execute(chunk_query, list(map(int, chunk)))
        except Exception as exc:
            logger.error("ошибка SELECT пользователей: %s", exc)
            raise
        for row in cursor.fetchall():
            record = dict(zip(("user_id",) + USER_FIELDS, row))
            users_data[record["user_id"]] = record
    return user_ids, users_data


# сборка одного события

def _find_subcategory(context: dict, category_id: int, subcategory_id: int):
    """Находит словарь подкатегории в списке категории."""

    for sub in context["subcategories_by_category"][category_id]:
        if sub["subcategory_id"] == subcategory_id:
            return sub
    return None


def _generate_amount(sub: dict):
    """Генерирует сумму по типу подкатегории; может вернуть None (сгорание)."""

    config = sub["config"]
    if sub["type"] == "continuous":
        return generate_continuous_sum(config["min"], config["max"], config["average"])
    if sub["type"] == "discrete":
        return generate_discrete_sum(config["tariffs"])
    logger.error(
        "неизвестный тип подкатегории %r (subcategory_id=%s)",
        sub["type"], sub["subcategory_id"],
    )
    raise ValueError(f"unknown subcategory type: {sub['type']!r}")


def _build_event(
    context: dict,
    prepared: tuple,
    users_data: dict,
    tick_start: datetime,
    tick_end: datetime,
    user_id: int,
    merchant_sum_weight: float,
) -> dict | None:
    """Собирает одно событие либо возвращает None (сгорело)."""

    category_id, subcategory_id = prepared

    merchants = context["merchants_by_subcat"].get(subcategory_id, [])
    if not merchants:
        return None  # empty_subcategory
    if merchant_sum_weight == 0:
        return None  # zero_weight

    sub = _find_subcategory(context, category_id, subcategory_id)
    if sub is None:
        logger.error(
            "подкатегория %s не найдена в категории %s - баг контекста",
            subcategory_id, category_id,
        )
        raise ValueError(f"subcategory {subcategory_id} missing in category {category_id}")

    amount = _generate_amount(sub)
    if amount is None:
        return None  # amount_none

    merchant = random.choices(merchants, weights=[m["weight"] for m in merchants], k=1)[0]

    user = users_data.get(user_id)
    if user is None:
        return None  # user_data_missing

    config = context["config"]
    channel_probs = _get_config_value(config, "channel_probs", {"mobile": 0.5, "web": 0.3, "terminal": 0.2})
    device_probs_mobile = _get_config_value(config, "device_probs_mobile", {"iOS": 0.4, "Android": 0.6})
    status_probs = _get_config_value(config, "status_probs", {"success": 0.9, "failed": 0.1})
    error_code_probs = _get_config_value(config, "error_code_probs", {})

    channel = random.choices(list(channel_probs.keys()), weights=list(channel_probs.values()), k=1)[0]
    if channel == "mobile":
        device_type = random.choices(
            list(device_probs_mobile.keys()), weights=list(device_probs_mobile.values()), k=1
        )[0]
    else:
        device_type = None

    status = random.choices(list(status_probs.keys()), weights=list(status_probs.values()), k=1)[0]
    if status == "failed" and error_code_probs:
        error_code = random.choices(
            list(error_code_probs.keys()), weights=list(error_code_probs.values()), k=1
        )[0]
    else:
        error_code = None

    ip_address = (
        f"{random.randint(1, 255)}.{random.randint(0, 255)}"
        f".{random.randint(0, 255)}.{random.randint(1, 255)}"
    )

    # timestamp: случайная секунта внутри тика, нормализованная в Europe/Moscow
    ts = tick_start + timedelta(seconds=random.randint(0, 59))
    if ts.tzinfo is not None:
        ts = ts.astimezone(MSK)
    else:
        ts = ts.replace(tzinfo=MSK)

    # Фиксированный порядок 23 полей.
    event = {
        "event_id": str(uuid.uuid4()),
        "timestamp": ts.isoformat(),
        "event_type": "purchase",
        "user_id": user["user_id"],
        "user_fio": user["fio"],
        "user_birth_date": user["birth_date"],
        "user_phone": user["phone"],
        "user_email": user["email"],
        "user_register_date": user["register_date"],
        "user_passport_number": user["passport_number"],
        "user_address": user["address"],
        "user_account_number": user["account_number"],
        "merchant_id": merchant["merchant_id"],
        "merchant_name": merchant["name"],
        "merchant_address": merchant["address"],
        "subcategory_id": subcategory_id,
        "channel": channel,
        "device_type": device_type,
        "ip_address": ip_address,
        "amount": amount,
        "currency": "RUB",
        "status": status,
        "error_code": error_code,
    }
    return event


def _burn_reason(
    context: dict,
    prepared: tuple,
    users_data: dict,
    user_id: int,
    merchant_sum_weight: float,
) -> str:
    """Определяет причину сгорания заготовки."""

    category_id, subcategory_id = prepared
    merchants = context["merchants_by_subcat"].get(subcategory_id, [])
    if not merchants:
        return "empty_subcategory"
    if merchant_sum_weight == 0:
        return "zero_weight"
    sub = _find_subcategory(context, category_id, subcategory_id)
    if sub is None or _generate_amount(sub) is None:
        return "amount_none"
    if user_id not in users_data:
        return "user_data_missing"
    return "user_data_missing"


# Фалаш буфера

def _flush_buffer(file_handle, buffer: list) -> None:
    """Пишет все строки буфера одним вызовом write и очищает буфер."""

    if not buffer:
        return
    try:
        file_handle.write(b"".join(buffer))
    except Exception as exc:
        logger.error("ошибка записи в файл: %s", exc)
        raise
    buffer.clear()


# Основной цикл

REQUIRED_CONTEXT_KEYS = (
    "config",
    "categories",
    "subcategories_by_category",
    "activity",
    "active_user_ids",
    "merchants_by_subcat",
    "pool_size_users",
    "pool_size_merchants",
)


def run(context: dict, start: datetime, end: datetime, num_ticks: int, file_handle) -> dict:
    """Основной цикл генерации: пишет события в file_handle, возвращает статистику."""

    # Логические ошибки входа - логируем и падаем.
    missing = [k for k in REQUIRED_CONTEXT_KEYS if k not in context]
    if missing:
        logger.error("в context отсутствуют ключи: %s", ", ".join(missing))
        raise ValueError(f"context is missing keys: {missing}")
    if num_ticks <= 0:
        logger.error("некорректный num_ticks=%s - должно быть > 0", num_ticks)
        raise ValueError(f"num_ticks must be > 0, got {num_ticks}")

    stats = {
        "num_ticks": num_ticks,
        "total_prepared": 0,
        "total_written": 0,
        "total_burned": 0,
        "burned_by_subcat": {},
        "burned_reasons": {reason: 0 for reason in BURN_REASONS},
    }
    buffer: list = []

    logger.debug(
        "старт, окно [%s, %s], num_ticks=%s, пользователей %s, мерчантов %s",
        start.isoformat(), end.isoformat(), _fmt(num_ticks),
        _fmt(context["pool_size_users"]), _fmt(context["pool_size_merchants"]),
    )

    t_start = time.perf_counter()

    written_before_tick = 0
    burned_before_tick = 0
    for tick_index, tick_start, tick_end, day_name, hour in _iter_ticks(start, end, num_ticks):
        coefficients = context["activity"][_activity_key(context, day_name, hour)]
        hour_coefficient = sum(coefficients.values()) / len(coefficients)
        intensity = _calculate_intensity(context, day_name, hour)
        n = int(np.random.poisson(intensity))
        stats["total_prepared"] += n

        if n == 0:
            logger.debug(
                "тик %d/%d, hour_coeff=%.3f, intensity=%.3f, n=0, written=0, burned=0",
                tick_index + 1, num_ticks, hour_coefficient, intensity,
            )
            continue

        # 1. Заготовки (category_id, subcategory_id).
        prepared = _prepare_tick(context, coefficients, n)

        # 2. Пользователи одним пакетом SELECT'ов (чанки по 900).
        user_ids, users_data = _fetch_users(context, n)

        # Суммы весов мерчантов по подкатегориям считаем один раз за тик -
        # используются для проверки zero_weight и в сборке, и при подсчёте
        # причин сгорания (иначе был бы повторный проход по списку на каждое
        # событие).
        sum_weights_cache: dict = {}

        def _sum_weight(subcategory_id: int) -> float:
            if subcategory_id not in sum_weights_cache:
                merchants = context["merchants_by_subcat"].get(subcategory_id, [])
                sum_weights_cache[subcategory_id] = sum(m["weight"] for m in merchants)
            return sum_weights_cache[subcategory_id]

        # 3. Сборка событий: идём по массиву ID в порядке np.random.choice.
        for i in range(n):
            user_id = int(user_ids[i])
            event = _build_event(
                context, prepared[i], users_data, tick_start, tick_end,
                user_id, _sum_weight(prepared[i][1]),
            )
            if event is None:
                stats["total_burned"] += 1
                subcategory_id = prepared[i][1]
                stats["burned_by_subcat"][subcategory_id] = (
                    stats["burned_by_subcat"].get(subcategory_id, 0) + 1
                )
                reason = _burn_reason(
                    context, prepared[i], users_data, user_id, _sum_weight(subcategory_id)
                )
                stats["burned_reasons"][reason] += 1
                continue
            buffer.append(orjson.dumps(event))
            buffer.append(b"\n")
            stats["total_written"] += 1

        # 4. Флаш в конце тика.
        _flush_buffer(file_handle, buffer)

        written_this_tick = stats["total_written"] - written_before_tick
        burned_this_tick = stats["total_burned"] - burned_before_tick
        written_before_tick = stats["total_written"]
        burned_before_tick = stats["total_burned"]
        logger.debug(
            "тик %d/%d, hour_coeff=%.3f, intensity=%.3f, n=%d, written=%d, burned=%d",
            tick_index + 1, num_ticks, hour_coefficient, intensity, n,
            written_this_tick, burned_this_tick,
        )

    _flush_buffer(file_handle, buffer)  # на случай, если остались

    total_time_ms = (time.perf_counter() - t_start) * 1000
    stats["total_time_ms"] = total_time_ms

    logger.debug(
        "готово, подготовлено %s, записано %s, сгорело %s, время %.0f мс",
        _fmt(stats["total_prepared"]), _fmt(stats["total_written"]),
        _fmt(stats["total_burned"]), total_time_ms,
    )
    logger.debug(
        "окно [%s, %s], записано %s событий, сгорело %s, время %.0f мс",
        start.isoformat(), end.isoformat(),
        _fmt(stats["total_written"]), _fmt(stats["total_burned"]), total_time_ms,
    )
    if stats["total_burned"]:
        logger.debug(
            "разбивка сгоревших по причинам: %s",
            ", ".join(f"{r}: {_fmt(c)}" for r, c in stats["burned_reasons"].items() if c),
        )
    return stats


def _count_burn_reason(stats: dict, context: dict, prepared: tuple, users_data: dict, user_id: int) -> None:
    """Определяет причину сгорания заготовки и инкрементирует счётчик."""

    category_id, subcategory_id = prepared
    merchants = context["merchants_by_subcat"].get(subcategory_id, [])
    if not merchants:
        stats["burned_reasons"]["empty_subcategory"] += 1
        return
    if sum(m["weight"] for m in merchants) == 0:
        stats["burned_reasons"]["zero_weight"] += 1
        return
    sub = _find_subcategory(context, category_id, subcategory_id)
    if sub is None:
        stats["burned_reasons"]["amount_none"] += 1
        return
    amount = _generate_amount(sub)
    if amount is None:
        stats["burned_reasons"]["amount_none"] += 1
        return
    if user_id not in users_data:
        stats["burned_reasons"]["user_data_missing"] += 1
        return
    stats["burned_reasons"]["user_data_missing"] += 1