"""Entidades e dataclasses de domínio para o módulo de investimentos e patrimônio."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum


class TipoOperacaoInvestimento(str, Enum):
    COMPRA = "Compra / Aporte"
    VENDA = "Venda / Resgate"
    PROVENTO = "Provento / Dividendo"
    AMORTIZACAO = "Amortização"


@dataclass(frozen=True)
class CotacaoMercado:
    """Representa a cotação ou Preço Unitário de um ativo obtido do mercado."""

    identificador: str
    preco_unitario: Decimal
    data_referencia: str
    fonte: str  # 'B3', 'Tesouro Transparente', 'CVM', 'BCB'


@dataclass(frozen=True)
class AtivoCarteira:
    """Representa um ativo financeiro consolidado na carteira."""

    id: str
    nome: str
    tipo: str
    qtd: Decimal
    valor_atual: Decimal
    valor_investido: Decimal
    fracao_titulos: Decimal | None = None

    @property
    def lucro(self) -> Decimal:
        return self.valor_atual - self.valor_investido

    def to_dict(self) -> dict[str, str | Decimal | None]:
        dados: dict[str, str | Decimal | None] = {
            "id": self.id,
            "nome": self.nome,
            "tipo": self.tipo,
            "qtd": self.qtd,
            "valor_atual": self.valor_atual,
            "valor_investido": self.valor_investido,
            "lucro": self.lucro,
        }
        if self.fracao_titulos is not None:
            dados["fracao_titulos"] = self.fracao_titulos
        return dados


@dataclass(frozen=True)
class ResumoCarteira:
    """Panorama consolidado da carteira de investimentos."""

    ativos: tuple[AtivoCarteira, ...] = field(default_factory=tuple)

    @property
    def total_ativos(self) -> int:
        return len(self.ativos)

    @property
    def patrimonio_total(self) -> Decimal:
        return sum((a.valor_atual for a in self.ativos), Decimal("0.00"))

    @property
    def total_investido(self) -> Decimal:
        return sum((a.valor_investido for a in self.ativos), Decimal("0.00"))

    @property
    def lucro_total(self) -> Decimal:
        return self.patrimonio_total - self.total_investido

    def __getitem__(self, key: str) -> object:
        mapa = {
            "total_ativos": self.total_ativos,
            "patrimonio_total": self.patrimonio_total,
            "total_investido": self.total_investido,
            "lucro_total": self.lucro_total,
            "ativos": [a.to_dict() for a in self.ativos],
        }
        return mapa[key]


@dataclass(frozen=True)
class SnapshotPatrimonial:
    """Foto de um ativo em uma data específica de fechamento."""

    registro: str
    data_foto: str
    investimento_id: str
    valor_no_dia: Decimal
    mes_id: str | None = None
    qtd_cotas: Decimal | None = None
    observacao: str = ""
    page_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        dados: dict[str, object] = {
            "registro": self.registro,
            "data_foto": self.data_foto,
            "investimento_id": self.investimento_id,
            "valor_no_dia": self.valor_no_dia,
            "qtd_cotas": self.qtd_cotas,
            "mes_id": self.mes_id,
        }
        if self.page_id:
            dados["id"] = self.page_id
        return dados


@dataclass(frozen=True)
class AporteInvestimento:
    """Registro de compra, venda ou provento de investimento."""

    nome: str
    data: date | str
    tipo: TipoOperacaoInvestimento | str
    investimento_id: str
    quantidade: Decimal
    preco_unitario: Decimal
    taxas: Decimal = Decimal("0.00")
    observacao: str = ""

    @property
    def valor_total(self) -> Decimal:
        return (self.quantidade * self.preco_unitario) + self.taxas

    @property
    def eh_venda(self) -> bool:
        return str(self.tipo) == TipoOperacaoInvestimento.VENDA.value or self.tipo == TipoOperacaoInvestimento.VENDA

    @property
    def fluxo_financeiro(self) -> Decimal:
        return -self.valor_total if self.eh_venda else self.valor_total

    @property
    def fluxo_quantidade(self) -> Decimal:
        return -self.quantidade if self.eh_venda else self.quantidade
