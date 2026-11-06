"""Serviço de aplicação para consolidação de investimentos, cotações e fechamentos."""

from __future__ import annotations

import datetime
import logging
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from financas.config import AppConfig
from financas.domain.billing import formatar_mes_competencia
from financas.domain.investments import ResumoCarteira, SnapshotPatrimonial
from financas.notion.builders import NotionParser
from financas.notion.client import NotionClient
from financas.notion.repositories.investimentos import InvestimentosRepository
from financas.notion.repositories.meses import MesesRepository
from financas.services.market_data import MarketDataService

logger = logging.getLogger(__name__)

_RE_TICKER_B3 = re.compile(r"\b[A-Z]{4}\d{1,2}F?\b")
_RE_TESOURO_ANO = re.compile(r"(tesouro\s+[a-zA-ZÀ-ÿ\+]+)\s+(\d{2}/\d{2}/\d{4}|\d{4})", re.IGNORECASE)

DEFAULT_MAPA_TESOURO: dict[str, tuple[str, str]] = {
    "Tesouro Selic 2031": ("Tesouro Selic", "01/03/2031"),
    "Tesouro IPCA+ 2032": ("Tesouro IPCA+", "15/08/2032"),
    "IPCA+": ("Tesouro IPCA+", "15/08/2032"),
}


