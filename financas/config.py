"""Configurações centralizadas e imutáveis do projeto.

Credenciais e identificadores de databases são lidos de variáveis de ambiente (.env)
e nunca são expostos em logs ou representações de string (repr=False).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from financas.domain.models import DEFAULT_CARTOES, CartaoConfig, RegraFechamento

NOTION_API_VERSION = "2022-06-28"
NOTION_BASE_URL = "https://api.notion.com/v1"


def _carregar_dotenv(env_path: Path) -> None:
    """Carrega variáveis do arquivo .env usando `python-dotenv` (se instalado) ou fallback nativo."""
    try:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=env_path)
    except ImportError:
        if not env_path.is_file():
            return
        for linha in env_path.read_text(encoding="utf-8").splitlines():
            limpa = linha.strip()
            if not limpa or limpa.startswith("#") or "=" not in limpa:
                continue
            chave, valor = limpa.split("=", maxsplit=1)
            chave_limpa = chave.strip()
            if chave_limpa and chave_limpa not in os.environ:
                os.environ[chave_limpa] = valor.strip().strip("'\"")


def _ler_id_env(var_name: str, default: str = "") -> str:
    return os.getenv(var_name, default).replace("-", "").strip()


@dataclass(frozen=True)
class NotionDatabaseIds:
    """IDs (sem hífens) das databases mapeadas no Notion."""

    transacoes: str = ""
    meses: str = ""
    contas: str = ""
    categorias: str = ""
    uso_dinheiro: str = ""
    gastos_fixos: str = ""
    pessoas: str = ""
    parcelas: str = ""
    meus_investimentos: str = ""
    aportes: str = ""
    historico_saldos: str = ""

    @classmethod
    def from_env(cls) -> NotionDatabaseIds:
        return cls(
            transacoes=_ler_id_env("NOTION_TRANSACOES_DB_ID"),
            meses=_ler_id_env("NOTION_MESES_DB_ID"),
            contas=_ler_id_env("NOTION_CONTAS_DB_ID"),
            categorias=_ler_id_env("NOTION_CATEGORIAS_DB_ID"),
            uso_dinheiro=_ler_id_env("NOTION_USO_DINHEIRO_DB_ID"),
            gastos_fixos=_ler_id_env("NOTION_GASTOS_FIXOS_DB_ID"),
            pessoas=_ler_id_env("NOTION_PESSOAS_DB_ID"),
            parcelas=_ler_id_env("NOTION_PARCELAS_DB_ID"),
            meus_investimentos=_ler_id_env("NOTION_MEUS_INVESTIMENTOS_DB_ID"),
            aportes=_ler_id_env("NOTION_APORTES_DB_ID"),
            historico_saldos=_ler_id_env("NOTION_HISTORICO_SALDOS_DB_ID"),
        )

    def as_name_map(self) -> dict[str, str]:
        pares = (
            (self.contas, "Contas, cartões e reservas"),
            (self.meses, "Meses"),
            (self.categorias, "Categorias"),
            (self.uso_dinheiro, "Uso do dinheiro"),
            (self.transacoes, "Transações"),
            (self.gastos_fixos, "Gastos Fixos"),
            (self.pessoas, "Pessoas"),
            (self.parcelas, "📦 Parcelas"),
            (self.meus_investimentos, "Meus Investimentos"),
            (self.aportes, "📦 Aportes & Movimentações"),
            (self.historico_saldos, "📸 Histórico de Saldos"),
        )
        return {db_id: nome for db_id, nome in pares if db_id}


@dataclass(frozen=True)
class NotionPageIds:
    """IDs (sem hífens) das páginas-mãe do workspace."""

    financas_pessoais: str = ""
    gastos_fixos: str = ""

    @classmethod
    def from_env(cls) -> NotionPageIds:
        return cls(
            financas_pessoais=_ler_id_env("NOTION_PAGE_FINANCAS"),
            gastos_fixos=_ler_id_env("NOTION_PAGE_GASTOS_FIXOS"),
        )


@dataclass(frozen=True)
class AppConfig:
    """Configuração global da aplicação."""

    notion_token: str = field(repr=False)
    gemini_api_key: str | None = field(default=None, repr=False)
    nome_titular: str = ""
    databases: NotionDatabaseIds = field(default_factory=NotionDatabaseIds)
    pages: NotionPageIds = field(default_factory=NotionPageIds)
    cartoes: dict[str, CartaoConfig] = field(default_factory=lambda: dict(DEFAULT_CARTOES))

    @classmethod
    def from_env(cls, env_path: Path | None = None) -> AppConfig:
        resolved_path = env_path or (Path(__file__).resolve().parents[1] / ".env")
        _carregar_dotenv(resolved_path)
        token = os.getenv("NOTION_TOKEN")
        if not token or not token.strip():
            raise RuntimeError("Variável NOTION_TOKEN ausente ou vazia no arquivo .env.")
        gemini_key = os.getenv("GEMINI_API_KEY")
        nome_titular = os.getenv("NOME_TITULAR", "").strip()
        return cls(
            notion_token=token.strip(),
            gemini_api_key=gemini_key.strip() if gemini_key else None,
            nome_titular=nome_titular,
            databases=NotionDatabaseIds.from_env(),
            pages=NotionPageIds.from_env(),
        )

    def obter_cartao_ativo(self, nome: str) -> CartaoConfig:
        cartao = self.cartoes.get(nome.strip())
        if not cartao:
            raise KeyError(f"Cartão não configurado: '{nome}'")
        if not cartao.ativo:
            raise ValueError(f"O cartão '{nome}' está marcado como inativo/descartado.")
        return cartao


__all__ = [
    "DEFAULT_CARTOES",
    "NOTION_API_VERSION",
    "NOTION_BASE_URL",
    "AppConfig",
    "CartaoConfig",
    "NotionDatabaseIds",
    "NotionPageIds",
    "RegraFechamento",
]
