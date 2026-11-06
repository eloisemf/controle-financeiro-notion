"""Repositório da database `Pessoas` (Terceiros e Reembolsos) no Notion."""

from __future__ import annotations

from decimal import Decimal
import logging
from typing import Any

from financas.config import NotionDatabaseIds, NotionPageIds
from financas.domain.models import Pessoa, normalizar_texto_chave
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
from financas.notion.schema import ColunasPessoas

logger = logging.getLogger(__name__)


class PessoasRepository(BaseNotionRepository):
    """Gerencia a database `Pessoas` (Terceiros e Reembolsos)."""

    TITULO_DB = "Pessoas"

    def __init__(
        self,
        client: NotionClient,
        page_ids: NotionPageIds | None,
        db_ids: NotionDatabaseIds | None,
        database_id: str | None = None,
    ) -> None:
        super().__init__(client)
        self._page_ids = page_ids
        self._db_ids = db_ids
        self._database_id: str | None = database_id or (db_ids.pessoas if db_ids else None) or None
        self._mapa_pessoas: dict[str, str] | None = None
        self._id_para_pessoa: dict[str, str] | None = None

    def _localizar_pagina_me_devem(self) -> str:
        """Busca a página 'Me devem'; se não encontrar, usa 'Finanças pessoais'."""
        paginas = self._client.search(query="Me devem", filter_object="page")
        for p in paginas:
            titulo = NotionParser.page_title(p)
            if normalizar_texto_chave(titulo) == "me devem":
                return NotionParser.normalize_id(p["id"])
        return self._page_ids.financas_pessoais if self._page_ids else ""

    def localizar_database_id(self) -> str | None:
        if self._database_id:
            return self._database_id
        dbs = self._client.search(query=self.TITULO_DB, filter_object="database")
        for db in dbs:
            titulo = NotionParser.plain_text(db.get("title", [])).strip()
            if titulo == self.TITULO_DB:
                self._database_id = NotionParser.normalize_id(db["id"])
                return self._database_id
        return None

    def garantir_database(self) -> tuple[str, bool]:
        """Retorna `(database_id, criada_agora)`."""
        existente = self.localizar_database_id()
        if existente:
            return existente, False

        parent_page_id = self._localizar_pagina_me_devem()
        criada = self._client.create_database(
            {
                "parent": {"type": "page_id", "page_id": parent_page_id},
                "is_inline": True,
                "title": [{"type": "text", "text": {"content": self.TITULO_DB}}],
                "properties": {
                    ColunasPessoas.NOME: Schema.title(),
                    ColunasPessoas.NOME_PIX_FATURA: Schema.rich_text(),
                    ColunasPessoas.SALDO_INICIAL: Schema.number("real"),
                    ColunasPessoas.OBSERVACAO: Schema.rich_text(),
                },
            }
        )
        self._database_id = NotionParser.normalize_id(criada["id"])
        return self._database_id, True

    def renomear_coluna_relacao_transacoes(self, database_id: str) -> bool:
        """Garante que a propriedade sincronizada com Transações se chame 'Transações'."""
        transacoes_db_id = self._db_ids.transacoes if self._db_ids else ""
        return renomear_coluna_relacao(
            self._client, database_id, transacoes_db_id, ColunasPessoas.TRANSACOES
        )

    def _recarregar_cache(self, database_id: str | None = None) -> None:
        db_id = database_id or self.localizar_database_id()
        if not db_id:
            self._mapa_pessoas = {}
            self._id_para_pessoa = {}
            return
        self._mapa_pessoas = self._carregar_mapa_titulo_id(db_id)
        self._id_para_pessoa = {v: k for k, v in self._mapa_pessoas.items()}

    def listar_todas(self, database_id: str | None = None) -> list[Pessoa]:
        """Lê todas as pessoas cadastradas na database Pessoas do Notion."""
        db_id = database_id or self.localizar_database_id()
        if not db_id:
            return []
        paginas = self._client.query_database(db_id)
        pessoas: list[Pessoa] = []
        for p in paginas:
            nome = NotionParser.page_title(p)
            if not nome:
                continue
            nome_pix = (
                NotionParser.prop_rich_text(p, ColunasPessoas.NOME_PIX_FATURA)
                or NotionParser.prop_rich_text(p, ColunasPessoas.NOME_PIX_FATURA_ALT)
            )
            saldo = NotionParser.prop_decimal(p, ColunasPessoas.SALDO_INICIAL) or Decimal("0.00")
            obs = NotionParser.prop_rich_text(p, ColunasPessoas.OBSERVACAO)
            page_id = NotionParser.normalize_id(p["id"])
            pessoas.append(
                Pessoa(
                    nome=nome,
                    nome_no_pix_fatura=nome_pix,
                    saldo_inicial=saldo,
                    observacao=obs,
                    page_id=page_id,
                )
            )
        return pessoas

    @property
    def pessoas_por_nome(self) -> dict[str, str]:
        if self._mapa_pessoas is None:
            self._recarregar_cache()
        return self._mapa_pessoas or {}

    def id_pessoa(self, nome: str) -> str:
        chave = nome.strip()
        if chave not in self.pessoas_por_nome:
            raise KeyError(f"Pessoa não encontrada na database Pessoas: '{chave}'")
        return self.pessoas_por_nome[chave]

    def nome_pessoa_por_id(self, page_id: str) -> str:
        if self._id_para_pessoa is None:
            self._recarregar_cache()
        mapa = self._id_para_pessoa or {}
        return mapa.get(NotionParser.normalize_id(page_id), "")

    @staticmethod
    def _montar_propriedades_pessoa(item: Pessoa) -> dict[str, Any]:
        return {
            ColunasPessoas.NOME: Prop.title(item.nome),
            ColunasPessoas.NOME_PIX_FATURA: Prop.rich_text(item.nome_no_pix_fatura),
            ColunasPessoas.SALDO_INICIAL: Prop.number_decimal(item.saldo_inicial),
            ColunasPessoas.OBSERVACAO: Prop.rich_text(item.observacao),
        }

    def sincronizar_pessoas(
        self, database_id: str, itens: tuple[Pessoa, ...]
    ) -> tuple[list[str], list[str]]:
        """Faz upsert das Pessoas na database `Pessoas`."""
        criadas, atualizadas, existentes = upsert_paginas_por_titulo(
            client=self._client,
            database_id=database_id,
            itens=itens,
            obter_nome=lambda item: item.nome,
            montar_propriedades=self._montar_propriedades_pessoa,
        )
        self._mapa_pessoas = dict(existentes)
        self._id_para_pessoa = {v: k for k, v in self._mapa_pessoas.items()}
        return criadas, atualizadas
