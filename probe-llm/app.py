"""probe-llm - sonda de diagnostico da LLM interna.

Testa, por etapas e SEM derrubar o servidor:
  1. DNS   -> resolve o hostname do LLM_URL
  2. TCP   -> conecta na porta do endpoint
  3. TLS   -> handshake com verify=False (cert autogerado, regra do manual)
  4. MODEL -> POST /api/show {"model": LLM_MODEL} (checa so o modelo configurado)
  5. LLM   -> POST /api/generate com LLM_MODEL

Cada etapa reporta ok/falha + erro; nenhuma falha impede o app de subir.
Conforme manual.md: escuta em 0.0.0.0, porta via PORT (padrao 8080),
processo em primeiro plano, sem VOLUME.
"""

import os
import socket
import ssl
import time
from urllib.parse import urlparse

import json

import requests
from flask import Flask, Response, jsonify, render_template, request as req

APP_NAME = "probe-llm"

# Configuracao da LLM vem SEMPRE por variavel de ambiente (definida no dashboard).
# NAO ha default aqui de proposito: o endereco real e do ambiente institucional
# e nao deve ser versionado no repositorio publico.
LLM_URL = os.environ.get("LLM_URL", "").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
# Modelos "thinking" geram um raciocinio longo ANTES de responder; em prompt
# trivial isso pode passar de 30s. Por isso o teto padrao de uma chamada e 120s
# (nao 30). Override por env se quiser mais/menos.
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "120"))

# Timeout efetivo do worker gunicorn (--timeout no CMD do Dockerfile). Esta
# constante e a FONTE UNICA desse numero para diagnosticos: mantenha-a igual ao
# valor passado em --timeout no Dockerfile. Se mudar la, mude aqui.
GUNICORN_TIMEOUT = float(os.environ.get("GUNICORN_TIMEOUT", "180"))

# Orcamento de tempo do /probe (segundos). O gunicorn mata o worker quando uma
# requisicao ultrapassa --timeout (GUNICORN_TIMEOUT). Como cada chamada LLM pode
# levar ate LLM_TIMEOUT, o /probe roda em cascata apenas ate este budget e
# retorna o que completou, marcando o resto como 'skipped'. Mantenha ABAIXO do
# --timeout do gunicorn.
PROBE_BUDGET = float(os.environ.get("PROBE_BUDGET", "55"))

# Teto da pagina /chat: cada pergunta deve responder antes do --timeout do
# Gunicorn (GUNICORN_TIMEOUT). Um modelo thinking gera raciocinio longo, entao
# uma resposta real pode passar de 120s; 150s fica abaixo do timeout do worker.
CHAT_BUDGET = float(os.environ.get("CHAT_BUDGET", "150"))

_parsed = urlparse(LLM_URL)
HOST = _parsed.hostname or ""
PORT = _parsed.port or 443

app = Flask(APP_NAME)
# NUNCA verificar certificado: a LLM usa SSL autogerado (manual.md, secao FAQ).
requests.packages.urllib3.disable_warnings()


# Contexto de deadline do /probe: None nas rotas individuais (usam LLM_TIMEOUT
# cheio); durante a cascata aponta para o instante-limite da requisicao. Assim
# TODA etapa de rede (tcp/tls/http/generate/chat) e encurtada ao restante do
# budget e nenhuma delas consegue pendurar o worker alem do --timeout gunicorn.
_PROBE_DEADLINE = None


def _net_timeout(default=None):
    """Timeout de rede respeitando o budget do /probe, quando ativo."""
    base = default if default is not None else LLM_TIMEOUT
    if _PROBE_DEADLINE is None:
        return base
    left = _PROBE_DEADLINE[0] - time.time()
    return max(0.5, min(base, left))


def step_dns():
    t0 = time.time()
    try:
        infos = socket.getaddrinfo(HOST, None)
        ips = sorted({i[4][0] for i in infos})
        return {"ok": True, "ips": ips, "ms": round((time.time() - t0) * 1000)}
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def step_tcp():
    t0 = time.time()
    try:
        with socket.create_connection((HOST, PORT), timeout=_net_timeout()):
            return {"ok": True, "target": f"{HOST}:{PORT}",
                    "ms": round((time.time() - t0) * 1000)}
    except OSError as exc:
        return {"ok": False, "target": f"{HOST}:{PORT}",
                "error": f"{type(exc).__name__}: {exc}"}


