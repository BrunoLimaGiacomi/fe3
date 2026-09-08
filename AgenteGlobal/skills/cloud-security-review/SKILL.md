---
name: cloud-security-review
description: Use para revisar arquitetura, mudança operacional ou desenho de segurança em AWS, Azure, GCP ou Huawei Cloud.
---

# Cloud Security Review

## Escopo

Avalie somente os domínios relevantes para a decisão:

- Identidade, autenticação, autorização e federação.
- Rede, exposição pública, segmentação e caminhos administrativos.
- Proteção de dados, criptografia, KMS e segredos.
- Logs, auditoria, alertas e SIEM.
- Resiliência, backup, DR e dependências regionais.
- Governança, custo, operação e responsabilidade compartilhada.

## Regras

- Separe fato confirmado, inferência, premissa e lacuna.
- Use documentação oficial ou evidência live read-only para comportamento instável.
- Não faça deploy, IAM change, cleanup ou teste destrutivo sem autorização explícita.
- Para threat model, aplique STRIDE de forma proporcional, com impacto, probabilidade e mitigação.

## Entrega

Priorize riscos reais, recomendações práticas, validação e risco residual.