class InvestimentosService:
    """Orquestrador do módulo de investimentos integrado com a API do Notion."""

    def __init__(
        self,
        client: NotionClient,
        config: AppConfig,
        market_data: MarketDataService | None = None,
        investimentos_repo: InvestimentosRepository | None = None,
        meses_repo: MesesRepository | None = None,
        mapa_vencimentos_tesouro: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self._investimentos_repo = investimentos_repo or InvestimentosRepository(
            client, config.databases
        )
        self._meses_repo = meses_repo or MesesRepository(client, config.databases.meses)
        self.market_data = market_data or MarketDataService()
        self._mapa_tesouro = {**DEFAULT_MAPA_TESOURO, **(mapa_vencimentos_tesouro or {})}

    def obter_resumo_carteira(self) -> ResumoCarteira:
        """Retorna o panorama consolidado dos ativos na carteira."""
        ativos = self._investimentos_repo.listar_ativos_na_carteira()
        return ResumoCarteira(ativos=tuple(ativos))

    def _identificar_titulo_tesouro(self, nome: str) -> tuple[str, str] | None:
        if nome in self._mapa_tesouro:
            return self._mapa_tesouro[nome]
        m = _RE_TESOURO_ANO.search(nome)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        return None

    def _eh_renda_variavel(self, nome: str, tipo: str) -> bool:
        return (
            "Ações" in tipo
            or "Renda Variável" in tipo
            or bool(_RE_TICKER_B3.search(nome.upper()))
        )

    def sincronizar_aportes_carteira(self, dry_run: bool = False) -> list[dict[str, Any]]:
        """Recalcula `Qtd` e `Preço de Compra` em `Meus Investimentos` a partir de `Aportes`."""
        resumo = self.obter_resumo_carteira()
        aportes = self._investimentos_repo.listar_aportes()

        aportes_por_ativo: dict[str, list[Any]] = {}
        for ap in aportes:
            aportes_por_ativo.setdefault(NotionParser.normalize_id(ap.investimento_id), []).append(ap)

        sincronizados: list[dict[str, Any]] = []
        for ativo in resumo.ativos:
            norm_id = NotionParser.normalize_id(ativo.id)
            lista = aportes_por_ativo.get(norm_id, [])
            if not lista:
                continue

            total_aportado = sum((a.fluxo_financeiro for a in lista), Decimal("0.00")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            qtd_liquida = sum((a.fluxo_quantidade for a in lista), Decimal("0")).quantize(
                Decimal("0.0001")
            )

            if self._eh_renda_variavel(ativo.nome, ativo.tipo) and qtd_liquida > 0:
                nova_qtd = qtd_liquida.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                novo_pm = (total_aportado / qtd_liquida).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            else:
                nova_qtd = Decimal("1.00")
                novo_pm = total_aportado

            nova_fracao = ativo.fracao_titulos
            diff_aporte = total_aportado - ativo.valor_investido
            match_td = self._identificar_titulo_tesouro(ativo.nome)
            if match_td and ativo.fracao_titulos and abs(diff_aporte) >= Decimal("0.01"):
                cotacao_td = self.market_data.obter_pu_tesouro_direto(match_td[0], match_td[1])
                if cotacao_td and cotacao_td.preco_unitario > 0:
                    delta_titulos = (diff_aporte / cotacao_td.preco_unitario).quantize(
                        Decimal("0.000001"), rounding=ROUND_HALF_UP
                    )
                    nova_fracao = ativo.fracao_titulos + delta_titulos

            if not dry_run:
                self._investimentos_repo.atualizar_posicao_carteira(
                    ativo.id,
                    qtd=nova_qtd,
                    preco_compra=novo_pm,
                    fracao_titulos=nova_fracao,
                )

            sincronizados.append(
                {
                    "ativo": ativo.nome,
                    "qtd": nova_qtd,
                    "preco_medio": novo_pm,
                    "total_investido": total_aportado,
                    "fracao_titulos": nova_fracao,
                }
            )
        return sincronizados

    def atualizar_cotacoes_carteira(self, dry_run: bool = False) -> list[dict[str, Any]]:
        """Atualiza cotações de mercado para ativos de Renda Variável e Tesouro Direto."""
        resumo = self.obter_resumo_carteira()
        atualizacoes: list[dict[str, Any]] = []

        for ativo in resumo.ativos:
            nome = ativo.nome
            tipo = ativo.tipo
            qtd = ativo.qtd
            ativo_id = ativo.id

            # 1. Renda Variável / Ações / FIIs
            if self._eh_renda_variavel(nome, tipo):
                cotacao = self.market_data.obter_cotacao_acao(nome)
                if cotacao:
                    novo_total = (cotacao.preco_unitario * qtd).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    atualizacoes.append(
                        {
                            "ativo": nome,
                            "tipo": tipo,
                            "anterior": ativo.valor_atual,
                            "novo": novo_total,
                            "pu": cotacao.preco_unitario,
                            "fonte": cotacao.fonte,
                        }
                    )
                    if not dry_run:
                        self._investimentos_repo.atualizar_valor_atual(ativo_id, novo_total)
                        logger.info("Notion atualizado: %s = R$ %s", nome, novo_total)

            # 2. Tesouro Direto (Selic e IPCA+)
            else:
                match_info = self._identificar_titulo_tesouro(nome)
                if match_info:
                    tit_nome, tit_venc = match_info
                    cotacao_td = self.market_data.obter_pu_tesouro_direto(tit_nome, tit_venc)
                    if cotacao_td:
                        fator = ativo.fracao_titulos
                        if fator is None and qtd != Decimal("1.00") and qtd > 0:
                            fator = qtd

                        registro: dict[str, Any] = {
                            "ativo": nome,
                            "tipo": tipo,
                            "anterior": ativo.valor_atual,
                            "pu": cotacao_td.preco_unitario,
                            "fonte": cotacao_td.fonte,
                            "data_base": cotacao_td.data_referencia,
                        }
                        if fator is not None and fator > 0:
                            novo_td = (cotacao_td.preco_unitario * fator).quantize(
                                Decimal("0.01"), rounding=ROUND_HALF_UP
                            )
                            registro["novo"] = novo_td
                            registro["fracao_titulos"] = fator
                            if not dry_run:
                                self._investimentos_repo.atualizar_valor_atual(ativo_id, novo_td)
                                logger.info(
                                    "Notion atualizado (Tesouro): %s = R$ %s (PU %s x %s)",
                                    nome,
                                    novo_td,
                                    cotacao_td.preco_unitario,
                                    fator,
                                )
                        atualizacoes.append(registro)

        return atualizacoes

    @staticmethod
    def _mes_anterior(ano: int, mes: int) -> tuple[int, int]:
        if mes == 1:
            return ano - 1, 12
        return ano, mes - 1

    def fechar_mes_snapshot(
        self, data_foto: str | None = None, dry_run: bool = False
    ) -> dict[str, Any]:
        """Tira a foto consolidada de fechamento de todos os ativos da carteira no Notion (Idempotente)."""
        data_str = data_foto or datetime.date.today().isoformat()
        partes = data_str.split("-")
        ano, mes_num = int(partes[0]), int(partes[1])
        nome_mes_alvo = formatar_mes_competencia(ano, mes_num)

        logger.info("Iniciando fechamento mensal: %s (Data: %s)", nome_mes_alvo, data_str)

        mes_page_id: str | None = None
        if not dry_run:
            mes_page_id, _ = self._meses_repo.garantir_mes(ano, mes_num)
        elif nome_mes_alvo in self._meses_repo.meses_por_nome:
            mes_page_id = self._meses_repo.meses_por_nome[nome_mes_alvo]

        existentes: dict[str, str] = {}
        if mes_page_id:
            existentes = self._investimentos_repo.buscar_snapshots_do_mes(mes_page_id)

        resumo = self.obter_resumo_carteira()
        created_snaps: list[dict[str, Any]] = []
        qtd_criados = 0
        qtd_atualizados = 0

        for ativo in resumo.ativos:
            reg_titulo = f"{ativo.nome} - {data_str}"
            norm_inv_id = NotionParser.normalize_id(ativo.id)
            existing_snap_id = existentes.get(norm_inv_id)
            acao = "updated" if existing_snap_id else "created"

            snapshot = SnapshotPatrimonial(
                registro=reg_titulo,
                data_foto=data_str,
                investimento_id=ativo.id,
                valor_no_dia=ativo.valor_atual,
                mes_id=mes_page_id,
                qtd_cotas=ativo.fracao_titulos or ativo.qtd,
            )

            if not dry_run and mes_page_id:
                if existing_snap_id:
                    page_id = self._investimentos_repo.atualizar_snapshot(
                        existing_snap_id, snapshot
                    )
                    qtd_atualizados += 1
                else:
                    page_id = self._investimentos_repo.criar_snapshot(snapshot)
                    qtd_criados += 1
                created_snaps.append({"id": page_id, "acao": acao, **snapshot.to_dict()})
            else:
                if existing_snap_id:
                    qtd_atualizados += 1
                else:
                    qtd_criados += 1
                created_snaps.append({"acao": acao, **snapshot.to_dict()})

        # Sincronizar aportes do mês e calcular evolução / rendimento real
        aportes_mes = Decimal("0.00")
        if mes_page_id:
            aportes_mes = self._investimentos_repo.sincronizar_aportes_do_mes(
                ano, mes_num, mes_page_id, dry_run=dry_run
            )

        ano_ant, mes_ant = self._mes_anterior(ano, mes_num)
        nome_mes_ant = formatar_mes_competencia(ano_ant, mes_ant)
        mes_ant_id = self._meses_repo.meses_por_nome.get(nome_mes_ant)
        patrimonio_anterior: Decimal | None = None
        rendimento_mes: Decimal | None = None

        if mes_ant_id:
            patrimonio_anterior = self._investimentos_repo.obter_patrimonio_do_mes(mes_ant_id)
            if patrimonio_anterior is not None:
                rendimento_mes = (
                    resumo.patrimonio_total - patrimonio_anterior - aportes_mes
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if not dry_run and mes_page_id:
                    self._investimentos_repo.atualizar_patrimonio_anterior_mes(
                        mes_page_id, patrimonio_anterior
                    )

        if not dry_run and mes_page_id:
            snap_ids = [str(s["id"]) for s in created_snaps if "id" in s]
            self._investimentos_repo.vincular_snapshots_ao_mes(mes_page_id, snap_ids)

        return {
            "mes": nome_mes_alvo,
            "data_foto": data_str,
            "total_patrimonio": resumo.patrimonio_total,
            "patrimonio_anterior": patrimonio_anterior,
            "aportes_mes": aportes_mes,
            "rendimento_mes": rendimento_mes,
            "total_ativos": len(created_snaps),
            "criados": qtd_criados,
            "atualizados": qtd_atualizados,
            "snapshots": created_snaps,
        }
