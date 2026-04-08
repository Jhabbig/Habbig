FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN apt-get purge -y --auto-remove gcc && rm -rf /var/lib/apt/lists/*
COPY . .
RUN adduser --disabled-password --gecos "" appuser
USER appuser
EXPOSE 18789
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18789"]
