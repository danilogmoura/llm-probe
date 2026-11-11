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
| `PORT` | `8080` | Porta de **escuta deste app** (fallback `python app.py`; o gunicorn fixa 8080). A porta do **LLM** vem embutida em `LLM_URL` e vive internamente como `LLM_PORT` — são coisas distintas. |
| `LLM_API_KEY` | *(sem default)* | Chave opcional para compatibilidade OpenAI (`Authorization: Bearer …`). Se ausente, o probe envia um placeholder; o valor **nunca** é exposto no `/env` (só presença). |
| `PROBE_PROMPT` | `Qual e 2+2? Responda so o numero.` | Pergunta padrão usada por `/llm/generate-stream` quando `?prompt=` é omitido. |
| `WARMUP_ENABLED` | `1` | Liga/desliga o aquecimento automático em background no startup. |
| `WARMUP_PROMPT` | `ping` | Prompt mínimo enviado para esquentar o modelo (sempre com `think=false`). |
| `WARMUP_TIMEOUT` | `150` | Timeout (s) de leitura de cada tentativa de aquecimento; mantenha abaixo do `--timeout`. |
| `WARMUP_ATTEMPTS` | `8` | Máximo de tentativas de aquecimento antes de marcar `failed`. |
| `WARMUP_INTERVAL` | `10` | Pausa (s) entre tentativas de aquecimento. |
| `WARMUP_BUDGET` | `300` | Teto (s) **global** do esforço de aquecimento. Se a LLM estiver morta, o warmup desiste em ~5 min em vez de somar todas as tentativas (~20 min). A pausa nunca ultrapassa o orçamento restante. |
| `BENCH_RUNS` | `3` | Nº padrão de gerações por ponto no `/bench` (p50/p95). A query `?runs=` sobrepõe; teto duro 20 para não estourar o worker. |
| `BENCH_PROMPT` | `Explique em duas frases o que faz um servidor HTTP.` | Pergunta padrão de `/bench` e `/sweep` quando `?prompt=` é omitido. |
| `SWEEP_MAX_POINTS` | `12` | Teto de combinações `num_ctx × keep_alive` no `/sweep`, limitando o tempo total do request. |

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

O app expõe **17 rotas**: as de uso normal (chat + streaming), as de diagnóstico/aquecimento,
as de performance (`/bench`, `/sweep`) e as de recursos do container (`/system/stats`, `/perf`).
A conectividade com a LLM interna é testada pela cascata `/probe` (DNS → TCP → TLS →
modelo → versão), que roda todas as etapas em ordem num único JSON, sem nunca derrubar o servidor.

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
- `GET /ready` → health de **prontidão REAL**: devolve `200` apenas quando o modelo já
  **respondeu de verdade** (cold start pago), não só quando existe. Enquanto o aquecimento
  em background roda, responde **`503`** com `retry_after_s` (bom para polling); se o
  aquecimento estiver desligado/falho, cai no teste leve `POST /api/show` para dar um
  veredito honesto. Nunca bloqueia a request esperando o generate — a espera acontece na
  thread de warmup. Checa **só o seu modelo** — não lista os demais.
- `GET /warmup` → mostra o **estado do aquecimento** (`pending/warming/warm/failed/disabled/
  skipped`), quantas tentativas já rodaram e a configuração efetiva; se ainda não disparou,
  (re)dispara em background e responde imediato (não espera o generate terminar).
- `GET /llm/generate-stream` → **streaming SSE** (`text/event-stream`) sobre
  `POST {LLM_URL}/api/generate` com `stream:true`. Aceita `?prompt=` e `?think=on|off`.
  Emite eventos `data: {"event":"token","response":"..."}` (e `thinking` quando ligado)
  até `data: {"event":"done",...}`. É a forma recomendada de gerar com modelo thinking:
  o timeout vira *inatividade* (tempo entre chunks), não duração total, então o worker
  não é morto enquanto há tokens chegando.
