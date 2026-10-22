"""Пачканье транзакций.

Модуль читает чистый JSONL построчно, с заданной вероятностью (level)
портит строку одним из восьми типов грязи и пишет результат в выходной файл.
Строки, которые не портятся, копируются без парсинга и
сериализации.
"""


import os
import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import orjson


# Настройки логирования
import pathlib
import logging
from include.config.log_config import setup_logging

setup_logging()

module_name = pathlib.Path(__file__).stem
logger = logging.getLogger(module_name)


# Уровни пачканья
LEVEL_PRESETS = {
    "none": 0.0,
    "low": 0.01,
    "medium": 0.05,   # дефолт
    "high": 0.15,
    "hardcore": 0.5,
}

DEFAULT_LEVEL = "medium"

# Типы пачканья
ALL_DIRT_TYPES = [
    "amount_as_string",
    "wrong_timestamp_format",
    "negative_amount",
    "huge_amount",
    "future_timestamp",
    "future_birth_date",
    "wrong_category",
    "broken_pii",
]

MSK_TZ = ZoneInfo("Europe/Moscow")


def _fmt(value) -> str:
    """Читаемый вывод больших чисел: 123456 → 123_456."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}".replace(",", "_")
    return str(value)


def _resolve_level(level) -> float:
    """Возвращает rate (float в [0, 1]) по пресету или числу."""

    if isinstance(level, (int, float)) and not isinstance(level, bool):
        if 0.0 <= level <= 1.0:
            return float(level)
        logger.error(
            "level вне диапазона [0, 1]: %s", level
        )
        raise ValueError(f"level должен быть в диапазоне [0.0, 1.0], получен {level!r}")

    if isinstance(level, str) and level in LEVEL_PRESETS:
        return LEVEL_PRESETS[level]

    logger.error("неизвестный level: %r", level)
    raise ValueError(
        f"неизвестный level {level!r}: допустимы пресеты "
        f"{sorted(LEVEL_PRESETS)} или число от 0.0 до 1.0"
    )


# Применение одного типа пачканья
def _apply_dirt(record: dict, dirt_type: str) -> tuple[dict, str | None]:
    """Применяет один тип грязи к записи."""

    handler = _DIRT_HANDLERS[dirt_type]
    applied = handler(record)
    return record, (dirt_type if applied else None)


def _is_number(value) -> bool:
    """Настоящее число (int/float), bool не считается."""

    return isinstance(value, (int, float)) and not isinstance(value, bool)


# отдельные обработчики

def _dirt_amount_as_string(record: dict) -> bool:
    amount = record.get("amount")
    if "amount" not in record or not _is_number(amount):
        return False
    fmt = random.choice([
        lambda a: f"{a:,.2f} ₽".replace(",", " ").replace(".", ","),  # 1 234,56 ₽
        lambda a: f"{a:.2f}",                                          # 1234.56
        lambda a: f"{a:.2f}".replace(".", ","),                        # 1234,56
        lambda a: f"{a:,.2f}",                                         # 1,234.56
    ])
    record["amount"] = fmt(amount)
    return True


def _dirt_wrong_timestamp_format(record: dict) -> bool:
    ts = record.get("timestamp")
    if "timestamp" not in record:
        return False
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return False
    fmt = random.choice([
        lambda d: d.strftime("%d/%m/%Y %H:%M:%S"),
        lambda d: d.strftime("%Y%m%d_%H%M%S"),
        lambda d: str(int(d.timestamp())),
    ])
    record["timestamp"] = fmt(dt)
    return True


def _dirt_negative_amount(record: dict) -> bool:
    amount = record.get("amount")
    if "amount" not in record or not _is_number(amount):
        return False
    record["amount"] = -abs(amount)
    return True


def _dirt_huge_amount(record: dict) -> bool:
    amount = record.get("amount")
    if "amount" not in record or not _is_number(amount):
        return False
    record["amount"] = round(amount * random.uniform(100, 1000), 2)
    return True


def _dirt_future_timestamp(record: dict) -> bool:
    ts = record.get("timestamp")
    if "timestamp" not in record:
        return False
    try:
        datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return False
    future = datetime.now(tz=MSK_TZ) + timedelta(days=random.uniform(1, 30))
    record["timestamp"] = future.isoformat()
    return True


def _dirt_future_birth_date(record: dict) -> bool:
    if "user_birth_date" not in record:
        return False
    future = datetime.now(tz=MSK_TZ) + timedelta(days=random.uniform(365, 3650))
    record["user_birth_date"] = future.strftime("%Y-%m-%d")
    return True


def _dirt_wrong_category(record: dict) -> bool:
    field = random.choice(["event_type", "status"])
    if field not in record:
        return False
    if field == "event_type":
        record[field] = random.choice(["purchse", "PURCHASE", "purchase ", "Purchase"])
    else:
        record[field] = random.choice(["succes", "SUCCESS", "1", "pending"])
    return True


def _dirt_broken_pii(record: dict) -> bool:
    pii_fields = ["user_email", "user_phone", "user_fio"]
    available = [
        f for f in pii_fields
        if f in record and record[f] is not None
    ]
    if not available:
        return False
    field = random.choice(available)
    value = record[field]
    if not isinstance(value, str):
        return False
    if field == "user_email":
        record[field] = random.choice([
            value.replace("@", "_at_"),
            value.upper(),
            value.split("@")[0],
            value.replace("@", " "),
        ])
    elif field == "user_phone":
        record[field] = random.choice([
            value.replace("+", ""),
            value.replace(" ", "-"),
            "123",
            "abc",
        ])
    else:  # user_fio
        record[field] = random.choice([
            f"  {value}  ",
            value.lower(),
            "",
        ])
    return True


_DIRT_HANDLERS = {
    "amount_as_string": _dirt_amount_as_string,
    "wrong_timestamp_format": _dirt_wrong_timestamp_format,
    "negative_amount": _dirt_negative_amount,
    "huge_amount": _dirt_huge_amount,
    "future_timestamp": _dirt_future_timestamp,
    "future_birth_date": _dirt_future_birth_date,
    "wrong_category": _dirt_wrong_category,
    "broken_pii": _dirt_broken_pii,
}


# Статистика

def _init_stats(rate: float) -> dict:
    return {
        "total_read": 0,
        "total_written": 0,
        "dirtied": 0,
        "unapplied": 0,
        "by_type": {t: 0 for t in ALL_DIRT_TYPES},
        "dirt_rate": rate,
        "time_ms": 0.0,
    }


# Основная функция

def run(input_path, output_path, level=DEFAULT_LEVEL) -> dict:
    """Пачкает транзакции из input_path и пишет в output_path."""

    rate = _resolve_level(level)
    stats = _init_stats(rate)
    t_start = time.perf_counter()

    logger.info("старт: level=%s, rate=%s", level, rate)

    input_file = None
    output_file = None
    try:
        try:
            input_file = open(os.fspath(input_path), "rb")
        except OSError as exc:
            logger.error(
                "не удалось открыть входной файл %s: %s",
                input_path, exc,
            )
            raise
        try:
            output_file = open(os.fspath(output_path), "wb")
        except OSError as exc:
            logger.error(
                "не удалось открыть выходной файл %s: %s",
                output_path, exc,
            )
            raise

        # Цикл по строкам
        for line in input_file:
            stats["total_read"] += 1

            dirty_chosen = rate > 0 and random.random() < rate

            if not dirty_chosen:
                output_file.write(line)
                stats["total_written"] += 1
                continue

            stripped = line.rstrip(b"\n")
            record = orjson.loads(stripped)
            dirt_type = random.choice(ALL_DIRT_TYPES)
            result, applied_type = _apply_dirt(record, dirt_type)

            if applied_type is None:
                # Тип не применился — строка уходит без изменений.
                stats["unapplied"] += 1
                output_file.write(line)
                stats["total_written"] += 1
            else:
                output_file.write(orjson.dumps(result) + b"\n")
                stats["dirtied"] += 1
                stats["by_type"][applied_type] += 1
                stats["total_written"] += 1
    finally:
        if input_file is not None:
            input_file.close()
        if output_file is not None:
            output_file.close()

    stats["time_ms"] = (time.perf_counter() - t_start) * 1000

    logger.info(
        "готово: прочитано %s, испорчено %s, пропущено %s",
        _fmt(stats["total_read"]),
        _fmt(stats["dirtied"]),
        _fmt(stats["unapplied"]),
    )
    logger.info("Время выполнения: %.0f мс", stats["time_ms"])

    applied_by_type = {t: c for t, c in stats["by_type"].items() if c > 0}
    logger.debug("по типам: %s", applied_by_type)

    return stats


if __name__ == "__main__":
    # тестовый ручной запуск
    clean_trz_path = 'include/transaction_generator/clean_transactions.jsonl'
    dirty_trz_path = 'include/transaction_generator/dirty_transactions.jsonl'

    data = run(clean_trz_path, dirty_trz_path)

    # удобный вывод статистики
    print()
    import json
    print(json.dumps(data, indent=4, ensure_ascii=False))