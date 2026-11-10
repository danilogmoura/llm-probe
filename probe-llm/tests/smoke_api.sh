#!/usr/bin/env bash
# Smoke test de API do probe-llm contra um servidor VIVO.
#
# Camadas cobertas, sem tocar em segredos:
#   FASE 1  Docker local      - container sobe, /health responde, contrato do manual
#   FASE 2  App deployado     - inventario de rotas (HTML e JSON), status + JSON valido
#   FASE 2b Validacao         - entrada ruim nao derruba o servico
#   FASE 3  LLM institucional - conectividade (dns/tcp/tls/modelo) sem gerar tokens
#   FASE 4  Geracoes reais    - bench, keep_alive, overflow via POST, OpenAI-compat, SSE
#
# Uso (BASE e OPCIONAL - o script descobre a porta sozinho):
#   ./tests/smoke_api.sh                          # tudo, com autodescoberta
#   ./tests/smoke_api.sh --skip-llm               # fases 1, 2 e 2b (sem geracoes caras)
#   ./tests/smoke_api.sh --docker-only            # so a fase 1 (offline, sem rede)
#   BASE=http://SEU-HOST-PUBLICO:PORTA ./tests/smoke_api.sh
#   HOST=<seu-host-publico> ./tests/smoke_api.sh   # host usado na autodescoberta
#   IMAGE=meu-registro/probe-llm:tag ./tests/smoke_api.sh
#
# A FASE 4 roda GERACOES REAIS no LLM institucional: pode levar varios minutos e
# consome VRAM/gpu-hora. Rode uma fase por vez se o servidor estiver instavel.
# Variaveis: BASE, IMAGE, RUNS (default 2), NUM_CTX_SMALL (default 512).
#
# ATENCAO - a porta publica MUDA a cada deploy do .tar no App Service: o Azure
# realoca o container na VM e sorteia outra porta externa (o container continua
# ouvindo 8080 internamente). Voce NUNCA precisa informar a porta:
#   1. sem BASE, o script sonda PORT_CANDIDATES e usa o primeiro /health que
#      responder 200 (atualize PORT_CANDIDATES apos um redeploy para acelerar);
#   2. se nenhuma candidata funcionar, rode com --scan-all (varre 32768-32899);
#   3. BASE="http://HOST:PORT" so e necessario como ultimo recurso manual.
#
# FLAKINESS CONHECIDA do ambiente (nao e bug do app):
#   - O gunicorn roda com UM worker: uma geracao em andamento bloqueia os demais
#     pedidos. Use RUNS=1 na FASE 4 e nunca rode duas instancias do teste juntas.
#   - O gateway institucional devolve as vezes HTML "Servico Indisponivel -
#     Tentando novamente" dentro de last_error enquanto o modelo carrega na GPU;
#     nesse caso o teste falha numa passada e passa na seguinte.
#   - O teto de request line do gateway e 4094 bytes: prompts longos so entram
#     pelo corpo do POST (/bench e /sweep aceitam GET e POST).

set -uo pipefail

# Host publico do portal (sem credencial; cada usuario define o seu via env HOST).
HOST_DEFAULT="${HOST:-}"
BASE="${BASE:-}"
IMAGE="${IMAGE:-probe-llm:smoke}"
RUNS="${RUNS:-2}"
NUM_CTX_SMALL="${NUM_CTX_SMALL:-512}"
PORT_CANDIDATES="${PORT_CANDIDATES:-32880 32878 32877 32879 32881 32882}"
DOCKER_ONLY=0
SKIP_LLM=0
SCAN_ALL=0

for a in "$@"; do
  case "$a" in
    --docker-only) DOCKER_ONLY=1 ;;
    --skip-llm)    SKIP_LLM=1 ;;
    --scan-all)    SCAN_ALL=1 ;;
    -h|--help)     sed -n '2,40p' "$0"; exit 0 ;;
  esac
done

