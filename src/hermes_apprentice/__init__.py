"""hermes-apprentice — aprendiz de observação para o harness Hermes Agent.

O pacote implementa quatro capacidades independentes:

* ``probe``      — identifica versão e configuração do Hermes instalado (somente leitura).
* ``collector``  — coleta eventos de sessões do Hermes em **modo somente observação**.
* ``detectors``  — detecta padrões e loops no fluxo de eventos.
* ``model``      — camada opcional de modelo local (Qwen3-0.6B) para classificação.
* ``report``     — gera o relatório de padrões e loops detectados.

Nenhum módulo escreve em ``state.db``, em ``config.yaml`` ou em qualquer arquivo
do Hermes. Ver ``docs/etica-e-limites.md``.
"""

__version__ = "0.1.0"

SCHEMA_VERSION = 1

__all__ = ["__version__", "SCHEMA_VERSION"]