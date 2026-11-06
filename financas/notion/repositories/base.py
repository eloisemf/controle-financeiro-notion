"""Classes base e funções utilitárias compartilhadas entre os repositórios do Notion."""

from __future__ import annotations

from typing import Any, Callable, Iterable, TypeVar

from financas.notion.builders import NotionParser
from financas.notion.client import NotionClient

T = TypeVar("T")


def renomear_coluna_relacao(
    client: NotionClient,
    database_id: str,
    target_database_id: str,
    novo_nome_coluna: str,
) -> bool:
    """Garante que a coluna de relação apontando para `target_database_id` tenha o nome `novo_nome_coluna`."""
    schema = client.get_database(database_id)
    for nome_col, prop in schema.get("properties", {}).items():
        if prop.get("type") == "relation":
            alvo = NotionParser.normalize_id(prop.get("relation", {}).get("database_id"))
            if alvo == target_database_id and nome_col != novo_nome_coluna:
                client.update_database(
                    database_id, {"properties": {nome_col: {"name": novo_nome_coluna}}}
                )
                return True
    return False


def upsert_paginas_por_titulo(
    client: NotionClient,
    database_id: str,
    itens: Iterable[T],
    obter_nome: Callable[[T], str],
    montar_propriedades: Callable[[T], dict[str, Any]],
) -> tuple[list[str], list[str], dict[str, str]]:
    """Faz upsert de itens em uma database do Notion usando o título da página como chave."""
    paginas = client.query_database(database_id)
    existentes = {
        NotionParser.page_title(p): NotionParser.normalize_id(p["id"])
        for p in paginas
        if NotionParser.page_title(p)
    }
    criados: list[str] = []
    atualizados: list[str] = []

    for item in itens:
        nome = obter_nome(item)
        props = montar_propriedades(item)
        if nome in existentes:
            client.update_page(page_id=existentes[nome], properties=props)
            atualizados.append(nome)
        else:
            nova = client.create_page(database_id=database_id, properties=props)
            existentes[nome] = NotionParser.normalize_id(nova["id"])
            criados.append(nome)

    return criados, atualizados, existentes


class BaseNotionRepository:
    """Classe base para repositórios do Notion com utilitários de mapeamento e cache."""

    def __init__(self, client: NotionClient) -> None:
        self._client = client

    def _carregar_mapa_titulo_id(self, database_id: str) -> dict[str, str]:
        """Carrega o mapa `{titulo_pagina: page_id_normalizado}` de uma database."""
        if not database_id:
            return {}
        paginas = self._client.query_database(database_id)
        return {
            NotionParser.page_title(p): NotionParser.normalize_id(p["id"])
            for p in paginas
            if NotionParser.page_title(p)
        }


# Aliases preservados internamente
_renomear_coluna_relacao = renomear_coluna_relacao
_upsert_paginas_por_titulo = upsert_paginas_por_titulo
