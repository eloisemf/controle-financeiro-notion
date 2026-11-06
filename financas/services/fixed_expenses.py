"""Serviço de lançamento mensal de Gastos Fixos (`Status = Previsto`).

Lê os Gastos Fixos ativos diretamente do Notion, calcula a data exata de cobrança
que cai na competência `(ano, mes)` (respeitando o fechamento de cada cartão)
e insere na tabela `Transações` de forma 100% idempotente, com suporte a `dry_run`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from financas.config import AppConfig
from financas.domain.billing import CalculadoraFatura, formatar_mes_competencia
from financas.domain.models import (
    CartaoConfig,
    GastoFixo,
    LancamentoTransacao,
    Recorrencia,
    StatusTransacao,
    TipoTransacao,
)
from financas.notion.client import NotionClient
from financas.notion.repositories import NotionRepositories


@dataclass
class FixedExpensesReport:
    """Relatório do lançamento de gastos fixos previstos de um mês."""

    mes_competencia: str
    dry_run: bool = False
    ativos_encontrados: int = 0
    previstos_criados: list[str] = field(default_factory=list)
    datas_previstas_ajustadas: list[str] = field(default_factory=list)
    ignorados_ja_lancados: list[str] = field(default_factory=list)
    ignorados_incompletos: list[str] = field(default_factory=list)

    @property
    def tem_alteracoes(self) -> bool:
        return bool(self.previstos_criados or self.datas_previstas_ajustadas)

    def formatar_resumo(self) -> str:
        prefixo = "[DRY-RUN / SIMULAÇÃO] " if self.dry_run else ""
        acao_criados = "Previstos que seriam criados" if self.dry_run else "Previstos criados"
        linhas = [
            f"{prefixo}Competência: {self.mes_competencia} | Gastos fixos ativos lidos: {self.ativos_encontrados}",
            f"[+] {acao_criados} ({len(self.previstos_criados)}): {self.previstos_criados}",
            f"[=] Já lançados no mês ({len(self.ignorados_ja_lancados)}): {self.ignorados_ja_lancados}",
        ]
        if self.datas_previstas_ajustadas:
            linhas.append(
                f"[~] Datas previstas ajustadas ({len(self.datas_previstas_ajustadas)}): {self.datas_previstas_ajustadas}"
            )
        if self.ignorados_incompletos:
            linhas.append(
                f"[!] Ignorados por falta de conta/valor ({len(self.ignorados_incompletos)}): {self.ignorados_incompletos}"
            )
        return "\n".join(linhas)


class GastosFixosService:
    """Orquestra a geração mensal de lançamentos previstos a partir da tabela Gastos Fixos."""

    def __init__(
        self,
        client: NotionClient | None,
        config: AppConfig,
        calculadora: CalculadoraFatura | None = None,
        repos: NotionRepositories | None = None,
    ) -> None:
        self._cartoes_fallback = dict(config.cartoes)
        self._calc = calculadora or CalculadoraFatura()
        if repos is None:
            if client is None:
                raise ValueError("Informe `client` ou `repos` ao instanciar GastosFixosService.")
            repos = NotionRepositories.from_client(client, config)
        self._repos = repos
        self._cartoes_cache: dict[str, CartaoConfig] | None = None

    def _obter_cartoes(self) -> dict[str, CartaoConfig]:
        if self._cartoes_cache is None:
            if self._repos.catalogo is not None:
                self._cartoes_cache = self._repos.catalogo.obter_configuracao_cartoes(
                    self._cartoes_fallback
                )
            else:
                self._cartoes_cache = dict(self._cartoes_fallback)
        return self._cartoes_cache

    def _obter_cartao_se_aplicavel(self, pago_com: str) -> CartaoConfig | None:
        cartoes = self._obter_cartoes()
        cartao = cartoes.get(pago_com.strip())
        if cartao and cartao.ativo:
            return cartao
        return None

    def construir_lancamento_previsto(
        self, fixo: GastoFixo, ano: int, mes: int
    ) -> LancamentoTransacao:
        valor_efetivo = fixo.valor_lancamento_para_competencia(ano, mes)
        if not fixo.pago_com or valor_efetivo is None:
            raise ValueError(f"Gasto fixo '{fixo.nome}' sem conta/cartão ou valor definido.")

        mes_comp = formatar_mes_competencia(ano, mes)
        cartao = self._obter_cartao_se_aplicavel(fixo.pago_com)
        data_cobranca = self._calc.data_cobranca_para_competencia(fixo, ano, mes, cartao)

        return LancamentoTransacao(
            descricao=fixo.nome,
            valor=valor_efetivo,
            data_compra=data_cobranca,
            mes_competencia=mes_comp,
            tipo=TipoTransacao.SAIDA,
            categoria=fixo.categoria,
            conta_ou_cartao=fixo.pago_com,
            recorrencia=Recorrencia.FIXO,
            status=StatusTransacao.PREVISTO,
            gasto_fixo_id=fixo.page_id,
            uso_dinheiro=fixo.uso_dinheiro,
            pessoa=fixo.pessoa,
            valor_a_receber=fixo.valor_a_receber_para_competencia(ano, mes),
        )

    def lancar_previstos_do_mes(
        self, ano: int, mes: int, dry_run: bool = False
    ) -> FixedExpensesReport:
        mes_comp = formatar_mes_competencia(ano, mes)
        relatorio = FixedExpensesReport(mes_competencia=mes_comp, dry_run=dry_run)

        if not dry_run:
            self._repos.meses.garantir_mes(ano, mes)
        ativos = self._repos.gastos_fixos.listar_ativos()
        relatorio.ativos_encontrados = len(ativos)

        ja_vinculados = self._repos.transacoes.buscar_vinculos_gasto_fixo_no_mes(mes_comp)

        for fixo in sorted(ativos, key=lambda x: x.dia_cobranca or 99):
            if not fixo.pago_com or fixo.valor_lancamento is None or not fixo.page_id:
                relatorio.ignorados_incompletos.append(fixo.nome)
                continue

            lancamento = self.construir_lancamento_previsto(fixo, ano, mes)

            if fixo.page_id in ja_vinculados:
                transacao_id, status_atual, data_iso = ja_vinculados[fixo.page_id]
                if (
                    status_atual == StatusTransacao.PREVISTO.value
                    and data_iso != lancamento.data_compra.isoformat()
                ):
                    if not dry_run:
                        self._repos.transacoes.atualizar_data_previsto(
                            transacao_id, lancamento.data_compra
                        )
                    msg_ajuste = (
                        f"{fixo.nome} (Previsto - data ajustada: {data_iso} -> {lancamento.data_compra.isoformat()})"
                    )
                    relatorio.datas_previstas_ajustadas.append(msg_ajuste)
                    relatorio.ignorados_ja_lancados.append(msg_ajuste)
                else:
                    relatorio.ignorados_ja_lancados.append(f"{fixo.nome} ({status_atual})")
                continue

            if not dry_run:
                self._repos.transacoes.criar_lancamento(lancamento)
            relatorio.previstos_criados.append(
                f"{fixo.nome} | {lancamento.data_compra.isoformat()} | {fixo.pago_com} | R$ {lancamento.valor}"
            )

        return relatorio
