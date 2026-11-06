"""Repositório das databases `Meus Investimentos`, `Aportes` e `Histórico de Saldos` no Notion."""

from __future__ import annotations

from decimal import Decimal
import logging
from typing import Any

from financas.config import NotionDatabaseIds
from financas.domain.investments import (
    AporteInvestimento,
    AtivoCarteira,
    SnapshotPatrimonial,
)
from financas.notion.builders import NotionParser, NotionPropertyBuilder as Prop
from financas.notion.client import NotionClient
from financas.notion.repositories.base import BaseNotionRepository
from financas.notion.schema import (
    ColunasAportes,
    ColunasHistoricoSaldos,
    ColunasInvestimentos,
    ColunasMeses,
)

logger = logging.getLogger(__name__)


class InvestimentosRepository(BaseNotionRepository):
    """Gerencia a leitura da carteira, aportes e gravação de snapshots patrimoniais no Notion."""

    def __init__(self, client: NotionClient, db_ids: NotionDatabaseIds) -> None:
        super().__init__(client)
        self._db_ids = db_ids

    def listar_ativos_na_carteira(self) -> list[AtivoCarteira]:
        """Retorna a lista de ativos com `Status = Na Carteira`."""
        pages = self._client.query_database(
            self._db_ids.meus_investimentos,
            filter_payload={
                "property": ColunasInvestimentos.STATUS,
                "status": {"equals": ColunasInvestimentos.STATUS_NA_CARTEIRA},
            },
        )
        ativos: list[AtivoCarteira] = []
        for p in pages:
            nome = NotionParser.page_title(p)
            tipo = NotionParser.prop_select(p, ColunasInvestimentos.TIPO) or "Outros"
            val_atual = NotionParser.prop_decimal(p, ColunasInvestimentos.VALOR_ATUAL) or Decimal("0.00")
            val_inv = (
                NotionParser.prop_decimal(p, ColunasInvestimentos.VALOR_INVESTIDO)
                or NotionParser.prop_decimal(p, ColunasInvestimentos.VALOR_TOTAL)
                or Decimal("0.00")
            )
            qtd = NotionParser.prop_decimal(p, ColunasInvestimentos.QTD) or Decimal("1.00")
            fracao_td = NotionParser.prop_decimal_raw(p, ColunasInvestimentos.FRACAO_TITULOS)
            ativos.append(
                AtivoCarteira(
                    id=p["id"],
                    nome=nome,
                    tipo=tipo,
                    qtd=qtd,
                    valor_atual=val_atual,
                    valor_investido=val_inv,
                    fracao_titulos=fracao_td,
                )
            )
        return ativos

    def atualizar_valor_atual(self, ativo_id: str, novo_total: Decimal) -> None:
        """Atualiza o campo `Valor Atual` de um ativo na carteira."""
        logger.debug("Atualizando valor atual do ativo %s para R$ %s", ativo_id, novo_total)
        self._client.update_page(
            ativo_id,
            properties={ColunasInvestimentos.VALOR_ATUAL: Prop.number_decimal(novo_total)},
        )

    def atualizar_posicao_carteira(
        self,
        ativo_id: str,
        qtd: Decimal,
        preco_compra: Decimal,
        fracao_titulos: Decimal | None = None,
    ) -> None:
        """Sincroniza `Qtd`, `Preço de Compra` e opcionalmente `Fração de Títulos` no Notion."""
        props: dict[str, Any] = {
            ColunasInvestimentos.QTD: Prop.number_decimal(qtd),
            ColunasInvestimentos.PRECO_COMPRA: Prop.number_decimal(preco_compra),
        }
        if fracao_titulos is not None:
            props[ColunasInvestimentos.FRACAO_TITULOS] = Prop.number_raw(fracao_titulos)
        self._client.update_page(ativo_id, properties=props)

    def listar_aportes(self) -> list[AporteInvestimento]:
        """Lista todos os registros de `Aportes & Movimentações`."""
        pages = self._client.query_database(self._db_ids.aportes)
        aportes: list[AporteInvestimento] = []
        for p in pages:
            inv_ids = NotionParser.prop_relation_ids(p, ColunasAportes.INVESTIMENTO)
            if not inv_ids:
                continue
            nome = NotionParser.page_title(p)
            data = NotionParser.prop_date_start(p, ColunasAportes.DATA)
            tipo = NotionParser.prop_select(p, ColunasAportes.TIPO) or "Compra / Aporte"
            qtd = NotionParser.prop_decimal_raw(p, ColunasAportes.QUANTIDADE) or Decimal("1")
            pu = NotionParser.prop_decimal(p, ColunasAportes.PRECO_UNITARIO) or Decimal("0.00")
            taxas = NotionParser.prop_decimal(p, ColunasAportes.TAXAS) or Decimal("0.00")
            obs = NotionParser.prop_rich_text(p, ColunasAportes.OBSERVACAO)
            aportes.append(
                AporteInvestimento(
                    nome=nome,
                    data=data,
                    tipo=tipo,
                    investimento_id=inv_ids[0],
                    quantidade=qtd,
                    preco_unitario=pu,
                    taxas=taxas,
                    observacao=obs,
                )
            )
        return aportes

    def buscar_snapshots_do_mes(self, mes_page_id: str) -> dict[str, str]:
        """Retorna mapa `{investimento_id_normalizado: snapshot_page_id}` já existentes no mês."""
        pages = self._client.query_database(
            self._db_ids.historico_saldos,
            filter_payload={
                "property": ColunasHistoricoSaldos.MES_COMPETENCIA,
                "relation": {"contains": mes_page_id},
            },
        )
        existentes: dict[str, str] = {}
        for p in pages:
            inv_ids = NotionParser.prop_relation_ids(p, ColunasHistoricoSaldos.INVESTIMENTO)
            if inv_ids:
                existentes[inv_ids[0]] = str(p["id"])
        return existentes

    def _montar_propriedades_snapshot(self, snapshot: SnapshotPatrimonial) -> dict[str, Any]:
        props: dict[str, Any] = {
            ColunasHistoricoSaldos.REGISTRO: Prop.title(snapshot.registro),
            ColunasHistoricoSaldos.DATA_FOTO: Prop.date_iso(snapshot.data_foto),
            ColunasHistoricoSaldos.INVESTIMENTO: Prop.relation(snapshot.investimento_id),
            ColunasHistoricoSaldos.VALOR_NO_DIA: Prop.number_decimal(snapshot.valor_no_dia),
            ColunasHistoricoSaldos.OBSERVACAO: Prop.rich_text(
                snapshot.observacao or f"Snapshot gerado em {snapshot.data_foto}"
            ),
        }
        if snapshot.mes_id:
            props[ColunasHistoricoSaldos.MES_COMPETENCIA] = Prop.relation(snapshot.mes_id)
        if snapshot.qtd_cotas is not None:
            props[ColunasHistoricoSaldos.QTD_COTAS] = Prop.number_decimal(snapshot.qtd_cotas)
        return props

    def criar_snapshot(self, snapshot: SnapshotPatrimonial) -> str:
        """Cria um registro na database `Histórico de Saldos` e retorna o `page_id`."""
        props = self._montar_propriedades_snapshot(snapshot)
        page_created = self._client.create_page(
            self._db_ids.historico_saldos, properties=props
        )
        return str(page_created["id"])

    def atualizar_snapshot(self, snapshot_page_id: str, snapshot: SnapshotPatrimonial) -> str:
        """Atualiza um snapshot existente no mesmo mês (Upsert idempotente)."""
        props = self._montar_propriedades_snapshot(snapshot)
        self._client.update_page(snapshot_page_id, properties=props)
        return snapshot_page_id

    def vincular_snapshots_ao_mes(self, mes_page_id: str, snapshot_ids: list[str]) -> None:
        """Vincula os snapshots gerados na propriedade `Histórico de Saldos` do Mês."""
        self._client.update_page(
            page_id=mes_page_id,
            properties={ColunasMeses.HISTORICO_SALDOS: Prop.relation(snapshot_ids)},
        )

    def sincronizar_aportes_do_mes(self, ano: int, mes: int, mes_page_id: str, dry_run: bool = False) -> Decimal:
        """Garante que os aportes de `AAAA-MM` estejam vinculados ao `mes_page_id` e soma o total."""
        prefixo = f"{ano:04d}-{mes:02d}"
        pages = self._client.query_database(self._db_ids.aportes)
        total_mes = Decimal("0.00")
        mes_norm = NotionParser.normalize_id(mes_page_id)

        for p in pages:
            dt = NotionParser.prop_date_start(p, ColunasAportes.DATA)
            if not dt.startswith(prefixo):
                continue
            qtd = NotionParser.prop_decimal_raw(p, ColunasAportes.QUANTIDADE) or Decimal("1")
            pu = NotionParser.prop_decimal(p, ColunasAportes.PRECO_UNITARIO) or Decimal("0.00")
            taxas = NotionParser.prop_decimal(p, ColunasAportes.TAXAS) or Decimal("0.00")
            tipo = NotionParser.prop_select(p, ColunasAportes.TIPO) or "Compra / Aporte"
            bruto = (qtd * pu) + taxas
            total_mes += -bruto if tipo == "Venda / Resgate" else bruto

            mes_rels = NotionParser.prop_relation_ids(p, ColunasAportes.MES_COMPETENCIA)
            if not dry_run and mes_norm and mes_norm not in mes_rels:
                self._client.update_page(
                    str(p["id"]),
                    properties={ColunasAportes.MES_COMPETENCIA: Prop.relation(mes_page_id)},
                )
        return total_mes.quantize(Decimal("0.01"))

    def obter_patrimonio_do_mes(self, mes_page_id: str) -> Decimal | None:
        """Calcula o patrimônio somado dos snapshots vinculados a um determinado mês."""
        pages = self._client.query_database(
            self._db_ids.historico_saldos,
            filter_payload={
                "property": ColunasHistoricoSaldos.MES_COMPETENCIA,
                "relation": {"contains": mes_page_id},
            },
        )
        if not pages:
            return None
        soma = Decimal("0.00")
        for p in pages:
            val = NotionParser.prop_decimal(p, ColunasHistoricoSaldos.VALOR_NO_DIA) or Decimal("0.00")
            soma += val
        return soma

    def atualizar_patrimonio_anterior_mes(self, mes_page_id: str, patrimonio_anterior: Decimal) -> None:
        """Registra o `Patrimônio Mês Anterior` na página do mês para cálculo de rendimento puro."""
        self._client.update_page(
            mes_page_id,
            properties={ColunasMeses.PATRIMONIO_ANTERIOR: Prop.number_decimal(patrimonio_anterior)},
        )
