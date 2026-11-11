# devel-llm

Repositório para imagens Docker enviadas ao portal **Devel LLM**.

Este repo mantém **três aplicações**, cada uma na sua pasta com `app.py`,
`Dockerfile`, `requirements.txt` e `README.md` próprios:

```text
probe-llm/     # sonda de diagnóstico (Flask/Gunicorn): rede, ambiente do container,
               #   cascata de conectividade, chat, bench/sweep e monitor de recursos.
llm-bridge/    # proxy transparente da API OpenAI para a LLM interna (Ollama).
simple-llm/    # exemplo mínimo: uma página com campo de pergunta + thinking on/off.
```

A imagem principal de entrega é o **`probe-llm/`**; as outras duas são utilitários de
referência que compartilham as mesmas regras de plataforma (escuta em `0.0.0.0:8080`,
processo em primeiro plano, sem VOLUME, SSL `verify=False`).

## Build da imagem principal

A imagem deve ter um **identificador unico, sem caracteres especiais** (ponto vira
hifen). Substitua `<seu-identificador>` pelo seu:

```bash
cd probe-llm
docker build -t <seu-identificador>-probe-llm:latest .
docker save --output probe-llm.tar <seu-identificador>-probe-llm:latest
```

No dashboard, envie o `.tar` gerado informando a porta interna `8080`.

## Documentação

- `probe-llm/README.md`: documentação específica da imagem mantida.
- Regras gerais da plataforma Devel LLM: consulte o manual fornecido pela instituição (não versionado aqui).
