# Reddit crowd desk: three PAPER accounts and a dashboard. No broker, no real money.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PORT=8792 HOST=0.0.0.0
WORKDIR /app
COPY requirements-cloud.txt .
RUN pip install --no-cache-dir -r requirements-cloud.txt
COPY config.yaml .
COPY bot ./bot
EXPOSE 8792
CMD ["python", "-m", "bot.cloud.app"]
