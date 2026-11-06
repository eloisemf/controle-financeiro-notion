"""Parser determinístico para faturas PDF do cartão Inter Credito."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import re

from financas.domain.billing import CalculadoraFatura, formatar_mes_competencia
from financas.domain.classification import ClassificadorCategoria
from financas.domain.models import (
    DEFAULT_CARTOES,
    CartaoConfig,
    LancamentoTransacao,
)
from financas.parsers.base import BasePdfParser, MESES_ABREV_PT, exigir_decimal


class InterFaturaPdfParser(BasePdfParser):
    """Parser determinístico para faturas PDF do cartão Inter Credito."""

    _RE_VENCIMENTO_EXPLICITO = re.compile(
        r"vencimento\s*:?\s*(\d{2})/(\d{2})/(\d{4})", re.IGNORECASE
    )
    _RE_VENCIMENTO = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")
    _RE_LINHA_LANCAMENTO = re.compile(
        r"^(\d{1,2})\s+de\s+([a-zç]{3})\.?\s+(\d{4})\s+(.+?)\s+-\s+(\+\s+)?R\$\s*([\d\.,\s]+)$",
        re.IGNORECASE,
    )
    _RE_PARCELA = re.compile(r"\(?\s*Parcela\s+(\d{1,2})\s+de\s+(\d{1,2})\s*\)?", re.IGNORECASE)

    def __init__(
        self,
        nome_cartao: str = "Inter Credito",
        calculadora: CalculadoraFatura | None = None,
        cartoes: dict[str, CartaoConfig] | None = None,
        classificador: ClassificadorCategoria | None = None,
    ) -> None:
        self._nome_cartao = nome_cartao
        self._calc = calculadora or CalculadoraFatura()
        self._cartoes = cartoes if cartoes is not None else DEFAULT_CARTOES
        self._classificador = classificador or ClassificadorCategoria()

    def pode_processar(self, arquivo: Path, texto: str = "") -> bool:
        if arquivo.suffix.lower() != ".pdf":
            return False
        t = texto.lower()
        return "banco inter" in t or bool(self._RE_LINHA_LANCAMENTO.search(texto))

    def parse_texto(self, texto: str) -> list[LancamentoTransacao]:
        mes_fatura = self._extrair_mes_competencia(texto)
        lancamentos: list[LancamentoTransacao] = []

        for linha_bruta in texto.splitlines():
            linha = linha_bruta.strip()
            match = self._RE_LINHA_LANCAMENTO.match(linha)
            if not match:
                continue

            dia_str, mes_abrev, ano_str, descricao_bruta, sinal_credito, valor_str = match.groups()

            if sinal_credito or "PAGTO DEBITO AUTOMATICO" in descricao_bruta.upper():
                continue

            mes_num = MESES_ABREV_PT.get(mes_abrev.lower()[:3])
            if not mes_num:
                continue
            data_compra = date(int(ano_str), mes_num, int(dia_str))
            valor_limpo = re.sub(r"\s+", "", valor_str)
            valor = exigir_decimal(valor_limpo, contexto=descricao_bruta)

            parcela_atual: int | None = None
            total_parcelas: int | None = None
            match_parc = self._RE_PARCELA.search(descricao_bruta)
            if match_parc:
                parcela_atual = int(match_parc.group(1))
                total_parcelas = int(match_parc.group(2))
                descricao_limpa = self._RE_PARCELA.sub("", descricao_bruta).strip(" -")
            else:
                descricao_limpa = descricao_bruta.strip()

            categoria, incerto = self._classificador.classificar_descricao(descricao_limpa)
            lancamentos.append(
                LancamentoTransacao.criar_saida_classificada(
                    descricao=descricao_limpa,
                    valor=valor,
                    data_compra=data_compra,
                    mes_competencia=mes_fatura,
                    conta_ou_cartao=self._nome_cartao,
                    categoria=categoria,
                    incerto=incerto,
                    parcela_atual=parcela_atual,
                    total_parcelas=total_parcelas,
                )
            )

        return lancamentos

    def _extrair_mes_competencia(self, texto: str) -> str:
        """Extrai o vencimento do cabeçalho do PDF e retorna o mês de fechamento (competência)."""
        match = self._RE_VENCIMENTO_EXPLICITO.search(texto) or self._RE_VENCIMENTO.search(texto)
        if not match:
            raise ValueError(
                "Não foi possível localizar a data de vencimento (DD/MM/YYYY) na fatura."
            )
        _, mes_str, ano_str = match.groups()
        ano_venc, mes_venc = int(ano_str), int(mes_str)
        cartao = self._cartoes.get(self._nome_cartao)
        if cartao:
            ano_comp, mes_comp = self._calc.competencia_por_vencimento(cartao, ano_venc, mes_venc)
            return formatar_mes_competencia(ano_comp, mes_comp)
        return formatar_mes_competencia(ano_venc, mes_venc)
