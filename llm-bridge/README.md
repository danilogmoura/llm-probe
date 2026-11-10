# llm-bridge

**Proxy transparente** da API OpenAI para a LLM interna (Ollama). Permite que um
projeto **local** (ex.: o P0, que usa a biblioteca `openai`) consuma a LLM
institucional através do **link público** que o portal gera ao rodar o container.

## Por que existe

O link público do portal **não expõe a LLM** — ele expõe o *seu* container. A LLM
fica presa na `llm-network` e só é alcançável *de dentro* de um container
(`manual.md`). Esta bridge resolve isso:

```
Seu laptop (P0)  ──HTTP──►  http://<servidor>:<porta-pública>   [este container]
                                   │  repassa /v1/* sem traduzir
                                   └──llm-network──►  Ollama (/v1/chat/completions)
```

O container vive na rede interna e encaminha a chamada `/v1/*` ao servidor, que já
responde em formato OpenAI. Do lado do cliente local, é só apontar o `base_url`.

## Contrato exposto

| Rota | Método | Função |
| --- | --- | --- |
| `/v1/chat/completions` | POST | Repassa o corpo OpenAI ao servidor e devolve a resposta OpenAI (`stream: true` vira SSE cru; `false` vira JSON tal qual). |
| `/v1/models` | GET | Com `LLM_MODEL` definido, lista só o configurado (não enumera a LLM); sem ele, repassa a lista do servidor. |
| `/health` | GET | Config efetiva (sem valores sensíveis). |
| `/ready` | GET | Prontidão real via warmup (`200` quando o modelo respondeu; `503` + `Retry-After` enquanto aquece). |
| `/` | GET | Índice simples. |

### Como funciona (proxy, não conversor)

A camada compatível com OpenAI do servidor foi **validada empiricamente** pelo
probe (`GET /llm/openai-compat`): `GET /v1/models` responde `{"object":"list"}`, e
`POST /v1/chat/completions` responde `chat.completion` (sync) e chunks
`chat.completion.chunk` até `[DONE]` (stream). Portanto a bridge **não traduz**
mais: ela encaminha o corpo recebido ao `{LLM_URL}/v1/...` e devolve a resposta
exatamente como veio (só acrescenta `bridge.total_ms`, a latência medida).

> **Por que não usar a biblioteca `openai` aqui dentro?** A LLM usa certificado
> autogerado e exige `verify=False`; a lib (httpx) valida TLS por padrão e não
> expõe forma simples de desligar isso. Usamos `requests` com `verify=False`.

## Variáveis de ambiente

| Variável | Default | Função |
| --- | --- | --- |
| `LLM_URL` | *(sem default)* | Endpoint base do Ollama interno. Obrigatório. |
| `LLM_MODEL` | *(sem default)* | Nome/slug do modelo. Obrigatório. |
| `BRIDGE_TOKEN` | *(vazio)* | Se definido, toda chamada precisa de `Authorization: Bearer <token>`. Vazio = sem autenticação (só em teste local). |
| `LLM_TIMEOUT` | `150` | Timeout (s) de leitura de uma geração. Mantenha **abaixo** do `--timeout` do gunicorn (180). |
| `CONNECT_TIMEOUT` | `10` | Timeout (s) de conectividade (TCP/TLS). |
| `PORT` | `8080` | Porta HTTP interna (declarada como `ports` no portal). |
| `WARMUP_ENABLED` | `1` | Liga/desliga o aquecimento automático no startup. |
| `WARMUP_PROMPT` | `ping` | Prompt mínimo enviado para esquentar o modelo (sempre `think=false`). |
| `WARMUP_TIMEOUT` | `150` | Timeout (s) de cada tentativa de aquecimento. |
| `WARMUP_ATTEMPTS` | `8` | Máximo de tentativas antes de marcar `failed`. |
| `WARMUP_INTERVAL` | `10` | Pausa (s) entre tentativas. |

> ⚠️ **Nunca** versione a URL/endpoint institucional nem credenciais neste
> repositório. Passe tudo via `-e` (ou pelo dashboard do portal).

## Como enviar no portal

| Campo do formulário | Valor |
| --- | --- |
| Porta do container (`ports`) | `8080` |
| Variáveis de ambiente (`environment_variables`) | veja abaixo |
| Arquivo `.tar` (`tar_file`) | `llm-bridge.tar` |

Variáveis mínimas:

```text
LLM_URL=<endpoint da LLM informado no anúncio da plataforma>
LLM_MODEL=<nome do modelo disponível na plataforma>
BRIDGE_TOKEN=<um segredo seu, curto>
```

## Usar do projeto local (P0)

Depois que o portal devolver o link `http://<servidor>:<porta-pública>`, aponte o
cliente OpenAI para ele. No P0 isso é **só variável de ambiente**, sem tocar em código:

```bash
export LLM_BASE_URL=http://<servidor>:<porta-pública>   # sem /v1; a lib acrescenta /chat/completions
export LLM_API_KEY=<mesmo valor de BRIDGE_TOKEN>        # a lib manda como Bearer
export LLM_MODEL=<o mesmo nome configurado na bridge>
```

Teste rápido com curl:

```bash
curl -s http://<servidor>:<porta>/v1/chat/completions \
  -H "Authorization: Bearer <BRIDGE_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"model":"<modelo>","messages":[{"role":"user","content":"Qual é 2+2? Responda só o número."}]}'
```

## Rodar local (sem Docker)

```bash
pip install -r requirements.txt
export LLM_URL=https://SEU_ENDPOINT
export LLM_MODEL=seu-modelo
python app.py            # sobe em http://0.0.0.0:8080
```

## Cold start

A primeira geração da LLM institucional paga um cold start (~105s medidos; o
manual diz que a LLM fica disponível 1–2 min após o container subir). Para não
estourar o `--timeout` do worker na sua primeira requisição real, a bridge dispara
uma geração mínima em background no startup (warmup), chamando o próprio
`/v1/chat/completions`. Consulte `GET /ready` até virar `200` antes de usar.

## Arquivos

```
llm-bridge/
├── app.py             # Flask: proxy transparente /v1/* + warmup + /ready
├── requirements.txt   # Flask, gunicorn, requests, urllib3
├── Dockerfile         # python:3.12-slim, expõe 8080, gunicorn --timeout 180
└── README.md
```
