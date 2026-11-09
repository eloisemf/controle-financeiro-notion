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
    """Testa a extração determinística de despesas (formato dicionário e separador `;`)."""

    def setUp(self) -> None:
        self.parser = MensagemGastoParser(
            conta_padrao="Santader Credito",
            classificador=ClassificadorCategoria(),
            calculadora=CalculadoraFatura(),
            cartoes=DEFAULT_CARTOES,
        )

    def test_atalho_ponto_e_virgula_preserva_nome_composto_e_padrao_nao_essencial(self) -> None:
        """Garante que 'filtro de cafe' não seja confundido com Lanche e saia Nao essencial."""
        gasto = self.parser.parse_mensagem("filtro de cafe ; 20 ; casa")
        self.assertIsNotNone(gasto)
        assert gasto is not None
        self.assertEqual(gasto.descricao, "filtro de cafe")
        self.assertEqual(gasto.valor, Decimal("20.00"))
        self.assertEqual(gasto.categoria, "Casa")
        self.assertEqual(gasto.conta_ou_cartao, "Santader Credito")
        self.assertEqual(gasto.uso_dinheiro, "Nao essencial")

    def test_formato_dicionario_multilinha_com_bullets_e_sem_acentos(self) -> None:
        msg = (
            "• Item: filtro de café\n"
            "• Valor: 20,00\n"
            "• Conta/Cartão: Santander crédito\n"
            "• Categoria: casa\n"
            "• Mês Competência: "
        )
        gasto = self.parser.parse_mensagem(msg)
        self.assertIsNotNone(gasto)
        assert gasto is not None
        self.assertEqual(gasto.descricao, "filtro de café")
        self.assertEqual(gasto.valor, Decimal("20.00"))
        self.assertEqual(gasto.conta_ou_cartao, "Santader Credito")
        self.assertEqual(gasto.categoria, "Casa")
        self.assertEqual(gasto.uso_dinheiro, "Nao essencial")
        self.assertIsNone(gasto.mes_competencia)

    def test_tolerancia_a_falta_de_acentos_em_categorias_e_contas(self) -> None:
        casos = [
            ("almoco ; 35,90 ; alimentacao ; debito", "Alimentação", "Santader Conta"),
            ("vitamina ; 49,90 ; saude ; santander credito", "Saúde", "Santader Credito"),
            ("bilhete ; 5,00 ; transporte publico ; pix", "Transporte Público", "Santader Conta"),
            ("ingresso ; 40 ; diversao ; inter", "Diversão", "Inter Credito"),
            ("fone ; 120 ; eletronicos ; amazon", "Eletronicos", "Amazon bradescard"),
        ]
        for texto, cat_esperada, conta_esperada in casos:
            with self.subTest(texto=texto):
                gasto = self.parser.parse_mensagem(texto)
                self.assertIsNotNone(gasto)
                assert gasto is not None
                self.assertEqual(gasto.categoria, cat_esperada)
                self.assertEqual(gasto.conta_ou_cartao, conta_esperada)
                self.assertEqual(gasto.uso_dinheiro, "Nao essencial")

    def test_sobrescrever_uso_para_essencial(self) -> None:
        gasto = self.parser.parse_mensagem("remedio ; 45,00 ; saude ; debito ; essencial")
        self.assertIsNotNone(gasto)
        assert gasto is not None
        self.assertEqual(gasto.categoria, "Saúde")
        self.assertEqual(gasto.conta_ou_cartao, "Santader Conta")
        self.assertEqual(gasto.uso_dinheiro, "Essencial")

    def test_gasto_sem_categoria_retorna_categoria_none(self) -> None:
        gasto = self.parser.parse_mensagem("bala de goma ; 5,00")
        self.assertIsNotNone(gasto)
        assert gasto is not None
        self.assertEqual(gasto.valor, Decimal("5.00"))
        self.assertEqual(gasto.descricao, "bala de goma")
        self.assertIsNone(gasto.categoria)

    def test_texto_solto_sem_separador_retorna_none_sem_adivinhacao(self) -> None:
        self.assertIsNone(self.parser.parse_mensagem("bom dia"))
        self.assertIsNone(self.parser.parse_mensagem("filtro de cafe 20 Santander crédito casa"))
        self.assertIsNone(self.parser.parse_mensagem("/resumo"))

    def test_criar_lancamento_transacao_valido(self) -> None:
        gasto = self.parser.parse_mensagem("cafe expresso ; 15 ; lanche")
        self.assertIsNotNone(gasto)
        assert gasto is not None
        lancamento = self.parser.criar_lancamento(gasto, data_referencia=date(2026, 11, 6))

        self.assertEqual(lancamento.descricao, "")
        self.assertEqual(lancamento.nome_fantasia, "cafe expresso")
        self.assertEqual(lancamento.valor, Decimal("15.00"))
        self.assertEqual(lancamento.categoria, "Lanche")
        self.assertEqual(lancamento.uso_dinheiro, "Nao essencial")
        self.assertEqual(lancamento.data_compra, date(2026, 11, 6))
        self.assertEqual(lancamento.mes_competencia, "2026-Novembro")
        self.assertEqual(lancamento.tipo, TipoTransacao.SAIDA)
        self.assertEqual(lancamento.status, StatusTransacao.REALIZADO)

    def test_criar_lancamento_sem_categoria_lanca_value_error(self) -> None:
        gasto = self.parser.parse_mensagem("bala ; 5")
        self.assertIsNotNone(gasto)
        assert gasto is not None
        with self.assertRaises(ValueError):
            self.parser.criar_lancamento(gasto)


