"""Harness offline para step_version/step_ps/step_show e a rota /llm/version.

Sobe um servidor HTTP fake que imita o Ollama (/api/version, /api/ps, POST
/api/show) e aponta LLM_URL para ele. Roda dentro da imagem Docker (Flask+
requests reais). Nao toca a rede institucional.

Como executar (precisa de Flask+requests -> use a imagem do probe):
    docker build -q -t probe-test:latest .
    docker run --rm -v "$PWD:/src" -w /src probe-test:latest \\
        python tests/test_offline_probes.py

Cobre: deteccao de versao, /api/show (fallbacks de parameter_count/quant) e as
correcoes do review: reason em HTTP 200 vazio, LLM_PORT (sem colisao com env
PORT), LLM_API_KEY presence-only no /env, deadline thread-local (sem race
/probe x /api/ask) e WARMUP_BUDGET (teto global de aquecimento).
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

# Localiza app.py na raiz do projeto (um nivel acima de tests/).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STATE = {"version": "0.12.7",
         "ps_models": [{"name": "qwen3.5:9b-fake", "size": 6_000_000_000}]}

SHOW_BODY = {
    "model_details": "qwen2",
    "template": "{{ .System }}\n" * 50,
    "details": {"format": "gguf", "family": "qwen2", "families": ["qwen2"],
                "parameter_size": "9B", "quantization_level": "Q4_K_M"},
    "model_info": {
        "general.architecture": "qwen2",
        "general.parameter_count": 9_000_000_000,
        "general.quantization_version": 2,
        "general.families": ["qwen2"],
        "qwen2.context_length": 4096,
        "qwen2.layer_count": 48,
        "qwen2.quantization_level": "Q4_K_M",
    },
    "parameters": "temperature = 0.7\nnum_ctx = 4096",
    "size": 5_899_000_000,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/version":
            self._send({"version": STATE["version"]})
        elif self.path == "/api/ps":
            self._send({"models": STATE["ps_models"]})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/api/show":
            self._send(SHOW_BODY)
        else:
            self._send({"error": "not found"}, 404)


srv = HTTPServer(("127.0.0.1", 8777), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()

os.environ["LLM_URL"] = "http://127.0.0.1:8777"
os.environ["LLM_MODEL"] = "qwen3.5:9b-fake"
os.environ["WARMUP_ENABLED"] = "0"

import app  # noqa: E402  (resolvido via sys.path acima)

client = app.app.test_client()
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


# 1. step_version
v = app.step_version()
check("step_version ok", v.get("ok") is True)
check("step_version value", v.get("version") == "0.12.7")

# 2. step_ps
ps = app.step_ps()
check("step_ps ok", ps.get("ok") is True)
check("step_ps count", ps.get("loaded_count") == 1)

# 3. step_show (novo)
sh = app.step_show()
check("step_show ok", sh.get("ok") is True)
check("step_show architecture", sh.get("architecture") == "qwen2")
check("step_show params human", sh.get("parameter_count_human") == "9.0B")
check("step_show quant", sh.get("quantization") == "Q4_K_M")
check("step_show context", sh.get("context_length") == 4096)
check("step_show size bytes", sh.get("size_bytes") == 5_899_000_000)
check("step_show size human GiB", sh.get("size_human").endswith("GiB"))
check("step_show no template body", "template" not in sh and sh.get("template_chars", 0) > 0)

# 4. rota /llm/version enriquecida
r = client.get("/llm/version")
j = r.get_json()
check("/llm/version 200", r.status_code == 200)
check("/llm/version confirmed", j.get("confirmed") is True)
check("/llm/version summary version", j["summary"]["ollama_version"] == "0.12.7")
check("/llm/version summary arch", j["summary"]["architecture"] == "qwen2")
check("/llm/version summary params", j["summary"]["parameter_count_human"] == "9.0B")
check("/llm/version summary ctx", j["summary"]["context_length"] == 4096)
check("/llm/version model_check present", j["model_check"]["ok"] is True)

# 5. fallback: skipped quando falta LLM_MODEL
app.LLM_MODEL = ""
sh2 = app.step_show()
check("step_show skipped sem model", sh2.get("skipped") is True)
app.LLM_MODEL = "qwen3.5:9b-fake"

# 5b. fallback: sem architecture -> general.parameter_count + details.quant
SHOW_BODY["model_info"].pop("general.architecture")
SHOW_BODY["model_info"].pop("qwen2.context_length")
SHOW_BODY["model_info"].pop("qwen2.quantization_level")
sh3 = app.step_show()
check("show sem arch ok", sh3.get("ok") is True)
check("show sem arch param", sh3.get("parameter_count_human") == "9.0B")
check("show sem arch quant fallback", sh3.get("quantization") == "Q4_K_M")

# 6. servidor sem /api/version nem /api/show -> ok False, nao explode
class BadHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


srv2 = HTTPServer(("127.0.0.1", 8778), BadHandler)
threading.Thread(target=srv2.serve_forever, daemon=True).start()
app.LLM_URL = "http://127.0.0.1:8778"
vb = app.step_version()
sb = app.step_show()
check("version 404 -> not ok", vb.get("ok") is False)
check("show 404 -> not ok", sb.get("ok") is False)
check("show 404 -> status", sb.get("status") == 404)

# ===== Extensão: casos para as 6 correções do review =====
app.LLM_URL = "http://127.0.0.1:8777"   # volta do BadHandler (porta 8778) p/ o bom

# HTTP 200 com corpo vazio -> reason diagnostico (era o erro '{}' do deploy).
class EmptyHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self): self._send({}) if hasattr(self,"_send") else (
        self.send_response(200), self.send_header("Content-Length","2"),
        self.end_headers(), self.wfile.write(b"{}"))
    def _send(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)

srv3 = HTTPServer(("127.0.0.1", 8779), EmptyHandler)
threading.Thread(target=srv3.serve_forever, daemon=True).start()
app.LLM_URL = "http://127.0.0.1:8779"
r = app.step_generate(prompt="oi", think=False)
check("generate: 200 vazio marca reason legivel",
      r.get("ok") is True and "sem conteudo" in str(r.get("reason", "")),
      f"reason={str(r.get('reason'))[:70]}")
check("generate: reason cita chaves retornadas",
      "chaves retornadas" in str(r.get("reason", "")), "")

# /env publica LLM_API_KEY presence-only.
app.LLM_URL = "http://127.0.0.1:8777"
k = app.step_env()["vars"]["LLM_API_KEY"]
check("env: LLM_API_KEY presence-only",
      k["present"] is False and "value" not in k, str(k))

# Porta da LLM mora em LLM_PORT; step_tcp usa target HOST:LLM_PORT.
check("app: LLM_PORT existe, sem global PORT",
      hasattr(app, "LLM_PORT") and not hasattr(app, "PORT"), "")
check("tcp: target HOST:LLM_PORT",
      app.step_tcp()["target"] == f"{app.HOST}:{app.LLM_PORT}", "")

# Deadline thread-local, isolado entre threads (sem race probe/api_ask).
check("deadline: ctx thread-local, sem global",
      hasattr(app, "_DEADLINE_CTX") and not hasattr(app, "_PROBE_DEADLINE"), "")
vistos = {}
def _sonda(nome, dl):
    app._DEADLINE_CTX.deadline = dl
    time.sleep(0.05)
    vistos[nome] = app._net_timeout(120)[1]
th_a = threading.Thread(target=_sonda, args=("a", [time.time() + 5]))
th_b = threading.Thread(target=_sonda, args=("b", None))
th_a.start(); th_b.start(); th_a.join(); th_b.join()
check("deadline: isolado por thread (sem clobber)",
      vistos["a"] < 6 and vistos["b"] == 120, f"a={vistos['a']} b={vistos['b']}")

# WARMUP_BUDGET limita esforco total; 200-vazio nao marca warm.
check("warmup: WARMUP_BUDGET declarado",
      isinstance(app.WARMUP_BUDGET, float) and app.WARMUP_BUDGET > 0, "")
oa, ob, om = app.WARMUP_ATTEMPTS, app.WARMUP_BUDGET, app.LLM_MODEL
try:
    app.WARMUP_ATTEMPTS, app.WARMUP_BUDGET, app.LLM_MODEL = 8, 0.0, "m"
    app.LLM_URL = "http://127.0.0.1:8779"   # sempre 200 {} -> nunca aquece
    t0 = time.time(); app._rodar_warmup(); dur = time.time() - t0
    st = dict(app._WARMUP_STATE)
    check("warmup: budget esgotado encerra rapido",
          dur < 2.0 and st["status"] == "failed" and "orcamento" in str(st.get("error","")),
          f"dur={dur:.2f} status={st['status']}")
finally:
    app.WARMUP_ATTEMPTS, app.WARMUP_BUDGET, app.LLM_MODEL = oa, ob, om
    app.LLM_URL = "http://127.0.0.1:8777"
try:
    app.WARMUP_ATTEMPTS, app.WARMUP_BUDGET, app.LLM_MODEL = 1, 300.0, "m"
    app.LLM_URL = "http://127.0.0.1:8779"
    app._rodar_warmup(); st = dict(app._WARMUP_STATE)
    check("warmup: 200 vazio nao aquece",
          st["status"] != "warm", f"status={st['status']}")
finally:
    app.WARMUP_ATTEMPTS, app.WARMUP_BUDGET, app.LLM_MODEL = oa, ob, om
    app.LLM_URL = "http://127.0.0.1:8777"

print("\nRESULTADO:", "TODOS PASSARAM" if all(c for _, c in results) else "HOUVE FALHAS")
print(f"{sum(1 for _, c in results if c)}/{len(results)} checks OK")
for n, c in results:
    if not c: print("  FAIL:", n)
