FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
# state.json (learned chat id) lives in /app/data so it survives restarts
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
