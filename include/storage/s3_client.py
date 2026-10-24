"""Генерирует ключ и загружает файл в S3"""

import logging
import mimetypes
import pathlib
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from include.config.s3_settings import (
    MINIO_ENDPOINT,
    MINIO_ACCESS_KEY,
    MINIO_SECRET_KEY,
    MINIO_BUCKET,
    MINIO_REGION,
)

# Настройки логирования
from include.config.log_config import setup_logging

setup_logging()

module_name = pathlib.Path(__file__).stem
logger = logging.getLogger(module_name)


MSK = ZoneInfo("Europe/Moscow")


def _to_msk(dt: datetime) -> datetime:
    """Перевод времени в МСК: замена pendulum.
    DateTime на stdlib datetime из-за ошибки astimezone(ZoneInfo)."""

    if hasattr(dt, "isoformat"):
        dt = datetime.fromisoformat(dt.isoformat().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=MSK)
    return dt.astimezone(MSK)


def build_object_key(
    start: datetime,
    end: datetime,
    data_type: str = "transactions",
    extension: str = "jsonl",
) -> str:
    """
    Формирует ключ объекта в бакете.

    Формат: raw/<data_type>/YYYY/MM/DD/<data_type>_HH-MM_HH-MM.<ext>

    Дата в пути - из start (Europe/Moscow). Время в имени файла -
    start и end (HH-MM). Если окно переходит через полночь, дата
    остаётся датой начала, end отображается как 00-00.
    """

    start_msk = _to_msk(start)
    end_msk = _to_msk(end)

    year = start_msk.strftime("%Y")
    month = start_msk.strftime("%m")
    day = start_msk.strftime("%d")
    start_hm = start_msk.strftime("%H-%M")
    end_hm = end_msk.strftime("%H-%M")

    ext = extension.lstrip(".")
    return (
        f"raw/{data_type}/{year}/{month}/{day}/"
        f"{data_type}_{start_hm}_{end_hm}.{ext}"
    )


def _get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name=MINIO_REGION,
    )


def _guess_content_type(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    if ext == ".jsonl":
        return "application/x-ndjson"

    content_type, _ = mimetypes.guess_type(str(file_path))
    return content_type or "application/octet-stream"


def upload_to_s3(
    file_path: str | Path,
    object_key: str,
    content_type: str | None = None,
) -> int:
    """
    Загружает файл в S3 хранилище потоково.

    Использует upload_file - multipart, не держит файл целиком в памяти.
    Возвращает размер загруженных данных в байтах.
    """

    file_path = Path(file_path)
    if not file_path.is_file():
        logger.error("File not found: %s", file_path)
        raise FileNotFoundError(file_path)

    if content_type is None:
        content_type = _guess_content_type(file_path)

    size = file_path.stat().st_size
    logger.info(
        "Загрузка %s (%d bytes) to s3://%s/%s (Content-Type: %s)",
        file_path,
        size,
        MINIO_BUCKET,
        object_key,
        content_type,
    )

    client = _get_s3_client()
    try:
        client.upload_file(
            Filename=str(file_path),
            Bucket=MINIO_BUCKET,
            Key=object_key,
            ExtraArgs={"ContentType": content_type},
        )
    except (BotoCoreError, ClientError):
        logger.exception("Failed to upload s3://%s/%s", MINIO_BUCKET, object_key)
        raise

    logger.info("Загрузка завершена: s3://%s/%s", MINIO_BUCKET, object_key)
    return size


if __name__ == "__main__":
    pass
    # import tempfile

    # logging.basicConfig(
    #     level=logging.INFO,
    #     format="%(asctime)s [%(levelname)s]: %(message)s",
    # )

    # start = datetime(2026, 6, 1, 14, 15, tzinfo=MSK)
    # end = datetime(2026, 6, 1, 14, 30, tzinfo=MSK)
    # key = build_object_key(start, end)

    # logger.debug("Object key: %s", key)

    # with tempfile.NamedTemporaryFile(
    #     suffix=".jsonl",
    #     mode="w",
    #     delete=False,
    #     encoding="utf-8",
    # ) as f:
    #     f.write('{"event_id": "test", "amount": 500}\n')
    #     f.write('{"event_id": "test2", "amount": 0}\n')
    #     tmp_path = f.name

    # try:
    #     size = upload_to_s3(tmp_path, key)
    #     print(f"Uploaded {size} bytes. Done.")
    # finally:
    #     Path(tmp_path).unlink()