PASS=0; FAIL=0; SKIP=0
c_g=$'\033[32m'; c_r=$'\033[31m'; c_y=$'\033[33m'; c_0=$'\033[0m'

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { PASS=$((PASS+1)); printf '  %sPASS%s %s\n' "$c_g" "$c_0" "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  %sFAIL%s %s\n' "$c_r" "$c_0" "$1"; [ $# -gt 1 ] && printf '       %s\n' "$2"; }
skip() { SKIP=$((SKIP+1)); printf '  %sSKIP%s %s\n' "$c_y" "$c_0" "$1"; }

# check <nome> <esperado> <obtido>
check() { [ "$2" = "$3" ] && ok "$1 ($3)" || bad "$1" "esperado=$2 obtido=$3"; }

TMPDIR_SMOKE="$(mktemp -d)"

# Containers desta execucao + temporarios, removidos mesmo em timeout/erro/Ctrl-C.
cleanup_all() {
  [ -n "${LOCAL:-}" ] && docker rm -f "$LOCAL" >/dev/null 2>&1
  [ -n "${CONT:-}" ]  && docker rm -f "$CONT"  >/dev/null 2>&1
  rm -rf "$TMPDIR_SMOKE"
  return 0
}
trap cleanup_all EXIT INT TERM

# http <nome> <metodo> <path> [curl args...]
#   WANT          codigo esperado            (default 200)
#   WANT_TIMEOUT  segundos maximos de curl   (default 60)
#   EXPECT_JSON   1 exige corpo JSON         (default); 0 aceita HTML
# Cada variavel vale para UMA chamada e e limpa em seguida.
http() {
  local name="$1" method="$2" path="$3"; shift 3
  local tmp code json want ej
  tmp="$(mktemp "$TMPDIR_SMOKE/body.XXXXXX")"
  want="${WANT:-200}"; ej="${EXPECT_JSON:-1}"
  WANT=""; EXPECT_JSON=""
  code="$(curl -sS -o "$tmp" -w '%{http_code}' --max-time "${WANT_TIMEOUT:-60}" \
            -X "$method" "$BASE$path" "$@" 2>/dev/null)"
  if [ "$code" = "000" ]; then
    bad "$name" "sem conexao (timeout/reset) em $method $path"
    rm -f "$tmp"; LAST_BODY=""; return 1
  fi
  json="$(python3 -c 'import json,sys
try:
    json.load(open(sys.argv[1])); print("json")
except Exception:
    print("html")' "$tmp" 2>/dev/null)"
  if [ "$code" != "$want" ]; then
    bad "$name" "HTTP $code (esperado $want); corpo=$(head -c 120 "$tmp" | tr -d '\n')"
    rm -f "$tmp"; LAST_BODY=""; return 1
  fi
  if [ "$ej" = 1 ] && [ "$json" = html ]; then
    bad "$name" "resposta nao e JSON (corpo=$(head -c 120 "$tmp" | tr -d '\n'))"
    rm -f "$tmp"; LAST_BODY=""; return 1
  fi
  ok "$name -> HTTP $code, $json"; LAST_BODY="$tmp"; return 0
}

# jget <arquivo> <expressao python sobre o dict d>
# O eval roda com builtins minimos (len/str/int/sorted/filter) para os asserts nao
# dependerem do ambiente; mesmo assim prefira list comprehension a any()/all().
jget() {
  python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
env={"d":d,"len":len,"str":str,"int":int,"float":float,"bool":bool,
     "sorted":sorted,"filter":filter,"map":map,"round":round,"abs":abs,
     "min":min,"max":max,"sum":sum,"list":list,"dict":dict,"tuple":tuple,
     "True":True,"False":False,"None":None}
try:
    print(eval(sys.argv[2], {"__builtins__":{}}, env))
except Exception as e:
    print("ERR:"+type(e).__name__+":"+str(e))' "$1" "$2" 2>/dev/null
}

# jcheck <nome> <arquivo> <expr sobre d> <valor esperado>
jcheck() {
  local got; got="$(jget "$2" "$3")"
  check "$1" "$4" "$got"
}

# jbool <nome> <arquivo> <expr sobre d que deve avaliar True>
jbool() {
  local got; got="$(jget "$2" "$3")"
  check "$1" True "$got"
}

summary() {
  say "RESULTADO"
  printf '  %sPASS=%d%s  %sFAIL=%d%s  %sSKIP=%d%s\n' \
    "$c_g" "$PASS" "$c_0" "$c_r" "$FAIL" "$c_0" "$c_y" "$SKIP" "$c_0"
  [ "$FAIL" -eq 0 ] || exit 1
}

# ======================================================================
# Descoberta da URL publica: a porta do App Service muda a cada deploy do .tar.
# Estrategia em camadas, SEM depender do usuario informar a porta:
#   1. BASE explicito (ultimo recurso manual);
#   2. ultima porta usada com sucesso, salva em tests/.last_port;
#   3. PORT_CANDIDATES padrao;
#   4. fallback automatico: varredura completa 32768-32899 (--scan-all).
# A confirmacao exige JSON do probe-llm em /health (nao apenas HTTP 200), pois
# o mesmo host publica outros apps em outras portas.
DISCOVER_TIMEOUT="${DISCOVER_TIMEOUT:-4}"
# Em Bash nao-interativo, BASH_SOURCE pode estar vazio na fonte principal;
# ${BASH_SOURCE[0]:-$0} cobre os dois casos (execucao direta e source).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
LAST_PORT_FILE="$SCRIPT_DIR/.last_port"

save_last_port() { printf '%s\n' "$1" > "$LAST_PORT_FILE" 2>/dev/null || true; }

discover_ports() {
  # $1 = lista de portas; ecoa a primeira cujo /health responde 200 COM JSON
  # proprio do probe-llm. Status 200 sozinho NAO basta: o host Azure abriga
  # outros apps (ex.: Streamlit em 32857) que tambem respondem 200 em /health.
  local p body
  for p in $1; do
    body="$(curl -sS --max-time "$DISCOVER_TIMEOUT" \
              "http://$HOST_DEFAULT:$p/health" 2>/dev/null | head -c 4096)"
    if printf '%s' "$body" | grep -q '"status"' && printf '%s' "$body" | grep -q '"port"'; then
      printf '%s' "$p"; return 0
    fi
    printf '  . porta %s -> sem JSON do probe-llm\n' "$p" >&2
  done
  return 1
}

discover_base() {
  [ -n "$BASE" ] && return 0
  if [ -z "$HOST_DEFAULT" ]; then
    bad "HOST nao definido" \
        "Defina o host publico do portal: HOST=<seu-host> $0  (ou BASE=http://<host>:<porta> $0)"
    return 1
  fi
  local ports p saved
  if [ "$SCAN_ALL" = 1 ]; then
    ports="$(seq 32768 32899)"
  else
    ports="$PORT_CANDIDATES"
    # porta descoberta na execucao anterior e a aposta mais provavel agora
    saved="$(cat "$LAST_PORT_FILE" 2>/dev/null)"
    if [ -n "$saved" ] && ! printf ' %s ' "$ports" | grep -q " $saved "; then
      ports="$saved $ports"
    fi
  fi
  say "PROCURANDO a porta publica em $HOST_DEFAULT (BASE nao definido)"
  if p="$(discover_ports "$ports")"; then
    BASE="http://$HOST_DEFAULT:$p"; ok "porta encontrada: $p"; save_last_port "$p"; return 0
  fi
  if [ "$SCAN_ALL" != 1 ]; then
    say "candidatas esgotadas; VARREDURA AUTOMATICA 32768-32899 (fallback)"
    if p="$(discover_ports "$(seq 32768 32899)")"; then
      BASE="http://$HOST_DEFAULT:$p"; ok "porta encontrada no scan: $p"
      save_last_port "$p"; return 0
    fi
  fi
  bad "nenhuma porta respondeu 200 em /health (ate o scan completo falhou)" \
      "Confira no portal Azure se o container esta rodando; depois informe: BASE=http://$HOST_DEFAULT:<porta> $0"
  return 1
}

if [ "$DOCKER_ONLY" = 1 ]; then
  skip "descoberta de porta (fase Docker nao usa rede institucional)"
elif ! discover_base; then
  summary; exit 1
fi

if [ "$DOCKER_ONLY" = 0 ]; then
  say "ALVO: $BASE"
  http "GET /health (servico vivo)" GET /health || {
    bad "Servico inacessivel" "Confira no portal Azure se o container esta rodando; tente --docker-only"
    summary; exit 1
  }
  jcheck "health.status=ok" "$LAST_BODY" 'd["status"]' ok
  jbool  "health.llm_model_set=true" "$LAST_BODY" 'd["llm_model_set"] is True'
fi

# ======================================================================
say "FASE 1 - Docker (contrato do manual.md, offline)"
if ! command -v docker >/dev/null 2>&1; then
  skip "docker ausente neste host"
else
  DPORT="${DPORT:-}"
  if [ -z "$DPORT" ]; then
    # Porta ephemeral livre: evita conflito com sobras de execucoes anteriores.
    DPORT="$(python3 -c 'import socket
for p in range(18080, 18200):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p)); print(p); break
    except OSError:
        pass
    finally:
        s.close()