- `GET /llm/version` → **identifica a versão exata do Ollama** interno. Consulta
  `GET {LLM_URL}/api/version` (endpoint nativo e público do Ollama, que responde
  `{"version":"0.x.y"}` sem autenticação nem modelo), `GET /api/ps` (modelos
  carregados na memória) e `POST /api/show` (**metadados do `LLM_MODEL`**: arquitetura,
  nº de parâmetros, quantização, janela de contexto e tamanho em disco). É leve (~ms),
  não gera tokens. Devolve um `summary` pronto para leitura (`ollama_version`,
  `architecture`, `parameter_count_human`, `quantization`, `context_length`,
  `size_human`) + o detalhe bruto de cada checagem. Se o servidor não expuser algum
  endpoint, marca `confirmed:false`/campo ausente sem derrubar o app. O chat template
  nunca é devolvido por inteiro — só o comprimento em caracteres.
- `GET /env` → mostra a configuração efetiva e **quais** variáveis existem no ambiente,
  **sem expor valores** (política de segurança: nada de credencial em logs). Para URLs
  revela apenas scheme/porta, jamais o hostname interno.
- `GET /llm/default-think` → descobre o **padrão de thinking** do modelo configurado. Roda
  o mesmo prompt (`?prompt=`) em três variantes — `omit` (não envia `think`), `off`
  (`think=false`) e `on` (`think=true`) — e compara se o campo `thinking` aparece. Se a
  variante `omit` trouxer `thinking`, o default do servidor é **LIGADO**. Devolve um JSON com
  os três casos (latência, presença de `thinking`, previews) e um `verdict`. Atenção: roda
  três gerações em série, então chame fora da cascata `/probe`.
- `GET /llm/openai-compat` → **valida se o Ollama interno expõe a camada compatível com
  OpenAI** (`/v1/...`). É a checagem que decide se um cliente OpenAI (ex.: o P0) pode falar
  direto com ele sem ponte de tradução. Roda, em série:
  1. `GET {LLM_URL}/v1/models` → existe? responde no formato `{"object":"list","data":[...]}`?
     Confirma apenas se o `LLM_MODEL` configurado aparece na lista (não revela os demais nomes).
  2. `POST {LLM_URL}/v1/chat/completions` (sync) → gera uma resposta `chat.completion` real?
  3. `POST {LLM_URL}/v1/chat/completions` (`stream:true`) → emite chunks SSE até `[DONE]`?
  Devolve `openai_compatible` (true/false), um `verdict` legível e o detalhe de cada teste
  (status, latência, preview). Aceita `?stream=off` para pular o teste de streaming. Usa o
  mesmo `Authorization: Bearer` que a biblioteca OpenAI mandaria (placeholder local; nenhum
  segredo é impresso). Atenção: roda duas gerações em série — chame fora da cascata `/probe`.

### Performance (bench / sweep)

Além do diagnóstico de conectividade, o probe mede o **desempenho real** da LLM. As métricas
vêm do próprio servidor: no último chunk do streaming (`"done": true`), o Ollama devolve
`total_duration`, `load_duration`, `eval_duration`, `prompt_eval_duration` (todos em
**nanossegundos**) e `eval_count`/`prompt_eval_count` (tokens). O probe converte para ms e
calcula throughput — nada é estimado no cliente, exceto o TTFT.

- `GET|POST /bench` → **benchmark sequencial**: repete a mesma geração `runs` vezes e
  agrega **p50/p95/min/max** das métricas-chave. Parâmetros (query ou corpo JSON):
  - `prompt` (default `BENCH_PROMPT`), `runs` (default `BENCH_RUNS`, teto duro 20),
    `think=on|off`, `num_ctx=<int>` (vai em `options.num_ctx`), `keep_alive=<s|-1|0>`.
  - Retorna `ttft_ms` (tempo até o primeiro token, medido no cliente), `eval_tps`
    (tokens/s de saída, do servidor), `load_ms` (custo de carregar o modelo — alto indica
    `keep_alive` curto / cold start), `total_ms`, além de `per_run` com os runs brutos.
  - Ex.: `GET /bench?runs=3&num_ctx=4096&keep_alive=-1&think=off`
