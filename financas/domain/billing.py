"""Serviço de domínio determinístico para cálculo de faturas e projeção de parcelas.

Regras aplicadas:
- O nome do mês de competência da fatura corresponde ao mês de fechamento da fatura.
- Compras realizadas no dia do fechamento entram na próxima fatura.
- Dias úteis consideram segunda a sexta exceto feriados nacionais (via `holidays`).
"""

from __future__ import annotations

import calendar
from dataclasses import replace
from datetime import date, timedelta
from typing import Container

import holidays

from financas.domain.models import (
    CartaoConfig,
    GastoFixo,
    LancamentoTransacao,
    RegraFechamento,
)

MESES_NOMES: tuple[str, ...] = (
    "Janeiro",
    "Fevereiro",
    "Março",
    "Abril",
    "Maio",
    "Junho",
    "Julho",
    "Agosto",
    "Setembro",
    "Outubro",
    "Novembro",
    "Dezembro",
)


def adicionar_meses(ano: int, mes: int, delta: int) -> tuple[int, int]:
    """Soma `delta` meses a `(ano, mes)` retornando `(novo_ano, novo_mes)`."""
    indice_total = ano * 12 + (mes - 1) + delta
    return indice_total // 12, (indice_total % 12) + 1


def formatar_mes_competencia(ano: int, mes: int) -> str:
    """Converte `(2026, 11)` para `'2026-Novembro'`."""
    if not (1 <= mes <= 12):
        raise ValueError(f"Mês inválido: {mes}")
    return f"{ano:04d}-{MESES_NOMES[mes - 1]}"


def parse_mes_competencia(label: str) -> tuple[int, int]:
    """Converte `'2026-Novembro'` para `(2026, 11)`."""
    partes = label.strip().split("-", maxsplit=1)
    if len(partes) != 2 or partes[1] not in MESES_NOMES:
        raise ValueError(f"Formato de mês inválido: '{label}' (esperado YYYY-Mes)")
    return int(partes[0]), MESES_NOMES.index(partes[1]) + 1