else:
    raise SystemExit("sem porta livre")' 2>/dev/null)" || DPORT=18080
  fi
  CONT=probe-smoke-$$
  say "  build da imagem $IMAGE"
  if docker build -q -t "$IMAGE" "$(dirname "$0")/.." >/dev/null 2>&1; then
    ok "docker build"
  else
    bad "docker build" "falhou; rode manualmente para ver o log"
  fi

  # Sem LLM_URL valido o app DEVE subir mesmo assim (requisito do docstring).
  say "  container sem LLM configurada (subida resiliente)"
  if docker run -d --rm --name "$CONT" -p "$DPORT:8080" \
       -e LLM_MODEL="" -e WARMUP_ENABLED=0 "$IMAGE" >/dev/null 2>&1; then
    up=0
    for _ in $(seq 1 30); do
      curl -fsS --max-time 2 "http://127.0.0.1:$DPORT/health" >/dev/null 2>&1 && { up=1; break; }
      sleep 1
    done
    [ "$up" = 1 ] && ok "gunicorn escuta em 0.0.0.0:8080 sem LLM_URL valida" \
                 || bad "container nao respondeu em 30s"
    check "health.port=8080 (env PORT default)" 8080 \
      "$(curl -sS --max-time 5 "http://127.0.0.1:$DPORT/health" \
          | python3 -c 'import json,sys;print(json.load(sys.stdin)["port"])' 2>/dev/null)"
    # As rotas locais usam o container recem-subido, nunca a URL publica.
    SAVED_BASE="$BASE"; BASE="http://127.0.0.1:$DPORT"
    WANT=503 http "GET /bench sem LLM_MODEL -> 503 honesto" GET "/bench" || true
    BASE="$SAVED_BASE"
    docker rm -f "$CONT" >/dev/null 2>&1
  else
    bad "docker run (sem LLM)"
  fi

  # Env fake apontando para porta fechada: exercita POST /bench e /sweep sem rede.
  say "  POST /bench e /sweep com corpo JSON (local, Ollama fake)"
  LOCAL=probe-fake-$$
  if docker run -d --rm --name "$LOCAL" -p "$((DPORT+1)):8080" \
       -e LLM_URL="http://127.0.0.1:9" -e LLM_MODEL="fake:m" \
       -e CONNECT_TIMEOUT=2 -e WARMUP_ENABLED=0 "$IMAGE" >/dev/null 2>&1; then
    sleep 3
    SAVED_BASE="$BASE"; BASE="http://127.0.0.1:$((DPORT+1))"
    # 127.0.0.1:9 nada escuta => conexao recusada imediata (DNS interno nao resolve).
    # Ollama inexistente => a rota responde 200 com ok:false, NAO derruba o worker.
    WANT=200 http "POST /bench com corpo -> 200 (erro capturado, sem crash)" POST /bench \
      -H 'Content-Type: application/json' \
      -d '{"prompt":"palavra pal","runs":1,"num_ctx":2048,"think":"off","keep_alive":"-1"}' || true
    if [ -n "${LAST_BODY:-}" ] && [ -f "${LAST_BODY:-}" ]; then
      jbool "resposta traz ok_runs=0 (geracao falhou, app vive)" "$LAST_BODY" 'd["ok_runs"] == 0'
    fi
    WANT=200 http "POST /sweep com lista JSON -> 200" POST /sweep \
      -H 'Content-Type: application/json' \
      -d '{"num_ctx_list":[2048],"keep_alive_list":["-1"],"think":"off"}' || true
    # Prompt maior que a request line do gateway (4 KB): via corpo deve ser aceito.
    big="$(python3 -c 'print("{\"prompt\":\""+"palavra "*2000+"\",\"runs\":1,\"num_ctx\":512,\"think\":\"off\"}")')"
    WANT_TIMEOUT=90 http "POST /bench prompt 16 KB (> teto de URL) -> 200" POST /bench \
      -H 'Content-Type: application/json' -d "$big" || true
    BASE="$SAVED_BASE"
    docker rm -f "$LOCAL" >/dev/null 2>&1
  else
    bad "docker run (com env fake)"
  fi