class TestTelegramBotServicePrevistos(unittest.TestCase):
    """Testa lembretes de gastos fixos previstos, exclusão por botão e comandos."""

    def setUp(self) -> None:
        self.mock_config = MagicMock()
        self.mock_config.telegram_bot_token = "fake_token"
        self.mock_config.telegram_chat_id = "123456"
        self.mock_config.cartoes = {}

        self.mock_client = MagicMock()
        self.mock_telegram = MagicMock()
        self.mock_repos = MagicMock()
        self.mock_repos.catalogo.categorias = {
            "Casa": "id-casa",
            "Lanche": "id-lanche",
            "Saúde": "id-saude",
        }

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

    def test_tratar_callback_excluir_arquiva_lancamento_no_notion(self) -> None:
        self.service._tratar_callback(
            cb_id="cb-del",
            data="excluir:page-999",
            chat_id="123456",
            dry_run=False,
        )
        self.mock_repos.transacoes.excluir_lancamento.assert_called_once_with("page-999")
        self.mock_telegram.responder_callback_query.assert_called_once_with(
            "cb-del", texto="Lançamento excluído do Notion!"
        )
        self.mock_telegram.enviar_mensagem.assert_called_once()

    def test_lancamento_envia_botao_excluir_no_reply_markup(self) -> None:
        self.mock_repos.transacoes.criar_lancamento.return_value = "page-novo-1"
        self.service._tratar_gasto_texto("filtro de cafe ; 20 ; casa", chat_id="123456", dry_run=False)
        self.mock_repos.transacoes.criar_lancamento.assert_called_once()
        chamada = self.mock_telegram.enviar_mensagem.call_args
        self.assertIn("filtro de cafe", chamada[0][0])
        self.assertIn("Casa (Nao essencial)", chamada[0][0])
        markup = chamada[1].get("reply_markup", {})
        botao = markup["inline_keyboard"][0][0]
        self.assertEqual(botao["callback_data"], "excluir:page-novo-1")

    def test_comando_categorias_lista_opcoes(self) -> None:
        self.service._tratar_comando("/categorias", chat_id="123456")
        self.mock_telegram.enviar_mensagem.assert_called_once()
        texto_enviado = self.mock_telegram.enviar_mensagem.call_args[0][0]
        self.assertIn("CATEGORIAS DISPONÍVEIS NO NOTION", texto_enviado)
        self.assertIn("Casa", texto_enviado)

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