- `GET|POST /sweep` → **varredura de parâmetros**: testa o produto `num_ctx_list × keep_alive_list`
  (1 geração por ponto) e reporta as métricas de cada combinação, para escolher configuração.
  Limitado por `SWEEP_MAX_POINTS` para caber no `--timeout`. Parâmetros:
  `num_ctx_list` (default `2048,4096,8192`), `keep_alive_list` (default `30s,-1`),
  `think` (default `off`), `prompt`.
  - Ex.: `GET /sweep?num_ctx_list=2048,8192&keep_alive_list=0,-1`

> **Prompt longo use `POST` com corpo JSON.** O App Service limita a *request line*
> a ~4 KB e devolve `400 Request Line is too large` antes de chegar ao Flask — um
> prompt de teste de contexto via query estoura esse teto. Os mesmos campos vão no
> corpo (a query ainda tem prioridade quando presente):
>
> ```bash
> curl -X POST "$BASE/bench" -H 'Content-Type: application/json' \
>   -d '{"prompt":"<24 mil chars>","runs":1,"num_ctx":2048,"think":"off"}'
> ```
>
> No Postman: método `POST`, aba **Body → raw → JSON**. Em `num_ctx_list` /
> `keep_alive_list` do `/sweep` envie lista JSON (`[2048,8192]`) ou string CSV
> (`"2048,8192"`) — as duas formas funcionam.

> **Custo:** `/bench` e `/sweep` rodam **gerações reais em série** — podem levar minutos.
> Assim como `/llm/default-think` e `/llm/openai-compat`, ficam **fora** da cascata `/probe`;
> chame um por vez. Cada geração usa streaming, então a conexão fica viva enquanto chegam
> bytes e o worker não é morto pelo `--timeout` enquanto há tokens fluindo.

O evento `done` do SSE de `/llm/generate-stream` agora inclui `ttft_ms` e `stats` (as mesmas
métricas nativas), e a resposta de `/api/ask` traz `stats` enriquecido — dá para acompanhar
throughput sem sair do chat.

### Recursos do container (`/system/stats`, `/perf`)

O probe roda como container isolado (Azure App Service): **não** há socket Docker montado
nem como rodar `docker stats` contra si mesmo. A alternativa é a **autopsia** — ler os
arquivos internos do próprio container.

- `GET /system/stats` → JSON com a autopsia do container:
  - **Memória/CPU do container** via **cgroup** (v2 em `/sys/fs/cgroup/*`, com *fallback* v1
    em `memory/`, `cpu/`): uso, limite, % do limite, cache, segundos de CPU acumulados e
    núcleos (quota e visíveis).
  - **Processo**: PID, RSS e tempo de CPU do worker gunicorn (`/proc/self`).
  - **Host** (referência compartilhada, não isolada): RAM total/disponível (`/proc/meminfo`),
    load average, uptime e nº de CPUs.
  - **Rede**: bytes recebidos/enviados agregados das interfaces (`/proc/net/dev`).
  Tudo é *best-effort*: arquivo ausente ou ilegível vira `null`, nunca exceção. Não expõe
  hostname, IP interno, ambiente nem credenciais.
- `GET /perf` → **página HTML** que reúne tudo numa tela: painel de recursos que se
  auto-atualiza (5 s), estado do modelo na memória (`/api/ps`) e um formulário que roda o
  `/bench` ao vivo no navegador e pinta a tabela de p50/p95.

> **GPU/VRAM não são obtíveis pela API.** O Ollama institucional não expõe o endpoint
> Prometheus `/metrics` (retorna 404), então uso de GPU em % não está acessível. Use
> `load_ms` do `/bench` como proxy do custo de carga do modelo.

## Ordem recomendada para as checagens

Use esta sequência ao diagnosticar um container recém-subido. A regra geral é **barato e
sem dependência primeiro, caro depois**, parando na primeira falha:

