# Ética, privacidade e limites

## Observar é ler

Um agente de terminal processa comandos, caminhos e conteúdo de arquivos. Um agente de mensagens
processa conversas. Portanto um coletor de eventos lê, necessariamente, dados sensíveis: tokens de
API em linhas de comando, caminhos com nomes de clientes, trechos de mensagens privadas, mensagens
de erro de provedores que ecoam o payload enviado.

Este projeto trata isso como requisito de projeto, não como detalhe:

1. **Redação antes da gravação.** Todo texto passa por `redact_text()` antes de virar NDJSON ou
   relatório. A função cobre prefixos conhecidos (`sk-`, `ghp_`, `xoxb-`, `AKIA`, `AIza`, `hf_`,
   `nvapi-`), pares `NOME=valor` de variáveis sensíveis e cabeçalhos `Bearer`.
2. **Arquivos de segredo nunca são lidos como conteúdo.** `.env` e `auth.json` só aparecem na sonda
   como "existe / não existe".
3. **Truncamento por padrão.** Pré-visualizações são curtas (200–400 caracteres) e centradas em
   metadados (tipo, ferramenta, digests) em vez de conteúdo integral.

A redação é conservadora de propósito: prefere borrar demais a vazar. Ela **não** é um mecanismo de
segurança para produção — dados de pesquisa ainda merecem revisão humana antes de publicação.

## Somente observação, imposto pelo motor

| Risco | Como é evitado |
| --- | --- |
| Corromper o histórico de sessões | SQLite aberto com `mode=ro` **e** `PRAGMA query_only = ON` |
| Disputar o lock de escrita do WAL | nenhuma transação de escrita é iniciada; leituras usam timeout curto |
| Alterar a configuração | a sonda só lê `config.yaml`; nunca executa `config set` |
| Comandar o agente | a Runs API é consumida só com `GET`; não há chamada a `/steer` ou `/approval` |
| Deixar rastros em arquivos do Hermes | offsets de leitura vivem apenas na memória do processo |

Verificação executável (`assert_readonly`, usada nos testes):

```python
>>> from hermes_apprentice.collector.state_db import assert_readonly
>>> assert_readonly("examples/hermes-home-fixture/state.db")
{'path': 'examples/hermes-home-fixture/state.db',
 'write_blocked': True,
 'error': 'attempt to write a readonly database'}
```

## O modo `spool` e o consentimento

Ler `state.db` e logs é passivo. O modo `spool` **não** é: ele exige que alguém registre um hook em
`~/.hermes/config.yaml`, o que é uma alteração de configuração do harness e passa pelo consentimento
por par `(evento, comando)` do próprio Hermes.

Se você usar esse modo:

- o hook deve ser um **observador puro**: não devolver `block`, `approve`, `modify` nem contexto;
- o hook escreve apenas no diretório de spool definido por você;
- trate o spool como material sensível — ele contém argumentos de ferramenta antes da redação do
  Hermes.

O caminho recomendado continua sendo `state_db` + `logs` (passivo) ou a Runs API (contrato
publicado).

## Limites metodológicos

- **Acurácia em cenários sintéticos não é generalização.** Os cenários são rotulados por construção;
  os detectores foram desenhados a partir das mesmas assinaturas. O número de acurácia deles mede
  *fidelidade da regra à assinatura*, não desempenho em campo.
- **Falsos positivos não foram medidos em sessões reais.** Rodar o coletor em tráfego real, com
  consentimento e redação, é o próximo passo declarado no relatório.
- **`loop.cycle` pode superdetectar** em sessões longas com poucas ferramentas disponíveis (um agente
  que só tem `read_file` e `terminal` produz alternância natural). O limiar `cycle_min_repeats` é o
  botão a calibrar primeiro.
- **Latência do modelo depende do hardware.** Por isso a bancada grava núcleos, RAM, dispositivo e
  dtype ao lado dos números.
- **Um modelo de 0,6 B não é um detector.** Ele serve para medir o piso da escala local e para a
  hipótese de "segunda opinião" sobre janelas ambíguas.

## Uso responsável

- Colete apenas de instalações que você administra ou para as quais tem autorização explícita.
- Antes de publicar um NDJSON, rode uma revisão: a redação cobre os formatos conhecidos, não todos.
- Se um achado for compartilhado, prefira compartilhar `rule`, `severity`, `tool` e digests — não
  `args_preview`.