"""probe-llm - sonda de diagnostico da LLM interna.

Testa, por etapas e SEM derrubar o servidor:
  1. DNS   -> resolve o hostname do LLM_URL
  2. TCP   -> conecta na porta do endpoint
  3. TLS   -> handshake com verify=False (cert autogerado, regra do manual)
  4. MODEL -> POST /api/show {"model": LLM_MODEL} (checa so o modelo configurado)
  5. LLM   -> POST /api/generate com LLM_MODEL
  6. OPENAI-> GET /v1/models + POST /v1/chat/completions (sync e stream SSE),
     para confirmar se a camada OpenAI-compativel do Ollama esta exposta.
  7. VERSION-> GET /api/version (e /api/ps) para identificar a VERSAO exata do
     Ollama que serve a LLM interna.

Cada etapa reporta ok/falha + erro; nenhuma falha impede o app de subir.
Conforme manual.md: escuta em 0.0.0.0, porta via PORT (padrao 8080),
processo em primeiro plano, sem VOLUME.
"""

import json
import math
import os
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlparse

import requests
from flask import (Flask, Response, jsonify, render_template,
                   request as req, send_from_directory)

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

# Timeout de CONECTIVIDADE (TCP/TLS ate abrir a conexao). Um host morto ou porta
# fechada deveria falhar rapido em TODAS as rotas, nao esperar LLM_TIMEOUT inteiro
# por um SYN que nunca vem. Valores de leitura (resposta da LLM) seguem LLM_TIMEOUT.
# Mantido consistente com o streaming, que ja usava 10s de connect.
CONNECT_TIMEOUT = float(os.environ.get("CONNECT_TIMEOUT", "10"))

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

# --- Aquecimento (warmup) da primeira chamada -------------------------------
# O modelo institucional paga um COLD START na PRIMEIRA geracao: medimos ~105s
# para carregar/sair do sleep, contra ~6s (thinking) ou ~0,2s (off) depois de
# quente. O manual tambem avisa que a LLM so fica disponivel 1-2 min apos o
# container subir. Em vez de fazer o USUARIO esperar isso numa requisicao (que
# estouraria o --timeout do gunicorn), disparamos UMA geracao minima em
# BACKGROUND no startup; /ready so vira 200 quando ela realmente responder.
WARMUP_ENABLED = os.environ.get("WARMUP_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off")
# Prompt curto; 'think=false' para esquentar o mais rapido possivel (sem raciocinio).
WARMUP_PROMPT = os.environ.get("WARMUP_PROMPT", "ping")
# Timeout de LEITURA de cada tentativa de aquecimento. Mantido ABAIXO do
# --timeout do gunicorn (regra dos budgets): como roda em thread separada nao mata
# o worker, mas limita quanto tempo uma tentativa pendurada consome.
WARMUP_TIMEOUT = float(os.environ.get("WARMUP_TIMEOUT", "150"))
# Quantas tentativas antes de desistir (cobre a janela de 1-2 min do manual).
WARMUP_ATTEMPTS = int(os.environ.get("WARMUP_ATTEMPTS", "8"))
# Pausa entre tentativas, em segundos.
WARMUP_INTERVAL = float(os.environ.get("WARMUP_INTERVAL", "10"))
# Teto GLOBAL do aquecimento, em segundos. Sem ele, WARMUP_ATTEMPTS x
# (WARMUP_TIMEOUT + WARMUP_INTERVAL) pode passar de 20 minutos quando a LLM esta
# simplesmente MORTA - muito mais que a janela de 1-2 min do manual. Ao exceder,
# a thread para e marca 'failed' com motivo 'budget esgotado'.
WARMUP_BUDGET = float(os.environ.get("WARMUP_BUDGET", "300"))

# --------------------------------------------------------------------------
# Bench / sweep (metricas de performance). Valores padrao conservadores: um
# /bench ou /sweep e uma SEQUENCIA de geracoes reais, entao pode demorar muito.
# BENCH_RUNS  : quantas geracoes por ponto do bench.
# BENCH_PROMPT: pergunta padrao quando a query nao informa prompt.
# SWEEP_MAX_POINTS: teto de combinacoes num_ctx x keep_alive p/ limitar tempo.
# --------------------------------------------------------------------------
BENCH_RUNS = int(os.environ.get("BENCH_RUNS", "3"))
BENCH_PROMPT = os.environ.get(
    "BENCH_PROMPT", "Explique em duas frases o que faz um servidor HTTP.")
SWEEP_MAX_POINTS = int(os.environ.get("SWEEP_MAX_POINTS", "12"))

_parsed = urlparse(LLM_URL)
HOST = _parsed.hostname or ""
# Porta da LLM (vem embutida em LLM_URL). NAO confundir com a variavel de
# ambiente PORT, que e a porta de ESCUTA desta app (8080). Usar um nome proprio
# evita que uma referencia futura a PORT pegue silenciosamente a porta da LLM.
LLM_PORT = _parsed.port or 443

app = Flask(APP_NAME)
# NUNCA verificar certificado: a LLM usa SSL autogerado (manual.md, secao FAQ).
requests.packages.urllib3.disable_warnings()


# Contexto de deadline: None fora de cascata (usa LLM_TIMEOUT cheio); durante uma
# requisicao com budget aponta para o instante-limite DELA, encurtando toda etapa
# de rede ao restante do orcamento. thread-local e NAO global de proposito: com o
# gunicorn servindo varias requests em threads dentro do mesmo worker, um /probe
# em andamento nao pode ser "resetado" pelo finally de um /api/ask concorrente
# (o que liberaria a cascata para pendurar alem do --timeout). Cada request ve
# apenas o proprio deadline.
_DEADLINE_CTX = threading.local()


def _net_timeout(default=None):
    """Devolve a dupla (connect, read) de timeout de rede p/ requests.

    connect fica fixo em CONNECT_TIMEOUT (falha rapido se o host nao responde);
    read segue LLM_TIMEOUT, encurtado ao restante do budget quando a requisicao
    atual esta em cascata. Rotas de socket puro (tcp/tls) usam _read_timeout()."""
    base = default if default is not None else LLM_TIMEOUT
    read = base
    deadline = getattr(_DEADLINE_CTX, "deadline", None)
    if deadline is not None:
        left = deadline[0] - time.time()
        read = max(0.5, min(base, left))
    return (CONNECT_TIMEOUT, read)


def _read_timeout(default=None):
    """Escalar-only: valor unico p/ socket.create_connection (que aceita float)."""
    return _net_timeout(default)[1]


def _endpoint_mask():
    """Endpoint do LLM sem o hostname interno.

    O repo de entrega e PUBLICO (manual/convencao do P0) e as paginas HTML,
    /health e /probe sao servidos a qualquer visitante. Para nao publicar o
    hostname institucional, mostra apenas host mascarado + porta, ex.:
      https://host-interno.exemplo:9001  ->  https://***:9001
    A URL completa continua viva apenas em memoria (LLM_URL), usada para as
    chamadas reais - nunca em resposta HTTP.
    """
    if not LLM_URL:
        return "(LLM_URL nao definido)"
    port = LLM_PORT
    return f"{_parsed.scheme or 'http'}://***:{port}"


def step_dns():
    t0 = time.time()
    try:
        infos = socket.getaddrinfo(HOST, None)
        ips = sorted({i[4][0] for i in infos})
        return {"ok": True, "ips": ips, "ms": round((time.time() - t0) * 1000)}
    except OSError as exc:
        # str(gaierror) traz o hostname pedido ("[Errno -3] Temporary failure
        # in name resolution" e variantes com host). Como /probe e pagina para
        # visitante de repo PUBLICO, so mostra a classe do erro + endpoint
        # mascarado; o hostname fica apenas no log interno do container.
        app.logger.debug("dns fail %s: %s", type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__} em {_endpoint_mask()}"}


def step_tcp():
    t0 = time.time()
    try:
        with socket.create_connection((HOST, LLM_PORT), timeout=_read_timeout()):
            return {"ok": True, "target": f"***:{LLM_PORT}",
                    "ms": round((time.time() - t0) * 1000)}
    except OSError as exc:
        return {"ok": False, "target": f"***:{LLM_PORT}",
                "error": f"{type(exc).__name__}: {exc}"}


