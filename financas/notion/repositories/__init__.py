"""Subpacote de repositórios de acesso a dados (Data Access Layer) sobre as tabelas do Notion."""

from __future__ import annotations

from dataclasses import dataclass

from financas.config import AppConfig
from financas.notion.client import NotionClient
from financas.notion.repositories.base import (
    BaseNotionRepository,
    renomear_coluna_relacao,
    upsert_paginas_por_titulo,
)
from financas.notion.repositories.catalogo import CatalogoRepository
from financas.notion.repositories.gastos_fixos import GastosFixosRepository
from financas.notion.repositories.investimentos import InvestimentosRepository
from financas.notion.repositories.meses import MesesRepository
from financas.notion.repositories.pessoas import PessoasRepository
from financas.notion.repositories.transacoes import TransacoesRepository


@dataclass(frozen=True)
class NotionRepositories:
    """Agrupa os repositórios da camada de dados para injeção nos serviços."""

    catalogo: CatalogoRepository
    meses: MesesRepository
    pessoas: PessoasRepository
    gastos_fixos: GastosFixosRepository
    transacoes: TransacoesRepository
    investimentos: InvestimentosRepository | None = None

    @classmethod
    def from_client(cls, client: NotionClient, config: AppConfig) -> NotionRepositories:
        catalogo = CatalogoRepository(client, config.databases)
        meses = MesesRepository(client, config.databases.meses)
        pessoas = PessoasRepository(client, config.pages, config.databases)
        gastos_fixos = GastosFixosRepository(
            client, config.pages, config.databases, catalogo, pessoas
        )
        transacoes = TransacoesRepository(
            client, config.databases.transacoes, catalogo, meses, pessoas
        )
        investimentos = InvestimentosRepository(client, config.databases)
        return cls(
            catalogo=catalogo,
            meses=meses,
            pessoas=pessoas,
            gastos_fixos=gastos_fixos,
            transacoes=transacoes,
            investimentos=investimentos,
        )


__all__ = [
    "BaseNotionRepository",
    "CatalogoRepository",
    "GastosFixosRepository",
    "InvestimentosRepository",
    "MesesRepository",
    "NotionRepositories",
    "PessoasRepository",
    "TransacoesRepository",
    "renomear_coluna_relacao",
    "upsert_paginas_por_titulo",
]