class CalculadoraFatura:
    """Calcula datas de fechamento, mês de competência da fatura e projeção de parcelas."""

    def __init__(self, feriados: Container[date] | None = None) -> None:
        self._feriados: Container[date] = (
            feriados if feriados is not None else holidays.country_holidays("BR")
        )

    def eh_dia_util(self, d: date) -> bool:
        """Retorna True se `d` cair de segunda a sexta e não for feriado."""
        return d.weekday() < 5 and d not in self._feriados

    @staticmethod
    def data_vencimento(cartao: CartaoConfig, ano: int, mes: int) -> date:
        """Retorna a data nominal de vencimento da fatura em `(ano, mes)`."""
        return date(ano, mes, cartao.dia_vencimento)

    def data_fechamento(self, cartao: CartaoConfig, ano: int, mes: int) -> date:
        """Calcula a data de fechamento da fatura cujo vencimento ocorre em `(ano, mes)`."""
        venc = self.data_vencimento(cartao, ano, mes)
        if cartao.regra_fechamento == RegraFechamento.DIAS_CORRIDOS:
            return venc - timedelta(days=cartao.dias_antes_vencimento)

        dias_uteis_contados = 0
        cursor = venc
        while dias_uteis_contados < cartao.dias_antes_vencimento:
            cursor -= timedelta(days=1)
            if self.eh_dia_util(cursor):
                dias_uteis_contados += 1
        return cursor

    def competencia_por_vencimento(
        self, cartao: CartaoConfig, ano_vencimento: int, mes_vencimento: int
    ) -> tuple[int, int]:
        """Retorna `(ano, mes)` de competência (mês de fechamento) da fatura que vence em `(ano_vencimento, mes_vencimento)`."""
        fech = self.data_fechamento(cartao, ano_vencimento, mes_vencimento)
        return fech.year, fech.month

    def competencia_fatura(self, cartao: CartaoConfig, data_compra: date) -> tuple[int, int]:
        """Retorna `(ano, mes)` de fechamento da fatura em que `data_compra` será cobrada.

        Se `data_compra < fechamento(ano_v, mes_v)`, cai nessa fatura.
        Se `data_compra >= fechamento(ano_v, mes_v)`, avança para a fatura seguinte.
        """
        ano_v, mes_v = data_compra.year, data_compra.month
        for _ in range(3):
            fech = self.data_fechamento(cartao, ano_v, mes_v)
            if data_compra < fech:
                return fech.year, fech.month
            ano_v, mes_v = adicionar_meses(ano_v, mes_v, 1)
        raise RuntimeError(
            f"Não foi possível determinar a fatura para compra em {data_compra} no cartão {cartao.nome}"
        )

    @staticmethod
    def _criar_data_segura(ano: int, mes: int, dia: int) -> date:
        ultimo_dia = calendar.monthrange(ano, mes)[1]
        return date(ano, mes, min(max(1, dia), ultimo_dia))

    def data_cobranca_para_competencia(
        self,
        gasto: GastoFixo,
        ano_competencia: int,
        mes_competencia: int,
        cartao: CartaoConfig | None = None,
    ) -> date:
        """Determina a data de cobrança cuja competência é `(ano_competencia, mes_competencia)`.

        - Se `cartao` for `None` (débito automático ou Pix em conta corrente), a cobrança
          ocorre no próprio mês `(ano_competencia, mes_competencia)`.
        - Se `cartao` for informado, testa os meses candidatos anteriores/atual e retorna
          a data cuja fatura vence em `(ano_competencia, mes_competencia)`.
        """
        dia = gasto.dia_cobranca or 1
        if cartao is None:
            return self._criar_data_segura(ano_competencia, mes_competencia, dia)

        for delta in (-2, -1, 0):
            ano_cand, mes_cand = adicionar_meses(ano_competencia, mes_competencia, delta)
            data_cand = self._criar_data_segura(ano_cand, mes_cand, dia)
            if self.competencia_fatura(cartao, data_cand) == (ano_competencia, mes_competencia):
                return data_cand

        return self._criar_data_segura(ano_competencia, mes_competencia, dia)

    def data_parcela_na_competencia(self, data_compra_original: date, competencia: str) -> date:
        """Retorna a data de referência (`Criado em`) da parcela dentro do seu mês de competência."""
        ano_comp, mes_comp = parse_mes_competencia(competencia)
        return self._criar_data_segura(ano_comp, mes_comp, data_compra_original.day)

    def projetar_parcelas_futuras(
        self, lancamento: LancamentoTransacao
    ) -> list[LancamentoTransacao]:
        """Dada uma compra parcelada (ex.: Parc 02/06 em 2026-Novembro),
        gera os lançamentos das parcelas restantes (03/06 a 06/06) nos meses subsequentes,
        ajustando `data_compra` (`Criado em`) para o mês de cada parcela.
        """
        if (
            lancamento.parcela_atual is None
            or lancamento.total_parcelas is None
            or lancamento.parcela_atual >= lancamento.total_parcelas
        ):
            return []

        ano_base, mes_base = parse_mes_competencia(lancamento.mes_competencia)
        futuras: list[LancamentoTransacao] = []

        for prox_parcela in range(lancamento.parcela_atual + 1, lancamento.total_parcelas + 1):
            deslocamento = prox_parcela - lancamento.parcela_atual
            ano_fut, mes_fut = adicionar_meses(ano_base, mes_base, deslocamento)
            mes_comp_fut = formatar_mes_competencia(ano_fut, mes_fut)
            data_parcela = self._criar_data_segura(
                ano_fut, mes_fut, lancamento.data_compra.day
            )
            novo_lancamento = replace(
                lancamento,
                data_compra=data_parcela,
                mes_competencia=mes_comp_fut,
                parcela_atual=prox_parcela,
            )
            # Atualiza a descrição padronizada com o número da nova parcela
            futuras.append(
                replace(novo_lancamento, descricao=novo_lancamento.descricao_com_parcela)
            )
        return futuras
