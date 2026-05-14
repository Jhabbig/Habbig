FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN adduser --disabled-password --gecos "" appuser && \
    mkdir -p /app/data/cache && \
    chown -R appuser:appuser /app
USER appuser
EXPOSE 8053
CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8053"]
