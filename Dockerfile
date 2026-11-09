FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt && useradd --create-home watcher \
    && mkdir /app/media-cache /app/backups && chmod 700 /app/backups \
    && chown watcher:watcher /app/media-cache /app/backups
COPY --chown=watcher:watcher . .
USER watcher
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