def step_tls():
    t0 = time.time()
    try:
        raw = socket.create_connection((HOST, LLM_PORT), timeout=_read_timeout())
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
        out = {"ok": resp.ok, "status": resp.status_code, "url": _endpoint_mask() + "/api/show",
               "method": "POST", "checked_model": LLM_MODEL,
               "ms": round((time.time() - t0) * 1000)}
        if not resp.ok:
            # Guarda o erro bruto p/ diferenciar 'model not found' de indisponibilidade.
            out["error_body"] = resp.text[:300]
        return out
    except requests.RequestException as exc:
        return {"ok": False, "url": _endpoint_mask() + "/api/show", "method": "POST",
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
        # Metricas nativas do Ollama (chunk final): throughput real do servidor,
        # custo de carga do modelo (efeito do keep_alive) e contagem de tokens.
        out["stats"] = _ollama_stats(body)
        if not resp.ok:
            # Alguns modelos recusam 'think' com 400; guardar o erro bruto.
            out["error_body"] = str(body)[:500]
        elif not str(body.get("response") or "").strip():
            # HTTP 200 porem corpo vazio: o modelo respondeu sem conteudo (pode
            # acontecer durante o cold start). Sem isso, o warmup registraria o
            # erro como o literal '{}', que nao diagnostica nada.
            out["reason"] = ("HTTP 200 sem conteudo em 'response' "
                             f"(chaves retornadas: {sorted(body.keys())})")
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
    ttft_ms = None  # tempo ate o PRIMEIRO chunk com conteudo (thinking ou resposta)
    try:
        with requests.post(url, json=payload, stream=True,
                           timeout=_net_timeout(), verify=False) as resp:
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
                if ttft_ms is None and (obj.get("thinking") or obj.get("response")):
                    ttft_ms = round((time.time() - t0) * 1000)
                evt = {"event": "token"}
                if obj.get("thinking"):
                    evt["thinking"] = obj["thinking"]
                if obj.get("response"):
                    evt["response"] = obj["response"]
                if obj.get("done"):
                    evt = {"event": "done",
                           "wall_ms": round((time.time() - t0) * 1000),
                           "ttft_ms": ttft_ms,
                           "stats": _ollama_stats(obj)}
                yield _sse(evt)
    except requests.RequestException as exc:
        yield _sse({"event": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "wall_ms": round((time.time() - t0) * 1000)})


def _bench_generate(prompt, think=None, options=None, keep_alive=None):
    """Uma geracao de BENCH (streaming) que retorna um resumo com metricas.

    Reaproveita o streaming real (mesmo caminho do /chat e do warmup) para medir
    o que o servidor faz de verdade: TTFT no primeiro chunk com conteudo e as
    metricas nativas do Ollama no chunk final. Nao e SSE: acumula em memoria e
    devolve um dict, porque /bench e /sweep precisam do resultado agregado.
    'options' vai cru para /api/generate (ex.: {"num_ctx":4096}); 'keep_alive'
    controla a permanencia do modelo na VRAM ('30s', '-1', 0). Erro de rede vira
    ok=False em vez de excecao, para o bench continuar os proximos runs."""
    if not LLM_MODEL:
        return {"ok": False, "reason": "LLM_MODEL nao definido"}
    url = f"{LLM_URL}/api/generate"
    payload = {"model": LLM_MODEL, "prompt": prompt, "stream": True}
    if think is not None:
        payload["think"] = think
    if options:
        payload["options"] = options
    if keep_alive is not None:
        payload["keep_alive"] = keep_alive
    t0 = time.time()
    ttft_ms = None
    text_parts = []
    thinking_seen = False
    stats = {}
    try:
        with requests.post(url, json=payload, stream=True,
                           timeout=_net_timeout(), verify=False) as resp:
            if not resp.ok:
                return {"ok": False, "status": resp.status_code,
                        "error_body": resp.text[:300],
                        "wall_ms": round((time.time() - t0) * 1000)}
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if ttft_ms is None and (obj.get("thinking") or obj.get("response")):
                    ttft_ms = round((time.time() - t0) * 1000)
                if obj.get("thinking"):
                    thinking_seen = True
                if obj.get("response"):
                    text_parts.append(obj["response"])
                if obj.get("done"):
                    stats = _ollama_stats(obj)
    except requests.RequestException as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "wall_ms": round((time.time() - t0) * 1000)}
    wall_ms = round((time.time() - t0) * 1000)
    text = "".join(text_parts)
    out = {"ok": True, "ttft_ms": ttft_ms, "wall_ms": wall_ms,
           "chars": len(text), "thinking_seen": thinking_seen}
    out.update(stats)
    return out


def _bench_runs(prompt, think=None, runs=1, options=None, keep_alive=None):
    """Executa N geracoes iguais e agrega p50/p95/min/max das metricas-chave.

    Um ponto de bench = varios runs do MESMO setup; a mediana (p50) e o pico
    (p95) resumem a distribuicao sem numpy. Retorna tambem a lista crua de runs
    para inspecao. Cada run ja captura seu proprio erro (ok=False) sem abortar o
    conjunto."""
    resultados = [_bench_generate(prompt, think=think, options=options,
                                  keep_alive=keep_alive)
                  for _ in range(max(1, runs))]
    bons = [r for r in resultados if r.get("ok")]

    def agg(campo):
        vals = [r[campo] for r in bons if isinstance(r.get(campo), (int, float))]
        if not vals:
            return {"p50": None, "p95": None, "min": None, "max": None}
        return {"p50": _percentile(vals, 50), "p95": _percentile(vals, 95),
                "min": round(min(vals), 2), "max": round(max(vals), 2)}

    return {
        "runs": len(resultados),
        "ok_runs": len(bons),
        "failed_runs": len(resultados) - len(bons),
        "ttft_ms": agg("ttft_ms"),
        "total_ms": agg("total_ms"),
        "wall_ms": agg("wall_ms"),
        "eval_tps": agg("eval_tps"),
        "load_ms": agg("load_ms"),
        "last_error": next((r.get("error") or r.get("reason") or
                            r.get("error_body") for r in resultados
                            if not r.get("ok")), None),
        "per_run": resultados,
    }


def _sse(data):
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _ns_to_ms(v):
    """Converte nanosegundos (formato Ollama) em ms inteiro; None se ausente/ruim."""
    return round(v / 1_000_000) if isinstance(v, (int, float)) and v >= 0 else None


def _safe_rate(count, dur_ms):
    """tokens/s a partir de contagem e duracao em ms; None se dados insuficientes."""
    if not isinstance(count, (int, float)) or count <= 0:
        return None
    if not isinstance(dur_ms, (int, float)) or dur_ms <= 0:
        return None
    return round(count / (dur_ms / 1000.0), 2)


def _ollama_stats(body):
    """Extrai as metricas nativas do chunk final do Ollama (campos *_duration em
    NANOSEGUNDOS e *_count em tokens).

    Devolve um dict enxuto p/ consumo por /bench e /sweep: throughput real de
    geracao (eval_count/eval_duration), custo de carga do modelo (load_duration,
    que revela o efeito do keep_alive) e contagens de tokens. Campos ausentes
    viram None em vez de quebrar - nem todo build/version preenche tudo."""
    total_ms = _ns_to_ms(body.get("total_duration"))
    load_ms = _ns_to_ms(body.get("load_duration"))
    eval_ms = _ns_to_ms(body.get("eval_duration"))
    prompt_eval_ms = _ns_to_ms(body.get("prompt_eval_duration"))
    eval_count = body.get("eval_count")
    prompt_eval_count = body.get("prompt_eval_count")
    return {
        "total_ms": total_ms,
        "load_ms": load_ms,
        "eval_ms": eval_ms,
        "prompt_eval_ms": prompt_eval_ms,
        "eval_count": eval_count,
        "prompt_eval_count": prompt_eval_count,
        "eval_tps": _safe_rate(eval_count, eval_ms),
        "prompt_tps": _safe_rate(prompt_eval_count, prompt_eval_ms),
    }


