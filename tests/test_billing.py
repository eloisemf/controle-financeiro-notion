"""Testes unitários para `CalculadoraFatura` e projeção de parcelas."""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from financas.domain.billing import (
    CalculadoraFatura,
    adicionar_meses,
    formatar_mes_competencia,
)
from financas.domain.models import (
    DEFAULT_CARTOES,
    GastoFixo,
    LancamentoTransacao,
    TipoTransacao,
)


class TestCalculadoraFatura(unittest.TestCase):
    def setUp(self) -> None:
        self.calc = CalculadoraFatura()
        self.inter = DEFAULT_CARTOES["Inter Credito"]
        self.santander = DEFAULT_CARTOES["Santander Credito"]
        self.bradesco = DEFAULT_CARTOES["Bradesco Credito"]

    def test_fechamento_inter_dias_corridos(self) -> None:
        self.assertEqual(
            self.calc.data_fechamento(self.inter, 2026, 11),
            date(2026, 10, 31),
        )

    def test_fechamento_santander_pula_feriado_consciencia_negra(self) -> None:
        self.assertEqual(
            self.calc.data_fechamento(self.santander, 2026, 11),
            date(2026, 11, 19),
        )

    def test_fechamento_bradesco_dias_uteis(self) -> None:
        self.assertEqual(
            self.calc.data_fechamento(self.bradesco, 2026, 12),
            date(2026, 11, 16),
        )

    def test_compra_antes_do_fechamento_entra_na_fatura_atual_mes_fechamento(self) -> None:
        self.assertEqual(
            self.calc.competencia_fatura(self.inter, date(2026, 10, 30)),
            (2026, 10),
        )

    def test_compra_no_dia_do_fechamento_entra_na_proxima_fatura(self) -> None:
        self.assertEqual(
            self.calc.competencia_fatura(self.inter, date(2026, 10, 31)),
            (2026, 11),
        )

    def test_competencia_bradesco_mes_fechamento(self) -> None:
        self.assertEqual(
            self.calc.competencia_fatura(self.bradesco, date(2026, 11, 15)),
            (2026, 11),
        )
        self.assertEqual(
            self.calc.competencia_fatura(self.bradesco, date(2026, 11, 16)),
            (2026, 12),
        )

    def test_competencia_por_vencimento(self) -> None:
        self.assertEqual(
            self.calc.competencia_por_vencimento(self.inter, 2026, 10),
            (2026, 9),
        )
        self.assertEqual(
            self.calc.competencia_por_vencimento(self.bradesco, 2026, 11),
            (2026, 10),
        )
        self.assertEqual(
            self.calc.competencia_por_vencimento(self.santander, 2026, 11),
            (2026, 11),
        )

    def test_competencia_santander(self) -> None:
        self.assertEqual(
            self.calc.competencia_fatura(self.santander, date(2026, 11, 18)),
            (2026, 11),
        )
        self.assertEqual(
            self.calc.competencia_fatura(self.santander, date(2026, 11, 19)),
            (2026, 12),
        )

    def test_projecao_parcelas_futuras(self) -> None:
        compra = LancamentoTransacao(
            descricao="Curso Python - Parc 02/04",
            valor=Decimal("150.00"),
            data_compra=date(2026, 9, 15),
            mes_competencia="2026-Novembro",
            tipo=TipoTransacao.SAIDA,
            categoria="Cursos",
            conta_ou_cartao="Inter Credito",
            parcela_atual=2,
            total_parcelas=4,
        )
        futuras = self.calc.projetar_parcelas_futuras(compra)
        self.assertEqual(len(futuras), 2)
        self.assertEqual(futuras[0].mes_competencia, "2026-Dezembro")
        self.assertEqual(futuras[0].parcela_atual, 3)
        self.assertEqual(futuras[0].descricao_com_parcela, "Curso Python - Parc 03/04")
        self.assertEqual(futuras[1].mes_competencia, "2027-Janeiro")
        self.assertEqual(futuras[1].parcela_atual, 4)
        self.assertEqual(futuras[1].descricao_com_parcela, "Curso Python - Parc 04/04")

    def test_utilitarios_mes(self) -> None:
        self.assertEqual(adicionar_meses(2026, 11, 3), (2027, 2))
        self.assertEqual(formatar_mes_competencia(2026, 3), "2026-Março")

    def test_data_cobranca_para_competencia_cartao_e_conta(self) -> None:
        spotify = GastoFixo(
            nome="Spotify",
            valor_meu=Decimal("10.89"),
            valor_cobrado=Decimal("10.89"),
            categoria="Streamings",
            pago_com="Santander Credito",
            dia_cobranca=17,
        )
        meli = GastoFixo(
            nome="Meli+",
            valor_meu=Decimal("14.90"),
            valor_cobrado=Decimal("19.90"),
            categoria="Streamings",
            pago_com="Santander Credito",
            dia_cobranca=29,
        )
        aluguel = GastoFixo(
            nome="Aluguel",
            valor_meu=Decimal("519.85"),
            valor_cobrado=Decimal("519.85"),
            categoria="Casa",
            pago_com="Santander Conta",
            dia_cobranca=1,
        )
        self.assertEqual(
            self.calc.data_cobranca_para_competencia(spotify, 2026, 11, self.santander),
            date(2026, 11, 17),
        )
        self.assertEqual(
            self.calc.data_cobranca_para_competencia(meli, 2026, 11, self.santander),
            date(2026, 10, 29),
        )
        self.assertEqual(
            self.calc.data_cobranca_para_competencia(aluguel, 2026, 11, None),
            date(2026, 11, 1),
        )
        hbo = GastoFixo(
            nome="HBO Max",
            valor_meu=Decimal("22.45"),
            valor_cobrado=Decimal("22.45"),
            categoria="Streamings",
            pago_com="Bradesco Credito",
            dia_cobranca=28,
        )
        self.assertEqual(
            self.calc.data_cobranca_para_competencia(hbo, 2026, 11, self.bradesco),
            date(2026, 10, 28),
        )
        celular = GastoFixo(
            nome="Celular",
            valor_meu=Decimal("30.00"),
            valor_cobrado=Decimal("30.00"),
            categoria="Utilidade",
            pago_com="Bradesco Credito",
            dia_cobranca=15,
        )
        self.assertEqual(
            self.calc.data_cobranca_para_competencia(celular, 2026, 11, self.bradesco),
            date(2026, 11, 15),
        )


if __name__ == "__main__":
    unittest.main()
