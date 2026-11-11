# simple-llm

O **mínimo possível** para chamar uma LLM Ollama a partir de um container: uma
página com um campo de pergunta e um checkbox **thinking (on/off)**.

Sem diagnóstico, sem streaming, sem templates, sem gunicorn — só Flask + 1 rota.

## Como funciona

- `GET /` → formulário (pergunta + opção *thinking*).
- `POST /ask` → chama a API nativa do Ollama:

  ```json
  POST {LLM_URL}/api/generate
  { "model": "<LLM_MODEL>", "prompt": "<sua pergunta>", "stream": false, "think": true|false }
  ```

- A página exibe, separados, o raciocínio (`thinking`) e a resposta final.

O checkbox desmarcado envia `"think": false`; marcado envia `"think": true`.
Modelos que não suportam thinking podem ignorar o campo ou responder com erro —
o app mostra o erro bruto na tela.

## Configuração (variáveis de ambiente)

| Variável | Obrigatória | Descrição |
|----------|-------------|-----------|
| `LLM_URL`  | sim | Endpoint base do Ollama, ex.: `https://...` ou `http://host:11434` |
| `LLM_MODEL`| sim | Nome/slug do modelo, ex.: visto em `GET /api/tags` |
| `PORT`     | não | Porta HTTP interna (padrão `8080`) |

> ⚠️ **Nunca** coloque a URL/endpoint institucional ou credenciais neste repositório
> nem em arquivos versionados. Passe tudo via `-e` ao rodar o container.

## Rodar local (sem Docker)

```bash
pip install -r requirements.txt
export LLM_URL=https://SEU_ENDPOINT
export LLM_MODEL=seu-modelo
python app.py            # sobe em http://0.0.0.0:8080
```

## Rodar com Docker

```bash
docker build -t simple-llm .
docker run -p 8080:8080 \
  -e LLM_URL=https://SEU_ENDPOINT \
  -e LLM_MODEL=seu-modelo \
  simple-llm
```

Abra `http://localhost:8080`, escreva a pergunta, marque/desmarque *thinking* e
envie.

## Testar via curl (sem abrir o navegador)

```bash
curl -s -X POST http://localhost:8080/ask \
  --data-urlencode "prompt=Qual e 2+2? Responda so o numero." \
  --data-urlencode "think=on"
```

## Arquivos

```
simple-llm/
├── app.py             # Flask: 3 rotas (GET /, POST /ask e GET /favicon.ico)
├── requirements.txt   # Flask, requests, urllib3
├── Dockerfile         # python:3.12-slim, expõe 8080, CMD python app.py
└── README.md
```

## Notas de deploy

- O container escuta em `0.0.0.0:8080` em primeiro plano (padrão exigido pela
  plataforma).
- O certificado da LLM é autogerado, então a verificação SSL está desabilitada
  (`verify=False`). Não use esse comportamento contra endpoints públicos reais.