fi

# ======================================================================
if [ "$DOCKER_ONLY" = 1 ]; then
  skip "FASE 2 (app deployado) -- --docker-only nao exercita a URL publica"
  skip "FASE 2b (validacao de parametros na URL publica)"
else
  say "FASE 2 - App deployado: inventario e estado"
  # Paginas estaticas devolvem HTML, nao JSON -> EXPECT_JSON=0.
  EXPECT_JSON=0 http "GET / (home HTML)"    GET /                || true
  EXPECT_JSON=0 http "GET /chat (pagina)"   GET /chat            || true
  EXPECT_JSON=0 http "GET /perf (painel)"   GET /perf            || true
  EXPECT_JSON=0 http "GET /static/perf.css" GET /static/perf.css || true
  http "GET /env (config)"      GET /env          || true
  http "GET /system/stats"      GET /system/stats || true
  # /ready devolve 503 ENQUANTO o modelo aquece (cold start real de 1-3 min no
  # App Service). Isso nao e bug: esperar pelo ready com paciencia, aceitando
  # os 503 intermediarios, e so falhar se nunca ficar pronto.
  READY_OK=0
  for i in $(seq 1 24); do
    C=$(curl -s -o /dev/null -w '%{http_code}' -m 30 "$BASE/ready" 2>/dev/null)
    if [ "$C" = "200" ]; then READY_OK=1; break; fi
    sleep 10
  done
  if [ "$READY_OK" = "1" ]; then
    ok "GET /ready -> 200 apos espera de warmup ($i tentativa(s))"
  else
    bad "GET /ready nunca ficou pronto (24 tentativas, ~4 min)"
  fi
  http "GET /warmup"            GET /warmup       || true
  WANT_TIMEOUT=120 http "GET /probe (cascata)" GET /probe || true
  WANT_TIMEOUT=60  http "GET /llm/version"     GET /llm/version || true

  # Contrato de segredo: /env revela apenas presenca/comprimento e, em URLs,
  # somente scheme+porta. O hostname interno jamais aparece.
  ENV_BODY="$LAST_BODY"
  WANT_TIMEOUT=60 http "GET /env (leitura de segredo)" GET /env || true
  if [ -n "${LAST_BODY:-}" ] && [ -f "${LAST_BODY:-}" ]; then
    jbool "/env nao expoe chave llm_url inteira" "$LAST_BODY" \
      '"llm_url" not in d'
    jbool "/env entrega apenas scheme+porta da URL" "$LAST_BODY" \
      'sorted(d["vars"]["LLM_URL"]) == ["length","port","present","scheme"]'
    jbool "/env nunca traz a chave value" "$LAST_BODY" \
      'not [k for k in d["vars"] if "value" in d["vars"][k]]'
    jbool "/env declara politica de ocultacao" "$LAST_BODY" \
      '"credenciais" in d["note"].lower()'
  fi

  # Nenhuma rota de debug expoe o ambiente real do processo.
  WANT=404 EXPECT_JSON=0 http "GET /proc/env -> 404 (sem rota de debug)" GET /proc/env || true

  say "FASE 2b - Validacao de parametros (defesa contra entrada ruim)"
  # Toda chamada a /bench dispara geracoes reais: num_ctx pequeno + think=off para
  # manter barato, e timeout folgado porque o worker e unico e pode estar ocupado.
  WANT_TIMEOUT=150 http "GET /bench?runs=abc (nao numerico cai no default)" \
    GET "/bench?runs=abc&num_ctx=$NUM_CTX_SMALL&think=off" || true
  if [ -n "${LAST_BODY:-}" ] && [ -f "${LAST_BODY:-}" ]; then
    # runs nao numerico nao deve virar 0 nem derrubar a rota: cai no default (>=1).
    jbool "runs=abc virou um inteiro valido (>=1)" "$LAST_BODY" 'int(d["runs"]) >= 1'
  fi
  WANT_TIMEOUT=200 http "GET /bench?runs=999 (teto duro 20 aplicado)" \
    GET "/bench?runs=999&num_ctx=$NUM_CTX_SMALL&think=off" || true
  if [ -n "${LAST_BODY:-}" ] && [ -f "${LAST_BODY:-}" ]; then
    jcheck "runs=999 limitado ao teto duro" "$LAST_BODY" 'int(d["runs"])' 20
  fi
  WANT_TIMEOUT=150 http "GET /bench?think=talvez (parse tolerante)" \
    GET "/bench?runs=1&think=talvez&num_ctx=$NUM_CTX_SMALL" || true
  WANT_TIMEOUT=150 http "GET /bench?num_ctx=-5 (aceito e repassado)" \
    GET "/bench?runs=1&num_ctx=-5&think=off" || true
  WANT_TIMEOUT=200 http "GET /sweep?num_ctx_list=abc (default aplicado)" \
    GET "/sweep?num_ctx_list=abc&keep_alive_list=-1&think=off" || true
