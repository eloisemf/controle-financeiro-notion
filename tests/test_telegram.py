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
            conta_padrao="Santander Credito",
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
        self.assertEqual(gasto.conta_ou_cartao, "Santander Credito")
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
        self.assertEqual(gasto.conta_ou_cartao, "Santander Credito")
        self.assertEqual(gasto.categoria, "Casa")
        self.assertEqual(gasto.uso_dinheiro, "Nao essencial")
        self.assertIsNone(gasto.mes_competencia)

    def test_tolerancia_a_falta_de_acentos_em_categorias_e_contas(self) -> None:
        casos = [
            ("almoco ; 35,90 ; alimentacao ; debito", "Alimentação", "Santander Conta"),
            ("vitamina ; 49,90 ; saude ; santander credito", "Saúde", "Santander Credito"),
            ("bilhete ; 5,00 ; transporte publico ; pix", "Transporte Público", "Santander Conta"),
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
        self.assertEqual(gasto.conta_ou_cartao, "Santander Conta")
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

    def test_parse_reembolso_e_criacao_lancamento(self) -> None:
        reemb = self.parser.parse_reembolso("/reembolso amigo ; 150,00")
        self.assertIsNotNone(reemb)
        assert reemb is not None
        self.assertEqual(reemb.pessoa_informada, "amigo")
        self.assertEqual(reemb.valor, Decimal("150.00"))
        self.assertEqual(reemb.conta_destino, "Santander Conta")

        reemb_espaco = self.parser.parse_reembolso("/reembolso mae 200")
        self.assertIsNotNone(reemb_espaco)
        assert reemb_espaco is not None
        self.assertEqual(reemb_espaco.pessoa_informada, "mae")
        self.assertEqual(reemb_espaco.valor, Decimal("200.00"))

        lanc = self.parser.criar_lancamento_reembolso(
            reemb,
            nome_oficial_pessoa="Amigo",
            data_referencia=date(2026, 11, 10),
        )
        self.assertEqual(lanc.descricao, "Pix Reembolso Amigo")
        self.assertEqual(lanc.tipo, TipoTransacao.REEMBOLSO)
        self.assertEqual(lanc.categoria, "Emprestimo cartão")
        self.assertEqual(lanc.conta_ou_cartao, "Santander Conta")
        self.assertEqual(lanc.mes_competencia, "2026-Novembro")
        self.assertEqual(lanc.pessoa, "Amigo")


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
                "conta": "Santander Conta",
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

    def test_comando_reembolso_cria_lancamento_tipo_reembolso(self) -> None:
        self.mock_repos.pessoas.resolver_nome_pessoa.return_value = "Amigo"
        self.mock_repos.pessoas.pessoas_por_nome = {"Amigo": "id-amigo", "Mãe": "id-mae"}
        self.mock_repos.transacoes.criar_lancamento.return_value = "page-reemb-1"

        self.service._tratar_comando("/reembolso amigo ; 150,00", chat_id="123456", dry_run=False)

        self.mock_repos.transacoes.criar_lancamento.assert_called_once()
        lanc = self.mock_repos.transacoes.criar_lancamento.call_args[0][0]
        self.assertEqual(lanc.tipo, TipoTransacao.REEMBOLSO)
        self.assertEqual(lanc.pessoa, "Amigo")
        self.assertEqual(lanc.valor, Decimal("150.00"))
        self.assertEqual(lanc.conta_ou_cartao, "Santander Conta")

        chamada = self.mock_telegram.enviar_mensagem.call_args
        self.assertIn("Reembolso Registrado no Notion", chamada[0][0])
        self.assertIn("Amigo", chamada[0][0])
        markup = chamada[1].get("reply_markup", {})
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "excluir:page-reemb-1")

    def test_telegram_client_retry_em_timeout_de_rede(self) -> None:
        import requests
        from unittest.mock import patch
        from financas.telegram.client import TelegramClient

        client = TelegramClient(token="fake:token", max_retries=3, backoff_s=0.0)
        resp_ok = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"ok": True, "result": [{"update_id": 10}]}),
        )
        with patch.object(
            client._session,
            "request",
            side_effect=[requests.exceptions.ReadTimeout("Read timed out"), resp_ok],
        ) as mock_req:
            updates = client.obter_atualizacoes(timeout=0)
            self.assertEqual(len(updates), 1)
            self.assertEqual(mock_req.call_count, 2)

    def test_lancamento_parcelado_texto_atalho_e_dicionario(self) -> None:
        parser = MensagemGastoParser(
            conta_padrao="Santander Credito",
            regras_categorias=(("vestuario", "Vestuário"),),
        )
        gasto_5x = parser.parse_mensagem("tenis ; 549,99 ; vestuario ; 5x")
        self.assertIsNotNone(gasto_5x)
        assert gasto_5x is not None
        self.assertEqual(gasto_5x.valor, Decimal("110.00"))
        self.assertEqual((gasto_5x.parcela_atual, gasto_5x.total_parcelas), (1, 5))

        lanc = parser.criar_lancamento(gasto_5x, data_referencia=date(2026, 11, 12))
        self.assertEqual(lanc.descricao, "tenis - Parc 01/05")
        self.assertEqual(lanc.nome_fantasia, "tenis")

        gasto_frac = parser.parse_mensagem("tenis ; 110 ; vestuario ; 02/05")
        self.assertIsNotNone(gasto_frac)
        assert gasto_frac is not None
        self.assertEqual(gasto_frac.valor, Decimal("110"))
        self.assertEqual((gasto_frac.parcela_atual, gasto_frac.total_parcelas), (2, 5))

        gasto_dict = parser.parse_mensagem(
            "Item: jaqueta\nValor: 300\nCategoria: Vestuario\nParcelas: 3x"
        )
        self.assertIsNotNone(gasto_dict)
        assert gasto_dict is not None
        self.assertEqual(gasto_dict.valor, Decimal("100.00"))
        self.assertEqual((gasto_dict.parcela_atual, gasto_dict.total_parcelas), (1, 3))

    def test_bot_service_projeta_parcelas_futuras_ao_lancar_texto_5x(self) -> None:
        self.mock_repos.parcelas.garantir_parcela.return_value = "parcela-mestre-1"
        self.mock_repos.transacoes.criar_lancamento.side_effect = [
            "p1",
            "p2",
            "p3",
            "p4",
            "p5",
        ]
        self.service._tratar_gasto_texto("tenis ; 500 ; casa ; 5x", chat_id="123456", dry_run=False)

        self.mock_repos.parcelas.garantir_parcela.assert_called_once()
        self.assertEqual(self.mock_repos.transacoes.criar_lancamento.call_count, 5)
        chamadas = [c[0][0] for c in self.mock_repos.transacoes.criar_lancamento.call_args_list]
        self.assertEqual([c.descricao for c in chamadas], [
            "tenis - Parc 01/05",
            "tenis - Parc 02/05",
            "tenis - Parc 03/05",
            "tenis - Parc 04/05",
            "tenis - Parc 05/05",
        ])
        self.assertTrue(all(c.valor == Decimal("100.00") for c in chamadas))
        self.assertTrue(all(c.compra_parcelada_id == "parcela-mestre-1" for c in chamadas))
        msg = self.mock_telegram.enviar_mensagem.call_args[0][0]
        self.assertIn("01/05 (+4 futura(s) projetada(s))", msg)
        self.assertIn("vinculado em 📦 Parcelas", msg)

    def test_comando_investir_debita_conta_e_registra_aporte(self) -> None:
        from financas.domain.investments import AtivoCarteira

        self.mock_config.databases = MagicMock(
            aportes="db-aportes",
            meus_investimentos="db-invest",
        )
        self.service._invest_service = MagicMock()
        self.service._invest_service.resolver_ativo.return_value = AtivoCarteira(
            id="ativo-ipca",
            nome="IPCA+",
            tipo="Renda Fixa - Inflação (IPCA+)",
            qtd=Decimal("1"),
            valor_atual=Decimal("3000.00"),
            valor_investido=Decimal("2800.00"),
        )
        self.mock_repos.transacoes.criar_lancamento.return_value = "tx-aporte-1"

        self.service._tratar_comando(
            "/investir IPCA+ ; 500,00 ; santander conta ; 08/11/2026",
            chat_id="123456",
            dry_run=False,
        )

        self.mock_repos.transacoes.criar_lancamento.assert_called_once()
        lanc = self.mock_repos.transacoes.criar_lancamento.call_args[0][0]
        self.assertEqual(lanc.tipo, TipoTransacao.MOVIMENTACAO_INTERNA)
        self.assertEqual(lanc.valor, Decimal("500.00"))
        self.assertEqual(lanc.conta_ou_cartao, "Santander Conta")
        self.assertEqual(lanc.data_compra, date(2026, 11, 8))
        self.assertEqual(lanc.categoria, "Investimento")

        self.service._invest_service.registrar_aporte.assert_called_once_with(
            ativo_id="ativo-ipca",
            ativo_nome="IPCA+",
            valor_total=Decimal("500.00"),
            data_aporte=date(2026, 11, 8),
            quantidade=None,
            dry_run=False,
        )
        msg = self.mock_telegram.enviar_mensagem.call_args[0][0]
        self.assertIn("Investimento Registrado no Notion", msg)
        self.assertIn("IPCA+", msg)
        self.assertIn("500,00", msg)

    def test_parcelas_repository_cria_e_reaproveita_idempotente(self) -> None:
        from financas.domain.models import LancamentoTransacao, StatusTransacao
        from financas.notion.repositories.parcelas import ParcelasRepository

        mock_client = MagicMock()
        mock_client.query_database.return_value = []
        mock_client.create_page.return_value = {"id": "11111111-1111-1111-1111-111111111111"}
        mock_catalogo = MagicMock()
        mock_catalogo.id_conta.return_value = "conta-santander"

        repo = ParcelasRepository(mock_client, "db-parcelas", mock_catalogo)
        lanc = LancamentoTransacao(
            descricao="Loja Exemplo - Parc 01/05",
            valor=Decimal("100.00"),
            data_compra=date(2026, 11, 12),
            mes_competencia="2026-Novembro",
            tipo=TipoTransacao.SAIDA,
            categoria="Diversos",
            conta_ou_cartao="Santander Credito",
            status=StatusTransacao.REALIZADO,
            parcela_atual=1,
            total_parcelas=5,
        )
        id_1 = repo.garantir_parcela(lanc)
        id_2 = repo.garantir_parcela(lanc)
        self.assertEqual(id_1, "11111111111111111111111111111111")
        self.assertEqual(id_2, id_1)
        mock_client.create_page.assert_called_once()

    def test_dry_run_nao_avanca_offset_nem_atualiza_cotacoes(self) -> None:
        self.service._invest_service.atualizar_cotacoes_carteira = MagicMock(return_value=[])
        self.service._market.obter_cdi_mes_atual = MagicMock(return_value=None)
        self.service._invest_service.obter_resumo_carteira = MagicMock(
            return_value=MagicMock(
                patrimonio_total=Decimal("0.00"),
                lucro_total=Decimal("0.00"),
                total_ativos=0,
            )
        )
        self.mock_telegram.obter_atualizacoes.return_value = [
            {
                "update_id": 500,
                "message": {
                    "chat": {"id": 123456},
                    "text": "/investimentos",
                    "date": 1700000000,
                },
            }
        ]
        processadas = self.service.processar_mensagens_pendentes(dry_run=True)
        self.assertEqual(processadas, 1)
        self.mock_telegram.obter_atualizacoes.assert_called_once_with(timeout=0)
        self.service._invest_service.atualizar_cotacoes_carteira.assert_called_once_with(dry_run=True)

    def test_resumo_semanal_usa_ciclo_dia_25_e_faturas_de_meses(self) -> None:
        from financas.telegram.reports import TelegramReportBuilder

        self.mock_repos.meses.obter_resumo_mes.return_value = {
            "meus_gastos": Decimal("1200.00"),
            "saldo_livre": Decimal("900.00"),
            "meta_mensal": Decimal("2000.00"),
            "receita": Decimal("0.00"),
            "fatura_santander": Decimal("800.00"),
            "fatura_bradesco": Decimal("350.00"),
            "fatura_inter": Decimal("0.00"),
        }
        self.mock_client.query_database.return_value = []

        builder = TelegramReportBuilder(
            self.mock_client,
            self.mock_repos,
            self.service._invest_service,
        )
        texto = builder.gerar_resumo_semanal(data_ref=date(2026, 11, 25))

        self.mock_repos.meses.obter_resumo_mes.assert_called_once_with("2026-Dezembro")
        self.assertIn("2026-Dezembro", texto)
        self.assertIn("Santander Credito: *R$ 800,00*", texto)
        self.assertIn("Bradesco Credito: *R$ 350,00*", texto)
        self.assertIn("~R$ 30,00/dia (ou R$ 210,00/sem)", texto)
        self.assertNotIn("Inter Credito", texto)
        self.mock_repos.transacoes.listar_previstos_periodo.assert_called_once_with(
            self.mock_repos.meses.id_mes.return_value,
            date(2026, 11, 25),
            date(2026, 12, 5),
        )

    def test_disparar_resumo_semanal_dry_run_e_context_manager(self) -> None:
        self.service._invest_service.atualizar_cotacoes_carteira = MagicMock(return_value=[])
        self.service._reporter.gerar_resumo_semanal = MagicMock(return_value="resumo-teste")
        with self.service as bot:
            resumo = bot.disparar_resumo_semanal(dry_run=True)

        self.assertEqual(resumo, "resumo-teste")
        self.service._invest_service.atualizar_cotacoes_carteira.assert_called_once_with(dry_run=True)
        self.mock_telegram.enviar_mensagem.assert_not_called()
        self.mock_telegram.close.assert_not_called()

    def test_comando_analise_gera_top_categorias_uso_e_poupanca(self) -> None:
        self.mock_repos.meses.obter_resumo_mes.return_value = {
            "meus_gastos": Decimal("1000.00"),
            "saldo_livre": Decimal("2000.00"),
            "meta_mensal": Decimal("2850.00"),
            "receita": Decimal("3000.00"),
            "total_parcelado_mes": Decimal("300.00"),
            "aportes_mes": Decimal("500.00"),
            "taxa_poupanca": Decimal("0.6667"),
        }
        self.mock_repos.transacoes.listar_analise_gastos_mes.return_value = [
            {
                "nome": "Mercado",
                "valor_meu": Decimal("600.00"),
                "categoria": "Mercado",
                "uso_dinheiro": "Essencial",
                "parcelado": False,
            },
            {
                "nome": "Tenis - Parc 01/05",
                "valor_meu": Decimal("400.00"),
                "categoria": "Vestuario",
                "uso_dinheiro": "Nao essencial",
                "parcelado": True,
            },
        ]

        self.service._tratar_comando("/analise", chat_id="123456", dry_run=False)
        msg = self.mock_telegram.enviar_mensagem.call_args[0][0]
        self.assertIn("ANÁLISE FINANCEIRA", msg)
        self.assertIn("Mercado: *R$ 600,00* (60.0%)", msg)
        self.assertIn("Essencial: *R$ 600,00* (60.0%)", msg)
        self.assertIn("Comprometido em Parcelas: *R$ 400,00*", msg)
        self.assertIn("Taxa de Poupança: *66.7%*", msg)

    def test_comando_parcelas_lista_compras_ativas_e_totais(self) -> None:
        self.mock_repos.parcelas.listar_parcelas_ativas.return_value = [
            {
                "gasto": "Tenis",
                "parcela_atual": 2,
                "total_parcelas": 5,
                "valor_parcela": Decimal("110.00"),
                "falta_pagar": Decimal("440.00"),
                "fim": "março/27",
                "cartao": "Santander Credito",
                "de_quem": "Meu",
            }
        ]
        self.service._tratar_comando("/parcelas", chat_id="123456", dry_run=False)
        msg = self.mock_telegram.enviar_mensagem.call_args[0][0]
        self.assertIn("COMPRAS PARCELADAS ATIVAS", msg)
        self.assertIn("Comprometimento Mensal (Meu): *R$ 110,00/mês*", msg)
        self.assertIn("Total Restante a Pagar (Meu): *R$ 440,00*", msg)
        self.assertIn("*Tenis*: 02/05", msg)

    def test_comprometimento_futuro_3_meses_alerta_acima_50_por_cento(self) -> None:
        from financas.domain.models import GastoFixo
        from financas.telegram.reports import TelegramReportBuilder

        self.mock_repos.meses.obter_resumo_mes.return_value = {
            "salario_base": Decimal("3000.00"),
            "bonus": Decimal("0.00"),
            "receita": Decimal("0.00"),
        }
        self.mock_repos.gastos_fixos.listar_ativos.return_value = [
            GastoFixo(
                nome="Aluguel",
                valor_meu=Decimal("1200.00"),
                valor_cobrado=Decimal("1200.00"),
                categoria="Aluguel",
                pago_com="Santander Conta",
            )
        ]
        self.mock_repos.parcelas.listar_parcelas_ativas.return_value = [
            {
                "gasto": "Eletrodomestico",
                "parcela_atual": 11,
                "total_parcelas": 12,
                "valor_parcela": Decimal("300.00"),
                "falta_pagar": Decimal("600.00"),
                "fim": "dezembro/26",
                "cartao": "Bradesco Credito",
                "de_quem": "Meu",
            }
        ]
        builder = TelegramReportBuilder(
            self.mock_client,
            self.mock_repos,
            self.service._invest_service,
        )
        texto = builder.gerar_comprometimento_futuro(
            data_ref=date(2026, 11, 16),
            parcela_extra=Decimal("150.00"),
            total_parcelas_extra=3,
        )
        self.assertIn("Comprometimento Próximos 3 Meses", texto)
        # Dezembro/26: 1200 fixo + 300 (ultima parcela) + 150 (extra) = 1650 = 55.0%
        self.assertIn("2026-Dezembro*: *R$ 1.650,00* (55.0% da renda", texto)
        # Janeiro/27: 1200 fixo + 0 + 150 (extra) = 1350 = 45.0%
        self.assertIn("2027-Janeiro*: *R$ 1.350,00* (45.0% da renda", texto)
        self.assertIn("acima de 50%", texto)


if __name__ == "__main__":
    unittest.main()
