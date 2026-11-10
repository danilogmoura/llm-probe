# devel-llm

Repositório para imagens Docker enviadas ao portal **Devel LLM**.

A única imagem mantida neste repo é:

```text
probe-llm/
```

Ela contém uma aplicação Flask/Gunicorn de diagnóstico para validar rede, storage, ambiente interno do container e acesso à LLM institucional.

## Build da imagem mantida

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
