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


class TestTelegramBotServicePrevistos(unittest.TestCase):
    """Testa lembretes de gastos fixos previstos e comando de confirmação."""

    def setUp(self) -> None:
        self.mock_config = MagicMock()
        self.mock_config.telegram_bot_token = "fake_token"
        self.mock_config.telegram_chat_id = "123456"
        self.mock_config.cartoes = {}

        self.mock_client = MagicMock()
        self.mock_telegram = MagicMock()
        self.mock_repos = MagicMock()

        from financas.telegram.service import TelegramBotService

        self.service = TelegramBotService(
            config=self.mock_config,
            client=self.mock_client,
            telegram_client=self.mock_telegram,
            repos=self.mock_repos,
        )

    def test_verificar_e_notificar_gastos_previstos_envia_inline_keyboard(self) -> None:
        self.mock_repos.transacoes.buscar_previstos_para_data.return_value = [
            {
                "page_id": "page-123",
                "descricao": "Aluguel",
                "valor": Decimal("1200.00"),
                "conta": "Santader Conta",
                "data": date(2026, 11, 7),
            }
        ]

        total = self.service.verificar_e_notificar_gastos_previstos(date(2026, 11, 7))
        self.assertEqual(total, 1)
        self.mock_telegram.enviar_mensagem.assert_called_once()
        chamada = self.mock_telegram.enviar_mensagem.call_args
        self.assertIn("Aluguel", chamada[0][0])
        self.assertIn("R$ 1.200,00", chamada[0][0])
        self.assertIn("reply_markup", chamada[1])

    def test_tratar_callback_confirmar_muda_status_para_realizado(self) -> None:
        self.service._tratar_callback(
            cb_id="cb-999",
            data="confirmar:page-123",
            chat_id="123456",
            dry_run=False,
        )
        self.mock_repos.transacoes.marcar_como_realizado.assert_called_once_with("page-123")
        self.mock_telegram.responder_callback_query.assert_called_once_with(
            "cb-999", texto="Status alterado para Realizado!"
        )
        self.mock_telegram.enviar_mensagem.assert_called_once()

    def test_comando_confirmar_por_nome(self) -> None:
        self.mock_repos.transacoes.buscar_previsto_por_nome.return_value = {
            "page_id": "page-456",
            "descricao": "Internet Claro",
            "valor": Decimal("120.00"),
        }
        self.service._tratar_comando("/confirmar Internet", chat_id="123456", dry_run=False)
        self.mock_repos.transacoes.marcar_como_realizado.assert_called_once_with("page-456")
        self.mock_telegram.enviar_mensagem.assert_called_once()
        self.assertIn("Internet Claro", self.mock_telegram.enviar_mensagem.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
