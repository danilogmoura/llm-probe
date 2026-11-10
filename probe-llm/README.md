# probe-llm — app mínima de teste da LLM

App Flask enxuta cujo único objetivo é **testar a LLM interna da plataforma**: uma
página de chat (`/chat`) e um conjunto de rotas de diagnóstico que validam rede,
ambiente e acesso à LLM etapa por etapa.

A chamada de geração usa `POST {LLM_URL}/api/generate`. Como o modelo institucional é
de **raciocínio longo (thinking)**, a forma confiável de obter uma resposta completa é a
rota de **streaming** `GET /llm/generate-stream` (SSE): os tokens chegam conforme o modelo
gera, mantendo a conexão viva e evitando `WORKER TIMEOUT`. A chamada síncrona
`/api/ask` (usada pelo chat) pode estourar o timeout em prompts que exigem muito raciocínio.

O certificado é autogerado, então a verificação SSL é desabilitada (`verify=False`).

> A LLM interna fica disponivel **1 a 2 minutos apos** a inicializacao do container.
> Se as primeiras chamadas falharem, aguarde e tente de novo.

## Como enviar no portal

Na tela **Enviar imagem compactada**, preencha assim:

| Campo do formulário | Valor |
| --- | --- |
| Porta do container (`ports`) | `8080` |
| Variáveis de ambiente (`environment_variables`) | veja abaixo |
| Arquivo .tar (`tar_file`) | `probe-llm.tar` |

O campo `name`/`tag` é preenchido automaticamente pela plataforma — não edite.

### Variáveis de ambiente (uma por linha)

```text
LLM_URL=<endpoint da LLM informado no anuncio da plataforma>
LLM_MODEL=<nome do modelo disponivel na plataforma>
CHAT_BUDGET=150
PROBE_BUDGET=55
```

### Todas as variáveis de ambiente suportadas

| Variável | Default no código | Função |
| --- | --- | --- |
| `LLM_URL` | *(sem default)* | Endpoint da LLM interna. Obrigatório para qualquer chamada real. |
| `LLM_MODEL` | *(sem default)* | Nome do modelo escolhido. Sem ele, o chat avisa que falta configuração. |
| `LLM_TIMEOUT` | `120` | Timeout (s) de **leitura** (espera pela resposta da LLM). Alto porque o modelo thinking gera raciocínio longo antes de responder. |
| `CONNECT_TIMEOUT` | `10` | Timeout (s) de **conectividade** (abrir TCP/TLS). Host morto ou porta fechada falha rápido em todas as rotas, sem esperar o `LLM_TIMEOUT` inteiro. |
| `PROBE_BUDGET` | `55` | Teto (s) da cascata completa do `/probe`. |
| `CHAT_BUDGET` | `150` | Teto (s) de cada pergunta na página `/chat` (rota `/api/ask`). |
| `PORT` | `8080` | Porta de escuta (usada no fallback `python app.py`; o gunicorn fixa 8080). |
| `PROBE_PROMPT` | `Qual e 2+2? Responda so o numero.` | Pergunta padrão usada por `/llm/generate-stream` quando `?prompt=` é omitido. |

- `LLM_URL` e `LLM_MODEL` **não têm default no código** de propósito: o endereço real é
  do ambiente institucional e **não deve ser versionado no repositório público**. Consulte
  o anúncio/manual da plataforma e informe os valores em *Variáveis de ambiente*. Sem eles,
  o chat responde com um aviso de configuração pendente.
- **Regra dos budgets:** `PROBE_BUDGET` e `CHAT_BUDGET` devem ficar **abaixo** do
  `--timeout` do gunicorn (180s no Dockerfile), senão o worker é morto no meio da
  requisição.
- **Modelo thinking:** o endpoint institucional roda um modelo de raciocínio longo. O
  `POST /api/show` responde em ~12ms (modelo existe), mas a **geração síncrona** pode levar
  mais de 30–55s mesmo num prompt trivial e estourar o budget do `/probe`. Por isso:
  (1) o `/probe` **não roda generate síncrono** — ele marca `generate` como `deferred` e
  orienta usar a rota de streaming; (2) para gerar de verdade, use `GET /llm/generate-stream`
  (SSE), que entrega token a token sem derrubar o worker. Um `ReadTimeout` só em `generate`
  é custo de inferência, não falha de rede.

