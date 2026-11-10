# llm-bridge - ponte OpenAI-compativel para a LLM interna (Ollama).
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

EXPOSE 8080

# --timeout 180: teto do worker. Toda geracao sincrona usa LLM_TIMEOUT=150s
# (< 180) e o streaming mantem a conexao viva; o warmup roda em thread separada.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--timeout", "180", "app:app"]
