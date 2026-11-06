"""Camada de infraestrutura de integração com a API REST do Notion."""

from financas.notion.builders import NotionParser, NotionPropertyBuilder, NotionSchemaBuilder
from financas.notion.client import NotionApiError, NotionClient
from financas.notion.repositories import (
    BaseNotionRepository,
    CatalogoRepository,
    GastosFixosRepository,
    InvestimentosRepository,
    MesesRepository,
    NotionRepositories,
    PessoasRepository,
    TransacoesRepository,
)

__all__ = [
    "BaseNotionRepository",
    "CatalogoRepository",
    "GastosFixosRepository",
    "InvestimentosRepository",
    "MesesRepository",
    "NotionApiError",
    "NotionClient",
    "NotionParser",
    "NotionPropertyBuilder",
    "NotionRepositories",
    "NotionSchemaBuilder",
    "PessoasRepository",
    "TransacoesRepository",
]
