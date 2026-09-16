# Padrão de qualidade para diagramas

Use somente as seções aplicáveis ao formato solicitado.

## Semântica antes da aparência

- Declare no título ou subtítulo: `Observado`, `Proposto` ou `Híbrido`.
- Use IDs estáveis e simples; labels podem ser amigáveis.
- Cada seta deve possuir direção e significado defensáveis.
- Diferencie fluxo síncrono, assíncrono e dependência com estilo e legenda, não
  apenas com cor.
- Represente trust boundaries, contas/projetos, regiões ou redes somente quando
  relevantes para a pergunta.
- Em arquitetura extraída de código, associe relações relevantes a evidências
  `arquivo:linha` e confidence `confirmed`, `inferred` ou `unknown`.

## Composição

- `TB` funciona bem para camadas; `LR` para jornadas e pipelines.
- Mantenha a visão principal em aproximadamente 5 a 12 blocos. Para sistemas
  maiores, gere visão geral mais diagramas de detalhe.
- Agrupe por responsabilidade: clientes, edge, identidade, API, compute, dados,
  mensageria, observabilidade e operação são exemplos, não uma lista obrigatória.
- Evite cruzamento excessivo de linhas e labels longos dentro dos nós.
- Use espaçamento, hierarquia tipográfica e contraste suficientes. Não dependa
  apenas de cor para transmitir estado.

## Terminal

- Renderize caixas ou uma árvore/fluxo diretamente em ASCII/Unicode.
- Inclua uma legenda curta e, para arquitetura observada, evidências compactas.
- Ajuste à largura do terminal; quebre labels, não a estrutura inteira.
- Não despeje a fonte Mermaid como substituto do desenho solicitado.

Exemplo reduzido:

```text
[Cliente]
    |
    v HTTPS
[API] ----> [Serviço]
                 |
                 v
              [Dados]

Legenda: ----> chamada síncrona
```

## Mermaid em HTML

- Parta de `assets/architecture-diagram-template.html`.
- Prefira `flowchart TB` ou `flowchart LR`, subgraphs com nomes claros e classes
  consistentes.
- Use a API `mermaid.initialize`, `startOnLoad: true` e
  `securityLevel: "strict"`.
- Dentro de `<pre class="mermaid">`, escape caracteres HTML. Por exemplo,
  escreva `A --&gt; B`; o navegador recompõe `A --> B` antes do parse.
- Não aceite `click`, callbacks, scripts ou diretivas de configuração vindos
  de fonte não confiável.
- Fixe a major version da dependência CDN e declare no rodapé que a primeira
  renderização requer rede. Para uso offline, forneça export estático.

## Aparência

- Use paleta escura de alto contraste por padrão quando não houver identidade
  visual definida. Um accent do provedor pode orientar a leitura sem colorir
  indiscriminadamente todos os elementos.
- Inclua título, subtítulo, legenda e data/escopo quando isso ajudar a evitar
  interpretação errada.
- Ícones são opcionais. Não imite logos oficiais nem dependa de fontes de ícones
  externas sem necessidade.

## Validação mínima

1. Verifique que o arquivo é UTF-8 e não possui placeholders pendentes.
2. Valide a fonte Mermaid com parser/CLI quando disponível; caso contrário,
   abra o HTML em navegador permitido e confirme a presença do SVG renderizado.
3. Teste viewport estreito e amplo quando o HTML for uma entrega relevante.
4. Confirme que nomes, direções, legenda e evidências correspondem à fonte.
5. Procure segredos e dados sensíveis antes da entrega.
6. Se a renderização não foi verificada, declare isso explicitamente.
