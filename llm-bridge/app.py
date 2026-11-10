"""llm-bridge - proxy transparente da API OpenAI para a LLM interna (Ollama).

Contexto: o link publico do portal expoe o SEU container, nao a LLM. A LLM fica
presa na `llm-network` e so e alcancavel de dentro de um container. Este app roda
dentro da rede e repassa as chamadas sem traduzir protocolo:

    cliente local (ex.: P0, lib OpenAI)
        POST /v1/chat/completions  {model, messages, stream}
            -> interno ->  POST {LLM_URL}/v1/chat/completions  (corpo identico)
            <- interno <-  Ollama (ja em formato OpenAI: chat.completion / SSE chunk)
        <- OpenAI: repassa o corpo/chunks exatamente como vieram

Diferente da versao anterior (conversor OpenAI->/api/generate), aqui NAO ha
achatamento de mensagens nem remontagem de JSON: validamos empiricamente que o
servidor expoe a camada compativel com OpenAI (probe `/llm/openai-compat`), entao
basta encaminhar. O contrato na porta publica permanece igual, portanto o P0 nao
precisa mudar nada -- continua apontando LLM_BASE_URL para esta URL publica.

Por que nao usar a lib `openai` aqui dentro: a LLM usa certificado autogerado e
exige verify=False; a lib (httpx) valida TLS por padrao e nao expoe uma forma
simples de desligar isso. Usamos `requests` com verify=False, que resolve.

Conforme manual.md: escuta em 0.0.0.0, porta via PORT (padrao 8080), processo em
primeiro plano, sem VOLUME. SSL verify=False (certificado autogerado da LLM).
"""

import json
import os
import threading
import time

import requests
import urllib3
from flask import Flask, Response, jsonify, request as req

APP_NAME = "llm-bridge"

# Config da LLM vem SEMPRE por variavel de ambiente (definida no dashboard).
# NAO ha default aqui de proposito: o endereco real e do ambiente institucional
# e nao deve ser versionado no repositorio publico.
LLM_URL = os.environ.get("LLM_URL", "").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "")

# Timeout de leitura de uma geracao. Mantido ABAIXO do --timeout do gunicorn
# (CMD do Dockerfile = 180s); teto default de 150s cobre o cold start (~105s).
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "150"))
# Timeout de conectividade (abrir TCP/TLS): host morto falha rapido.
CONNECT_TIMEOUT = float(os.environ.get("CONNECT_TIMEOUT", "10"))

# --- Protecao minima da porta publica ---------------------------------------
# Uma rota aberta + sem-auth num servidor monitorado convida terceiros a gastar
# sua cota de inferencia. Se BRIDGE_TOKEN estiver definido, toda chamada deve
# trazer Authorization: Bearer <token>. Vazio = sem checagem (use apenas em
# teste local; evite em deploy publico).
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "").strip()

# --- Aquecimento (warmup) ---------------------------------------------------
# Paga o cold start (~105s) da PRIMEIRA geracao em background no startup, para
# que a primeira requisicao real do usuario nao estoure o --timeout do worker.
WARMUP_ENABLED = os.environ.get("WARMUP_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off")
WARMUP_PROMPT = os.environ.get("WARMUP_PROMPT", "ping")
WARMUP_TIMEOUT = float(os.environ.get("WARMUP_TIMEOUT", "150"))
WARMUP_ATTEMPTS = int(os.environ.get("WARMUP_ATTEMPTS", "8"))
WARMUP_INTERVAL = float(os.environ.get("WARMUP_INTERVAL", "10"))

app = Flask(APP_NAME)
# NUNCA verificar certificado: a LLM usa SSL autogerado (manual.md, secao FAQ).
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _net_timeout():
    """(connect, read) p/ requests: conecta rapido, espera a resposta ate o teto."""
    return (CONNECT_TIMEOUT, LLM_TIMEOUT)


def _fwd_headers():
    """Cabecalhos a repassar ao upstream: Content-Type sempre; Authorization so
    quando o bridge nao faz a propria autentricao (para nao vazar segredo ao
    servidor quando BRIDGE_TOKEN protege a porta publica)."""
    h = {"Content-Type": req.content_type or "application/json"}
    if not BRIDGE_TOKEN:
        auth = req.headers.get("Authorization")
        if auth:
            h["Authorization"] = auth
    return h


def _err(message, code, status=502):
    """Erro no formato OpenAI (`error.{message,type,param,code}`), sem credencial."""
    return {"error": {"message": message, "type": code, "param": None,
                      "code": code}}, status


