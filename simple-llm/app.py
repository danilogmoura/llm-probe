# simple-llm - o MINIMO possivel usando APENAS o protocolo OpenAI.
# Uma pagina que chama a LLM institucional pela lib oficial `openai`
# (client.chat.completions) com opcao de thinking on/off.
#
# Config vem SEMPRE de variaveis de ambiente:
#   - Local: arquivo .env (carregado por python-dotenv).
#   - Deploy: campo "Variaveis de ambiente" do painel da plataforma.
# Nenhuma credencial/endpoint tem default no codigo (repo publico).
import base64
import html
import os

import httpx2  # cliente HTTP nativo da openai 3.x (substitui o antigo httpx)
from dotenv import load_dotenv
from flask import Flask, Response, request
from openai import OpenAI

# Carrega .env se existir (local). No deploy o painel ja injeta as variaveis
# reais no ambiente; load_dotenv NAO sobrescreve o que ja esta definido.
load_dotenv()

LLM_URL = os.environ.get("LLM_URL", "").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
# A LLM interna nao exige chave real; o protocolo OpenAI so precisa de um
# valor nao vazio. Em producao, defina LLM_API_KEY no painel se quiser.
LLM_API_KEY = os.environ.get("LLM_API_KEY", "not-needed")
PORT = int(os.environ.get("PORT", "8080"))

app = Flask(__name__)

# Cliente OpenAI apontando para a camada compativel do servidor: {URL}/v1.
# verify=False porque o endpoint interno usa certificado autoassinado.
_client = None


def get_client():
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=f"{LLM_URL}/v1",
            api_key=LLM_API_KEY,
            http_client=httpx2.Client(verify=False),
            timeout=180.0,
        )
    return _client

PAGE = """<!doctype html><meta charset=utf-8><title>simple-llm</title>
<style>body{{font-family:system-ui;max-width:640px;margin:2rem auto;padding:0 1rem}}
textarea{{width:100%;height:5em}}pre{{white-space:pre-wrap;background:#f4f4f4;padding:1rem}}</style>
<h3>simple-llm &middot; OpenAI &middot; modelo: <code>{model}</code></h3>
<form method=post action=/ask>
<textarea name=prompt placeholder='Escreva sua pergunta'></textarea>
<label><input type=checkbox name=think value=on> thinking</label>
<button type=submit>Perguntar</button>
</form>
<pre>{out}</pre>
"""


@app.get("/")
def home():
    return PAGE.format(
        model=html.escape(LLM_MODEL or "(LLM_MODEL nao definido)"), out=""
    )


@app.post("/ask")
def ask():
    prompt = (request.form.get("prompt") or "").strip()
    think = request.form.get("think") == "on"  # checkbox -> thinking ligado
    if not prompt:
        return PAGE.format(model=html.escape(LLM_MODEL), out="Digite uma pergunta.")
    if not LLM_URL or not LLM_MODEL:
        return PAGE.format(
            model=html.escape(LLM_MODEL),
            out="Configure LLM_URL e LLM_MODEL (.env local ou painel de deploy).",
        )

    # Pensamento via parâmetro extra específico do provedor (Ollama/OpenAI-compat).
    # Se a camada ignorar o campo, simplesmente nao devolve reasoning_content.
    extra_body = {"think": think}
    try:
        resp = get_client().chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
            extra_body=extra_body,
        )
        msg = resp.choices[0].message
        out = ""
        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning:
            # Provedor devolve o raciocinio num campo separado.
            out += "== thinking ==\n" + str(reasoning) + "\n\n== resposta ==\n"
            out += str(msg.content)
        elif think:
            # think=on mas sem campo dedicado: este provedor embute o
            # raciocinio no proprio conteudo (resposta mais longa/deliberada).
            out += "== raciocinio + resposta (think on) ==\n" + str(msg.content)
        else:
            out += "== resposta ==\n" + str(msg.content)
    except Exception as exc:  # noqa: BLE001 - app minima, mostra qualquer falha
        out = f"[erro] {type(exc).__name__}: {exc}"

    return PAGE.format(model=html.escape(LLM_MODEL), out=html.escape(out))


# --- API JSON para testar a conexao OpenAI (sem navegador) -----------------
# Regras da plataforma: /health com assinatura UNICA (identifica o app entre
# varios no mesmo host) e payload longo sempre via POST (limite de ~4 KB na
# linha de request do front Azure).


@app.get("/health")
def health():
    # Assinatura unica + estado da config (sem revelar segredos).
    return {
        "service": "simple-llm",
        "protocol": "openai",
        "configured": bool(LLM_URL and LLM_MODEL),
        "model": LLM_MODEL or None,
    }


@app.post("/api/test/models")
def api_test_models():
    """Valida a camada OpenAI-compativel: GET {LLM_URL}/v1/models."""
    if not LLM_URL:
        return {"ok": False, "error": "LLM_URL nao configurado"}, 500
    try:
        models = get_client().models.list()
        names = [m.id for m in models.data]
        return {"ok": True, "count": len(names), "models": names}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 502


@app.post("/api/test/chat")
def api_test_chat():
    """Chamada minima de chat pela lib openai. Body JSON opcional:
    {"prompt": str, "think": bool}. Default: prompt curto, think off.
    Retorna content, reasoning (se houver), usage e latencia.
    """
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "Diga apenas: OK").strip()
    think = bool(data.get("think", False))
    if not LLM_URL or not LLM_MODEL:
        return {"ok": False, "error": "LLM_URL/LLM_MODEL nao configurados"}, 500

    import time

    started = time.perf_counter()
    try:
        resp = get_client().chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
            extra_body={"think": think},
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 502
    elapsed_ms = round((time.perf_counter() - started) * 1000)

    msg = resp.choices[0].message
    usage = getattr(resp, "usage", None)
    return {
        "ok": True,
        "model": resp.model,
        "finish_reason": resp.choices[0].finish_reason,
        "content": msg.content,
        "reasoning": getattr(msg, "reasoning_content", None),
        "usage": usage.model_dump() if usage else None,
        "latency_ms": elapsed_ms,
    }


@app.get("/favicon.ico")
def favicon():
    # 1x1 PNG transparente -> evita o 404 no log do navegador.
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    return Response(png, mimetype="image/png")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
