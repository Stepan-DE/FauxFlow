FROM apache/airflow:2.7.1-python3.10

# Добавляем путь к пользовательским пакетам и include
ENV PYTHONPATH="/opt/airflow/include:/home/airflow/.local/lib/python3.10/site-packages"

# Создаём пользователя с UID 1000
ARG AIRFLOW_UID=1000
USER root

# Устанавливаем только создание пользователя, без пакетов apt
RUN if ! getent passwd ${AIRFLOW_UID} >/dev/null; then \
        useradd -m -u ${AIRFLOW_UID} -s /bin/bash airflow_user; \
    fi

# Возвращаемся к рабочему пользователю
USER airflow

# Копируем requirements.txt из корня проекта
COPY --chown=airflow:0 requirements.txt /requirements.txt

# Ставим Python-зависимости
RUN pip install --no-cache-dir "apache-airflow==${AIRFLOW_VERSION}" -r /requirements.txt