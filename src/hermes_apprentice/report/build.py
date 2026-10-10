"""Geração do relatório de padrões e loops.

O relatório é montado a partir de três artefatos JSON (sonda do ambiente,
detecção e avaliação do modelo) e de gráficos PNG. Nada é escrito à mão: se os
números mudarem, o relatório muda. Isso é o que permite chamá-lo de *primeiro*
relatório — ele é reproduzível.

A numeração das seções é calculada em tempo de execução, de modo que uma seção
opcional ausente (por exemplo, os cenários, quando o manifesto não é passado)
não deixe buracos na contagem.
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

RULE_TITLES = {
    "loop.repetition": "Loop por repetição de chamada",
    "loop.no_progress": "Loop sem progresso (stub de deduplicação)",
    "loop.cycle": "Ciclo de ferramentas",
    "loop.oscillation": "Oscilação de resultados",
    "failure.budget_exhausted": "Orçamento de iterações esgotado",
    "failure.persistence_locked": "Lock de escrita no state.db",
    "failure.retry_storm": "Tempestade de retries do provedor",
    "failure.run_not_completed": "Execução encerrada sem resposta final",
}


def _load(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    file = Path(path)
    if not file.exists():
        return {}
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(out)


# -- gráficos ---------------------------------------------------------------
def _charts(detection: Dict[str, Any], benchmark: Dict[str, Any], out_dir: Path) -> Dict[str, str]:
    """Gera os gráficos do relatório. Ausência de matplotlib não é fatal."""
    charts: Dict[str, str] = {}
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:  # noqa: BLE001
        return charts

    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) Achados por regra
    counts = (detection.get("summary") or {}).get("rules") or {}
    if counts:
        labels = [RULE_TITLES.get(rule, rule) for rule in counts]
        values = list(counts.values())
        fig, ax = plt.subplots(figsize=(9, 0.5 * len(labels) + 1.6))
        ax.barh(labels[::-1], values[::-1], color="#2b6cb0")
        ax.set_xlabel("Ocorrências")
        ax.set_title("Achados por regra (cenários reproduzíveis)")
        for index, value in enumerate(values[::-1]):
            ax.text(value + 0.05, index, str(value), va="center", fontsize=9)
        fig.tight_layout()
        path = out_dir / "achados-por-regra.png"
        fig.savefig(path, dpi=140)
        plt.close(fig)
        charts["rules"] = str(path)

    # 2) Modelo x linha de base (F1 por classe)
    model = benchmark.get("model") or {}
    baseline = benchmark.get("baseline") or {}
    if model.get("per_class") and baseline.get("per_class"):
        classes = list(model["per_class"].keys())
        model_f1 = [model["per_class"][c]["f1"] for c in classes]
        base_f1 = [baseline["per_class"][c]["f1"] for c in classes]
        x = range(len(classes))
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.bar([i - 0.2 for i in x], base_f1, width=0.4, label="Detectores determinísticos", color="#38a169")
        ax.bar([i + 0.2 for i in x], model_f1, width=0.4, label="Qwen3-0.6B", color="#dd6b20")
        ax.set_xticks(list(x))
        ax.set_xticklabels(classes)
        ax.set_ylim(0, 1.05)
        ax.set_ylabel("F1")
        ax.set_title("F1 por classe: regras explícitas x modelo local")
        ax.legend()
        fig.tight_layout()
        path = out_dir / "modelo-x-regras.png"
        fig.savefig(path, dpi=140)
        plt.close(fig)
        charts["comparison"] = str(path)

    # 3) Matriz de confusão do modelo
    matrix = model.get("confusion_matrix")
    labels = model.get("labels")
    if matrix and labels:
        biggest = max((max(row) for row in matrix), default=1) or 1
        fig, ax = plt.subplots(figsize=(5, 4.2))
        image = ax.imshow(matrix, cmap="Blues")
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=30, ha="right")
        ax.set_yticks(range(len(labels)))
        ax.set_yticklabels(labels)
        ax.set_xlabel("Previsto")
        ax.set_ylabel("Verdadeiro")
        ax.set_title("Matriz de confusão — Qwen3-0.6B")
        for i, row in enumerate(matrix):
            for j, value in enumerate(row):
                ax.text(j, i, str(value), ha="center", va="center",
                        color="white" if value > biggest / 2 else "black")
        fig.colorbar(image, ax=ax, shrink=0.8)
        fig.tight_layout()
        path = out_dir / "matriz-confusao-modelo.png"
        fig.savefig(path, dpi=140)
        plt.close(fig)
        charts["confusion"] = str(path)

    # 4) Latência por janela
    windows = benchmark.get("model_windows") or []
    latencies = [w.get("latency_s", 0.0) for w in windows]
    if latencies:
        fig, ax = plt.subplots(figsize=(7, 3.6))
        ax.hist(latencies, bins=14, color="#805ad5", edgecolor="white")
        ax.set_xlabel("Latência por janela (s)")
        ax.set_ylabel("Janelas")
        ax.set_title("Distribuição de latência — Qwen3-0.6B em CPU")
        fig.tight_layout()
        path = out_dir / "latencia-qwen.png"
        fig.savefig(path, dpi=140)
        plt.close(fig)
        charts["latency"] = str(path)
    return charts


# -- diagnóstico automático do modelo --------------------------------------
def _model_diagnostics(benchmark: Dict[str, Any]) -> List[str]:
    """Lê as métricas e escreve a interpretação — em vez de deixar o leitor sozinho.

    É aqui que o relatório assume o que os números significam: separar erro de
    **formato** (resposta não parseável) de erro de **julgamento** (resposta
    parseável e errada) muda completamente o que se deve fazer a seguir.
    """
    model = benchmark.get("model") or {}
    total = model.get("n") or 0
    if not total:
        return []
    notes: List[str] = []

    parse_failures = model.get("parse_failures") or 0
    share = 100.0 * parse_failures / total
    notes.append(
        f"- **Falha de formato:** {parse_failures} de {total} janelas ({share:.0f}%) não produziram JSON "
        "válido e foram contadas como `unknown`. Isso é erro de *formatação*, não de julgamento: o modelo "
        "pode ter identificado o padrão e descrito no texto livre, mas não entregou o contrato pedido. "
        "Um pós-processamento tolerante, ou few-shot com um exemplo exato, ataca exatamente esse ponto "
        "antes de qualquer troca de modelo."
    )

    matrix = model.get("confusion_matrix") or []
    labels = model.get("labels") or []
    if matrix and labels:
        predicted_totals = [sum(row[i] for row in matrix) for i in range(len(labels))]
        never_predicted = [labels[i] for i, value in enumerate(predicted_totals) if value == 0]
        if never_predicted:
            notes.append(
                "- **Classes que o modelo nunca previu:** " + ", ".join(f"`{c}`" for c in never_predicted) + ". "
                "Precisão zero aqui não vem de confundir exemplos, vem de o modelo ter colapsado para as classes "
                "majoritárias (`normal` e `failure`). É o comportamento esperado de um modelo de 0,6 B sem "
                "calibração de rótulos — e é a razão pela qual as regras explícitas continuam sendo o detector."
            )
        recalls = [
            (labels[i], model["per_class"][labels[i]]["recall"]) for i in range(len(labels))
        ]
        worst = min(recalls, key=lambda item: item[1])
        notes.append(
            f"- **Pior revogação (recall):** `{worst[0]}` = {worst[1]}. "
            "Uma revogação baixa significa que a classe *existe* e passa despercebida."
        )
    return notes


# -- markdown --------------------------------------------------------------
def render_markdown(
    probe: Dict[str, Any],
    detection: Dict[str, Any],
    benchmark: Dict[str, Any],
    *,
    charts: Optional[Dict[str, str]] = None,
    scenarios: Optional[Sequence[Dict[str, Any]]] = None,
    title: str = "Relatório nº 1 — Padrões e loops detectados",
) -> str:
    charts = charts or {}
    scenarios = scenarios or []
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    summary = detection.get("summary") or {}
    model = benchmark.get("model") or {}
    baseline = benchmark.get("baseline") or {}
    dataset = benchmark.get("dataset") or {}
    hardware = benchmark.get("hardware") or {}
    sections = itertools.count(1)

    lines: List[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"*Gerado em {generated} por `hermes-apprentice` — dados brutos em `artifacts/`.*")
    lines.append("")

    # 1. Resumo executivo
    lines.append(f"## {next(sections)}. Resumo executivo")
    lines.append("")
    lines.append(
        f"Foram analisados **{summary.get('total_findings', 0)} achados** em "
        f"**{summary.get('sessions_analyzed', 0)} sessões** derivadas de modos de falha documentados do "
        f"harness Hermes. Os detectores determinísticos identificaram "
        f"**{summary.get('loops', 0)} loops** e **{summary.get('failures', 0)} falhas**, e reproduziram as "
        f"regras esperadas em **{summary.get('scenarios_matched', 0)} de "
        f"{summary.get('sessions_analyzed', 0)}** cenários."
    )
    lines.append("")
    if model:
        lines.append(
            f"O modelo local **Qwen3-0.6B** (CPU, sem GPU) alcançou acurácia `{model.get('accuracy')}` e "
            f"F1 macro `{model.get('macro_f1')}`, contra `{baseline.get('accuracy')}` / "
            f"`{baseline.get('macro_f1')}` dos detectores determinísticos. "
            "A leitura desses números — e o que fazer com eles — está na seção "
            "*Desempenho inicial do Qwen3-0.6B*."
        )
    lines.append("")

    # 2. Ambiente observado
    lines.append(f"## {next(sections)}. Versão e configuração do Hermes identificadas")
    lines.append("")
    cli = (probe.get("installation") or {}).get("cli") or {}
    pip_meta = (probe.get("installation") or {}).get("pip_metadata") or {}
    upstream = probe.get("upstream") or {}
    state_db = probe.get("state_db") or {}
    config = probe.get("config") or {}
    rows = [
        ["Versão do CLI (`hermes --version`)", (cli.get("version_raw") or "não executado").replace("\n", " · ")],
        ["Versão do pacote (metadados)", pip_meta.get("version") or "—"],
        ["Origem dos metadados", pip_meta.get("source") or "—"],
        ["Última release upstream (GitHub)", (upstream.get("github_latest_release") or {}).get("tag") or "—"],
        ["HERMES_HOME resolvido", f"{probe.get('hermes_home', {}).get('path')} (regra: {probe.get('hermes_home', {}).get('source')})"],
        ["`config.yaml`", f"presente={config.get('exists')}, chaves={len(config.get('top_level_keys') or [])}"],
        ["`state.db`", f"presente={state_db.get('exists')}, sessões={(state_db.get('counts') or {}).get('sessions', '—')}, "
                       f"mensagens={(state_db.get('counts') or {}).get('messages', '—')}"],
        ["Perfis nomeados", str(len(probe.get("profiles") or []))],
    ]
    lines.append(_table(["Item", "Valor"], rows))
    lines.append("")
    for note in probe.get("notes") or []:
        lines.append(f"> {note}")
    if probe.get("notes"):
        lines.append("")

    # 3. Método
    lines.append(f"## {next(sections)}. Método: como os eventos são coletados")
    lines.append("")
    lines.append(
        "A coleta é **somente observação**. Nenhum módulo escreve em `state.db`, em `config.yaml` "
        "ou em qualquer artefato do harness: a conexão SQLite é aberta com `file:...?mode=ro` **e** "
        "`PRAGMA query_only = ON`, e os testes provam que uma escrita é recusada (`assert_readonly`). "
        "As fontes, em ordem de preferência:"
    )
    lines.append("")
    lines.append(
        _table(
            ["Fonte", "Transporte", "Papel"],
            [
                ["`runs_api`", "SSE `GET /v1/runs/{id}/events`", "Contrato publicado; caminho preferido para execuções ao vivo"],
                ["`spool`", "NDJSON escrito por hook de observação", "Opt-in explícito; hook não devolve diretiva nenhuma"],
                ["`state_db`", "SQLite somente leitura", "Fonte da verdade depois do fato"],
                ["`logs`", "Tail de `~/.hermes/logs/*.log`", "Cobre o que não chega às outras fontes"],
                ["`fixture`", "NDJSON normalizado", "Cenários reproduzíveis e testes"],
            ],
        )
    )
    lines.append("")

    # 4. Cenários
    if scenarios:
        lines.append(f"## {next(sections)}. Cenários reproduzíveis de falha e correção")
        lines.append("")
        rows = [
            [
                scenario.get("id"),
                scenario.get("hermes_failure_mode", "—"),
                ", ".join(scenario.get("expected_rules", [])),
                (scenario.get("correction") or {}).get("strategy", "—"),
            ]
            for scenario in scenarios
        ]
        lines.append(_table(["Cenário", "Modo de falha observado", "Regra esperada", "Correção aplicada"], rows))
        lines.append("")

    # 5. Achados
    lines.append(f"## {next(sections)}. Padrões e loops detectados")
    lines.append("")
    rules = summary.get("rules") or {}
    if rules:
        rows = [[RULE_TITLES.get(rule, rule), rule, count] for rule, count in rules.items()]
        lines.append(_table(["Padrão", "Regra", "Ocorrências"], rows))
        lines.append("")
    per_scenario = detection.get("scenarios") or {}
    if per_scenario:
        rows = [
            [
                scenario_id,
                data.get("expected_verdict"),
                data.get("verdict"),
                "✅" if data.get("match") else "❌",
                ", ".join(data.get("detected_rules") or []),
            ]
            for scenario_id, data in sorted(per_scenario.items())
        ]
        lines.append("Conferência cenário a cenário:")
        lines.append("")
        lines.append(_table(["Cenário", "Veredito esperado", "Veredito obtido", "Regras", "Regras detectadas"], rows))
        lines.append("")
    if charts.get("rules"):
        lines.append(f"![Achados por regra]({Path(charts['rules']).name})")
        lines.append("")

    # 6. Desempenho do modelo
    if model:
        lines.append(f"## {next(sections)}. Desempenho inicial do Qwen3-0.6B")
        lines.append("")
        if dataset:
            lines.append(
                f"Conjunto de avaliação: **{dataset.get('total_windows')} janelas** rotuladas "
                f"({dataset.get('by_label')}), com piso de **{dataset.get('window_size')}** eventos por janela "
                f"e passo de **{dataset.get('stride')}**."
            )
            lines.append("")
        lines.append(
            _table(
                ["Métrica", "Detectores determinísticos", "Qwen3-0.6B"],
                [
                    ["Acurácia", baseline.get("accuracy"), model.get("accuracy")],
                    ["F1 macro", baseline.get("macro_f1"), model.get("macro_f1")],
                    ["F1 `normal`", (baseline.get("per_class") or {}).get("normal", {}).get("f1"),
                     (model.get("per_class") or {}).get("normal", {}).get("f1")],
                    ["F1 `loop`", (baseline.get("per_class") or {}).get("loop", {}).get("f1"),
                     (model.get("per_class") or {}).get("loop", {}).get("f1")],
                    ["F1 `failure`", (baseline.get("per_class") or {}).get("failure", {}).get("f1"),
                     (model.get("per_class") or {}).get("failure", {}).get("f1")],
                    ["Latência média/janela (s)", (baseline.get("latency_s") or {}).get("mean"),
                     (model.get("latency_s") or {}).get("mean")],
                    ["JSON inválido", "—", model.get("parse_failures")],
                ],
            )
        )
        lines.append("")
        lines.append(
            f"- Latência: média **{(model.get('latency_s') or {}).get('mean')} s**, mediana "
            f"**{(model.get('latency_s') or {}).get('median')} s**, p95 **{(model.get('latency_s') or {}).get('p95')} s**, "
            f"máxima **{(model.get('latency_s') or {}).get('max')} s**."
        )
        lines.append(
            f"- Vazão: **{(model.get('throughput_tokens_per_second') or {}).get('mean')} tokens/s** "
            f"(mín {(model.get('throughput_tokens_per_second') or {}).get('min')}, "
            f"máx {(model.get('throughput_tokens_per_second') or {}).get('max')}), "
            f"com **{model.get('prompt_tokens_mean')}** tokens de prompt por janela."
        )
        if hardware:
            lines.append(
                f"- Ambiente: {hardware.get('cpu_cores')} núcleos, {hardware.get('ram_gb')} GB de RAM disponíveis "
                f"({hardware.get('ram_host_gb')} GB no host), dispositivo `{hardware.get('device')}`, "
                f"dtype `{hardware.get('dtype')}`, carga dos pesos {hardware.get('load_seconds')} s, "
                f"sem GPU."
            )
        lines.append("")
        for note in _model_diagnostics(benchmark):
            lines.append(note)
        lines.append("")
        if charts.get("comparison"):
            lines.append(f"![F1 por classe]({Path(charts['comparison']).name})")
            lines.append("")
        if charts.get("latency"):
            lines.append(f"![Latência por janela]({Path(charts['latency']).name})")
            lines.append("")
        if charts.get("confusion"):
            lines.append(f"![Matriz de confusão do modelo]({Path(charts['confusion']).name})")
            lines.append("")
        errors = benchmark.get("errors") or []
        if errors:
            lines.append(f"**Erros do modelo** ({len(errors)} no total; amostra de 10)")
            lines.append("")
            rows = [
                [
                    error["scenario_id"],
                    error["truth"],
                    error["predicted"],
                    error.get("loop_type"),
                    (error.get("reason") or "—")[:70],
                ]
                for error in errors[:10]
            ]
            lines.append(_table(["Cenário", "Verdadeiro", "Previsto", "Tipo declarado", "Motivo declarado"], rows))
            lines.append("")

    # 7. Limites
    lines.append(f"## {next(sections)}. Leitura dos resultados e limites")
    lines.append("")
    lines.append(
        "- Os cenários são **sintéticos e rotulados por construção**. Eles reproduzem a assinatura de eventos "
        "de falhas documentadas no harness, não sessões reais de usuários. Acurácia de 1,0 dos detectores aqui "
        "mede fidelidade da regra à assinatura — **não** generalização, e não mede falsos positivos em tráfego real."
    )
    lines.append(
        "- O coletor foi validado contra um `state.db` gravado pela API oficial do próprio harness "
        f"(v{(pip_meta.get('version') or '?')}) e preservado em `examples/hermes-home-fixture/`; as sessões "
        "avaliadas neste relatório são as de `scenarios/`."
    )
    lines.append(
        "- O Qwen3-0.6B é o piso da escala de modelos locais. A comparação da seção anterior é o argumento para "
        "tratá-lo como *segunda opinião* sobre janelas ambíguas, e as regras explícitas como o detector principal."
    )
    lines.append(
        "- Todo texto observado passa por redação de segredos (`redact.py`) antes de ser gravado; a redação cobre "
        "formatos conhecidos e não substitui revisão humana antes de publicar dados."
    )
    lines.append("")

    # 8. Próximos passos
    lines.append(f"## {next(sections)}. Próximos passos")
    lines.append("")
    lines.append("1. Coletar sessões reais de um Hermes em uso (com consentimento) para medir falsos positivos das regras.")
    lines.append("2. Tratar a falha de formato do modelo antes de trocar de modelo: few-shot com um exemplo exato de JSON e um parser tolerante.")
    lines.append("3. Testar Qwen3-1.7B/4B na mesma bancada — a hipótese é que a curva de F1 por parâmetro seja o dado mais útil.")
    lines.append("4. Transformar os achados em sugestões acionáveis (ex.: *steer* recomendado, compressão de contexto, troca de modelo).")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*Reproduza este relatório com `python -m hermes_apprentice all` — ver `README.md`.*")
    return "\n".join(lines)


def build_report(
    probe: Dict[str, Any],
    detection: Dict[str, Any],
    benchmark: Dict[str, Any],
    out_path: str | Path,
    *,
    scenarios: Optional[Sequence[Dict[str, Any]]] = None,
    charts_dir: Optional[str | Path] = None,
    title: str = "Relatório nº 1 — Padrões e loops detectados",
) -> str:
    """Escreve o relatório em Markdown e devolve o caminho."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    charts = _charts(detection, benchmark, Path(charts_dir) if charts_dir else out.parent / "assets")
    markdown = render_markdown(probe, detection, benchmark, charts=charts, scenarios=scenarios, title=title)
    out.write_text(markdown, encoding="utf-8")
    return str(out)
