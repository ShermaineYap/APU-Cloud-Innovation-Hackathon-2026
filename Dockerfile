FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY campuspulse ./campuspulse
COPY bigquery ./bigquery
COPY web ./web

RUN useradd --create-home app && mkdir -p /app/data && chown app /app/data
USER app

CMD exec uvicorn campuspulse.api:app --host 0.0.0.0 --port ${PORT} --workers 1