| # | Rota | Endpoint Ollama | Custo | Responde |
| --- | --- | --- | --- | --- |
| 1 | `GET /health` | *(nenhum — só env)* | ~0 s | `LLM_URL`/`LLM_MODEL` estão configurados? |
| 2 | `GET /probe` | `/api/show` + `/api/version` (cascata) | < 1 s por etapa | A **rede** até o LLM está OK (DNS→TCP→TLS→modelo→versão)? |
| 3 | `GET /llm/version` | `/api/version` → `/api/ps` → `/api/show` | ~ms | Qual a **versão** do servidor e os **metadados** do modelo? |
| 4 | `GET /warmup` | *(lê estado da thread)* | ~0 s | O cold start já foi pago em background? |
| 5 | `GET /ready` | `/api/generate` leve ou `/api/show` | 0–2 min | O modelo **respondeu de verdade**? (`200` = pronto) |
| 6 | `GET /llm/generate-stream` | `/api/generate` (`stream:true`) | segundos–minutos | Geração real token a token (SSE). |
| 7 | `GET /llm/openai-compat` | `/v1/models` → `/v1/chat/completions` | 2 gerações | Dá para apontar um cliente OpenAI direto? |

Princípios por trás da ordem:

- **Configuração antes de rede**: `/health` não faz HTTP; se `LLM_URL` estiver vazio, as
  demais rotas só devolvem erro inútil.
- **Cascata antes de rota individual**: `/probe` já roda DNS→TCP→TLS→modelo→versão e **para
  na primeira falha**, então identifica *onde* a conexão quebra sem você chamar cada passo.
- **Leitura antes de inferência**: `/api/version`, `/api/ps` e `/api/show` custam
  milissegundos e **não geram tokens**; geração síncrona pode levar minutos num modelo
  thinking. Nunca coloque `generate` dentro de uma cascata com budget.
- **Prontidão antes de uso**: `/ready` retorna `503` + `retry_after_s` durante o aquecimento
  — faça polling nele em vez de arriscar um `WORKER TIMEOUT` na primeira pergunta.
- **As duas rotas caras ficam fora da cascata**: `/llm/default-think` (3 gerações) e
  `/llm/openai-compat` (2 gerações) — chame-as manualmente, uma por vez.

### Atalho para o caso comum

Se o objetivo é só saber **"o LLM está acessível e qual versão?"**, basta:

```text
GET /health  →  GET /llm/version
```

Ambas respondem em menos de um segundo e não dependem do aquecimento terminar.

### Cascata completa

- `GET /probe` → roda a **cascata de conectividade** (`dns → tcp → tls → model → version`)
  em ordem, parando na primeira falha, dentro de um **orçamento de tempo**
  (`PROBE_BUDGET`). Serve para responder "a rede até o LLM está OK?" num único JSON.
  Cada etapa de rede encurta seu timeout ao restante do budget, então nenhuma requisição
  pendurada consegue estourar o `--timeout` do gunicorn. Quando o budget acaba, as etapas
  restantes são marcadas como `skipped` (com a dica de usar as rotas individuais). O
  response inclui um bloco `_budget` com `budget_s`, `elapsed_ms` e `remaining_ms`.
  **Não executa geração**: o passo `generate` sai como `deferred`, pois modelos thinking
  podem levar minutos — para gerar, use `GET /llm/generate-stream` (SSE).

## Estratégia de aquecimento (cold start da primeira chamada)

Medimos que a **primeira geração** após o deploy paga um **cold start de ~105 s**
(carregar o modelo / sair do sleep), contra ~6 s (thinking) ou ~0,2 s (off) depois de
quente. O manual também avisa que a LLM só fica disponível **1–2 min após o container
subir**. Se essa espera acontecesse *dentro* de uma requisição, estouraria o `--timeout`
do gunicorn (180 s) e derrubaria o worker.

A solução: **esquentar em background no startup**, para o usuário nunca pagar o cold start.

- Ao importar o módulo (startup do worker), `step_warmup()` dispara **uma thread daemon**
  que roda uma geração mínima (`think=false`, prompt curto) até o modelo responder.
- A thread **encadeia tentativas** com pausa curta (`WARMUP_INTERVAL`) por um número
  máximo (`WARMUP_ATTEMPTS`), cobrindo a janela de 1–2 min do manual. Um teto global
  (`WARMUP_BUDGET`, default 300 s) limita o esforço total: se a LLM estiver morta, o
  warmup desiste em ~5 min (marca `failed` com "orçamento esgotado") em vez de somar
  todas as tentativas (~20 min). Só conta como **aquecido** um HTTP 200 **com conteúdo**
  em `response` — um 200 vazio de cold start é tratado como falha e tenta de novo.
