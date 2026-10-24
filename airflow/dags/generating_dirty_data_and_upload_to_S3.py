from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from airflow import DAG
from airflow.operators.python import PythonOperator

from include.transaction_generator.core_generator.orchestrator import (
    transactions_generator,
    OUTPUT_PATH as CLEAN_TRZ_PATH,
)
from include.transaction_generator.data_polluter.polluter import run as pollute_run
from include.storage.s3_client import build_object_key, upload_to_s3

MSK = ZoneInfo("Europe/Moscow")
DIRTY_TRZ_PATH = str(Path(CLEAN_TRZ_PATH).parent / "transactions_dirty.jsonl")

default_args = {
    "owner": "fauxflow",
    "retries": 2,
    "retry_delay": timedelta(minutes=1),
    "depends_on_past": False,
}

with DAG(
    dag_id="generating_dirty_data_and_upload_to_s3_v2",
    description="Генерирует транзакции, пачкает данные и грузит в minIO",
    start_date=datetime(2026, 10, 24, 0, 0, tzinfo=MSK),
    schedule="*/30 * * * *",
    catchup=True,
    max_active_runs=1,
    default_args=default_args,
    tags=["fauxflow", "generator", "data_source"],
) as dag:

    def _generate(**context):
        start = context["data_interval_start"]
        end = context["data_interval_end"]
        stats = transactions_generator(start, end)
        return {"written": stats["total_written"], "burned": stats["total_burned"]}

    def _pollute(**context):
        stats = pollute_run(CLEAN_TRZ_PATH, DIRTY_TRZ_PATH, level="medium")
        return {"dirtied": stats["dirtied"], "dirt_rate": stats["dirt_rate"]}

    def _upload(**context):
        start = context["data_interval_start"]
        end = context["data_interval_end"]
        key = build_object_key(start, end)
        size = upload_to_s3(DIRTY_TRZ_PATH, key)
        return {"key": key, "size": size}

    generate_task = PythonOperator(
        task_id="generate_data",
        python_callable=_generate,
    )
    pollute_task = PythonOperator(
        task_id="data_pollution",
        python_callable=_pollute,
    )
    upload_task = PythonOperator(
        task_id="upload_to_s3",
        python_callable=_upload,
    )

    generate_task >> pollute_task >> upload_task
