FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    DEALSCOPE_MODE=public_live \
    DEALSCOPE_PUBLIC_RSS_ONLY=1 \
    DEALSCOPE_ALLOW_PUBLIC_WECHAT_FALLBACK=0 \
    DEALSCOPE_REFRESH_COOLDOWN_SECONDS=900 \
    DEALSCOPE_DEEP_BASE_URL=/workbench/ \
    DEALSCOPE_RADAR_BASE_URL=/

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY . ./

RUN mkdir -p /app/data/output

EXPOSE 8080

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 4 --timeout 120 --access-logfile - cloud_app:application"]
