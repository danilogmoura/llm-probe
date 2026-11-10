# probe-llm - app minimo de teste de chat com a LLM institucional.
# Manual: escuta em 0.0.0.0, EXPOSE 8080, sem VOLUME, primeiro plano.
# SSL: verify desabilitado (certificado autogerado da LLM).

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ ./templates/
COPY static/ ./static/

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--timeout", "180", "app:app"]