def step_tls():
    t0 = time.time()
    try:
        raw = socket.create_connection((HOST, PORT), timeout=_net_timeout())
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with ctx.wrap_socket(raw, server_hostname=HOST) as tls:
            cert = tls.getpeercert(binary_form=True)
            return {"ok": True, "version": tls.version(),
                    "cert_bytes": len(cert) if cert else 0,
                    "verify": "disabled",
                    "ms": round((time.time() - t0) * 1000)}
    except (OSError, ssl.SSLError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def step_model():
    """Checa APENAS o modelo configurado (LLM_MODEL) via POST /api/show.
    NAO enumera os modelos disponiveis: nao chama /api/tags nem lista nomes.
    O endpoint nativo Ollama espera POST com {"model": ...}; GET devolve 405.
    Confirma que o MODELO escolhido existe/respondendo. Sem LLM_MODEL definido,
    devolve skipped (nada a checar)."""
    if not LLM_MODEL:
        return {"ok": False, "skipped": True,
                "reason": "LLM_MODEL nao definido - nada para checar"}
    url = f"{LLM_URL}/api/show"
    t0 = time.time()
    try:
        resp = requests.post(url, json={"model": LLM_MODEL},
                             timeout=_net_timeout(), verify=False)
        out = {"ok": resp.ok, "status": resp.status_code, "url": url,
               "method": "POST", "checked_model": LLM_MODEL,
               "ms": round((time.time() - t0) * 1000)}
        if not resp.ok:
            # Guarda o erro bruto p/ diferenciar 'model not found' de indisponibilidade.
            out["error_body"] = resp.text[:300]
        return out
    except requests.RequestException as exc:
        return {"ok": False, "url": url, "method": "POST",
                "checked_model": LLM_MODEL,
                "error": f"{type(exc).__name__}: {exc}"}


def _parse_think(value):
    """Converte query 'think' em True/False/None (None = nao enviar o campo)."""
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in ("on", "true", "1", "yes"):
        return True
    if v in ("off", "false", "0", "no"):
        return False
    return None


def step_generate(prompt="Qual e 2+2? Responda so o numero.", think=None, timeout=None):
    if not LLM_MODEL:
        return {"ok": False, "skipped": True,
                "reason": "LLM_MODEL nao definido - informe em Variaveis no dashboard"}
    url = f"{LLM_URL}/api/generate"
    payload = {"model": LLM_MODEL, "prompt": prompt, "stream": False}
    # API nativa Ollama: 'think' liga/desliga o raciocinio em modelos hibridos.
    if think is not None:
        payload["think"] = think
    t0 = time.time()
    try:
        resp = requests.post(url, json=payload,
                             timeout=timeout or _net_timeout(), verify=False)
        body = {}
        if resp.ok:
            try:
                body = resp.json()
            except ValueError:
                body = {"raw": resp.text[:500]}
        # O campo total_duration do Ollama vem em NANOSEGUNDOS; converte para ms.
        total_ns = body.get("total_duration")
        total_ms = round(total_ns / 1_000_000) if isinstance(total_ns, (int, float)) else None
        out = {"ok": resp.ok, "status": resp.status_code, "model": LLM_MODEL,
               "think_sent": think, "api": "/api/generate",
               "response": str(body.get("response", ""))[:500],
               "thinking": body.get("thinking"),
               "total_ms": total_ms,
               "ms": round((time.time() - t0) * 1000)}
        if not resp.ok:
            # Alguns modelos recusam 'think' com 400; guardar o erro bruto.
            out["error_body"] = str(body)[:500]
        return out
    except requests.RequestException as exc:
        return {"ok": False, "model": LLM_MODEL, "think_sent": think,
                "error": f"{type(exc).__name__}: {exc}"}


def step_generate_stream(prompt, think=None):
    """Gera em STREAMING (Ollama stream:true) e devolve um gerador de eventos SSE.

    Por que streaming: modelos thinking podem levar minutos emitindo
    raciocinio antes da resposta final. Numa chamada sincrona isso estoura o
    --timeout do gunicorn (WORKER TIMEOUT). Em streaming o backend envia tokens
    conforme produz, entao a conexao fica viva e o worker nao e morto enquanto
    ha bytes chegando. O timeout aqui e de INATIVIDADE (tempo entre chunks),
    nao de duracao total."""
    if not LLM_MODEL:
        yield _sse({"event": "error",
                    "reason": "LLM_MODEL nao definido - informe em Variaveis no dashboard"})
        return
    url = f"{LLM_URL}/api/generate"
    payload = {"model": LLM_MODEL, "prompt": prompt, "stream": True}
    if think is not None:
        payload["think"] = think
    t0 = time.time()
    try:
        with requests.post(url, json=payload, stream=True,
                           timeout=(10, LLM_TIMEOUT), verify=False) as resp:
            if not resp.ok:
                yield _sse({"event": "error", "status": resp.status_code,
                            "error_body": resp.text[:300]})
                return
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                evt = {"event": "token"}
                if obj.get("thinking"):
                    evt["thinking"] = obj["thinking"]
                if obj.get("response"):
                    evt["response"] = obj["response"]
                if obj.get("done"):
                    ns = obj.get("total_duration")
                    evt = {"event": "done",
                           "total_ms": round(ns / 1_000_000) if isinstance(ns, (int, float)) else None,
                           "wall_ms": round((time.time() - t0) * 1000)}
                yield _sse(evt)
    except requests.RequestException as exc:
        yield _sse({"event": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "wall_ms": round((time.time() - t0) * 1000)})


def _sse(data):
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def step_env():
    """Configuracao efetiva da app + presenca das variaveis relevantes.
    Nao expoe valores de variaveis (manual: sem credenciais em logs); mostra
    apenas defaults carregados e quais chaves existem no ambiente."""
    def present(name):
        val = os.environ.get(name)
        if val is None:
            return {"present": False}
        parsed = urlparse(val) if name.endswith("_URL") else None
        info = {"present": True, "length": len(val)}
        if parsed and parsed.scheme:
            # Para URLs revela so scheme/porta, jamais o hostname interno.
            info["scheme"] = parsed.scheme
            info["port"] = parsed.port
        return info

    keys = ["LLM_URL", "LLM_MODEL", "LLM_TIMEOUT", "PROBE_BUDGET",
            "CHAT_BUDGET", "PORT", "PROBE_PROMPT"]
    return {
        "ok": True,
        "app": APP_NAME,
        "llm_url_scheme": _parsed.scheme,
        "llm_url_port": PORT,
        "llm_model_set": bool(LLM_MODEL),
        "llm_timeout_s": LLM_TIMEOUT,
        "probe_budget_s": PROBE_BUDGET,
        "chat_budget_s": CHAT_BUDGET,
        "gunicorn_timeout_s": GUNICORN_TIMEOUT,
        "constraint": "todos os budgets devem ficar ABAIXO do gunicorn --timeout",
        "vars": {k: present(k) for k in keys},
        "count_total_env": len(os.environ),
        "note": "valores ocultos por politica de seguranca (manual: sem "
                "credenciais em logs).",
    }


def step_ready():
    """Health de PRONTIDAO: responde pronto apenas quando o MODELO configurado
    esta de pe. Atende o aviso do manual ('LLM disponivel 1-2 min apos iniciar').
    Faz um GET /api/show barato em LLM_MODEL (sem gerar texto e sem listar os
    outros modelos) e usa codigo HTTP 503 enquanto nao estiver pronto, para
    permitir polling automatizado."""
    model = step_model()
    ready = bool(model.get("ok"))
    body = {
        "ready": ready,
        "llm_reachable": ready,
        "model_checked": LLM_MODEL or None,
        "status": model.get("status"),
        "ms": model.get("ms"),
    }
    if model.get("skipped"):
        body["hint"] = model.get("reason", "LLM_MODEL nao definido")
    elif not ready:
        body["hint"] = ("LLM pode levar 1-2 min apos o inicio do container "
                        "(manual.md). Aguarde e tente de novo.")
    return body, (200 if ready else 503)


@app.get("/")
def home():
    return (
        "probe-llm | rotas de diagnostico:<br>"
        "<b>rede:</b> /health /env /ready /dns /tcp /tls<br>"
        "<b>LLM:</b> /probe /llm/show /llm/generate?prompt=...&amp;think=on|off "
        "/llm/generate-stream?prompt=... (SSE p/ thinking)<br>"
        '<a href="/chat">Ir para a pagina de perguntas (chat)</a>'
    )


# --------------------------------------------------------------------------
# Pagina interativa: voce digita uma pergunta e o modelo responde na tela.
# O HTML vive em templates/chat.html (Jinja); /api/ask faz o POST da pergunta.
# --------------------------------------------------------------------------
@app.get("/chat")
def chat_page():
    model = LLM_MODEL or "(LLM_MODEL nao definido - configure no dashboard)"
    return render_template("chat.html", model=model, url=LLM_URL)


@app.post("/api/ask")
def api_ask():
    """Endpoint JSON usado pela pagina /chat. Recebe {prompt, think}.
    Responde o texto do modelo + thinking quando pedido."""
    data = req.get_json(silent=True) or {}
    prompt = str(data.get("prompt", "")).strip()
    if not prompt:
        return jsonify(ok=False, reason="prompt vazio"), 400
    think = True if data.get("think") else False
    # Orçamento de tempo proprio: limita a chamada ao restante de CHAT_BUDGET,
    # evitando WORKER TIMEOUT quando o modelo esta frio/lento.
    global _PROBE_DEADLINE
    t0 = time.time()
    _PROBE_DEADLINE = [t0 + CHAT_BUDGET]
    try:
        result = step_generate(prompt=prompt, think=think)
    finally:
        _PROBE_DEADLINE = None
    result["_budget"] = {
        "budget_s": CHAT_BUDGET,
        "elapsed_ms": round((time.time() - t0) * 1000),
    }
    return jsonify(result)


@app.get("/health")
def health():
    return jsonify(status="ok", app=APP_NAME,
                   port=int(os.environ.get("PORT", 8080)),
                   llm_url=LLM_URL, llm_model_set=bool(LLM_MODEL))


@app.get("/probe")
def probe_all():
    """Cascata de CONECTIVIDADE com orcamento de tempo (PROBE_BUDGET).

    Roda dns -> tcp -> tls -> model (api/show) em ordem, parando na primeira falha,
    para responder 'a rede ate o LLM esta OK?'. Cada etapa encurta seu timeout ao
    restante do budget via _net_timeout(); quando o budget acaba, as etapas
    restantes saem 'skipped'. NAO executa geracao (modelos thinking podem levar
    minutos): o passo 'generate' sai 'deferred' e a geracao vive em
    /llm/generate-stream."""
    global _PROBE_DEADLINE
    t_start = time.time()
    out = {}
    _PROBE_DEADLINE = [t_start + PROBE_BUDGET]

    def remaining():
        return _PROBE_DEADLINE[0] - time.time()

    def run(name, fn, kwargs=None):
        # Sem budget: registra skipped em vez de executar (evita matar worker).
        if remaining() <= 1:
            out[name] = {"ok": False, "skipped": True,
                         "reason": "orcamento de tempo do /probe esgotado "
                                   "(PROBE_BUDGET); use as rotas individuais"}
            return False
        out[name] = fn(**(kwargs or {}))
        return out[name].get("ok", False)

    try:
        dns_ok = run("dns", step_dns)
        if dns_ok:
            run("tcp", step_tcp)
            run("tls", step_tls)
            run("model", step_model)
            # Geracao fica fora da cascata (ver docstring); so marca deferred.
            out["generate"] = {
                "ok": None,
                "deferred": True,
                "reason": "modelo thinking pode exceder o budget do /probe; "
                          "use GET /llm/generate-stream?prompt=... (SSE) ou "
                          "GET /llm/generate?prompt=... para uma chamada unica",
            }
        out["_budget"] = {
            "budget_s": PROBE_BUDGET,
            "elapsed_ms": round((time.time() - t_start) * 1000),
            "remaining_ms": round(remaining() * 1000),
        }
    finally:
        _PROBE_DEADLINE = None
    return jsonify(out)


@app.get("/dns")
def probe_dns():
    return jsonify(step_dns())


@app.get("/tcp")
def probe_tcp():
    return jsonify(step_tcp())


@app.get("/tls")
def probe_tls():
    return jsonify(step_tls())


@app.get("/llm/show")
def probe_show():
    return jsonify(step_model())


@app.get("/llm/generate")
def probe_generate():
    prompt = os.environ.get("PROBE_PROMPT", "Qual e 2+2? Responda so o numero.")
    prompt = req.args.get("prompt", prompt)
    think = _parse_think(req.args.get("think"))
    return jsonify(step_generate(prompt, think=think))


@app.get("/llm/generate-stream")
def probe_generate_stream():
    """Rota SSE de geracao; rationale do streaming em step_generate_stream.
    Uso: /llm/generate-stream?prompt=...&think=on|off"""
    prompt = os.environ.get("PROBE_PROMPT", "Qual e 2+2? Responda so o numero.")
    prompt = req.args.get("prompt", prompt)
    think = _parse_think(req.args.get("think"))
    return Response(step_generate_stream(prompt, think=think),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.get("/env")
def probe_env():
    return jsonify(step_env())


@app.get("/ready")
def probe_ready():
    body, code = step_ready()
    return jsonify(body), code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
