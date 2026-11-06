"""Serviço de provisionamento e migração idempotente das databases no Notion."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
import json
import logging
from pathlib import Path

from financas.config import AppConfig
from financas.domain.billing import adicionar_meses, formatar_mes_competencia
from financas.domain.models import GastoFixo, Periodicidade, Pessoa
from financas.notion.client import NotionClient
from financas.notion.repositories import NotionRepositories

logger = logging.getLogger(__name__)

ARQUIVO_SEED_PADRAO = Path(__file__).resolve().parents[2] / "seed.json"
ARQUIVO_SEED_EXEMPLO = Path(__file__).resolve().parents[2] / "seed.example.json"


def carregar_seed_json(
    caminho: Path | str | None = None,
) -> tuple[tuple[Pessoa, ...], tuple[GastoFixo, ...]]:
    """Carrega dados iniciais (`pessoas` e `gastos_fixos`) a partir de `seed.json` ou `seed.example.json`."""
    if caminho is not None:
        alvo = Path(caminho)
    elif ARQUIVO_SEED_PADRAO.is_file():
        alvo = ARQUIVO_SEED_PADRAO
    else:
        alvo = ARQUIVO_SEED_EXEMPLO

    if not alvo.is_file():
        return (), ()

    try:
        dados = json.loads(alvo.read_text(encoding="utf-8"))
        pessoas = tuple(
            Pessoa(
                nome=p["nome"],
                nome_no_pix_fatura=p.get("nome_no_pix_fatura", ""),
                saldo_inicial=Decimal(str(p.get("saldo_inicial", "0.00"))),
                observacao=p.get("observacao", ""),
            )
            for p in dados.get("pessoas", [])
        )
        gastos = tuple(
            GastoFixo(
                nome=g["nome"],
                nome_na_fatura=g.get("nome_na_fatura", ""),
                valor_meu=Decimal(str(g["valor_meu"])) if g.get("valor_meu") is not None else None,
                valor_cobrado=Decimal(str(g["valor_cobrado"])) if g.get("valor_cobrado") is not None else None,
                valor_a_receber=Decimal(str(g["valor_a_receber"])) if g.get("valor_a_receber") is not None else None,
                categoria=g.get("categoria", "Diversos"),
                uso_dinheiro=g.get("uso_dinheiro"),
                pago_com=g.get("pago_com"),
                pessoa=g.get("pessoa"),
                dia_cobranca=g.get("dia_cobranca"),
                periodicidade=Periodicidade(g.get("periodicidade", "Mensal")),
                ativo=bool(g.get("ativo", True)),
                observacao=g.get("observacao", ""),
            )
            for g in dados.get("gastos_fixos", [])
        )
        return pessoas, gastos
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("Falha ao carregar arquivo de seed (%s): %s", alvo, exc)
        return (), ()


@dataclass
class SetupReport:
    """Relatório estruturado da execução do setup no Notion."""

    pessoas_db_id: str = ""
    pessoas_db_criada: bool = False
    pessoas_criadas: list[str] = field(default_factory=list)
    pessoas_atualizadas: list[str] = field(default_factory=list)
    gastos_fixos_db_id: str = ""
    gastos_fixos_db_criada: bool = False
    colunas_transacoes_criadas: list[str] = field(default_factory=list)
    coluna_lancamentos_renomeada: bool = False
    coluna_pessoas_transacoes_renomeada: bool = False
    formulas_meses_faltantes: list[str] = field(default_factory=list)
    meses_criados: list[str] = field(default_factory=list)
    intervalo_meses_verificado: str = ""
    gastos_fixos_criados: list[str] = field(default_factory=list)
    gastos_fixos_atualizados: list[str] = field(default_factory=list)

    def formatar_resumo(self) -> str:
        linhas = [
            f"[{'+' if self.pessoas_db_criada else '='}] Database Pessoas: {self.pessoas_db_id}",
            f"[+] Pessoas criadas ({len(self.pessoas_criadas)}): {self.pessoas_criadas}",
            f"[~] Pessoas atualizadas ({len(self.pessoas_atualizadas)}): {self.pessoas_atualizadas}",
            f"[{'+' if self.gastos_fixos_db_criada else '='}] Database Gastos Fixos: {self.gastos_fixos_db_id}",
            (
                f"[+] Transações: colunas/opções adicionadas {self.colunas_transacoes_criadas}"
                if self.colunas_transacoes_criadas
                else "[=] Transações: colunas 'Status', 'Gasto Fixo', 'Pessoa', 'Valor a receber' e 'Reembolso' já presentes"
            ),
            (
                f"[!] Meses: fórmulas manuais pendentes {self.formulas_meses_faltantes}"
                if self.formulas_meses_faltantes
                else "[=] Meses: todas as fórmulas base do painel estão presentes"
            ),
            (
                f"[+] Meses criados ({len(self.meses_criados)}): {self.meses_criados}"
                if self.meses_criados
                else f"[=] Meses ({self.intervalo_meses_verificado}) já existentes"
            ),
            f"[+] Gastos fixos criados ({len(self.gastos_fixos_criados)}): {self.gastos_fixos_criados}",
            f"[~] Gastos fixos atualizados ({len(self.gastos_fixos_atualizados)}): {self.gastos_fixos_atualizados}",
        ]
        return "\n".join(linhas)


class WorkspaceSetupService:
    """Orquestra a configuração idempotente das databases no Notion."""

    def __init__(
        self,
        client: NotionClient | None,
        config: AppConfig,
        repos: NotionRepositories | None = None,
    ) -> None:
        self._cartoes = dict(config.cartoes)
        if repos is None:
            if client is None:
                raise ValueError("Informe `client` ou `repos` ao instanciar WorkspaceSetupService.")
            repos = NotionRepositories.from_client(client, config)
        self._repos = repos

    def executar(
        self,
        inicio_ano_mes: tuple[int, int] | None = None,
        fim_ano_mes: tuple[int, int] | None = None,
        pessoas_iniciais: tuple[Pessoa, ...] | None = None,
        gastos_iniciais: tuple[GastoFixo, ...] | None = None,
    ) -> SetupReport:
        hoje = date.today()
        inicio = inicio_ano_mes or (hoje.year, 1)
        fim = fim_ano_mes or (hoje.year + 1, 12)
        if pessoas_iniciais is None or gastos_iniciais is None:
            seed_pessoas, seed_gastos = carregar_seed_json()
            pessoas_ativas = pessoas_iniciais if pessoas_iniciais is not None else seed_pessoas
            gastos_ativos = gastos_iniciais if gastos_iniciais is not None else seed_gastos
        else:
            pessoas_ativas = pessoas_iniciais
            gastos_ativos = gastos_iniciais

        relatorio = SetupReport(
            intervalo_meses_verificado=(
                f"{formatar_mes_competencia(*inicio)} a {formatar_mes_competencia(*fim)}"
            )
        )

        # 1. Garantir database Pessoas e sincronizar pessoas iniciais
        pessoas_id, pessoas_criada = self._repos.pessoas.garantir_database()
        relatorio.pessoas_db_id = pessoas_id
        relatorio.pessoas_db_criada = pessoas_criada
        p_criadas, p_atualizadas = self._repos.pessoas.sincronizar_pessoas(
            pessoas_id, pessoas_ativas
        )
        relatorio.pessoas_criadas = p_criadas
        relatorio.pessoas_atualizadas = p_atualizadas

        # 2. Garantir database Gastos Fixos (com colunas Pessoa e Valor a receber)
        gf_id, gf_criada = self._repos.gastos_fixos.garantir_database(pessoas_db_id=pessoas_id)
        relatorio.gastos_fixos_db_id = gf_id
        relatorio.gastos_fixos_db_criada = gf_criada

        # 3. Garantir colunas Status, Gasto Fixo, Pessoa, Valor a receber e Reembolso em Transações
        relatorio.colunas_transacoes_criadas = (
            self._repos.transacoes.garantir_colunas_conciliacao(
                gastos_fixos_db_id=gf_id, pessoas_db_id=pessoas_id
            )
        )
        relatorio.coluna_lancamentos_renomeada = (
            self._repos.gastos_fixos.renomear_coluna_relacao_transacoes(gf_id)
        )
        relatorio.coluna_pessoas_transacoes_renomeada = (
            self._repos.pessoas.renomear_coluna_relacao_transacoes(pessoas_id)
        )

        # 4. Verificar fórmulas da tabela Meses
        relatorio.formulas_meses_faltantes = (
            self._repos.meses.verificar_formulas_faltantes(self._cartoes)
        )

        # 5. Garantir meses no intervalo solicitado
        ano, mes = inicio
        while (ano, mes) <= fim:
            _, criado = self._repos.meses.garantir_mes(ano, mes)
            if criado:
                relatorio.meses_criados.append(formatar_mes_competencia(ano, mes))
            ano, mes = adicionar_meses(ano, mes, 1)

        # 6. Sincronizar (upsert) itens de Gastos Fixos
        criados, atualizados = self._repos.gastos_fixos.sincronizar_gastos_fixos(
            gf_id, gastos_ativos
        )
        relatorio.gastos_fixos_criados = criados
        relatorio.gastos_fixos_atualizados = atualizados

        return relatorio