def _percentile(vals, pct):
    """Percentil simples (interp linear) sobre lista ja ordenada de numeros.
    None se vazia. Usado p/ p50/p95 do /bench sem depender de numpy."""
    nums = sorted(v for v in vals if isinstance(v, (int, float)))
    if not nums:
        return None
    if len(nums) == 1:
        return round(float(nums[0]), 2)
    k = (len(nums) - 1) * (pct / 100.0)
    lo = int(math.floor(k))
    hi = min(lo + 1, len(nums) - 1)
    frac = k - lo
    return round(nums[lo] + (nums[hi] - nums[lo]) * frac, 2)


def _normalize_keep_alive(raw):
    """Normaliza 'keep_alive' para o payload do Ollama.

    A API nativa parseia como Go time.ParseDuration, que exige unidade: a string
    '-1' retorna 400 ('missing unit in duration'). Regras:
      - None -> None (nao enviar o campo)
      - inteiro negativo (-1) -> numero -1 (forever; JSON number nao passa por
        ParseDuration)
      - inteiro >= 0 em string ('0', '30') -> '<n>s' (unidade obrigatoria)
      - string com unidade ('30s', '1h', '-1ns') -> mantem cru
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        n = int(s)
    except ValueError:
        return s  # ja tem unidade (ex.: '30s') ou texto livre: passa cru
    return n if n < 0 else f"{n}s"


def _split_csv(raw, default=None):
    """Divide 'a,b,c' em lista de strings limpas; usa default se vazio/None.

    Aceita tambem lista ja decodificada (corpo JSON de POST /sweep), evitando o
    str(list) que viraria um item unico tipo "['-1']"."""
    if isinstance(raw, (list, tuple)):
        parts = [str(p).strip() for p in raw]
        parts = [p for p in parts if p != ""]
        return parts or list(default or [])
    if not raw:
        return list(default or [])
    parts = [p.strip() for p in str(raw).split(",")]
    parts = [p for p in parts if p != ""]
    return parts or list(default or [])


def _parse_int_list(raw, default=None):
    """Como _split_csv mas convertendo cada item em int; descarta nao-numericos.
    Se nada validar, devolve default."""
    out = []
    for p in _split_csv(raw, default=None):
        try:
            out.append(int(p))
        except ValueError:
            continue
    return out or list(default or [])


# --------------------------------------------------------------------------
# Autopsia do PROPRIO container (cgroup + /proc). Le arquivos do filesystem;
# NAO usa socket Docker nem comandos externos, entao funciona dentro do App
# Service. Tudo best-effort: arquivo ausente/ilegivel vira None, nunca excecao.
# Suporta cgroup v2 (arquivos em /sys/fs/cgroup/<nome>) e v1 (subpasta memory/,
# cpu/). Valores brutos sao convertidos para unidades legiveis no final.
# --------------------------------------------------------------------------
_CGROUP_ROOT = "/sys/fs/cgroup"


def _read_first(lines):
    """Primeira linha de um texto ja lido, sem \\n; None se vazio."""
    if not lines:
        return None
    first = lines.split("\n", 1)[0].strip()
    return first or None


def _read_cgroup_file(rel_v2, rel_v1=None):
    """Le um arquivo de cgroup tentando v2 primeiro e depois v1.

    'rel_v2' e o caminho relativo a raiz (ex.: 'memory.current'); 'rel_v1' o
    equivalente v1 (ex.: 'memory/memory.usage_in_bytes'). Retorna o conteudo
    como string ou None se nenhum existir/for legivel."""
    candidates = [os.path.join(_CGROUP_ROOT, rel_v2)]
    if rel_v1:
        candidates.append(os.path.join(_CGROUP_ROOT, rel_v1))
    for path in candidates:
        try:
            with open(path, "r") as fh:
                return fh.read()
        except OSError:
            continue
    return None


def _cgroup_is_v2():
    """Detecta unified hierarchy: em v2 /proc/self/cgroup comeca com '0::/' e
    existe memory.current na raiz. Em v1 ha subpastas por controlador."""
    try:
        with open("/proc/self/cgroup", "r") as fh:
            txt = fh.read()
    except OSError:
        txt = ""
    if "0::/" in txt and os.path.exists(os.path.join(_CGROUP_ROOT, "memory.current")):
        return True
    return False


def _parse_mem_bytes(raw):
    """Converte conteudo de memory.current/limit em bytes int. 'max'/vazio -> None."""
    line = _read_first(raw)
    if line is None or line == "max":
        return None
    try:
        val = int(line)
    except ValueError:
        return None
    # Limite 'sem limite' costuma vir como numero gigante (~2^63); tratar como None.
    if val >= (1 << 62):
        return None
    return val


def _container_memory():
    """Uso e limite de RAM do CONTAINER via cgroup (nao do host).

    Retorna dict com used_bytes, limit_bytes, pct e human. Em cgroup v2 le
    memory.current/memory.max (+ stat p/ detalhes); em v1, usage/limit_in_bytes.
    Sem limite definido, limit fica None e pct tambem."""
    if _cgroup_is_v2():
        cur = _parse_mem_bytes(_read_cgroup_file("memory.current"))
        lim = _parse_mem_bytes(_read_cgroup_file("memory.max"))
        stat_raw = _read_cgroup_file("memory.stat") or ""
    else:
        cur = _parse_mem_bytes(
            _read_cgroup_file("memory.current", "memory/memory.usage_in_bytes"))
        lim = _parse_mem_bytes(
            _read_cgroup_file("memory.max", "memory/memory.limit_in_bytes"))
        stat_raw = _read_cgroup_file("memory.stat", "memory/memory.stat") or ""
    cache = None
    for token in ("file ", "cache "):  # v2 usa 'file'; v1 usa 'cache'
        for ln in stat_raw.splitlines():
            if ln.startswith(token):
                try:
                    cache = int(ln.split()[1])
                except (ValueError, IndexError):
                    pass
                break
        if cache is not None:
            break
    pct = round(cur / lim * 100, 1) if cur is not None and lim else None
    return {"used_bytes": cur, "limit_bytes": lim, "cache_bytes": cache,
            "pct_of_limit": pct, "used_human": _human_bytes(cur),
            "limit_human": _human_bytes(lim)}


def _container_cpu():
    """Tempo de CPU consumido pelo container (cgroup) e nucleos disponiveis.

    cgroup v2: cpu.stat traz usage_usec/total; cpu.max traz 'quota period'.
    v1: cpuacct.usage (ns) e cfs_quota_us/cfs_period_us. Retorna uso acumulado
    em segundos, nucleos efetivos (quota/period) e nucleos visiveis no host."""
    usage_sec = None
    cores_quota = None
    if _cgroup_is_v2():
        stat_raw = _read_cgroup_file("cpu.stat") or ""
        for ln in stat_raw.splitlines():
            if ln.startswith("usage_usec"):
                try:
                    usage_sec = int(ln.split()[1]) / 1_000_000.0
                except (ValueError, IndexError):
                    pass
        max_raw = _read_cgroup_file("cpu.max")
        line = _read_first(max_raw) if max_raw else None
        if line:
            parts = line.split()
            if len(parts) == 2 and parts[0] != "max":
                try:
                    cores_quota = round(int(parts[0]) / int(parts[1]), 2)
                except (ValueError, ZeroDivisionError):
                    pass
    else:
        raw = _read_cgroup_file("cpu.max", "cpuacct/cpuacct.usage")
        line = _read_first(raw) if raw else None
        if line:
            try:
                usage_sec = int(line) / 1_000_000_000.0
            except ValueError:
                pass
        quota = _read_first(_read_cgroup_file("cpu.max", "cpu/cpu.cfs_quota_us"))
        period = _read_first(_read_cgroup_file("cpu.max", "cpu/cpu.cfs_period_us"))
        try:
            if quota and period and int(quota) > 0:
                cores_quota = round(int(quota) / int(period), 2)
        except ValueError:
            pass
    return {"cpu_seconds_total": round(usage_sec, 2) if usage_sec is not None else None,
            "cores_quota": cores_quota, "cores_visible": os.cpu_count()}


def _host_memory():
    """RAM total/livre do HOST vista de /proc/meminfo (referencia; em containers
    compartilhada). None se nao conseguir ler."""
    total_kb = avail_kb = None
    try:
        with open("/proc/meminfo") as fh:
            for ln in fh:
                if ln.startswith("MemTotal:"):
                    total_kb = int(ln.split()[1])
                elif ln.startswith("MemAvailable:"):
                    avail_kb = int(ln.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    to_b = lambda kb: kb * 1024 if kb is not None else None
    return {"total_bytes": to_b(total_kb), "available_bytes": to_b(avail_kb),
            "total_human": _human_bytes(to_b(total_kb)),
            "available_human": _human_bytes(to_b(avail_kb))}


def _loadavg():
    """1/5/15 min via os.getloadavg(); None se indisponivel."""
    try:
        la = os.getloadavg()
        return {"load_1": round(la[0], 2), "load_5": round(la[1], 2),
                "load_15": round(la[2], 2)}
    except OSError:
        return {"load_1": None, "load_5": None, "load_15": None}


def _net_dev_bytes():
    """Soma recebidos/enviados de todas as interfaces exceto loopback, lendo
    /proc/net/dev. Retorna dict rx_bytes/tx_bytes + human. Best-effort."""
    rx = tx = 0
    got = False
    try:
        with open("/proc/net/dev") as fh:
            for ln in fh:
                if ":" not in ln:
                    continue
                iface, rest = ln.split(":", 1)
                if iface.strip() == "lo":
                    continue
                cols = rest.split()
                if len(cols) >= 9:
                    rx += int(cols[0])
                    tx += int(cols[8])
                    got = True
    except (OSError, ValueError, IndexError):
        pass
    if not got:
        return {"rx_bytes": None, "tx_bytes": None,
                "rx_human": None, "tx_human": None}
    return {"rx_bytes": rx, "tx_bytes": tx,
            "rx_human": _human_bytes(rx), "tx_human": _human_bytes(tx)}


def _uptime_s():
    """Segundos desde o boot do host (/proc/uptime)."""
    try:
        with open("/proc/uptime") as fh:
            return round(float(fh.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def _process_info():
    """Dados do processo da app: PID, RSS (residente) lendo /proc/self/status, e
    tempo de CPU do processo via /proc/self/stat (utime+stime em jiffies->s)."""
    rss_kb = None
    try:
        with open("/proc/self/status") as fh:
            for ln in fh:
                if ln.startswith("VmRSS:"):
                    rss_kb = int(ln.split()[1])
                    break
    except (OSError, ValueError, IndexError):
        pass
    proc_cpu_s = None
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    try:
        with open("/proc/self/stat") as fh:
            fields = fh.read().rsplit(")", 1)[-1].split()
            utime = int(fields[11]); stime = int(fields[12])
            proc_cpu_s = round((utime + stime) / hz, 2)
    except (OSError, ValueError, IndexError):
        pass
    return {"pid": os.getpid(), "rss_bytes": rss_kb * 1024 if rss_kb else None,
            "rss_human": _human_bytes(rss_kb * 1024 if rss_kb else None),
            "cpu_seconds": proc_cpu_s}


def system_stats():
    """Monta o retrato de recursos do container: memoria/CPU proprios (cgroup),
    referencia do host, rede, uptime e processo. So leitura de arquivos - nada de
    socket Docker, nada de comando externo, nada sensivel exposto."""
    return {
        "ok": True,
        "app": APP_NAME,
        "cgroup_version": 2 if _cgroup_is_v2() else 1,
        "container": {"memory": _container_memory(), "cpu": _container_cpu()},
        "process": _process_info(),
        "host": {"memory": _host_memory(), "loadavg": _loadavg(),
                 "uptime_s": _uptime_s(), "cpus": os.cpu_count()},
        "network": _net_dev_bytes(),
        "note": ("memoria/cpu do container vem de cgroup; host eh apenas "
                 "referencia compartilhada. Sem GPU/VRAM: a API do Ollama nao "
                 "expoe isso e /metrics (Prometheus) retorna 404."),
    }


def _openai_headers():
    """Headers p/ os endpoints OpenAI-compat do Ollama.

    A API /v1 do Ollama espera um Authorization Bearer; localmente ela aceita
    qualquer placeholder (nao valida). NAO ha segredo real aqui - e so o formato
    que a biblioteca OpenAI manda. LLM_API_KEY (se definida no ambiente) e usada
    como valor; senao, 'probe'. O valor nunca e impresso nas respostas."""
    chave = os.environ.get("LLM_API_KEY", "").strip() or "probe"
    return {"Authorization": f"Bearer {chave}",
            "Content-Type": "application/json"}


def step_openai_models():
    """GET {LLM_URL}/v1/models - confirma se a camada OpenAI-compat existe.

    Resposta esperada (formato OpenAI): {"object":"list","data":[{"id":...}...]}.
    Um 404/405 indica que o endpoint /v1 NAO esta exposto nesse host/porta."""
    url = f"{LLM_URL}/v1/models"
    t0 = time.time()
    try:
        resp = requests.get(url, headers=_openai_headers(),
                            timeout=_net_timeout(), verify=False)
        out = {"ok": resp.ok, "status": resp.status_code, "url_path": "/v1/models",
               "method": "GET", "ms": round((time.time() - t0) * 1000)}
        if resp.ok:
            try:
                body = resp.json()
                ids = [m.get("id") for m in body.get("data", []) if isinstance(m, dict)]
                # Formato OpenAI documentado: {"object":"list","data":[...]}. O
                # probe so marca compat se a resposta bater esse contrato.
                out["object"] = body.get("object")
                out["is_list_format"] = body.get("object") == "list"
                out["model_count"] = len(ids)
                # So revela se o MODELO configurado aparece; nao lista nomes crus.
                out["configured_model_present"] = (LLM_MODEL in ids) if LLM_MODEL else None
            except ValueError:
                out["error_body"] = resp.text[:300]
        else:
            out["error_body"] = resp.text[:300]
        return out
    except requests.RequestException as exc:
        return {"ok": False, "url_path": "/v1/models", "method": "GET",
                "error": f"{type(exc).__name__}: {exc}"}


def step_openai_chat(stream=False):
    """POST {LLM_URL}/v1/chat/completions - geracao real via API OpenAI.

    Manda uma pergunta trivial no formato da biblioteca OpenAI (messages) e
    confere se o Ollama responde em chat.completion (sync) ou em chunks SSE
    (stream). E o teste definitivo de que o P0 consegue falar direto com ele."""
    url = f"{LLM_URL}/v1/chat/completions"
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user",
                      "content": "Qual e 2+2? Responda so o numero."}],
        "stream": bool(stream),
    }
    t0 = time.time()
    try:
        with requests.post(url, json=payload, headers=_openai_headers(),
                           stream=bool(stream), timeout=_net_timeout(),
                           verify=False) as resp:
            out = {"ok": resp.ok, "status": resp.status_code,
                   "url_path": "/v1/chat/completions", "method": "POST",
                   "stream": bool(stream),
                   "ms": round((time.time() - t0) * 1000)}
            if not resp.ok:
                out["error_body"] = resp.text[:300]
                return out
            if not stream:
                try:
                    body = resp.json()
                    choice = (body.get("choices") or [{}])[0]
                    msg = choice.get("message") or {}
                    out["object"] = body.get("object")
                    # Sync deve vir como "chat.completion" (docs OpenAI/Ollama).
                    out["is_completion_object"] = body.get("object") == "chat.completion"
                    out["response_preview"] = str(msg.get("content", ""))[:200]
                    out["finish_reason"] = choice.get("finish_reason")
                    out["has_usage"] = isinstance(body.get("usage"), dict)
                    # reasoning_content = thinking exposto pela camada OpenAI.
                    out["has_reasoning_field"] = "reasoning_content" in msg
                except ValueError:
                    out["error_body"] = resp.text[:300]
                return out
            # streaming: conta chunks ate o [DONE], guarda amostra do primeiro.
            eventos = 0
            texto = ""
            done = False
            chunk_object = None
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                if not raw.startswith("data:"):
                    continue
                dado = raw[len("data:"):].strip()
                if dado == "[DONE]":
                    done = True
                    break
                try:
                    obj = json.loads(dado)
                except ValueError:
                    continue
                if chunk_object is None:
                    chunk_object = obj.get("object")
                delta = ((obj.get("choices") or [{}])[0]).get("delta") or {}
                conteudo = delta.get("content")
                if conteudo:
                    eventos += 1
                    if len(texto) < 200:
                        texto += conteudo
            out.update({"chunk_events": eventos,
                        "chunk_object": chunk_object,
                        "got_done": done,
                        "response_preview": texto[:200],
                        # Streaming correto emite chunks "chat.completion.chunk".
                        "streaming_ok": (eventos > 0 and done
                                         and chunk_object == "chat.completion.chunk")})
            return out
    except requests.RequestException as exc:
        return {"ok": False, "url_path": "/v1/chat/completions", "method": "POST",
                "stream": bool(stream), "error": f"{type(exc).__name__}: {exc}"}


def step_version():
    """Descobre a VERSAO do Ollama via GET {LLM_URL}/api/version.

    Endpoint nativo e publico do Ollama (documentado): responde
    {"version":"0.x.y"} sem autenticacao nem modelo. E a forma definitiva de
    saber qual build do servidor esta atras do hostname interno. Como fallback
    complementar, chama GET /api/ps (modelos carregados na memoria); um erro de
    conexao ai confirma que a porta nao responde, mas NUNCA derruba o app.
    Nao executa nada no alvo: so leitura HTTP."""
    url = f"{LLM_URL}/api/version"
    t0 = time.time()
    out = {"ok": False, "url_path": "/api/version", "method": "GET",
           "ms": None}
    try:
        resp = requests.get(url, timeout=_net_timeout(20), verify=False)
        out["status"] = resp.status_code
        out["ms"] = round((time.time() - t0) * 1000)
        if resp.ok:
            try:
                body = resp.json()
            except ValueError:
                body = {}
                out["error_body"] = resp.text[:300]
            # Formato documentado do Ollama: {"version": "0.x.y"}.
            versao = body.get("version")
            out["version"] = versao
            out["ok"] = bool(versao)
            if not versao and not out.get("error_body"):
                out["error_body"] = str(body)[:300]
        else:
            out["error_body"] = resp.text[:300]
    except requests.RequestException as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["ms"] = round((time.time() - t0) * 1000)
    return out


def step_ps():
    """Complemento leve: GET {LLM_URL}/api/ps lista modelos QUENTE na memoria.

    Nao revela a versao por si, mas corrobora o /api/version e mostra o estado
    de carga do modelo (nome, tamanho, expira). Best-effort: falha aqui nao
    invalida a descoberta de versao."""
    url = f"{LLM_URL}/api/ps"
    t0 = time.time()
    out = {"ok": False, "url_path": "/api/ps", "method": "GET"}
    try:
        resp = requests.get(url, timeout=_net_timeout(20), verify=False)
        out["status"] = resp.status_code
        out["ms"] = round((time.time() - t0) * 1000)
        if resp.ok:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            models = body.get("models") or []
            out["loaded_count"] = len(models)
            # So nomes/tamanhos; sem campos sensiveis.
            out["loaded"] = [{"name": m.get("name"),
                              "size_vram": m.get("size")} for m in models[:10]]
            out["ok"] = True
        else:
            out["error_body"] = resp.text[:200]
    except requests.RequestException as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _human_bytes(n):
    """Formata bytes em sufixo binario legivel (B/KiB/MiB/GiB/TiB)."""
    if not isinstance(n, (int, float)) or n < 0:
        return None
    val = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if val < 1024 or unit == "TiB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024
    return None


def _human_params(n):
    """Formata contagem de parametros em milhoes/bilhoes (ex.: '9.0B')."""
    if not isinstance(n, (int, float)) or n <= 0:
        return None
    if n >= 1e9:
        return f"{n / 1e9:.1f}B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    return str(int(n))


def step_show():
    """Metadados do MODELO configurado via POST {LLM_URL}/api/show.

    Enriquece a descoberta de versao: alem do build do servidor (/api/version),
    revela o que o modelo realmente e - arquitetura, numero de parametros,
    quantizacao, janela de contexto e tamanho em disco. O payload do Ollama traz:
      model_info {family, families, general.architecture, general.parameter_count,
                  general.quantization_version, ...}
      parameters (string GGUF 'key = value', ex.: context_length 4096)
      template   (chat template) -> so reportamos o comprimento, nunca o corpo
      size       (bytes do blob)
    Best-effort: sem LLM_MODEL faz skipped; falha nao derruba o app nem a rota."""
    if not LLM_MODEL:
        return {"ok": False, "skipped": True,
                "reason": "LLM_MODEL nao definido - nada para descrever"}
    url = f"{LLM_URL}/api/show"
    t0 = time.time()
    out = {"ok": False, "url_path": "/api/show", "method": "POST",
           "model": LLM_MODEL}
    try:
        resp = requests.post(url, json={"model": LLM_MODEL},
                             timeout=_net_timeout(20), verify=False)
        out["status"] = resp.status_code
        out["ms"] = round((time.time() - t0) * 1000)
        if not resp.ok:
            out["error_body"] = resp.text[:300]
            return out
        try:
            body = resp.json()
        except ValueError:
            out["error_body"] = resp.text[:300]
            return out
        info = body.get("model_info") or {}
        # O dicionario do Ollama usa chaves pontuadas literais: a arquitectura vem
        # em "general.architecture" (nao em "architecture"). As metricas especificas
        # ficam prefixadas pelo nome da arquitectura (ex.: "qwen2.context_length").
        architecture = info.get("general.architecture")
        # Contagem de parametros: o Ollama pode expor como numero bruto em
        # "<arch>.parameter_count" OU "general.parameter_count", e quase sempre
        # tambem traz um rotulo legivel em details.parameter_size ("9B").
        param_count = None
        if architecture:
            param_count = info.get(f"{architecture}.parameter_count")
        if param_count is None:
            param_count = info.get("general.parameter_count")
        # Quantizacao: preferir SEMPRE um rotulo legivel ("Q4_K_M"): primeiro o
        # especifico da arquitectura (<arch>.quantization_level), depois o campo
        # amigavel de details.quantization_level. So recorrer ao numero interno
        # general.quantization_version se nenhum rotulo existir.
        details = body.get("details") or {}
        quant = None
        if architecture:
            quant = info.get(f"{architecture}.quantization_level")
        if quant is None:
            quant = details.get("quantization_level")
        if quant is None:
            quant = info.get("general.quantization_version")
        families = info.get("general.families") or details.get("families")
        # A janela de contexto pode estar no dicionario de arquitectura OU na
        # string 'parameters' (GGUF). Extrai context_length de ambos.
        ctx = info.get(f"{architecture}.context_length") if architecture else None
        params_str = str(body.get("parameters") or "")
        if ctx is None:
            m = re.search(r"context_length\s*=\s*(\d+)", params_str)
            if m:
                ctx = int(m.group(1))
        out.update({
            "ok": True,
            "architecture": architecture,
            "families": families,
            "parameter_count": param_count,
            "parameter_count_human": _human_params(param_count)
                or details.get("parameter_size"),
            "quantization": quant,
            "context_length": ctx,
            "size_bytes": body.get("size"),
            "size_human": _human_bytes(body.get("size")),
            "details": details,
            # NAO devolve o template inteiro (pode ser grande); so o comprimento.
            "template_chars": len(str(body.get("template") or "")),
        })
        return out
    except requests.RequestException as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["ms"] = round((time.time() - t0) * 1000)
        return out


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

    keys = ["LLM_URL", "LLM_MODEL", "LLM_TIMEOUT", "CONNECT_TIMEOUT",
            "PROBE_BUDGET", "CHAT_BUDGET", "PORT", "PROBE_PROMPT",
            "LLM_API_KEY",
            "WARMUP_ENABLED", "WARMUP_PROMPT", "WARMUP_TIMEOUT",
            "WARMUP_ATTEMPTS", "WARMUP_INTERVAL", "WARMUP_BUDGET",
            "BENCH_RUNS", "BENCH_PROMPT", "SWEEP_MAX_POINTS"]
    return {
        "ok": True,
        "app": APP_NAME,
        "llm_url_scheme": _parsed.scheme,
        "llm_url_port": LLM_PORT,
        "llm_model_set": bool(LLM_MODEL),
        "llm_timeout_s": LLM_TIMEOUT,
        "connect_timeout_s": CONNECT_TIMEOUT,
        "probe_budget_s": PROBE_BUDGET,
        "chat_budget_s": CHAT_BUDGET,
        "gunicorn_timeout_s": GUNICORN_TIMEOUT,
        "warmup": {
            "enabled": WARMUP_ENABLED,
            "status": _estado_warmup().get("status"),
            "attempts": _estado_warmup().get("attempts"),
        },
        "bench": {"runs": BENCH_RUNS, "max_points": SWEEP_MAX_POINTS},
        "constraint": "todos os budgets devem ficar ABAIXO do gunicorn --timeout",
        "vars": {k: present(k) for k in keys},
        "count_total_env": len(os.environ),
        "note": "valores ocultos por politica de seguranca (manual: sem "
                "credenciais em logs).",
    }


# --------------------------------------------------------------------------
# Aquecimento (warmup): paga o cold start da PRIMEIRA geracao em background.
# Estado compartilhado entre a thread de aquecimento e a rota /ready.
# --------------------------------------------------------------------------
_WARMUP_LOCK = threading.Lock()
_WARMUP_STATE = {
    # status: 'pending' | 'warming' | 'warm' | 'failed' | 'disabled' | 'skipped'
    "status": "pending",
    "attempts": 0,
    "started_at": None,
    "finished_at": None,
    "duration_ms": None,
    "error": None,
}


def _marcar_warmup(**campos):
    """Atualiza o estado de aquecimento sob lock (unico ponto de escrita)."""
    with _WARMUP_LOCK:
        _WARMUP_STATE.update(campos)


def _rodar_warmup():
    """Corpo da thread: tenta uma geracao minima ate o modelo responder.

    Roda fora de qualquer requisicao HTTP, entao o cold start (~105s) e a janela
    de 1-2 min do manual NUNCA estouram o --timeout do gunicorn nem fazem o
    usuario esperar. Usa think=false para esquentar no menor tempo possivel.
    Encadeia tentativas com pausa curta; sucesso marca 'warm', exauridas marca
    'failed'. Um teto global (WARMUP_BUDGET) limita o esforco total: se a LLM
    estiver morta, desiste em minutos em vez de ~20. Sucesso real exige conteudo
    em 'response' - um HTTP 200 vazio de cold start nao conta como aquecido."""
    _marcar_warmup(status="warming", started_at=time.time(), attempts=0,
                   error=None, duration_ms=None, finished_at=None)
    t_inicio = time.time()
    ultimo_erro = None
    for tentativa in range(1, max(1, WARMUP_ATTEMPTS) + 1):
        # Teto global: nao adianta insistir por 20 minutos numa LLM indisponivel.
        if time.time() - t_inicio >= WARMUP_BUDGET:
            _marcar_warmup(
                status="failed", finished_at=time.time(),
                error=(f"orcamento de aquecimento esgotado "
                       f"({WARMUP_BUDGET:.0f}s, {tentativa - 1} tentativa(s)); "
                       f"ultima falha: {str(ultimo_erro)[:160]}"))
            return
        _marcar_warmup(attempts=tentativa)
        res = step_generate(prompt=WARMUP_PROMPT, think=False,
                            timeout=(CONNECT_TIMEOUT, WARMUP_TIMEOUT))
        if res.get("ok") and str(res.get("response") or "").strip():
            _marcar_warmup(
                status="warm",
                finished_at=time.time(),
                duration_ms=round((time.time() - _WARMUP_STATE["started_at"]) * 1000),
                error=None,
            )
            return
        ultimo_erro = (res.get("error") or res.get("reason")
                       or res.get("error_body") or f"HTTP {res.get('status')}")
        _marcar_warmup(error=str(ultimo_erro)[:300])
        if tentativa < WARMUP_ATTEMPTS:
            # Pausa nunca ultrapassa o orcamento restante: se so faltam 3s,
            # dorme 3s e a proxima iteracao encerra o loop por budget.
            restante = WARMUP_BUDGET - (time.time() - t_inicio)
            if restante <= 0:
                continue
            time.sleep(min(WARMUP_INTERVAL, restante))
    _marcar_warmup(status="failed", finished_at=time.time())


def step_warmup():
    """Dispara o aquecimento em background UMA vez (idempotente).

    Chamado no startup do worker. Sem LLM_MODEL nao ha o que esquentar ('skipped');
    desabilitado por env, 'disabled'. A thread e daemon: morre junto com o processo
    e nunca bloqueia o shutdown."""
    if not WARMUP_ENABLED:
        _marcar_warmup(status="disabled")
        return {"started": False, "status": "disabled"}
    if not LLM_MODEL:
        _marcar_warmup(status="skipped", error="LLM_MODEL nao definido")
        return {"started": False, "status": "skipped"}
    with _WARMUP_LOCK:
        ja_em_andamento = _WARMUP_STATE["status"] in ("warming", "warm")
    if ja_em_andamento:
        return {"started": False, "status": _WARMUP_STATE["status"]}
    threading.Thread(target=_rodar_warmup, name="warmup", daemon=True).start()
    return {"started": True, "status": "warming"}


def _estado_warmup():
    with _WARMUP_LOCK:
        return dict(_WARMUP_STATE)


def step_ready():
    """Health de PRONTIDAO REAL: so 200 quando o modelo respondeu de fato.

    Diferenca-chave vs. um mero /api/show: 'show' confirma que o MODELO existe, mas
    quem paga o cold start (~105s) e a primeira GERACAO. Entao /ready consulta o
    estado do aquecimento em background: 'warm' -> 200; 'warming'/'pending' -> 503
    com retry_after (polling); 'failed'/'skipped'/'disabled' -> cai no teste leve de
    /api/show para dar um veredito honesto. Nunca bloqueia a requisicao esperando o
    generate: a espera acontece na thread, nao aqui."""
    estado = _estado_warmup()
    status = estado.get("status")
    base = {
        "model_checked": LLM_MODEL or None,
        "warmup_status": status,
        "warmup_attempts": estado.get("attempts"),
    }

    if status == "warm":
        base.update({
            "ready": True,
            "llm_reachable": True,
            "warmup_duration_ms": estado.get("duration_ms"),
        })
        return base, 200

    if status in ("warming", "pending"):
        base.update({
            "ready": False,
            "llm_reachable": False,
            "retry_after_s": int(max(2, WARMUP_INTERVAL)),
            "hint": ("Aquecendo o modelo (cold start ~105s; LLM pode levar 1-2 min "
                     "apos o container subir - manual.md). Polling ate virar 200."),
        })
        if status == "warming":
            base["elapsed_ms"] = round(
                (time.time() - (estado.get("started_at") or time.time())) * 1000)
        return base, 503

    # 'failed' | 'skipped' | 'disabled': sem aquecimento ativo, faz o teste leve.
    model = step_model()
    ready = bool(model.get("ok"))
    base.update({
        "ready": ready,
        "llm_reachable": ready,
        "status": model.get("status"),
        "ms": model.get("ms"),
    })
    if status == "failed":
        base["hint"] = (f"Aquecimento falhou apos {base.get('warmup_attempts')} "
                        f"tentativa(s): {estado.get('error')}. O teste /api/show "
                        "acima ainda indica se o modelo existe.")
    elif model.get("skipped"):
        base["hint"] = model.get("reason", "LLM_MODEL nao definido")
    elif not ready:
        base["hint"] = ("LLM pode levar 1-2 min apos o inicio do container "
                        "(manual.md). Aguarde e tente de novo.")
    return base, (200 if ready else 503)


@app.get("/")
def home():
    return (
        "<!doctype html><html lang='pt-br'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>probe-llm</title>"
        "<link rel='stylesheet' href='/static/perf.css'></head><body>"
        "<h1>&#9635; probe-llm</h1>"
        "<div class='sub'>diagnóstico do LLM institucional &middot; "
        f"LLM_URL <code>{_endpoint_mask()}</code> &middot; modelo "
        f"{LLM_MODEL or '(nao definido)'} "
        "&middot; <a href='/perf'>monitor</a> &middot; <a href='/chat'>chat</a></div>"
        "<dl class='kv'>"
        "<dt>estado</dt><dd>/health &middot; /ready &middot; /env</dd>"
        "<dt>LLM</dt><dd>/probe (cascata dns-tcp-tls-model) &middot; "
        "/llm/generate-stream (SSE p/ thinking)</dd>"
        "<dt>OpenAI-compat</dt><dd>/llm/openai-compat (/v1/models, /v1/chat/completions)</dd>"
        "<dt>Versão</dt><dd>/llm/version (identifica o Ollama)</dd>"
        "<dt>Performance</dt><dd>/bench &middot; /sweep &middot; <a href='/perf'>/perf</a></dd>"
        "<dt>Recursos</dt><dd>/system/stats &middot; <a href='/perf'>painel</a></dd>"
        "</dl>"
        "<p class='muted'>Credenciais nunca aparecem em nenhuma rota.</p>"
        "</body></html>"
    )


# --------------------------------------------------------------------------
# Pagina interativa: voce digita uma pergunta e o modelo responde na tela.
# O HTML vive em templates/chat.html (Jinja); /api/ask faz o POST da pergunta.
# --------------------------------------------------------------------------
@app.get("/chat")
def chat_page():
    model = LLM_MODEL or "(LLM_MODEL nao definido - configure no dashboard)"
    return render_template("chat.html", model=model, url=_endpoint_mask())


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
    t0 = time.time()
    _DEADLINE_CTX.deadline = [t0 + CHAT_BUDGET]
    try:
        result = step_generate(prompt=prompt, think=think)
    finally:
        _DEADLINE_CTX.deadline = None
    result["_budget"] = {
        "budget_s": CHAT_BUDGET,
        "elapsed_ms": round((time.time() - t0) * 1000),
    }
    return jsonify(result)


@app.get("/health")
def health():
    return jsonify(status="ok", app=APP_NAME,
                   port=int(os.environ.get("PORT", 8080)),
                   llm_url=_endpoint_mask(), llm_model_set=bool(LLM_MODEL))


@app.get("/probe")
def probe_all():
    """Cascata de CONECTIVIDADE com orcamento de tempo (PROBE_BUDGET).

    Roda dns -> tcp -> tls -> model (api/show) em ordem, parando na primeira falha,
    para responder 'a rede ate o LLM esta OK?'. Cada etapa encurta seu timeout ao
    restante do budget via _net_timeout(); quando o budget acaba, as etapas
    restantes saem 'skipped'. NAO executa geracao (modelos thinking podem levar
    minutos): o passo 'generate' sai 'deferred' e a geracao vive em
    /llm/generate-stream."""
    t_start = time.time()
    out = {}
    _DEADLINE_CTX.deadline = [t_start + PROBE_BUDGET]

    def remaining():
        return _DEADLINE_CTX.deadline[0] - time.time()

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
            # Versao do servidor: leitura barata, entra na cascata apos o modelo.
            run("version", step_version)
            # Geracao fica fora da cascata (ver docstring); so marca deferred.
            out["generate"] = {
                "ok": None,
                "deferred": True,
                "reason": "modelo thinking pode exceder o budget do /probe; "
                          "para gerar, use GET /llm/generate-stream?prompt=... (SSE)",
            }
        out["_budget"] = {
            "budget_s": PROBE_BUDGET,
            "elapsed_ms": round((time.time() - t_start) * 1000),
            "remaining_ms": round(remaining() * 1000),
        }
    finally:
        _DEADLINE_CTX.deadline = None
    return jsonify(out)


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


@app.route("/bench", methods=["GET", "POST"])
def bench():
    """Benchmark sequencial: N geracoes iguais -> TTFT, tokens/s, p50/p95.

    Mede o throughput REAL do servidor (do chunk final do Ollama) e o tempo ate
    o primeiro token, repetindo o mesmo setup 'runs' vezes e agregando mediana e
    pico. ATENCAO: roda geracoes reais em SEQUENCIA, entao pode levar minutos -
    mantem-se abaixo do --timeout do gunicorn porque cada run e streaming (a
    conexao fica viva enquanto chegam bytes). Parametros de query:
      prompt   : texto a enviar (default BENCH_PROMPT)
      runs     : quantas geracoes (default BENCH_RUNS)
      think    : on|off (omitido = nao envia o campo)
      num_ctx  : janela de contexto (vai em options.num_ctx)
      keep_alive: permanencia na VRAM ('30s', '-1', '0')
    Uso: /bench?runs=3&num_ctx=4096&keep_alive=-1&think=off

    POST com JSON (mesmos campos no corpo): use quando o prompt e longo demais
    para URL -- gateways como o App Service limitam a request line a ~4 KB e
    respondem 400 'Request Line is too large' antes de chegar ao Flask.
    Uso: curl -X POST /bench -H 'Content-Type: application/json' \
         -d '{"prompt":"...","runs":1,"num_ctx":2048,"think":"off"}'
    Query string ainda tem prioridade quando presente."""
    if not LLM_MODEL:
        return jsonify(ok=False,
                       reason="LLM_MODEL nao definido - nada para medir"), 503
    body = req.get_json(silent=True) or {}

    def arg(name, default=None):
        # query vence; ausente em ambos -> default
        if name in req.args:
            return req.args.get(name)
        return body.get(name, default)

    prompt = str(arg("prompt", BENCH_PROMPT))
    try:
        runs = int(arg("runs", BENCH_RUNS))
    except (TypeError, ValueError):
        runs = BENCH_RUNS
    runs = max(1, min(runs, 20))  # teto duro p/ nao estourar o worker
    think = _parse_think(arg("think"))
    options = {}
    num_ctx = arg("num_ctx")
    if num_ctx:
        try:
            options["num_ctx"] = int(num_ctx)
        except (TypeError, ValueError):
            pass
    keep_alive_raw = arg("keep_alive")
    keep_alive = _normalize_keep_alive(keep_alive_raw)
    t_start = time.time()
    resumo = _bench_runs(prompt, think=think, runs=runs,
                         options=options or None, keep_alive=keep_alive)
    resumo["_meta"] = {
        "model": LLM_MODEL, "prompt_chars": len(prompt),
        "think_sent": think, "options": options or None,
        "keep_alive": keep_alive_raw,
        "keep_alive_sent": keep_alive,
        "elapsed_ms": round((time.time() - t_start) * 1000),
        "note": ("metricas vem do chunk final do Ollama (servidor); TTFT medido "
                 "no cliente. eval_tps = tokens de saida/s. load_ms alto = modelo "
                 "sendo recarregado (keep_alive curto)."),
    }
    return jsonify(resumo)


@app.route("/sweep", methods=["GET", "POST"])
def sweep():
    """Varredura de parametros: testa combinacoes de num_ctx x keep_alive e
    reporta as metricas de cada ponto. Serve para escolher configuracao.

    Cada ponto roda 1 geracao (barato mas real) e guarda TTFT/tokens/s/load_ms.
    O produto num_ctx x keep_alive e limitado por SWEEP_MAX_POINTS para o request
    inteiro caber no --timeout. Valores default das queries:
      num_ctx_list   : 2048,4096,8192
      keep_alive_list: 30s,-1
      think          : off (esquenta sem raciocinio longo)
      prompt         : BENCH_PROMPT
    Uso: /sweep?num_ctx_list=2048,8192&keep_alive_list=0,-1

    POST com JSON aceita os mesmos campos no corpo (mesmo motivo do /bench:
    prompts longos estouram o limite de ~4 KB da request line no gateway)."""
    if not LLM_MODEL:
        return jsonify(ok=False,
                       reason="LLM_MODEL nao definido - nada para medir"), 503
    body = req.get_json(silent=True) or {}

    def arg(name, default=None):
        # query vence; ausente em ambos -> default
        if name in req.args:
            return req.args.get(name)
        return body.get(name, default)

    ctx_vals = _parse_int_list(arg("num_ctx_list"),
                               default=[2048, 4096, 8192])
    ka_vals = _split_csv(arg("keep_alive_list"), default=["30s", "-1"])
    think = _parse_think(arg("think")) or False
    prompt = str(arg("prompt", BENCH_PROMPT))
    pontos = []
    for ctx in ctx_vals:
        for ka in ka_vals:
            if len(pontos) >= SWEEP_MAX_POINTS:
                break
            pontos.append({"num_ctx": ctx, "keep_alive": ka,
                           "keep_alive_sent": _normalize_keep_alive(ka)})
        else:
            continue
        break
    cortado = len(ctx_vals) * len(ka_vals) > len(pontos)
    resultados = []
    for p in pontos:
        r = _bench_generate(prompt, think=think,
                            options={"num_ctx": p["num_ctx"]},
                            keep_alive=p["keep_alive_sent"])
        resultados.append({
            "num_ctx": p["num_ctx"], "keep_alive": p["keep_alive"],
            "keep_alive_sent": p["keep_alive_sent"],
            "ok": r.get("ok"), "ttft_ms": r.get("ttft_ms"),
            "total_ms": r.get("total_ms"), "load_ms": r.get("load_ms"),
            "eval_tps": r.get("eval_tps"), "eval_count": r.get("eval_count"),
            "error": r.get("error") or r.get("reason") or r.get("error_body"),
        })
    return jsonify({
        "ok": True, "model": LLM_MODEL, "think_sent": think,
        "points_run": len(resultados), "truncated_by_max_points": cortado,
        "max_points": SWEEP_MAX_POINTS, "results": resultados,
        "note": ("compare eval_tps (throughput) e load_ms (carga de VRAM) entre "
                 "os pontos; keep_alive=0 descarrega apos cada chamada, -1 fixa."),
    })


@app.get("/static/perf.css")
def static_perf_css():
    """CSS compartilhado do tema btop-like (perf + chat + home). Servido pelo
    proprio app para nao depender da pasta static/ no empacotamento."""
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
        "perf.css", mimetype="text/css")


@app.get("/system/stats")
def system_stats_route():
    """Retrato de recursos do PROPRIO container (memoria/CPU via cgroup, rede,
    uptime, processo). Le apenas arquivos internos - sem socket Docker e sem
    comando externo. Nao expoe hostname/IP/credencial."""
    return jsonify(system_stats())


@app.get("/perf")
def perf_page():
    """Pagina HTML que agrega: autopsia do container (/system/stats), estado do
    modelo na memoria (/api/ps via /llm/version) e throughput sob demanda
    (/bench). O formulario roda /bench via fetch e pinta a tabela no navegador."""
    model = LLM_MODEL or "(LLM_MODEL nao definido)"
    return render_template("perf.html", model=model, url=_endpoint_mask())


@app.get("/env")
def probe_env():
    return jsonify(step_env())


@app.get("/llm/version")
def llm_version():
    """Identifica a VERSAO do Ollama interno (GET /api/version) + modelos quentes
    (/api/ps) + metadados do MODELO configurado (POST /api/show: arquitetura,
    parametros, quantizacao, contexto, tamanho). Leve e rapido (~ms); nao gera
    tokens. Uso: /llm/version"""
    if not LLM_URL:
        return jsonify(ok=False,
                       reason="LLM_URL nao definido - nada para consultar"), 503
    ver = step_version()
    ps = step_ps()
    show = step_show()
    info = {
        "ollama_version": ver.get("version"),
        "confirmed": bool(ver.get("ok")),
        "model": LLM_MODEL or None,
        "architecture": show.get("architecture"),
        "parameter_count_human": show.get("parameter_count_human"),
        "quantization": show.get("quantization"),
        "context_length": show.get("context_length"),
        "size_human": show.get("size_human"),
    }
    return jsonify({
        "summary": info,
        "ollama_version": ver.get("version"),
        "confirmed": bool(ver.get("ok")),
        "version_check": ver,
        "process_check": ps,
        "model_check": show,
    })


@app.get("/llm/default-think")
def llm_default_think():
    """Descobre o PADRAO de thinking do modelo configurado (LLM_MODEL).

    Roda o mesmo prompt em tres variantes e compara:
      - omit : NAO envia 'think'  -> revela o default real do modelo/servidor
      - off  : envia think=false
      - on   : envia think=true
    Se 'omit' trouxer campo 'thinking' preenchido, o padrao do modelo e LIGADO;
    se vier ausente/vazio, o padrao e DESLIGADO. Uso: /llm/default-think?prompt=...
    """
    prompt = req.args.get(
        "prompt", "Qual e 2+2? Responda so o numero.")
    cases = {"omit": None, "off": False, "on": True}
    results = {}
    for name, think in cases.items():
        res = step_generate(prompt=prompt, think=think)
        results[name] = {
            "think_sent": res.get("think_sent"),
            "ok": res.get("ok"),
            "status": res.get("status"),
            "has_thinking_field": bool(res.get("thinking")),
            "thinking_preview": str(res.get("thinking") or "")[:200],
            "response_preview": str(res.get("response") or "")[:200],
            "ms": res.get("ms"),
            "error": res.get("error") or res.get("reason") or res.get("error_body"),
        }
    # Conclusao heuristica baseada apenas no caso 'omit' (o default real).
    om = results["omit"]
    if not om.get("ok"):
        verdict = "inconclusive (chamada sem 'think' falhou)"
    elif om.get("has_thinking_field"):
        verdict = "padrao do modelo = THINKING ON (veio campo thinking sem enviar think)"
    else:
        verdict = "padrao do modelo = THINKING OFF (sem campo thinking quando 'think' omitido)"
    return jsonify({"model": LLM_MODEL, "prompt": prompt,
                    "verdict": verdict, "cases": results})


@app.get("/llm/openai-compat")
def llm_openai_compat():
    """Testa se o Ollama interno expoe a camada OpenAI-compativel (/v1/...).

    Se funcionar, um cliente OpenAI (ex.: o P0) pode apontar direto para ele sem
    nenhuma ponte de traducao - so falta resolver o ALCANCE de rede. Roda:
      1. GET  /v1/models            -> existe? lista modelos no formato OpenAI?
      2. POST /v1/chat/completions  sync   -> gera resposta chat.completion?
      3. POST /v1/chat/completions  stream -> emite chunks SSE ate [DONE]?
    Uso: /llm/openai-compat?stream=off (para pular o teste de streaming)."""
    if not LLM_URL or not LLM_MODEL:
        return jsonify(ok=False,
                       reason="LLM_URL/LLM_MODEL nao definidos - nada para testar"), 503
    version = step_version()
    with_models = step_openai_models()
    sync_chat = step_openai_chat(stream=False)
    quer_stream = _parse_think(req.args.get("stream")) is not False  # default on
    stream_chat = step_openai_chat(stream=True) if quer_stream else {"skipped": True}

    compat = bool(with_models.get("ok")) and bool(sync_chat.get("ok"))
    if compat:
        partes = ["GET /v1/models OK", "POST /v1/chat/completions (sync) OK"]
        if quer_stream and stream_chat.get("streaming_ok"):
            partes.append("streaming SSE OK")
        elif stream_chat.get("skipped"):
            partes.append("streaming NAO testado (?stream=off)")
        else:
            partes.append("streaming FALHOU (sync ainda funciona)")
        verdict = "COMPATIVEL com OpenAI: " + "; ".join(partes)
    else:
        falhas = []
        if not with_models.get("ok"):
            falhas.append(f"/v1/models -> {with_models.get('status') or with_models.get('error')}")
        if not sync_chat.get("ok"):
            falhas.append(f"/v1/chat/completions -> {sync_chat.get('status') or sync_chat.get('error')}")
        verdict = ("NAO confirmado como OpenAI-compat: " + "; ".join(falhas)
                   + ". Use a API nativa /api/generate.")
    return jsonify({
        "model": LLM_MODEL,
        "ollama_version": version.get("version"),
        "openai_compatible": compat,
        "verdict": verdict,
        "version": version,
        "models": with_models,
        "chat_sync": sync_chat,
        "chat_stream": stream_chat,
    })


@app.get("/ready")
def probe_ready():
    body, code = step_ready()
    headers = {} if body.get("ready") else {"Retry-After": str(int(max(2, WARMUP_INTERVAL)))}
    return jsonify(body), code, headers


@app.get("/warmup")
def probe_warmup():
    """Mostra o estado do aquecimento e, se preciso, (re)dispara em background.
    Responde imediato - nunca espera o generate terminar."""
    disparo = step_warmup()
    estado = _estado_warmup()
    return jsonify({
        "config": {
            "enabled": WARMUP_ENABLED,
            "prompt": WARMUP_PROMPT,
            "think_sent": False,
            "timeout_s": WARMUP_TIMEOUT,
            "attempts": WARMUP_ATTEMPTS,
            "interval_s": WARMUP_INTERVAL,
        },
        "trigger": disparo,
        "state": estado,
    })


# No import do modulo (startup do worker gunicorn / python app.py), ja dispara o
# aquecimento em background para o cold start rodar enquanto ninguem espera.
step_warmup()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