fi

# ======================================================================
if [ "$SKIP_LLM" = 1 ] || [ "$DOCKER_ONLY" = 1 ]; then
  skip "FASE 3 (conectividade com o LLM institucional)"
else
  say "FASE 3 - LLM institucional: conectividade (sem gerar tokens)"
  WANT_TIMEOUT=60 http "GET /llm/version (Ollama + api/show)" GET /llm/version || true
  if [ -n "${LAST_BODY:-}" ] && [ -f "${LAST_BODY:-}" ]; then
    jbool "ollama_version confirmada"      "$LAST_BODY" 'd.get("confirmed") is True'
    jbool "context_length lida do modelo"  "$LAST_BODY" 'd["summary"]["context_length"] is not None'
  fi
  WANT_TIMEOUT=120 http "GET /probe (dns->tcp->tls->modelo)" GET /probe || true
  if [ -n "${LAST_BODY:-}" ] && [ -f "${LAST_BODY:-}" ]; then
    jbool "probe dns ok"               "$LAST_BODY" 'd["dns"]["ok"] is True'
    jbool "probe tcp ok"               "$LAST_BODY" 'd["tcp"]["ok"] is True'
    jbool "probe tls ok"               "$LAST_BODY" 'd["tls"]["ok"] is True'
    jbool "probe modelo ok"            "$LAST_BODY" 'd["model"]["ok"] is True'
    jbool "probe generate adiado"      "$LAST_BODY" 'd["generate"].get("deferred") is True'
  fi
