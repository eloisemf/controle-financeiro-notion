"""Testes unitários para o módulo de integração com o Telegram."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import unittest
from unittest.mock import MagicMock

from financas.domain.billing import CalculadoraFatura
from financas.domain.classification import ClassificadorCategoria
from financas.domain.models import DEFAULT_CARTOES, StatusTransacao, TipoTransacao
from financas.telegram.parsers import MensagemGastoParser


class TestTelegramParsers(unittest.TestCase):
    """Testa a extração de despesas a partir de mensagens informais de texto."""

    def setUp(self) -> None:
        self.parser = MensagemGastoParser(
            conta_padrao="Santader Credito",
            classificador=ClassificadorCategoria(),
            calculadora=CalculadoraFatura(),
            cartoes=DEFAULT_CARTOES,
        )

    def test_gasto_sem_categoria_retorna_categoria_none(self) -> None:
        gasto = self.parser.parse_mensagem("5 bala")
        self.assertIsNotNone(gasto)
        assert gasto is not None
        self.assertEqual(gasto.valor, Decimal("5.00"))
        self.assertEqual(gasto.descricao, "bala")
        self.assertEqual(gasto.conta_ou_cartao, "Santader Credito")
        self.assertIsNone(gasto.categoria)

    def test_gasto_com_categoria_explicita(self) -> None:
        gasto = self.parser.parse_mensagem("5 bala lanche")
        self.assertIsNotNone(gasto)
        assert gasto is not None
        self.assertEqual(gasto.valor, Decimal("5.00"))
        self.assertEqual(gasto.descricao, "bala")
        self.assertEqual(gasto.conta_ou_cartao, "Santader Credito")
        self.assertEqual(gasto.categoria, "Lanche")
        self.assertEqual(gasto.uso_dinheiro, "Essencial")

    def test_gasto_com_virgula_e_categoria_conhecida(self) -> None:
        gasto = self.parser.parse_mensagem("12,50 ifood")
        self.assertIsNotNone(gasto)
        assert gasto is not None
        self.assertEqual(gasto.valor, Decimal("12.50"))
        self.assertEqual(gasto.descricao, "ifood")
        self.assertEqual(gasto.categoria, "Lanche")
        self.assertEqual(gasto.uso_dinheiro, "Essencial")

    def test_gasto_valor_no_fim(self) -> None:
        gasto = self.parser.parse_mensagem("bala de goma 5,00")
        self.assertIsNotNone(gasto)
        assert gasto is not None
        self.assertEqual(gasto.valor, Decimal("5.00"))
        self.assertEqual(gasto.descricao, "bala de goma")
        self.assertEqual(gasto.conta_ou_cartao, "Santader Credito")

    def test_gasto_com_conta_especifica_inter(self) -> None:
        gasto = self.parser.parse_mensagem("uber 22,50 inter")
        self.assertIsNotNone(gasto)
        assert gasto is not None
        self.assertEqual(gasto.valor, Decimal("22.50"))
        self.assertEqual(gasto.descricao, "uber")
        self.assertEqual(gasto.conta_ou_cartao, "Inter Credito")
        self.assertEqual(gasto.categoria, "Transporte Privado")

    def test_gasto_com_conta_debito_santander(self) -> None:
        gasto = self.parser.parse_mensagem("49,90 drogaria santander conta")
        self.assertIsNotNone(gasto)
        assert gasto is not None
        self.assertEqual(gasto.valor, Decimal("49.90"))
        self.assertEqual(gasto.descricao, "drogaria")
        self.assertEqual(gasto.conta_ou_cartao, "Santader Conta")
        self.assertEqual(gasto.categoria, "Saúde")

    def test_prefixo_r_cifrao(self) -> None:
        gasto = self.parser.parse_mensagem("R$ 35 almoço")
        self.assertIsNotNone(gasto)
        assert gasto is not None
        self.assertEqual(gasto.valor, Decimal("35.00"))
        self.assertEqual(gasto.descricao, "almoço")

    def test_mensagem_sem_valor_retorna_none(self) -> None:
        self.assertIsNone(self.parser.parse_mensagem("bom dia"))
        self.assertIsNone(self.parser.parse_mensagem("apenas texto sem numero"))

    def test_comando_barra_retorna_none(self) -> None:
        self.assertIsNone(self.parser.parse_mensagem("/resumo"))
        self.assertIsNone(self.parser.parse_mensagem("/investimentos"))

    def test_criar_lancamento_transacao_valido(self) -> None:
        gasto = self.parser.parse_mensagem("15 cafe lanche")
        self.assertIsNotNone(gasto)
        assert gasto is not None
        lancamento = self.parser.criar_lancamento(gasto, data_referencia=date(2026, 11, 6))

        self.assertEqual(lancamento.descricao, "cafe")
        self.assertEqual(lancamento.valor, Decimal("15.00"))
        self.assertEqual(lancamento.categoria, "Lanche")
        self.assertEqual(lancamento.data_compra, date(2026, 11, 6))
        self.assertEqual(lancamento.mes_competencia, "2026-Novembro")
        self.assertEqual(lancamento.tipo, TipoTransacao.SAIDA)
        self.assertEqual(lancamento.status, StatusTransacao.REALIZADO)

    def test_criar_lancamento_sem_categoria_lanca_value_error(self) -> None:
        gasto = self.parser.parse_mensagem("5 bala")
        self.assertIsNotNone(gasto)
        assert gasto is not None
        with self.assertRaises(ValueError):
            self.parser.criar_lancamento(gasto)


if __name__ == "__main__":
    unittest.main()
