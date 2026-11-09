"""Repositório de catálogos e configurações de contas/cartões no Notion."""

from __future__ import annotations

import logging
import re

from financas.config import NotionDatabaseIds
from financas.domain.models import (
    DEFAULT_CARTOES,
    CartaoConfig,
    RegraFechamento,
    normalizar_texto_chave,
)
from financas.notion.builders import NotionParser
from financas.notion.client import NotionClient
from financas.notion.repositories.base import BaseNotionRepository

logger = logging.getLogger(__name__)


class CatalogoRepository(BaseNotionRepository):
    """Mantém cache em memória dos catálogos de referência (Categorias, Contas, Uso do dinheiro)."""

    def __init__(self, client: NotionClient, db_ids: NotionDatabaseIds) -> None:
        super().__init__(client)
        self._db_ids = db_ids
        self._nome_para_id: dict[str, dict[str, str]] = {}
        self._id_para_nome: dict[str, dict[str, str]] = {}
        self._cartoes_cache: dict[str, CartaoConfig] | None = None

    def _obter_mapas(self, chave_catalogo: str, database_id: str) -> tuple[dict[str, str], dict[str, str]]:
        if chave_catalogo not in self._nome_para_id:
            mapa = self._carregar_mapa_titulo_id(database_id)
            self._nome_para_id[chave_catalogo] = mapa
            self._id_para_nome[chave_catalogo] = {v: k for k, v in mapa.items()}
        return self._nome_para_id[chave_catalogo], self._id_para_nome[chave_catalogo]

    def _buscar_id(self, chave_catalogo: str, database_id: str, nome: str, label_erro: str) -> str:
        mapa, _ = self._obter_mapas(chave_catalogo, database_id)
        chave = nome.strip()
        if chave in mapa:
            return mapa[chave]

        # Fallback insensível a acentos, emojis e maiúsculas/minúsculas
        chave_limpa = re.sub(r"[^a-z0-9\s]", "", normalizar_texto_chave(chave)).strip()
        if chave_limpa:
            for k, page_id in mapa.items():
                k_limpa = re.sub(r"[^a-z0-9\s]", "", normalizar_texto_chave(k)).strip()
                if k_limpa == chave_limpa:
                    return page_id

        raise KeyError(f"{label_erro} não encontrado(a) no Notion: '{chave}'")

    def _buscar_nome(self, chave_catalogo: str, database_id: str, page_id: str) -> str:
        _, reverso = self._obter_mapas(chave_catalogo, database_id)
        return reverso.get(NotionParser.normalize_id(page_id), "")

    @property
    def categorias(self) -> dict[str, str]:
        return self._obter_mapas("categorias", self._db_ids.categorias)[0]

    @property
    def contas(self) -> dict[str, str]:
        return self._obter_mapas("contas", self._db_ids.contas)[0]

    @property
    def uso_dinheiro(self) -> dict[str, str]:
        return self._obter_mapas("uso_dinheiro", self._db_ids.uso_dinheiro)[0]

    def id_categoria(self, nome: str) -> str:
        return self._buscar_id("categorias", self._db_ids.categorias, nome, "Categoria")

    def id_conta(self, nome: str) -> str:
        return self._buscar_id("contas", self._db_ids.contas, nome, "Conta/Cartão")

    def id_uso_dinheiro(self, nome: str) -> str:
        return self._buscar_id("uso_dinheiro", self._db_ids.uso_dinheiro, nome, "Uso do dinheiro")

    def nome_conta_por_id(self, page_id: str) -> str:
        return self._buscar_nome("contas", self._db_ids.contas, page_id)

    def nome_categoria_por_id(self, page_id: str) -> str:
        return self._buscar_nome("categorias", self._db_ids.categorias, page_id)

    def nome_uso_por_id(self, page_id: str) -> str:
        return self._buscar_nome("uso_dinheiro", self._db_ids.uso_dinheiro, page_id)

    def obter_cartoes_configurados(self) -> dict[str, CartaoConfig]:
        """Lê configurações de vencimento e fechamento dos cartões da tabela Contas (com cache)."""
        if self._cartoes_cache is not None:
            return dict(self._cartoes_cache)

        paginas = self._client.query_database(self._db_ids.contas)
        cartoes: dict[str, CartaoConfig] = {}
        for p in paginas:
            nome = NotionParser.page_title(p)
            if not nome:
                continue
            dia_venc = (
                NotionParser.prop_int(p, "Dia Vencimento")
                or NotionParser.prop_int(p, "Dia do Vencimento")
                or NotionParser.prop_int(p, "Vencimento")
            )
            if dia_venc is None or not (1 <= dia_venc <= 31):
                continue

            dias_antes = (
                NotionParser.prop_int(p, "Dias Fechamento")
                or NotionParser.prop_int(p, "Dias Antes do Vencimento")
                or NotionParser.prop_int(p, "Antecedência Fechamento")
                or 7
            )
            regra_str = (
                NotionParser.prop_select(p, "Regra Fechamento")
                or NotionParser.prop_select(p, "Regra de Fechamento")
                or "uteis"
            ).lower()
            regra = RegraFechamento.DIAS_CORRIDOS if "corrido" in regra_str else RegraFechamento.DIAS_UTEIS
            props = p.get("properties", {})
            ativo = NotionParser.prop_checkbox(p, "Ativo") if "Ativo" in props else True

            cartoes[nome] = CartaoConfig(
                nome=nome,
                dia_vencimento=dia_venc,
                regra_fechamento=regra,
                dias_antes_vencimento=dias_antes,
                ativo=ativo,
            )
        self._cartoes_cache = cartoes
        return dict(cartoes)

    def obter_configuracao_cartoes(
        self, fallback: dict[str, CartaoConfig] | None = None
    ) -> dict[str, CartaoConfig]:
        """Retorna as configurações de cartões lidas do Notion, mescladas com fallback."""
        resultado = dict(fallback or DEFAULT_CARTOES)
        try:
            dinamicos = self.obter_cartoes_configurados()
            resultado.update(dinamicos)
        except Exception as exc:
            logger.warning(
                "Não foi possível carregar configurações dinâmicas de cartões do Notion: %s",
                exc,
                exc_info=True,
            )
        return resultado
