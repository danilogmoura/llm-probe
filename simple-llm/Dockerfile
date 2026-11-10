# simple-llm - docker minimo: so chama a LLM com thinking on/off.
# Escuta em 0.0.0.0:8080, primeiro plano, sem VOLUME (mesma regra do manual).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8080

CMD ["python", "app.py"]