def _emit_stream(upstream):
    """Reemite o SSE cru do upstream linha a linha, mantendo a conexao viva."""
    for raw in upstream.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        # iter_lines remove o \n; devolve cada linha SSE como veio (inclui [DONE]).
        yield (raw + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Proxy transparente: repassa /v1/* ao servidor, preservando sync e streaming.
# ---------------------------------------------------------------------------
def _forward(path, payload=None):
    """Encaminha a request atual (ou um `payload` nosso) para {LLM_URL}{path}.

    Com `payload` definido, envia esse JSON (usado para preencher o `model`);
    senao repassa o corpo bruto do cliente byte-a-byte. Em streaming reemite o
    SSE cru do upstream; em sync repassa o JSON tal qual. Nunca propaga excecao
    crua nem credencial.
    """
    url = f"{LLM_URL}{path}"
    if payload is not None:
        body = None
        send_kwargs = {"json": payload}
        streaming = bool(payload.get("stream"))
    else:
        body = req.get_data(as_text=True) or None
        send_kwargs = {"data": body}
        streaming = False
        try:
            streaming = bool((json.loads(body) if body else {}).get("stream"))
        except ValueError:
            pass

    t0 = time.time()
    try:
        upstream = requests.request(
            req.method if payload is None else "POST", url,
            headers=_fwd_headers(), stream=streaming,
            timeout=_net_timeout(), verify=False, **send_kwargs)
    except requests.RequestException as exc:
        return jsonify(_err(
            f"Falha ao contatar a LLM interna: {type(exc).__name__}: {exc}",
            "connection_error")[0]), 502

    if not upstream.ok:
        detalhe = "" if streaming else upstream.text[:300]
        return jsonify(_err(
            f"A LLM interna respondeu HTTP {upstream.status_code}: {detalhe}",
            "upstream_error")[0]), 502

    if streaming:
        return Response(_emit_stream(upstream), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    # Sync: repassa o JSON do upstream sem remontar; adiciona latencia medida.
    try:
        corpo = upstream.json()
    except ValueError:
        return jsonify(_err(
            "Resposta da LLM interna nao e JSON valido.",
            "bad_upstream_json")[0]), 502
    if isinstance(corpo, dict):
        corpo.setdefault("bridge", {})["total_ms"] = round((time.time() - t0) * 1000)
    return jsonify(corpo), 200


# ---------------------------------------------------------------------------
# Autorizacao
# ---------------------------------------------------------------------------
def _autorizado():
    """True quando BRIDGE_TOKEN nao esta definido ou bate com o header enviado."""
    if not BRIDGE_TOKEN:
        return True
    cabecalho = req.headers.get("Authorization", "")
    enviado = cabecalho[7:].strip() if cabecalho.lower().startswith("bearer ") else ""
    return enviado == BRIDGE_TOKEN


def _nao_autorizado():
    """Resposta 401 pronta (nao so o corpo): evita jsonify(tupla) que viraria 200."""
    return jsonify({
        "error": {
            "message": "Token invalido ou ausente. Envie "
                       "'Authorization: Bearer <BRIDGE_TOKEN>'.",
            "type": "invalid_request_error",
            "param": None,
            "code": "unauthorized",
        }
    }), 401


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------
@app.get("/")
def home():
    return (
        "llm-bridge | proxy transparente OpenAI -> Ollama (/v1/*).<br>"
        "<b>API:</b> POST /v1/chat/completions, GET /v1/models "
        "(formato OpenAI; aceita stream)<br>"
        "<b>estado:</b> GET /health, GET /ready<br>"
        "Aponte LLM_BASE_URL do seu projeto local para esta URL publica."
    )


@app.get("/health")
def health():
    return jsonify(status="ok", app=APP_NAME, mode="transparent-proxy",
                   port=int(os.environ.get("PORT", 8080)),
                   llm_url_set=bool(LLM_URL), llm_model_set=bool(LLM_MODEL),
                   auth_required=bool(BRIDGE_TOKEN))


@app.get("/v1/models")
def models():
    """Lista modelos. Com LLM_MODEL definido, devolve so o configurado (nao
    enumera a LLM interna). Sem LLM_MODEL, repassa a lista completa do servidor."""
    if not LLM_URL:
        return jsonify(_err(
            "Bridge sem configuracao: defina LLM_URL no ambiente.",
            "config_error", status=503)[0]), 503
    if not LLM_MODEL:
        return _forward("/v1/models")
    return jsonify({
        "object": "list",
        "data": [{"id": LLM_MODEL, "object": "model", "owned_by": APP_NAME}],
    })


@app.post("/v1/chat/completions")
def chat_completions():
    """Endpoint principal: repassa o corpo OpenAI ao servidor e devolve a
    resposta OpenAI (sync ou SSE), sem traducao."""
    if not _autorizado():
        return _nao_autorizado()
    if not LLM_URL:
        return jsonify(_err(
            "Bridge sem configuracao: defina LLM_URL no ambiente.",
            "config_error", status=503)[0]), 503

    data = req.get_json(silent=True) or {}
    if not data.get("messages"):
        return jsonify(_err(
            "Campo 'messages' e obrigatorio.",
            "invalid_request_error", status=400)[0]), 400
    # Garante um modelo: respeita o pedido pelo cliente, senao injeta o configurado.
    if not data.get("model") and LLM_MODEL:
        return _forward("/v1/chat/completions", payload=dict(data, model=LLM_MODEL))
    return _forward("/v1/chat/completions")


# ---------------------------------------------------------------------------
# Aquecimento (warmup): mesmo padrao do probe-llm, adaptado para a bridge.
# ---------------------------------------------------------------------------
_WARMUP_LOCK = threading.Lock()
_WARMUP_STATE = {
    "status": "pending",  # pending | warming | warm | failed | disabled | skipped
    "attempts": 0,
    "started_at": None,
    "finished_at": None,
    "duration_ms": None,
    "error": None,
}


def _marcar_warmup(**campos):
    with _WARMUP_LOCK:
        _WARMUP_STATE.update(campos)


def _tentativa_warmup():
    """Uma geracao minima sincrona ao /v1/chat/completions. Retorna (ok, erro)."""
    url = f"{LLM_URL}/v1/chat/completions"
    payload = {"model": LLM_MODEL, "stream": False,
               "messages": [{"role": "user", "content": WARMUP_PROMPT}]}
    try:
        resp = requests.post(url, json=payload, timeout=_net_timeout(),
                             verify=False)
    except requests.RequestException as exc:
        return (False, f"{type(exc).__name__}: {exc}")
    if not resp.ok:
        return (False, f"HTTP {resp.status_code}: {resp.text[:200]}")
    return (True, None)


def _rodar_warmup():
    _marcar_warmup(status="warming", started_at=time.time(), attempts=0,
                   error=None, duration_ms=None, finished_at=None)
    for tentativa in range(1, max(1, WARMUP_ATTEMPTS) + 1):
        _marcar_warmup(attempts=tentativa)
        ok, erro = _tentativa_warmup()
        if ok:
            _marcar_warmup(
                status="warm", finished_at=time.time(),
                duration_ms=round((time.time() - _WARMUP_STATE["started_at"]) * 1000),
                error=None)
            return
        _marcar_warmup(error=str(erro)[:300])
        if tentativa < WARMUP_ATTEMPTS:
            time.sleep(WARMUP_INTERVAL)
    _marcar_warmup(status="failed", finished_at=time.time())


def step_warmup():
    if not WARMUP_ENABLED:
        _marcar_warmup(status="disabled")
        return {"started": False, "status": "disabled"}
    if not LLM_MODEL or not LLM_URL:
        _marcar_warmup(status="skipped", error="LLM_URL/LLM_MODEL nao definidos")
        return {"started": False, "status": "skipped"}
    with _WARMUP_LOCK:
        em_andamento = _WARMUP_STATE["status"] in ("warming", "warm")
    if em_andamento:
        return {"started": False, "status": _WARMUP_STATE["status"]}
    threading.Thread(target=_rodar_warmup, name="warmup", daemon=True).start()
    return {"started": True, "status": "warming"}


def _estado_warmup():
    with _WARMUP_LOCK:
        return dict(_WARMUP_STATE)


@app.get("/ready")
def ready():
    """Prontidao real: 200 so quando o modelo respondeu de fato (warm).

    Enquanto aquece, 503 + Retry-After. Sem aquecimento ativo (failed/skipped/
    disabled), faz um teste leve de geracao para dar veredito honesto.
    """
    estado = _estado_warmup()
    status = estado.get("status")
    base = {"warmup_status": status, "warmup_attempts": estado.get("attempts")}
    if status == "warm":
        base.update({"ready": True,
                     "warmup_duration_ms": estado.get("duration_ms")})
        return jsonify(base), 200
    if status in ("warming", "pending"):
        base.update({
            "ready": False,
            "retry_after_s": int(max(2, WARMUP_INTERVAL)),
            "hint": "Aquecendo o modelo (cold start ~105s; LLM pode levar 1-2 min "
                    "apos o container subir). Faca polling ate virar 200.",
        })
        return jsonify(base), 503, {"Retry-After": str(int(max(2, WARMUP_INTERVAL)))}
    # failed | skipped | disabled -> teste leve de geracao.
    ok, erro = _tentativa_warmup()
    base.update({"ready": ok})
    if not ok:
        base["hint"] = erro
    return jsonify(base), (200 if ok else 503)


# No import do modulo (startup do worker gunicorn / python app.py), dispara o
# aquecimento em background para o cold start rodar enquanto ninguem espera.
step_warmup()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
