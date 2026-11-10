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
| Autodescoberta de porta obrigatória | Como a porta roda, todo cliente precisa descobrir a porta atual. Padrão validado no `probe-llm/tests/smoke_api.sh`: memória da última porta boa → lista de candidatas → varredura automática do intervalo. |
| HTTP 200 **não** identifica seu app | O **mesmo host publica outros apps** em outras portas (ex.: um Streamlit respondendo 200 em `/health`). A descoberta exige uma **assinatura própria** no `/health` (campos JSON únicos), senão escolhe a porta errada. |
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
3. `/health` com **assinatura única** (JSON próprio) para autodescoberta de porta.
4. `/ready` com warmup tolerante (503 + Retry-After) para lidar com modelo frio; warmup em thread no boot.
5. Env App Service: `WEBSITES_CONTAINER_START_TIME_LIMIT=300`, `WEBSITES_ENABLE_APP_SERVICE_STORAGE=false`.
6. Cliente/ponte usa **HTTP** (não HTTPS) no host público; `verify=False` no interno.
7. Descoberta de porta: última boa salva → candidatas → varredura automática.
8. Payloads grandes via POST; nada longo na query string.
9. Sempre enviar `think` explícito (default do servidor é ON e custa ~6 s).
10. Budget de request abaixo do timeout do worker (`CHAT_BUDGET` < `gunicorn --timeout`).

---

*Fonte: medições do `probe-llm` e `llm-bridge` em produção, nov/2026. Valores
sensíveis do ambiente institucional permanecem fora deste repo.*