fi

# ======================================================================
if [ "$SKIP_LLM" = 1 ] || [ "$DOCKER_ONLY" = 1 ]; then
  skip "FASE 4 - geracoes reais"
else
  say "FASE 4 - Geracao real curta (aquecimento + TTFT/throughput)"
  WANT_TIMEOUT=280 http "GET /bench?runs=$RUNS&think=off&num_ctx=2048" \
    GET "/bench?runs=$RUNS&think=off&num_ctx=2048" || true
  if [ -n "${LAST_BODY:-}" ] && [ -f "${LAST_BODY:-}" ]; then
    jcheck "bench todos os runs ok" "$LAST_BODY" 'str(d["ok_runs"])' "$RUNS"
    jbool  "bench eval_tps medido"  "$LAST_BODY" 'd["eval_tps"]["p50"] is not None'
    jbool  "bench ttft medido"      "$LAST_BODY" 'd["ttft_ms"]["p50"] is not None'
  fi

  say "FASE 4b - keep_alive: -1 (numero) vs 0 (descarrega) - prova do fix"
  WANT_TIMEOUT=200 http "GET /bench?keep_alive=-1" \
    GET "/bench?runs=1&think=off&keep_alive=-1&num_ctx=$NUM_CTX_SMALL" || true
  if [ -n "${LAST_BODY:-}" ] && [ -f "${LAST_BODY:-}" ]; then
    jcheck "keep_alive bruto ecoado"          "$LAST_BODY" 'str(d["_meta"]["keep_alive"])' "-1"
    jcheck "keep_alive enviado como numero"   "$LAST_BODY" 'str(d["_meta"]["keep_alive_sent"])' "-1"
    jbool  "sem erro ParseDuration do Ollama" "$LAST_BODY" 'd["last_error"] is None'
  fi
  WANT_TIMEOUT=200 http "GET /bench?keep_alive=0" \
    GET "/bench?runs=1&think=off&keep_alive=0&num_ctx=$NUM_CTX_SMALL" || true
  if [ -n "${LAST_BODY:-}" ] && [ -f "${LAST_BODY:-}" ]; then
    jcheck "keep_alive=0 enviado como '0s'" "$LAST_BODY" 'str(d["_meta"]["keep_alive_sent"])' "0s"
  fi

  say "FASE 4c - Estouro de contexto VIA POST (contorna teto de 4 KB da URL)"
  PROMPT_FILE="$(mktemp "$TMPDIR_SMOKE/prompt.XXXXXX")"
  BODY_FILE="$(mktemp "$TMPDIR_SMOKE/body.XXXXXX")"
  python3 -c 'print("palavra "*3000, end="")' > "$PROMPT_FILE"   # ~24 KB >> num_ctx
  python3 -c 'import json,sys
