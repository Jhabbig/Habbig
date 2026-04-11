FROM python:3.12-slim

# Install Playwright system dependencies
RUN apt-get update && apt-get install -y \
    wget \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY scraper/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium
RUN playwright install-deps chromium

COPY scraper/ ./scraper/
EXPOSE 8001
CMD ["uvicorn", "scraper.main:app", "--host", "0.0.0.0", "--port", "8001"]
