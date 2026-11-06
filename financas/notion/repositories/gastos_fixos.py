"""Repositório da database `Gastos Fixos` no Notion."""

from __future__ import annotations

import logging
from typing import Any

from financas.config import NotionDatabaseIds, NotionPageIds
from financas.domain.models import CATEGORIA_DIVERSOS, GastoFixo, Periodicidade
from financas.notion.builders import (
    NotionParser,
    NotionPropertyBuilder as Prop,
    NotionSchemaBuilder as Schema,
)
from financas.notion.client import NotionClient
from financas.notion.repositories.base import (
    BaseNotionRepository,
    renomear_coluna_relacao,
    upsert_paginas_por_titulo,
)
from financas.notion.repositories.catalogo import CatalogoRepository
from financas.notion.repositories.pessoas import PessoasRepository
from financas.notion.schema import ColunasGastosFixos

logger = logging.getLogger(__name__)


class GastosFixosRepository(BaseNotionRepository):
    """Gerencia a database `Gastos Fixos` e seus itens."""

    TITULO_DB = "Gastos Fixos"

    def __init__(
        self,
        client: NotionClient,
        page_ids: NotionPageIds,
        db_ids: NotionDatabaseIds,
        catalogo: CatalogoRepository,
        pessoas_repo: PessoasRepository | None = None,
    ) -> None:
        super().__init__(client)
        self._page_ids = page_ids
        self._db_ids = db_ids
        self._catalogo = catalogo
        self._pessoas = pessoas_repo
        self._database_id: str | None = db_ids.gastos_fixos or None

    def localizar_database_id(self) -> str | None:
        if self._database_id:
            return self._database_id
        blocos = self._client.get_block_children(self._page_ids.gastos_fixos)
        for bloco in blocos:
            if bloco.get("type") == "child_database":
                titulo = bloco.get("child_database", {}).get("title", "").strip()
                if titulo == self.TITULO_DB:
                    self._database_id = NotionParser.normalize_id(bloco["id"])
                    return self._database_id
        return None

    def garantir_database(self, pessoas_db_id: str | None = None) -> tuple[str, bool]:
        """Retorna `(database_id, criada_agora)`."""
        existente = self.localizar_database_id()
        if existente:
            schema = self._client.get_database(existente)
            props_existentes = schema.get("properties", {})
            novas: dict[str, Any] = {}
            if ColunasGastosFixos.USO_DINHEIRO not in props_existentes:
                novas[ColunasGastosFixos.USO_DINHEIRO] = Schema.relation_single(self._db_ids.uso_dinheiro)
            if ColunasGastosFixos.VALOR_A_RECEBER not in props_existentes:
                novas[ColunasGastosFixos.VALOR_A_RECEBER] = Schema.number("real")
            if pessoas_db_id and ColunasGastosFixos.PESSOA not in props_existentes:
                novas[ColunasGastosFixos.PESSOA] = Schema.relation_single(pessoas_db_id)
            if novas:
                self._client.update_database(existente, {"properties": novas})
            return existente, False

        props_criacao: dict[str, Any] = {
            ColunasGastosFixos.NOME: Schema.title(),
            ColunasGastosFixos.NOME_NA_FATURA: Schema.rich_text(),
            ColunasGastosFixos.VALOR_MEU: Schema.number("real"),
            ColunasGastosFixos.VALOR_COBRADO: Schema.number("real"),
            ColunasGastosFixos.VALOR_A_RECEBER: Schema.number("real"),
            ColunasGastosFixos.PERIODICIDADE: Schema.select([("Mensal", "blue"), ("Anual", "purple")]),
            ColunasGastosFixos.CATEGORIA: Schema.relation_single(self._db_ids.categorias),
            ColunasGastosFixos.USO_DINHEIRO: Schema.relation_single(self._db_ids.uso_dinheiro),
            ColunasGastosFixos.PAGO_COM: Schema.relation_single(self._db_ids.contas),
            ColunasGastosFixos.DIA_COBRANCA: Schema.number("number"),
            ColunasGastosFixos.ATIVO: Schema.checkbox(),
            ColunasGastosFixos.INICIO: Schema.date(),
            ColunasGastosFixos.FIM: Schema.date(),
            ColunasGastosFixos.OBSERVACAO: Schema.rich_text(),
        }
        if pessoas_db_id:
            props_criacao[ColunasGastosFixos.PESSOA] = Schema.relation_single(pessoas_db_id)

        criada = self._client.create_database(
            {
                "parent": {"type": "page_id", "page_id": self._page_ids.gastos_fixos},
                "is_inline": True,
                "title": [{"type": "text", "text": {"content": self.TITULO_DB}}],
                "properties": props_criacao,
            }
        )
        self._database_id = NotionParser.normalize_id(criada["id"])
        return self._database_id, True

    def renomear_coluna_relacao_transacoes(self, database_id: str) -> bool:
        """Garante que a propriedade sincronizada com Transações se chame 'Lançamentos'."""
        return renomear_coluna_relacao(
            self._client, database_id, self._db_ids.transacoes, ColunasGastosFixos.LANCAMENTOS
        )

    def _montar_propriedades_gasto_fixo(self, item: GastoFixo) -> dict[str, Any]:
        props: dict[str, Any] = {
            ColunasGastosFixos.NOME: Prop.title(item.nome),
            ColunasGastosFixos.VALOR_COBRADO: Prop.number_decimal(item.valor_lancamento),
            ColunasGastosFixos.VALOR_A_RECEBER: Prop.number_decimal(item.valor_a_receber),
            ColunasGastosFixos.CATEGORIA: Prop.relation(self._catalogo.id_categoria(item.categoria)),
            ColunasGastosFixos.ATIVO: Prop.checkbox(item.ativo),
            ColunasGastosFixos.OBSERVACAO: Prop.rich_text(item.observacao),
            ColunasGastosFixos.PERIODICIDADE: Prop.select(
                item.periodicidade.value if item.periodicidade else None
            ),
            ColunasGastosFixos.PAGO_COM: (
                Prop.relation(self._catalogo.id_conta(item.pago_com))
                if item.pago_com
                else Prop.relation([])
            ),
            ColunasGastosFixos.USO_DINHEIRO: (
                Prop.relation(self._catalogo.id_uso_dinheiro(item.uso_dinheiro))
                if item.uso_dinheiro
                else Prop.relation([])
            ),
            ColunasGastosFixos.DIA_COBRANCA: Prop.number_int(item.dia_cobranca),
        }
        if item.nome_na_fatura:
            props[ColunasGastosFixos.NOME_NA_FATURA] = Prop.rich_text(item.nome_na_fatura)
        if self._pessoas is not None:
            props[ColunasGastosFixos.PESSOA] = (
                Prop.relation(self._pessoas.id_pessoa(item.pessoa))
                if item.pessoa
                else Prop.relation([])
            )
        return props

    def _pagina_para_gasto_fixo(self, pagina: dict[str, Any]) -> GastoFixo:
        cat_ids = NotionParser.prop_relation_ids(pagina, ColunasGastosFixos.CATEGORIA)
        conta_ids = NotionParser.prop_relation_ids(pagina, ColunasGastosFixos.PAGO_COM)
        uso_ids = NotionParser.prop_relation_ids(pagina, ColunasGastosFixos.USO_DINHEIRO)
        pessoa_ids = NotionParser.prop_relation_ids(pagina, ColunasGastosFixos.PESSOA)
        per_str = NotionParser.prop_select(pagina, ColunasGastosFixos.PERIODICIDADE)
        periodicidade = Periodicidade(per_str) if per_str in ("Mensal", "Anual") else None

        nome_pessoa: str | None = None
        if pessoa_ids and self._pessoas is not None:
            nome_pessoa = self._pessoas.nome_pessoa_por_id(pessoa_ids[0]) or None

        nome_gf = NotionParser.page_title(pagina)

        return GastoFixo(
            page_id=NotionParser.normalize_id(pagina["id"]),
            nome=nome_gf,
            nome_na_fatura=NotionParser.prop_rich_text(pagina, ColunasGastosFixos.NOME_NA_FATURA),
            valor_meu=NotionParser.prop_decimal(pagina, ColunasGastosFixos.VALOR_MEU),
            valor_cobrado=NotionParser.prop_decimal(pagina, ColunasGastosFixos.VALOR_COBRADO),
            valor_a_receber=NotionParser.prop_decimal(pagina, ColunasGastosFixos.VALOR_A_RECEBER),
            categoria=self._catalogo.nome_categoria_por_id(cat_ids[0]) if cat_ids else CATEGORIA_DIVERSOS,
            uso_dinheiro=self._catalogo.nome_uso_por_id(uso_ids[0]) if uso_ids else None,
            pago_com=self._catalogo.nome_conta_por_id(conta_ids[0]) if conta_ids else None,
            pessoa=nome_pessoa,
            dia_cobranca=NotionParser.prop_int(pagina, ColunasGastosFixos.DIA_COBRANCA),
            periodicidade=periodicidade,
            ativo=NotionParser.prop_checkbox(pagina, ColunasGastosFixos.ATIVO),
            observacao=NotionParser.prop_rich_text(pagina, ColunasGastosFixos.OBSERVACAO),
        )

    def listar_ativos(self, database_id: str | None = None) -> list[GastoFixo]:
        """Lê do Notion todos os Gastos Fixos marcados com `Ativo = True`."""
        db_id = database_id or self.localizar_database_id()
        if not db_id:
            return []
        paginas = self._client.query_database(
            db_id,
            filter_payload={"property": ColunasGastosFixos.ATIVO, "checkbox": {"equals": True}},
        )
        return [self._pagina_para_gasto_fixo(p) for p in paginas if NotionParser.page_title(p)]

    def sincronizar_gastos_fixos(
        self, database_id: str, itens: tuple[GastoFixo, ...]
    ) -> tuple[list[str], list[str]]:
        """Faz upsert (cria ou atualiza) da lista de Gastos Fixos.

        Retorna `(nomes_criados, nomes_atualizados)`.
        """
        criados, atualizados, _ = upsert_paginas_por_titulo(
            client=self._client,
            database_id=database_id,
            itens=itens,
            obter_nome=lambda item: item.nome,
            montar_propriedades=self._montar_propriedades_gasto_fixo,
        )
        logger.info(
            "Gastos Fixos sincronizados: %d criados, %d atualizados.",
            len(criados),
            len(atualizados),
        )
        return criados, atualizados
