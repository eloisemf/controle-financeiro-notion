"""Testes unitários para entidades de domínio (`Decimal`, idempotência e Builders)."""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from financas.domain.models import (
    GastoFixo,
    LancamentoTransacao,
    RegraCompraTerceiro,
    TipoTransacao,
    para_decimal,
)
from financas.notion.builders import NotionPropertyBuilder


class TestDomainModels(unittest.TestCase):
    def test_para_decimal_rejeita_float(self) -> None:
        with self.assertRaises(TypeError):
            para_decimal(10.89)  # type: ignore[arg-type]

    def test_para_decimal_aceita_string_br_e_decimal(self) -> None:
        self.assertEqual(para_decimal("R$ 1.234,56"), Decimal("1234.56"))
        self.assertEqual(para_decimal("1.234"), Decimal("1234.00"))
        self.assertEqual(para_decimal("10,89"), Decimal("10.89"))
        self.assertEqual(para_decimal(Decimal("10.895")), Decimal("10.90"))

    def test_chave_idempotencia_normaliza_espacos_e_acentos(self) -> None:
        t1 = LancamentoTransacao(
            descricao="  Farmácia   São João ",
            valor=Decimal("45.90"),
            data_compra=date(2026, 10, 10),
            mes_competencia="2026-Outubro",
            tipo=TipoTransacao.SAIDA,
            categoria="Saúde",
            conta_ou_cartao="Inter Credito ",
        )
        t2 = LancamentoTransacao(
            descricao="farmacia sao joao",
            valor="45,90",  # type: ignore[arg-type]
            data_compra=date(2026, 10, 10),
            mes_competencia="2026-Outubro",
            tipo=TipoTransacao.SAIDA,
            categoria="Saúde",
            conta_ou_cartao="inter credito",
        )
        self.assertEqual(t1.chave_idempotencia, t2.chave_idempotencia)
        self.assertEqual(
            t1.chave_idempotencia,
            ("farmacia sao joao", 4590, "2026-Outubro", "inter credito", "2026-10-10"),
        )

    def test_property_builder_number_decimal_e_int(self) -> None:
        payload = NotionPropertyBuilder.number_decimal(Decimal("59.90"))
        self.assertEqual(payload, {"number": 59.9})
        self.assertEqual(NotionPropertyBuilder.number_decimal(None), {"number": None})
        self.assertEqual(NotionPropertyBuilder.number_int(15), {"number": 15})
        self.assertEqual(NotionPropertyBuilder.number_int(None), {"number": None})

    def test_gasto_fixo_corresponde_a_nome_na_fatura(self) -> None:
        spotify = GastoFixo(
            nome="Spotify",
            nome_na_fatura="ASAAS *Kotas",
            valor_meu=Decimal("10.89"),
            valor_cobrado=Decimal("10.89"),
            categoria="Streamings",
        )
        hbo = GastoFixo(
            nome="HBO Max",
            nome_na_fatura="DM*HELP.HBOMAX.CO",
            valor_meu=Decimal("7.45"),
            valor_cobrado=Decimal("22.45"),
            categoria="Streamings",
        )
        self.assertTrue(spotify.corresponde_a_descricao("ASAAS *Kotas SAO PAULO"))
        self.assertFalse(spotify.corresponde_a_descricao("LOJA ONLINE"))
        # Descrições curtas não devem casar por substring reversa com o padrão
        self.assertFalse(spotify.corresponde_a_descricao("AS"))
        self.assertTrue(hbo.corresponde_a_descricao("DM*HELP.HBOMAX.CO"))

    def test_gasto_fixo_reajuste_programado(self) -> None:
        gasto = GastoFixo(
            nome="Plano Compartilhado",
            nome_na_fatura="Assinatura Amigo",
            valor_meu=Decimal("0.00"),
            valor_cobrado=Decimal("149.99"),
            valor_a_receber=Decimal("149.99"),
            valor_reajuste=Decimal("199.99"),
            inicio_reajuste=(2026, 12),
            categoria="Saúde",
            pessoa="Amigo 1",
        )
        self.assertEqual(gasto.valor_lancamento_para_competencia(2026, 11), Decimal("149.99"))
        self.assertEqual(gasto.valor_a_receber_para_competencia(2026, 11), Decimal("149.99"))
        self.assertEqual(gasto.valor_lancamento_para_competencia(2026, 12), Decimal("199.99"))
        self.assertEqual(gasto.valor_a_receber_para_competencia(2026, 12), Decimal("199.99"))

    def test_regra_compra_terceiro(self) -> None:
        regra_cel = RegraCompraTerceiro(
            padrao_descricao="LOJA*ELETRONICOS",
            conta_ou_cartao="Bradesco Credito",
            pessoa="Amigo 1",
            valor_a_receber=Decimal("100.00"),
        )
        self.assertTrue(
            regra_cel.corresponde("LOJA*ELETRONICOS 11/12", "Bradesco Credito")
        )
        self.assertFalse(
            regra_cel.corresponde("LOJA*ELETRONICOS 11/12", "Inter Credito")
        )
        self.assertEqual(regra_cel.valor_a_receber, Decimal("100.00"))


if __name__ == "__main__":
    unittest.main()
