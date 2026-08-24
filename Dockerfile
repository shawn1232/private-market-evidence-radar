FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    DEALSCOPE_MODE=public_readonly \
    DEALSCOPE_DISABLE_NETWORK=1 \
    DEALSCOPE_DEEP_BASE_URL=/workbench/ \
    DEALSCOPE_RADAR_BASE_URL=/

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY . ./

RUN addgroup --system dealscope \
    && adduser --system --ingroup dealscope --home /home/dealscope dealscope \
    && mkdir -p /app/data/output \
    && chown -R dealscope:dealscope /app /home/dealscope

USER dealscope
EXPOSE 8080

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 4 --timeout 120 --access-logfile - cloud_app:application"]