print(json.dumps({"prompt":open(sys.argv[1]).read(),"runs":1,"num_ctx":2048,"think":"off"}))' \
    "$PROMPT_FILE" > "$BODY_FILE"
  WANT_TIMEOUT=280 http "POST /bench com prompt de 24 KB" POST /bench \
    -H 'Content-Type: application/json' --data-binary "@$BODY_FILE" || true
  if [ -n "${LAST_BODY:-}" ] && [ -f "${LAST_BODY:-}" ]; then
    jcheck "prompt longo chegou inteiro ao app" "$LAST_BODY" 'str(d["_meta"]["prompt_chars"])' 24000
    echo "       comportamento do overflow:"
    python3 - "$LAST_BODY" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
r=(d.get("per_run") or [{}])[0]
print("         ok=%s error=%s" % (r.get("ok"),
      str(r.get("error") or r.get("error_body") or "")[:160]))
print("         prompt_eval_count=%s (vs num_ctx=2048) eval_count=%s"
      % (r.get("prompt_eval_count"), r.get("eval_count")))
print("         INTERPRETACAO: prompt_eval_count<=2048 => truncou silenciosamente;"
      " erro/HTTP!=200 => rejeitou; prompt_eval_count>2048 => servidor ampliou contexto.")
PY
  fi

  say "FASE 4d - Camada OpenAI-compativel (/v1/models, chat sync + SSE)"
  WANT_TIMEOUT=240 http "GET /llm/openai-compat (varredura completa)" GET /llm/openai-compat || true

  say "FASE 4e - Default de thinking (omit vs on vs off)"
  WANT_TIMEOUT=280 http "GET /llm/default-think" GET /llm/default-think || true

  say "FASE 4f - Sweep de configuracao num_ctx x keep_alive (caro)"
  WANT_TIMEOUT=280 http "GET /sweep (matriz default)" GET "/sweep?think=off" || true
  if [ -n "${LAST_BODY:-}" ] && [ -f "${LAST_BODY:-}" ]; then
    jbool "sweep rodou pontos" "$LAST_BODY" 'd["points_run"] > 0'
    jbool "sweep sem erros"    "$LAST_BODY" 'len([p for p in d["results"] if not p["ok"]]) == 0'
  fi

  say "FASE 4g - Streaming SSE do proprio probe (/llm/generate-stream)"
  sse="$(curl -sSN --max-time 200 "$BASE/llm/generate-stream?think=off&prompt=Responda%20so%20OK" 2>/dev/null)"
  if printf '%s' "$sse" | grep -q '^data:'; then
    ok "SSE emite eventos data:"
    if printf '%s' "$sse" | grep -q '"done"'; then
      ok "SSE encerra com evento done"
    else
      bad "SSE sem done" "corpo=${sse:0:200}"
    fi
  else
    bad "SSE /llm/generate-stream" "corpo=${sse:0:200}"
  fi

  say "FASE 4h - /api/ask (POST do /chat) com think=false"
  WANT_TIMEOUT=200 http "POST /api/ask" POST /api/ask \
    -H 'Content-Type: application/json' \
    -d '{"prompt":"Responda apenas com a palavra OK","think":false}' || true
  WANT=400 http "POST /api/ask prompt vazio -> 400" POST /api/ask \
    -H 'Content-Type: application/json' -d '{"prompt":""}' || true
fi

summary