- `/ready` consulta o estado dessa thread: vira **`200` somente quando o modelo respondeu
  de fato** (`warm`); enquanto aquece devolve **`503` + `retry_after_s`**; se esgotar as
  tentativas marca `failed` e cai no teste leve `/api/show`.
- Como o trabalho pesado vive numa **thread separada**, nenhuma requisição HTTP bloqueia
  nem mata o worker — a restrição "budget abaixo do `--timeout`" continua valendo para as
  rotas síncronas, mas o aquecimento em si é assíncrono.

### Variáveis de ambiente do aquecimento

| Variável | Default | Função |
| --- | --- | --- |
| `WARMUP_ENABLED` | `1` | `0`/`false` desliga o aquecimento automático. |
| `WARMUP_PROMPT` | `ping` | Prompt mínimo usado para esquentar. |
| `WARMUP_TIMEOUT` | `150` | Timeout de leitura de cada tentativa (s). Mantido abaixo do `--timeout`. |
| `WARMUP_ATTEMPTS` | `8` | Máximo de tentativas antes de marcar `failed`. |
| `WARMUP_INTERVAL` | `10` | Pausa (s) entre tentativas. |
| `WARMUP_BUDGET` | `300` | Teto (s) global do esforço total de aquecimento; ao estourar, marca `failed` com "orçamento esgotado" em vez de insistir por ~20 min. |

O aquecimento usa sempre `think=false`: o objetivo é só tirar o modelo do cold start no
menor tempo possível, não raciocinar. Consulte o progresso em `GET /warmup` ou `GET /env`
(bloco `warmup`).

## Modo "thinking" (raciocínio) on/off

Modelos hibridos suportam ligar/desligar o raciocinio pela API
nativa `/api/generate` com o campo `think` (`true`/`false`); o raciocínio vem no
campo `thinking` da resposta. Na página `/chat`, basta marcar a caixa
"pensar antes (thinking)".

### Descoberta: o padrão do modelo é THINKING ON

Medido com `GET /llm/default-think` no modelo institucional, **reprodutível em duas
execuções independentes** (prompt `"Qual e 2+2? Responda so o numero."`):

| Variante | `think` enviado | Campo `thinking` | Run 1 (frio) | Run 2 (quente) |
| --- | --- | --- | --- | --- |
| `omit` | *(ausente)* | ✅ presente | 105.490 ms | 6.986 ms |
| `off` | `false` | ❌ ausente | 181 ms | 183 ms |
| `on` | `true` | ✅ presente | 6.008 ms | 5.865 ms |

Conclusões:

- **Omitir `think` liga o raciocínio por padrão** (o caso `omit` sempre traz `thinking`).
  Ou seja, não dá para confiar no default: se você não enviar `think`, o modelo pensa.
- **Sempre envie `think` explícito**: `false` para respostas diretas (~0,2 s), `true` (ou a
  rota SSE `/llm/generate-stream`) quando quiser raciocinar sem matar o worker.
- O custo de pensar é real: ~6 s (com o modelo quente) contra ~0,2 s no modo direto.
- **Só a primeira execução é lenta; depois os tempos ficam estáveis.** Os ~105 s do `omit`
  no run 1 foram **cold-start** (modelo carregando na primeira chamada após o deploy), não o
  preço fixo do thinking: no run 2 o `omit` caiu para ~7 s, igual ao `on`. Como a rota roda os
  casos em ordem `omit → off → on`, o `omit` é sempre o primeiro a pagar o aquecimento — por
  isso o `off` aparece rápido (~0,18 s) nos dois runs, pois já encontrou o modelo quente. Isso
  reforça o aviso do manual: a LLM leva 1–2 min para ficar pronta e a primeira request paga
  essa conta.

## Build local

O nome da imagem deve ser um **identificador unico, sem caracteres especiais**
(ponto vira hifen). Troque `<seu-identificador>` pelo seu (ex.: sobrenome + matricula):

```bash
docker build -t <seu-identificador>-probe-llm:latest .
docker save --output probe-llm.tar <seu-identificador>-probe-llm:latest
```
