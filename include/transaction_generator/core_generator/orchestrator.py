"""Точка входа в ядро генерации транзакций. 
Связывает все модули генератора и оркестрирует их работу"""

import atexit
import logging
import os
import time
import pathlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Общий модуль подключения к БД
from include.transaction_generator.db import close_connection, get_connection


# Модули ядра
from include.transaction_generator.core_generator import loader, generator, pool_updater


# Настройки логирования
from include.config.log_config import setup_logging

setup_logging()

module_name = pathlib.Path(__file__).stem
logger = logging.getLogger(module_name)


# Таймзона окна генерации - Europe/Moscow (все timestamp в событиях +03:00).
MSK = ZoneInfo("Europe/Moscow")


OUTPUT_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "clean_transactions.jsonl",
    )
)


def _fmt(n) -> str:
    """Форматирует число для читаемого вывода: 123456 -> 123_456."""

    return f"{int(n):,}".replace(",", "_")


def _normalize_moscow(dt, name):
    """Приводим pendulum.DateTime к stdlib datetime - иначе astimezone 
    с zoneinfo падает с AttributeError: NoneType object has no attribute convert"""

    if hasattr(dt, "isoformat"):
        dt = datetime.fromisoformat(dt.isoformat().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=MSK)
    return dt.astimezone(MSK)


def _round_start_down(dt: datetime) -> datetime:
    """Округляет начало окна вниз до минуты: секунды и микросекунды в 0."""

    return dt.replace(second=0, microsecond=0)


def _round_end_up(dt: datetime) -> datetime:
    """Округляет конец окна вверх до минуты."""

    if dt.second or dt.microsecond:
        return (dt + timedelta(minutes=1)).replace(second=0, microsecond=0)
    return dt.replace(microsecond=0)


def transactions_generator(start: datetime, end: datetime) -> dict:
    """Генерирует транзакции за окно [start, end) и обновляет пулы."""

    t_start = time.perf_counter()

    # Обработка дат: таймзона + округление
    start = _round_start_down(_normalize_moscow(start, "start"))
    end = _round_end_up(_normalize_moscow(end, "end"))

    # Логируем старт. Уже нормализованное окно
    
    logger.info("старт генерации: %s", datetime.now(MSK).replace(microsecond=0))

    # считаем и красиво оформлеяем интервал окна
    delta = end - start
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes < 60:
        window_interval = f"{total_minutes} мин."
    else:
        hours = total_minutes // 60
        minutes = total_minutes % 60
        window_interval = f"{hours} ч." if minutes == 0 else f"{hours} ч. {minutes} мин."

    logger.info("окно: [%s, %s] (%s)", start.isoformat(), end.isoformat(), window_interval)

    # Проверка валидности окна
    if end <= start:
        logger.error(
            "некорректное окно после округления: "
            "end=%s <= start=%s",
            end.isoformat(), start.isoformat(),
        )
        raise ValueError(
            f"end must be greater than start after rounding: "
            f"start={start.isoformat()}, end={end.isoformat()}"
        )

    # num_ticks: целое число минутных тиков в окне
    num_ticks = int((end - start).total_seconds() // 60)
    if num_ticks <= 0:
        logger.error(
            "num_ticks=%s <= 0 - окно слишком короткое даже "
            "после округления [%s, %s]",
            num_ticks, start.isoformat(), end.isoformat(),
        )
        raise ValueError(f"num_ticks must be > 0, got {num_ticks}")
    logger.debug("num_ticks=%s", _fmt(num_ticks))

    # Загрузка контекста (справочники и пулы в память)
    t_load_start = time.perf_counter()
    context = loader.load_all()
    time_load_ms = (time.perf_counter() - t_load_start) * 1000

    file_handle = open(OUTPUT_PATH, "wb")
    logger.debug("файл открыт - %s", OUTPUT_PATH)
    try:
        t_gen_start = time.perf_counter()
        gen_stats = generator.run(context, start, end, num_ticks, file_handle)
        time_generate_ms = (time.perf_counter() - t_gen_start) * 1000
    finally:
        # Если generator.run упадёт - файл всё равно должен закрыться
        file_handle.close()
        logger.debug("файл закрыт")

    # Обновление пулов в конце окна
    t_update_start = time.perf_counter()
    pool_stats = pool_updater.update_pools(context, num_ticks)
    time_update_ms = (time.perf_counter() - t_update_start) * 1000

    # Сводная статистика
    result = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "num_ticks": num_ticks,
        "total_prepared": gen_stats["total_prepared"],
        "total_written": gen_stats["total_written"],
        "total_burned": gen_stats["total_burned"],
        "burned_by_subcat": gen_stats["burned_by_subcat"],
        "burned_reasons": gen_stats["burned_reasons"],
        "pool_users": {
            "generated": pool_stats["users_generated"],
            "deactivated": pool_stats["users_deactivated"],
            "activated": pool_stats["users_activated"],
        },
        "pool_merchants": {
            "generated": pool_stats["merchants_generated"],
            "deactivated": pool_stats["merchants_deactivated"],
            "activated": pool_stats["merchants_activated"],
        },
        "time_total_ms": (time.perf_counter() - t_start) * 1000,
        "time_load_ms": time_load_ms,
        "time_generate_ms": time_generate_ms,
        "time_update_ms": time_update_ms,
    }

    # Итоговое логирование
    logger.info(
        "готово, записано %s, сгорело %s",
        _fmt(result["total_written"]),
        _fmt(result["total_burned"])
    )

    logger.debug(
        "готово, записано %s, сгорело %s, время %.0f мс "
        "(load: %.0f, generate: %.0f, update: %.0f)",
        _fmt(result["total_written"]),
        _fmt(result["total_burned"]),
        result["time_total_ms"],
        result["time_load_ms"],
        result["time_generate_ms"],
        result["time_update_ms"],
    )
    logger.debug(
        "users +%s/-%s/↺%s, merchants +%s/-%s/↺%s",
        _fmt(pool_stats["users_generated"]),
        _fmt(pool_stats["users_deactivated"]),
        _fmt(pool_stats["users_activated"]),
        _fmt(pool_stats["merchants_generated"]),
        _fmt(pool_stats["merchants_deactivated"]),
        _fmt(pool_stats["merchants_activated"]),
    )

    
    logger.info("время выполнения генерации: %.0f мс", result["time_total_ms"])

    return result


def shutdown() -> None:
    """Явное закрытие соединения с БД по завершении процесса."""

    close_connection()

# Гарантированное закрытие соединения. подстраховкаю
atexit.register(shutdown)


if __name__ == "__main__":
    # тестовый ручной запуск

    window_start_date, window_start_time = '2026-10-19', '18:00:00'
    window_end_date, window_end_time = '2026-10-19', '18:01:00'

    window_start = datetime.fromisoformat(window_start_date + 'T' + window_start_time)
    window_end = datetime.fromisoformat(window_end_date + 'T' + window_end_time)

    data = transactions_generator(window_start, window_end)

    # удобный вывод статистики
    print()
    import json
    print(json.dumps(data, indent=4, ensure_ascii=False))
