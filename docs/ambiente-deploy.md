# Ambiente Devel LLM Portal — conhecimento de deploy e da LLM

Consolida o que foi **verificado empiricamente** nos testes do `probe-llm`
(nov/2026). Serve de guia para iniciar qualquer projeto nesse ambiente.

> Este documento é de repositório **público**: não contém hostname, URL ou
> credencial reais do ambiente institucional. Os valores exatos ficam apenas na
> configuração do painel de deploy (variáveis de ambiente).

## 1. Docker / plataforma de deploy

| Regra | Detalhe verificado |
| --- | --- |
| Upload é `.tar` pronto | O servidor **não compila** Dockerfile. Pipeline: `docker build` → `docker save --output app.tar <img>` → upload. |
| Tamanho da imagem | Limite **≤ 1024 MB**. Exemplo real: imagem ~192 MB, tar ~45 MB (`python:3.12-slim` + Flask + gunicorn). |
| Porta interna fixa, pública aleatória | Container escuta `0.0.0.0:8080`; a plataforma mapeia para uma porta pública **que muda a cada redeploy**. Nunca hardcode a porta pública. |
| Porta pública é **sempre informada**, nunca adivinhada | A porta externa **não se repete nem é previsível**, então nenhum cliente/teste deve tentar descobri-la por varredura. Quem acabou de fazer o deploy conhece a porta atual e a fornece explicitamente (no `probe-llm`, via `BASE=http://host:porta`). Rodar sem essa informação falha na hora — não tenta adivinhar. |
| HTTP 200 **não** identifica seu app | O **mesmo host publica outros apps** em outras portas (ex.: um Streamlit respondendo 200 em `/health`). Por isso o `/health` mantém uma **assinatura própria** (campos JSON únicos): serve para **confirmar** que a porta informada é realmente do seu app, não para procurar portas. |
| Acesso somente HTTP | A porta pública **não fala TLS**. Abrir com `https://` trava o navegador (handshake nunca completa). Sempre `http://host:porta`. |
| Processo foreground | O container morre se o app for para background. gunicorn sem `--daemon`. |
| Storage persistente só em `/persistent` | Qualquer outro caminho é efêmero entre restarts. |
| Rede interna por hostname | Serviços do ambiente (ex.: a LLM) só são alcançáveis **dentro da rede interna**, por hostname — nunca do navegador/host externo. Daí a necessidade de uma **ponte/proxy** publicado para uso externo. |
| **Deadline de boot do App Service** | O App Service Linux **mata o container que não abre a porta em ~220 s**. Como o modelo frio pode levar mais, defina `WEBSITES_CONTAINER_START_TIME_LIMIT=300` e faça o **warmup em thread separada no boot** (não bloqueando o bind da porta). Também `WEBSITES_ENABLE_APP_SERVICE_STORAGE=false` em Linux. |

## 2. LLM (serviço interno do portal)

| Fato | Consequência de projeto |
| --- | --- |
| API **nativa Ollama** E camada **OpenAI-compatível** ambas operacionais (validado em prod) | Uma ponte pode ser **proxy transparente** para `/v1/*`, não conversor. `GET /v1/models`→200; `POST /v1/chat/completions` sync→200 com `usage`; stream→SSE `chat.completion.chunk` até `[DONE]`. |
| Modelo "frio" demora **~47 s** na 1ª chamada; LLM disponível **1–2 min** após subir o container | Implementar **warmup** + endpoint `/ready` retornando **503 + `Retry-After`** durante o aquecimento. Sem isso, o primeiro usuário toma timeout. O warmup roda em **thread daemon no boot** (paga o cold-start fora de qualquer request HTTP), e `/ready` só retorna 200 quando o status é `warm`. |
| **Thinking é ligado por padrão no servidor** (omitir `think` → campo `thinking` preenchido) | Custa ~6 s vs ~0,2 s direto. **Sempre enviar `think` explícito**: `false` para respostas rápidas, `true`/SSE quando quiser raciocínio. Não confiar no default. |
| TLS **autogerado** no endpoint interno | Cliente sempre com `verify=False` (nunca validar cert interno). |
| Raciocínio/thinking é **opt-in** | Na API nativa vem em `body["thinking"]`; controlado por flag `think`. Não assumir campo `reasoning` na camada OpenAI. |
| Budget de request deve ficar **abaixo do timeout do servidor** | Acoplamento config↔infra: `CHAT_BUDGET` < `gunicorn --timeout`. Estourar o timeout mata a request no meio. |

## 3. Limites de requisição (front-end Azure)

- A request line é limitada a **4094 bytes** no front da plataforma. Um prompt grande
  enviado via **query string (GET)** falha com `Bad Request (5472 > 4094)` antes
  de chegar à aplicação. Com URL-encoding (`%20` triplica espaços), o teto prático é
  ~1300 caracteres de prompt — na prática impossível estourar o contexto do modelo por GET.
- **Regra:** payload grande **somente via POST body** (ou GET com corpo pequeno).
  Nunca colocar texto longo na URL. Endpoints de benchmark aceitam os dois: query vence
  quando presente, senão lê o mesmo contrato do corpo JSON.

## 4. Diagnóstico: separar erro de app vs indisponibilidade

- Um `ECONNRESET`/timeout pode ser apenas **drenagem de deploy** (o container reinicia e
  **troca de IP e de porta pública**), não bug de código.
- **Sempre teste `GET /health` primeiro**: se ele também falhar, é indisponibilidade/plataforma;
  se passar mas a rota específica falhar, é aplicação.