## Rotas

O app expõe **8 rotas**: as de uso normal (chat + streaming) e as de diagnóstico.
A conectividade com a LLM interna é testada pela cascata `/probe` (DNS → TCP → TLS →
modelo), que roda todas as etapas em ordem num único JSON, sem nunca derrubar o servidor.

### Uso normal

- `GET /` → identifica a app, mostra o modelo configurado e link para o chat
- `GET /chat` → **página web interativa**: campo de texto + botão "Perguntar"; você
  digita a pergunta e a resposta aparece na tela, com opção de ligar o thinking e
  mostrando a latência. Abre na URL pública que a plataforma atribuir ao app
  (`http://<host-da-plataforma>:<porta>/chat`). A página chama `POST /api/ask` por
  caminho relativo, então funciona atrás de proxy ou direto pela porta sem mudar nada.
- `POST /api/ask` → endpoint JSON usado pela página `/chat`. Corpo
  `{"prompt": "...", "think": true|false}`; responde o texto do modelo (+thinking
  quando ligado). Usa `CHAT_BUDGET` como teto para não estourar o worker.

> **Segurança:** as rotas de diagnóstico **nunca listam os modelos disponíveis** na
> plataforma. A checagem mira **apenas o `LLM_MODEL` configurado**, consultando o endpoint
> interno `POST /api/show` com `{"model": <LLM_MODEL>}` (dados de um único modelo). O
> `/api/tags`, que enumeraria todos os modelos, **não é chamado** em nenhuma rota.

### Diagnóstico

- `GET /health` → status simples (porta interna, `LLM_URL` e se `LLM_MODEL` está definido)
- `GET /ready` → health de **prontidão**: faz um `POST /api/show` barato no `LLM_MODEL`
  e devolve `200` só quando o **modelo configurado** está de pé; enquanto não estiver,
  devolve **`503`** com um hint (atende o aviso do manual de que a LLM leva 1–2 min após
  iniciar). Bom para polling. Checa **só o seu modelo** — não lista os demais.
- `GET /llm/generate-stream` → **streaming SSE** (`text/event-stream`) sobre
  `POST {LLM_URL}/api/generate` com `stream:true`. Aceita `?prompt=` e `?think=on|off`.
  Emite eventos `data: {"event":"token","response":"..."}` (e `thinking` quando ligado)
  até `data: {"event":"done",...}`. É a forma recomendada de gerar com modelo thinking:
  o timeout vira *inatividade* (tempo entre chunks), não duração total, então o worker
  não é morto enquanto há tokens chegando.
- `GET /env` → mostra a configuração efetiva e **quais** variáveis existem no ambiente,
  **sem expor valores** (política de segurança: nada de credencial em logs). Para URLs
  revela apenas scheme/porta, jamais o hostname interno.

### Cascata completa

- `GET /probe` → roda a **cascata de conectividade** (`dns → tcp → tls → model`) em
  ordem, parando na primeira falha, dentro de um **orçamento de tempo**
  (`PROBE_BUDGET`). Serve para responder "a rede até o LLM está OK?" num único JSON.
  Cada etapa de rede encurta seu timeout ao restante do budget, então nenhuma requisição
  pendurada consegue estourar o `--timeout` do gunicorn. Quando o budget acaba, as etapas
  restantes são marcadas como `skipped` (com a dica de usar as rotas individuais). O
  response inclui um bloco `_budget` com `budget_s`, `elapsed_ms` e `remaining_ms`.
  **Não executa geração**: o passo `generate` sai como `deferred`, pois modelos thinking
  podem levar minutos — para gerar, use `GET /llm/generate-stream` (SSE).

## Modo "thinking" (raciocínio) on/off

Modelos hibridos suportam ligar/desligar o raciocinio pela API
nativa `/api/generate` com o campo `think` (`true`/`false`); o raciocínio vem no
campo `thinking` da resposta. Na página `/chat`, basta marcar a caixa
"pensar antes (thinking)".

## Build local

O nome da imagem deve ser um **identificador unico, sem caracteres especiais**
(ponto vira hifen). Troque `<seu-identificador>` pelo seu (ex.: sobrenome + matricula):

```bash
docker build -t <seu-identificador>-probe-llm:latest .
docker save --output probe-llm.tar <seu-identificador>-probe-llm:latest
```
