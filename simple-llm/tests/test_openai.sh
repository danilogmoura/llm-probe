#!/usr/bin/env bash
# Testes da API OpenAI do simple-llm ja DEPLOYADO.
# Uso:
#   ./test_openai.sh <PORTA>
#   HOST=devel-llm.eastus.cloudapp.azure.com ./test_openai.sh 12345
# A plataforma mapeia a porta interna 8080 para uma porta PUBLICA aleatoria a
# cada redeploy -> por isso a porta e SEMPRE um argumento (nunca chutar/scanear).
set -uo pipefail

HOST="${HOST:-devel-llm.eastus.cloudapp.azure.com}"
PORT="${1:-${PORT:-}}"
if [ -z "$PORT" ]; then
  echo "ERRO: informe a porta publica. Ex.: $0 12345" >&2
  exit 2
fi
BASE="http://${HOST}:${PORT}"

pass=0; fail=0
ok()   { echo "  PASS  $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $1"; fail=$((fail+1)); }

echo "== simple-llm OpenAI test =="
echo "BASE=$BASE"
echo

# 1) /health: precisa responder 200 com assinatura unica do app.
echo "[1] GET /health (assinatura)"
body=$(curl -sS --max-time 20 "$BASE/health"); code=$?
if [ $code -ne 0 ]; then bad "curl falhou (sem resposta?)"; else
  if echo "$body" | grep -q '"service"[[:space:]]*:[[:space:]]*"simple-llm"'; then
    ok "identidade simple-llm confirmada"
  else
    bad "resposta inesperada: $body"
  fi
  echo "      $body"
fi

# 2) /api/test/models: valida a camada OpenAI-compativel (/v1/models).
echo "[2] POST /api/test/models"
body=$(curl -sS --max-time 60 -X POST "$BASE/api/test/models"); code=$?
if [ $code -ne 0 ]; then bad "curl falhou"; else
  if echo "$body" | grep -q '"ok"[[:space:]]*:[[:space:]]*true'; then
    ok "models.list() OK"
  else
    bad "falha ao listar modelos: $body"
  fi
  echo "      $body"
fi

# 3) /api/test/chat think=off: chamada minima de chat.
echo "[3] POST /api/test/chat (think off)"
body=$(curl -sS --max-time 180 -X POST "$BASE/api/test/chat" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Responda apenas com a palavra: pronto","think":false}'); code=$?
if [ $code -ne 0 ]; then bad "curl falhou"; else
  if echo "$body" | grep -q '"ok"[[:space:]]*:[[:space:]]*true'; then
    ok "chat sync OK"
  else
    bad "chat falhou: $body"
  fi
  echo "      $body"
fi

# 4) /api/test/chat think=on: confirma campo reasoning quando suportado.
echo "[4] POST /api/test/chat (think on)"
body=$(curl -sS --max-time 180 -X POST "$BASE/api/test/chat" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Quanto e 12x8? Pense passo a passo.","think":true}'); code=$?
if [ $code -ne 0 ]; then bad "curl falhou"; else
  if echo "$body" | grep -q '"ok"[[:space:]]*:[[:space:]]*true'; then
    ok "chat think=on OK"
    if echo "$body" | grep -q '"reasoning"[[:space:]]*:[[:space:]]*null'; then
      echo "      nota: reasoning=null (provedor nao devolveu raciocinio)"
    fi
  else
    bad "chat think=on falhou: $body"
  fi
  echo "      $body"
fi

echo
echo "== RESULTADO: $pass PASS / $fail FAIL =="
[ "$fail" -eq 0 ]