- Erro de rede no meio do streaming deve ser capturado e virar `ok:false`, nunca derrubar o worker.
- Para inspecionar recursos do próprio container (não há socket Docker montado): ler **cgroup + `/proc`**
  do processo ("autopsia"). GPU/VRAM em % **não é obtível** se o Ollama não expõe Prometheus `/metrics`
  (404) — usar latência de carga como proxy.

## 5. Checklist para iniciar um novo projeto aqui

1. Dockerfile: base slim, `EXPOSE 8080`, ouvir `0.0.0.0:8080`, foreground, imagem < 1 GB.
2. Config **por variável de ambiente**, sem default sensível no código (endpoint/modelo).
3. `/health` com **assinatura única** (JSON próprio) para **confirmar** que a porta informada é do seu app.
4. `/ready` com warmup tolerante (503 + Retry-After) para lidar com modelo frio; warmup em thread no boot.
5. Env App Service: `WEBSITES_CONTAINER_START_TIME_LIMIT=300`, `WEBSITES_ENABLE_APP_SERVICE_STORAGE=false`.
6. Cliente/ponte usa **HTTP** (não HTTPS) no host público; `verify=False` no interno.
7. Porta pública **informada pelo operador do deploy** (nunca varrida/adivinhada); o teste exige `BASE` e falha imediatamente se faltar.
8. Payloads grandes via POST; nada longo na query string.
9. Sempre enviar `think` explícito (default do servidor é ON e custa ~6 s).
10. Budget de request abaixo do timeout do worker (`CHAT_BUDGET` < `gunicorn --timeout`).

## 6. Medições quantitativas da LLM institucional (nov/2026)

Números coletados ao vivo contra o servidor interno via `probe-llm`. Servem de
orçamento para dimensionar qualquer app (timeouts, UX de streaming, custo de RAG).
Modelo medido: `qwen3.5:9b-q4_K_M`; Ollama `0.40.13`; TLSv1.3.

### 6.1 Throughput e latência (bench, `think=off`)

| Estado | load_ms | TTFT | eval_tps |
| --- | --- | --- | --- |
| **Frio** (modelo recarregando na GPU) | ~3779 ms | ~3921 ms | **~35 tok/s** |
| **Quente** (keep_alive ativo) | ~2 ms | ~148–174 ms | **~69 tok/s** |

- **O gargalo é carga na GPU, não inferência**: ~96% do TTFT frio vem de
  `load_ms`. Com `keep_alive=-1` (nunca descarrega), o TTFT cai de ~3,9 s para
  ~0,15 s e o throughput dobra (~35 → ~69 tok/s).
- Consequência de projeto: manter o modelo aquecido (`keep_alive=-1`) é a maior
  alavanca de latência disponível; sem isso cada usuário após idle paga o reload.

### 6.2 Truncamento **silencioso** de contexto (armadilha crítica p/ RAG)

Ao enviar prompt maior que `num_ctx`, o servidor **não erro nem avisa** — responde
HTTP 200 com saída normal, mas processa só uma janela do prompt:

| Campo | Valor observado |
| --- | --- |
| `prompt_chars` enviado | 24 000 (~6 mil tokens) |
| `num_ctx` pedido | 2048 |
| `prompt_eval_count` real | **1026** (≈ metade dos tokens ignorada) |
| `eval_count` (saída) | 54 · `ok`: true |

- **Regra:** nunca confiar que "respondeu 200" significa "leu tudo". Sempre conferir
  `prompt_eval_count` contra o nº esperado de tokens do chunk. Em RAG, se a soma dos
  chunks passar de `num_ctx`, trechos são descartados **sem sinal de erro** — a
  resposta fica plausível porém cega ao conteúdo cortado.

### 6.3 Resiliência do worker sob geração longa (FASE 6)

Sob uma geração demorada (`think=on`, `num_ctx=2048`, `runs=2`):

- Os dois runs completaram: `eval_count` 1382 e 1562 tokens, `thinking_seen=true`,
  `failed_runs=0`.
- Enquanto a geração ocupava o **único worker gunicorn**, `GET /health` respondeu
  **HTTP 000 (bloqueado)** por ~48 s e voltou a **200** assim que a carga terminou.
- **Não houve crash nem queda do container** — apenas bloqueio temporário.
- O bloqueio é **por run individual**, não pela chamada inteira: num `bench runs=N`,
  o worker libera `GET /health` no **intervalo entre** um run e o próximo. Em uma
  medição posterior (`runs=2`), sondas durante a carga pegaram `/health` respondendo
  **200 em 1 das 3 tentativas** — justamente numa janela entre runs. Portanto "mudo
  durante a geração" é esperado mas **intermitente**, não permanente.
- Confirma a restrição operacional: **um worker = uma geração por vez (run a run)**.
  Nunca disparar duas cargas pesadas em paralelo; projetar timeout/UX assumindo que
  `/health` pode ficar indisponível durante cada geração, recuperando nos intervalos.
- Consequência para o smoke test (FASE 6): exigir `/health` 200 *durante* a carga é
  premissa de worker concorrente (`--workers >1`) e **não vale** para `--workers 1`.
  A resiliência real é provada por (a) JSON válido ao fim da chamada longa e (b)
  `/health` voltar 200 depois — nunca pelo silêncio momentâneo durante.

---

*Fonte: medições do `probe-llm` e `llm-bridge` em produção, nov/2026 (seção 6 =
coleta ao vivo FASE 3–6). Valores sensíveis do ambiente institucional permanecem
fora deste repo.*
