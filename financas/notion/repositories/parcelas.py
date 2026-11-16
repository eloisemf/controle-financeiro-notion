"""Repositório da database `📦 Parcelas` no Notion."""

from __future__ import annotations

from decimal import Decimal
import logging
import re
from typing import Any

from financas.domain.billing import calcular_inicio_fim_parcelas
from financas.domain.models import LancamentoTransacao, normalizar_texto_chave
from financas.notion.builders import NotionParser, NotionPropertyBuilder as Prop
from financas.notion.client import NotionClient
from financas.notion.repositories.base import BaseNotionRepository
from financas.notion.repositories.catalogo import CatalogoRepository
from financas.notion.schema import ColunasParcelas

logger = logging.getLogger(__name__)

_RE_SUFIXO_PARCELA = re.compile(r"\s*-\s*Parc\s+\d{1,2}/\d{1,2}\s*$", re.IGNORECASE)


def limpar_nome_parcela(descricao: str) -> str:
    """Remove o sufixo `- Parc XX/YY` para usar como título mestre em `📦 Parcelas`."""
    return _RE_SUFIXO_PARCELA.sub("", descricao).strip()


class ParcelasRepository(BaseNotionRepository):
    """Gerencia a criação e busca idempotente de compras parceladas em `📦 Parcelas`."""

    def __init__(
        self,
        client: NotionClient,
        database_id: str,
        catalogo: CatalogoRepository,
    ) -> None:
        super().__init__(client)
        self._database_id = database_id
        self._catalogo = catalogo
        self._cache: list[dict[str, Any]] | None = None

    def _listar_bruto(self) -> list[dict[str, Any]]:
        if self._cache is None:
            self._cache = list(self._client.query_database(self._database_id))
        return self._cache

    def invalidar_cache(self) -> None:
        self._cache = None

    def _buscar_existente(
        self,
        nome_base: str,
        total_parcelas: int,
        valor_parcela: Decimal,
        cartao_id: str | None,
    ) -> str | None:
        alvo_norm = normalizar_texto_chave(nome_base)
        cartao_norm = NotionParser.normalize_id(cartao_id) if cartao_id else ""

        for p in self._listar_bruto():
            titulo = normalizar_texto_chave(NotionParser.page_title(p))
            if titulo != alvo_norm:
                continue
            total_p = NotionParser.prop_decimal_raw(p, ColunasParcelas.TOTAL_PARCELAS)
            if total_p is None or int(total_p) != total_parcelas:
                continue
            val_p = NotionParser.prop_decimal(p, ColunasParcelas.VALOR_PARCELA)
            if val_p is None or abs(val_p - valor_parcela) > Decimal("0.05"):
                continue
            cartoes_p = NotionParser.prop_relation_ids(p, ColunasParcelas.CARTAO)
            if cartao_norm and cartoes_p and cartao_norm not in cartoes_p:
                continue
            return NotionParser.normalize_id(p["id"])
        return None

    def garantir_parcela(self, lancamento: LancamentoTransacao) -> str | None:
        """Localiza ou cria o registro mestre em `📦 Parcelas` e retorna o `page_id`."""
        if (
            not self._database_id
            or not lancamento.eh_parcelado
            or lancamento.parcela_atual is None
            or lancamento.total_parcelas is None
        ):
            return None

        nome_base = limpar_nome_parcela(lancamento.descricao)
        cartao_id = self._catalogo.id_conta(lancamento.conta_ou_cartao)

        existente = self._buscar_existente(
            nome_base=nome_base,
            total_parcelas=lancamento.total_parcelas,
            valor_parcela=lancamento.valor,
            cartao_id=cartao_id,
        )
        if existente:
            return existente

        inicio_lbl, fim_lbl = calcular_inicio_fim_parcelas(
            lancamento.mes_competencia,
            lancamento.parcela_atual,
            lancamento.total_parcelas,
        )
        de_quem = "Emprestado" if lancamento.pessoa else "Meu"

        props: dict[str, Any] = {
            ColunasParcelas.GASTO: Prop.title(nome_base),
            ColunasParcelas.DE_QUEM: Prop.select(de_quem),
            ColunasParcelas.PARCELA_ATUAL: Prop.number_raw(Decimal(lancamento.parcela_atual)),
            ColunasParcelas.TOTAL_PARCELAS: Prop.number_raw(Decimal(lancamento.total_parcelas)),
            ColunasParcelas.VALOR_PARCELA: Prop.number_decimal(lancamento.valor),
            ColunasParcelas.INICIO: Prop.rich_text(inicio_lbl),
            ColunasParcelas.FIM: Prop.rich_text(fim_lbl),
            ColunasParcelas.CARTAO: Prop.relation(cartao_id),
        }

        pagina = self._client.create_page(database_id=self._database_id, properties=props)
        page_id = NotionParser.normalize_id(pagina["id"])
        if self._cache is not None:
            props_cache = dict(props)
            props_cache[ColunasParcelas.GASTO] = {"title": [{"plain_text": nome_base}]}
            self._cache.append({"id": page_id, "properties": props_cache})
        logger.debug("Criada compra parcelada '%s' (%dx R$ %s) em 📦 Parcelas.", nome_base, lancamento.total_parcelas, lancamento.valor)
        return page_id

    def listar_parcelas_ativas(self) -> list[dict[str, Any]]:
        """Lista compras parceladas em andamento ordenadas pelo maior saldo restante."""
        if not self._database_id:
            return []

        ativas: list[dict[str, Any]] = []
        for p in self._listar_bruto():
            atual_raw = NotionParser.prop_decimal_raw(p, ColunasParcelas.PARCELA_ATUAL)
            total_raw = NotionParser.prop_decimal_raw(p, ColunasParcelas.TOTAL_PARCELAS)
            if atual_raw is None or total_raw is None:
                continue
            parcela_atual = int(atual_raw)
            total_parcelas = int(total_raw)
            if parcela_atual <= 0 or parcela_atual > total_parcelas:
                continue

            valor_parcela = NotionParser.prop_decimal(p, ColunasParcelas.VALOR_PARCELA) or Decimal("0.00")
            restantes = total_parcelas - parcela_atual + 1
            falta_pagar = (valor_parcela * Decimal(restantes)).quantize(Decimal("0.01"))
            cartoes_p = NotionParser.prop_relation_ids(p, ColunasParcelas.CARTAO)
            cartao_nome = self._catalogo.nome_conta_por_id(cartoes_p[0]) if cartoes_p else None
            ativas.append(
                {
                    "gasto": NotionParser.page_title(p),
                    "parcela_atual": parcela_atual,
                    "total_parcelas": total_parcelas,
                    "valor_parcela": valor_parcela,
                    "falta_pagar": falta_pagar,
                    "fim": NotionParser.prop_rich_text(p, ColunasParcelas.FIM),
                    "cartao": cartao_nome or "Cartão",
                    "de_quem": NotionParser.prop_select(p, ColunasParcelas.DE_QUEM) or "Meu",
                }
            )
        ativas.sort(key=lambda item: item["falta_pagar"], reverse=True)
        return ativas
