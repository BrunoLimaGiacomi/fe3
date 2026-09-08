---
name: terraform-security-review
description: Use para criar, modificar ou revisar Terraform com foco em segurança, estado, drift, substituição e rollout.
---

# Terraform Security Review

## Diretrizes

- Inspecione providers, backend, workspace, módulos, variáveis, outputs e convenções existentes.
- Não execute `terraform apply`, `destroy`, import ou migração de estado sem autorização explícita.
- Evite segredos em código, outputs, state, logs, provisioners e user data.
- Analise recursos criados, alterados, substituídos ou destruídos.
- Trate IAM wildcard, público, ingress amplo, falta de criptografia e falta de logs como riscos a avaliar.

## Validação

- Use `terraform fmt -check -recursive`, `terraform validate` e plano salvo quando o escopo autorizar.
- Reporte impacto no estado, rollback e riscos residuais.
