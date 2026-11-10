# simple-llm - o MINIMO possivel: uma pagina que chama a LLM (Ollama) com
# opcao de thinking on/off. Sem diagnostico, sem streaming, sem templates.
# Config via ambiente: LLM_URL, LLM_MODEL (e opcionalmente PORT).
import base64
import os

import requests
import urllib3
from flask import Flask, Response, request

# Endpoint interno usa certificado autoassinado -> chamadas usam verify=False.
# Silencia apenas o aviso InsecureRequestWarning (nao e falha).
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LLM_URL = os.environ.get("LLM_URL", "").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
PORT = int(os.environ.get("PORT", "8080"))

app = Flask(__name__)

PAGE = """<!doctype html><meta charset=utf-8><title>simple-llm</title>
<style>body{{font-family:system-ui;max-width:640px;margin:2rem auto;padding:0 1rem}}
textarea{{width:100%;height:5em}}pre{{white-space:pre-wrap;background:#f4f4f4;padding:1rem}}</style>
<h3>simple-llm &middot; modelo: <code>{model}</code></h3>
<form method=post action=/ask>
<textarea name=prompt placeholder='Escreva sua pergunta'></textarea>
<label><input type=checkbox name=think value=on> thinking</label>
<button type=submit>Perguntar</button>
</form>
<pre>{out}</pre>
"""


@app.get("/")
def home():
    return PAGE.format(model=LLM_MODEL or "(LLM_MODEL nao definido)", out="")


@app.post("/ask")
def ask():
    prompt = (request.form.get("prompt") or "").strip()
    think = request.form.get("think") == "on"  # checkbox -> thinking ligado
    if not prompt:
        return PAGE.format(model=LLM_MODEL, out="Digite uma pergunta.")
    payload = {"model": LLM_MODEL, "prompt": prompt, "stream": False, "think": think}
    try:
        r = requests.post(f"{LLM_URL}/api/generate", json=payload,
                          timeout=180, verify=False)
        body = r.json() if r.ok else {"erro": f"HTTP {r.status_code}: {r.text[:300]}"}
    except Exception as exc:  # noqa: BLE001 - app minima, mostra qualquer falha
        body = {"erro": f"{type(exc).__name__}: {exc}"}
    out = ""
    if body.get("thinking"):
        out += "== thinking ==\n" + str(body["thinking"]) + "\n\n"
    out += "== resposta ==\n" + str(body.get("response") or body.get("erro") or body)
    return PAGE.format(model=LLM_MODEL, out=out)


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
