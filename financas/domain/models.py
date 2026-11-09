"""Entidades e objetos de valor (Value Objects) do domínio financeiro.

Regras aplicadas:
- Valores monetários usam estritamente `decimal.Decimal` quantizado em centavos (0.01)
  com `ROUND_HALF_UP`. Passar `float` levanta `TypeError` explícito para evitar
  erros de ponto flutuante binário (IEEE 754).
- Entidades são imutáveis (`frozen=True`).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import NamedTuple
import re
import unicodedata
from dataclasses import dataclass

CENTAVO = Decimal("0.01")
CATEGORIA_DIVERSOS = "Diversos"
CATEGORIA_EMPRESTIMO = "Emprestimo cartão"


def para_decimal(valor: Decimal | int | str | None) -> Decimal | None:
    """Converte e quantiza valores monetários para Decimal(0.01) de forma segura.

    Rejeita `float` explicitamente para garantir rigor em cálculos financeiros.
    """
    if valor is None:
        return None
    if isinstance(valor, float):
        raise TypeError(
            f"Valores monetários não podem ser float ({valor!r}). "
            "Use Decimal('10.89'), int ou str."
        )
    if isinstance(valor, str):
        limpo = valor.strip().replace("R$", "").strip()
        if "," in limpo and "." in limpo:
            limpo = limpo.replace(".", "").replace(",", ".")
        elif "," in limpo:
            limpo = limpo.replace(",", ".")
        elif re.match(r"^-?\d{1,3}(\.\d{3})+$", limpo):
            limpo = limpo.replace(".", "")
        if not limpo:
            return None
        valor = Decimal(limpo)
    elif isinstance(valor, int):
        valor = Decimal(valor)
    elif not isinstance(valor, Decimal):
        raise TypeError(f"Tipo inválido para valor monetário: {type(valor).__name__}")

    return valor.quantize(CENTAVO, rounding=ROUND_HALF_UP)


def decimal_para_centavos(valor: Decimal | int | str | None) -> int:
    """Converte um valor monetário para inteiro em centavos com arredondamento ROUND_HALF_UP."""
    dec = para_decimal(valor)
    if dec is None:
        return 0
    return int((dec * 100).to_integral_exact(rounding=ROUND_HALF_UP))


def normalizar_texto_chave(texto: str) -> str:
    """Normaliza texto para compor chave de idempotência (minúsculas, sem acentos e espaços extras)."""
    sem_acentos = "".join(
        c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
    )
    compacto = re.sub(r"\s*\*\s*", "*", sem_acentos)
    return re.sub(r"\s+", " ", compacto).strip().lower()


class ChaveParcialTransacao(NamedTuple):
    """Chave parcial de identificação de transação (descrição, centavos, conta)."""

    descricao_norm: str
    centavos: int
    conta_norm: str


class ChaveIdempotencia(NamedTuple):
    """Chave composta determinística para impedir duplicidade em reprocessamentos."""

    descricao_norm: str
    centavos: int
    mes_competencia: str
    conta_norm: str
    data_iso: str

    @property
    def chave_parcial(self) -> ChaveParcialTransacao:
        return ChaveParcialTransacao(self.descricao_norm, self.centavos, self.conta_norm)


class VinculoGastoFixo(NamedTuple):
    """Representa um vínculo existente de gasto fixo no mês."""

    transacao_page_id: str
    status: str
    data_iso: str


class RegraFechamento(str, Enum):
    DIAS_CORRIDOS = "corridos"
    DIAS_UTEIS = "uteis"


class TipoTransacao(str, Enum):
    SAIDA = "Saida"
    ENTRADA = "Entrada"
    MOVIMENTACAO_INTERNA = "Movimentação Interna"
    PAGAR_FATURA = "Pagar fatura cartão"
    REEMBOLSO = "Reembolso"


class Recorrencia(str, Enum):
    FIXO = "Fixo"
    NAO_RECORRENTE = "Não recorrente"


class StatusTransacao(str, Enum):
    PREVISTO = "Previsto"
    REALIZADO = "Realizado"


class Periodicidade(str, Enum):
    MENSAL = "Mensal"
    ANUAL = "Anual"


class UsoDinheiro(str, Enum):
    ESSENCIAL = "Essencial"
    NAO_ESSENCIAL = "Nao essencial"
    EMPRESTIMO = "Emprestimo"
    INVESTIMENTO = "Investimento"

    @classmethod
    def por_incerteza(cls, incerto: bool) -> str:
        return cls.NAO_ESSENCIAL.value if incerto else cls.ESSENCIAL.value


@dataclass(frozen=True)
class CartaoConfig:
    """Regra de vencimento e fechamento de um cartão de crédito."""

    nome: str
    dia_vencimento: int
    regra_fechamento: RegraFechamento
    dias_antes_vencimento: int
    ativo: bool = True

    def __post_init__(self) -> None:
        if not (1 <= self.dia_vencimento <= 31):
            raise ValueError(f"Dia de vencimento inválido: {self.dia_vencimento}")
        if self.dias_antes_vencimento < 0:
            raise ValueError(f"Dias antes do vencimento inválido: {self.dias_antes_vencimento}")


DEFAULT_CARTOES: dict[str, CartaoConfig] = {
    "Inter Credito": CartaoConfig(
        nome="Inter Credito",
        dia_vencimento=7,
        regra_fechamento=RegraFechamento.DIAS_CORRIDOS,
        dias_antes_vencimento=7,
        ativo=True,
    ),
    "Santander Credito": CartaoConfig(
        nome="Santander Credito",
        dia_vencimento=27,
        regra_fechamento=RegraFechamento.DIAS_UTEIS,
        dias_antes_vencimento=5,
        ativo=True,
    ),
    "Santader Credito": CartaoConfig(
        nome="Santader Credito",
        dia_vencimento=27,
        regra_fechamento=RegraFechamento.DIAS_UTEIS,
        dias_antes_vencimento=5,
        ativo=True,
    ),
    "Bradesco Credito": CartaoConfig(
        nome="Bradesco Credito",
        dia_vencimento=1,
        regra_fechamento=RegraFechamento.DIAS_UTEIS,
        dias_antes_vencimento=10,
        ativo=True,
    ),
}


@dataclass(frozen=True)
class Pessoa:
    """Representa um terceiro (familiar ou amigo) na database `Pessoas`."""

    nome: str
    nome_no_pix_fatura: str = ""
    saldo_inicial: Decimal = Decimal("0.00")
    observacao: str = ""
    page_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "nome", self.nome.strip())
        object.__setattr__(self, "nome_no_pix_fatura", self.nome_no_pix_fatura.strip())
        saldo = para_decimal(self.saldo_inicial)
        object.__setattr__(self, "saldo_inicial", saldo if saldo is not None else Decimal("0.00"))


@dataclass(frozen=True)
class RegraCompraTerceiro:
    """Mapeia compras parceladas de terceiros na fatura para `Pessoa` e `Valor a receber`."""

    padrao_descricao: str
    conta_ou_cartao: str
    pessoa: str
    valor_a_receber: Decimal
    categoria: str = CATEGORIA_EMPRESTIMO
    uso_dinheiro: str = UsoDinheiro.EMPRESTIMO.value

    def __post_init__(self) -> None:
        val = para_decimal(self.valor_a_receber)
        if val is None or val <= Decimal("0.00"):
            raise ValueError(f"Valor a receber inválido: {self.valor_a_receber!r}")
        object.__setattr__(self, "valor_a_receber", val)

    def corresponde(self, descricao: str, conta_ou_cartao: str) -> bool:
        desc_norm = normalizar_texto_chave(descricao)
        padrao_norm = normalizar_texto_chave(self.padrao_descricao)
        conta_norm = normalizar_texto_chave(conta_ou_cartao)
        alvo_conta_norm = normalizar_texto_chave(self.conta_ou_cartao)
        return padrao_norm in desc_norm and (not alvo_conta_norm or alvo_conta_norm == conta_norm)


@dataclass(frozen=True)
class GastoFixo:
    """Representa um item da database Gastos Fixos."""

    nome: str
    valor_meu: Decimal | None
    valor_cobrado: Decimal | None
    categoria: str
    periodicidade: Periodicidade | None = Periodicidade.MENSAL
    ativo: bool = True
    pago_com: str | None = None
    dia_cobranca: int | None = None
    uso_dinheiro: str | None = None
    pessoa: str | None = None
    valor_a_receber: Decimal | None = None
    valor_reajuste: Decimal | None = None
    inicio_reajuste: tuple[int, int] | None = None
    nome_na_fatura: str = ""
    observacao: str = ""
    page_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "nome", self.nome.strip())
        object.__setattr__(self, "categoria", self.categoria.strip())
        object.__setattr__(self, "nome_na_fatura", self.nome_na_fatura.strip())
        object.__setattr__(self, "valor_meu", para_decimal(self.valor_meu))
        object.__setattr__(self, "valor_cobrado", para_decimal(self.valor_cobrado))
        object.__setattr__(self, "valor_a_receber", para_decimal(self.valor_a_receber))
        object.__setattr__(self, "valor_reajuste", para_decimal(self.valor_reajuste))

    @property
    def valor_lancamento(self) -> Decimal | None:
        """Retorna o valor efetivo para lançamento na fatura/extrato (prioriza valor_cobrado)."""
        return self.valor_cobrado if self.valor_cobrado is not None else self.valor_meu

    def valor_lancamento_para_competencia(self, ano: int, mes: int) -> Decimal | None:
        """Retorna o valor de lançamento considerando eventual reajuste programado."""
        if self.inicio_reajuste and self.valor_reajuste is not None and (ano, mes) >= self.inicio_reajuste:
            return self.valor_reajuste
        return self.valor_lancamento

    def valor_a_receber_para_competencia(self, ano: int, mes: int) -> Decimal | None:
        """Retorna o valor a receber considerando eventual reajuste programado."""
        if not self.pessoa and self.valor_a_receber is None:
            return None
        if self.inicio_reajuste and self.valor_reajuste is not None and (ano, mes) >= self.inicio_reajuste:
            return self.valor_reajuste
        return self.valor_a_receber if self.valor_a_receber is not None else self.valor_lancamento

    def corresponde_a_descricao(self, descricao_fatura: str) -> bool:
        """Verifica se a descrição vinda da fatura/extrato corresponde a este Gasto Fixo.

        Usa `nome_na_fatura` (quando preenchido, aceitando múltiplos padrões separados por `;`)
        ou `nome` como padrão de busca normalizado.
        """
        desc_norm = normalizar_texto_chave(descricao_fatura)
        if not desc_norm:
            return False

        bruto = self.nome_na_fatura or self.nome
        padroes = [normalizar_texto_chave(p) for p in bruto.split(";") if normalizar_texto_chave(p)]
        if not padroes:
            return False

        return any(padrao in desc_norm for padrao in padroes)


@dataclass(frozen=True)
class LancamentoTransacao:
    """Entidade de domínio representando um lançamento na tabela Transações."""

    descricao: str
    valor: Decimal
    data_compra: date
    mes_competencia: str  # Ex.: "2026-Novembro"
    tipo: TipoTransacao
    categoria: str
    conta_ou_cartao: str
    recorrencia: Recorrencia = Recorrencia.NAO_RECORRENTE
    status: StatusTransacao = StatusTransacao.REALIZADO
    parcela_atual: int | None = None
    total_parcelas: int | None = None
    gasto_fixo_id: str | None = None
    uso_dinheiro: str | None = None
    pessoa: str | None = None
    valor_a_receber: Decimal | None = None
    eh_entrada_na_conta: bool = False
    conta_destino: str | None = None
    nome_fantasia: str | None = None

    def __post_init__(self) -> None:
        val = para_decimal(self.valor)
        if val is None or val <= Decimal("0.00"):
            raise ValueError(f"O valor da transação deve ser positivo: {self.valor!r}")
        object.__setattr__(self, "valor", val)
        object.__setattr__(self, "valor_a_receber", para_decimal(self.valor_a_receber))
        object.__setattr__(self, "descricao", self.descricao.strip())
        object.__setattr__(self, "categoria", self.categoria.strip())
        object.__setattr__(self, "conta_ou_cartao", self.conta_ou_cartao.strip())
        if self.pessoa is not None:
            object.__setattr__(self, "pessoa", self.pessoa.strip() or None)
        if self.conta_destino is not None:
            object.__setattr__(self, "conta_destino", self.conta_destino.strip() or None)
        if self.nome_fantasia is not None:
            object.__setattr__(self, "nome_fantasia", self.nome_fantasia.strip() or None)

        if (self.parcela_atual is None) != (self.total_parcelas is None):
            raise ValueError("parcela_atual e total_parcelas devem ser ambos informados ou ambos None.")
        if self.parcela_atual is not None and self.total_parcelas is not None:
            if self.parcela_atual < 1 or self.total_parcelas < self.parcela_atual:
                raise ValueError(
                    f"Parcela inválida: {self.parcela_atual}/{self.total_parcelas}"
                )

    @classmethod
    def criar_saida_classificada(
        cls,
        descricao: str,
        valor: Decimal,
        data_compra: date,
        mes_competencia: str,
        conta_ou_cartao: str,
        categoria: str,
        incerto: bool,
        parcela_atual: int | None = None,
        total_parcelas: int | None = None,
    ) -> LancamentoTransacao:
        """Factory para criar lançamentos de saída com classificação e prioridade padronizadas."""
        return cls(
            descricao=descricao,
            valor=valor,
            data_compra=data_compra,
            mes_competencia=mes_competencia,
            tipo=TipoTransacao.SAIDA,
            categoria=categoria,
            conta_ou_cartao=conta_ou_cartao,
            recorrencia=Recorrencia.NAO_RECORRENTE,
            status=StatusTransacao.REALIZADO,
            parcela_atual=parcela_atual,
            total_parcelas=total_parcelas,
            uso_dinheiro=UsoDinheiro.por_incerteza(incerto),
        )

    @property
    def valor_em_centavos(self) -> int:
        return decimal_para_centavos(self.valor)

    @property
    def eh_parcelado(self) -> bool:
        return bool(self.parcela_atual and self.total_parcelas and self.total_parcelas > 1)

    @property
    def descricao_com_parcela(self) -> str:
        """Retorna a descrição padronizada com sufixo de parcela quando aplicável."""
        base = re.sub(r"\s*-?\s*parc\.?\s*\d{1,2}/\d{1,2}\s*$", "", self.descricao, flags=re.IGNORECASE).strip()
        if self.eh_parcelado:
            return f"{base} - Parc {self.parcela_atual:02d}/{self.total_parcelas:02d}"
        return self.descricao

    @property
    def chave_idempotencia(self) -> ChaveIdempotencia:
        """Chave composta determinística para impedir duplicidade em reprocessamentos.

        Formato: (descricao_normalizada, valor_em_centavos, mes_competencia, conta_normalizada, data_iso)
        """
        return ChaveIdempotencia(
            normalizar_texto_chave(self.descricao_com_parcela),
            self.valor_em_centavos,
            self.mes_competencia.strip(),
            normalizar_texto_chave(self.conta_ou_cartao),
            self.data_compra.isoformat(),
        )
