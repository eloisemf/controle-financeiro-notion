"""Testes unitários determinísticos para o módulo de investimentos, cotações e NotionClient."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import unittest
from unittest.mock import MagicMock

from financas.config import AppConfig, NotionDatabaseIds
from financas.domain.investments import (
    AporteInvestimento,
    CotacaoMercado,
    TipoOperacaoInvestimento,
)
from financas.services.investments import InvestimentosService
from financas.services.market_data import MarketDataService


class TestInvestmentsDomain(unittest.TestCase):
    """Testa modelos de domínio de investimentos."""

    def test_aporte_valor_total_calculado_com_taxas(self) -> None:
        aporte = AporteInvestimento(
            nome="1ª Compra AÇÃO3F",
            data=date(2026, 6, 15),
            tipo=TipoOperacaoInvestimento.COMPRA,
            investimento_id="fake-id",
            quantidade=Decimal("25"),
            preco_unitario=Decimal("20.91"),
            taxas=Decimal("0.14"),
        )
        self.assertEqual(aporte.valor_total, Decimal("522.89"))

    def test_aporte_sem_taxas(self) -> None:
        aporte = AporteInvestimento(
            nome="Aporte Renda Fixa",
            data="2026-08-28",
            tipo="Compra / Aporte",
            investimento_id="fake-id",
            quantidade=Decimal("1"),
            preco_unitario=Decimal("239.00"),
        )
        self.assertEqual(aporte.valor_total, Decimal("239.00"))


class TestMarketDataService(unittest.TestCase):
    """Testa lógica de tratamento e parsing de datas e tickers do MarketDataService."""

    def setUp(self) -> None:
        self.service = MarketDataService()

    def tearDown(self) -> None:
        self.service.close()

    def test_parse_dma_comparavel(self) -> None:
        d1 = self.service._parse_dma("30/10/2026")
        d2 = self.service._parse_dma("31/10/2025")
        self.assertEqual(d1, (2026, 10, 30))
        self.assertEqual(d2, (2025, 10, 31))
        self.assertGreater(d1, d2)

    def test_normalizar_ticker_b3_remove_apenas_sufixo_fracionario(self) -> None:
        self.assertEqual(MarketDataService.normalizar_ticker_b3("PETR4F"), "PETR4")
        self.assertEqual(MarketDataService.normalizar_ticker_b3("FLRY3F"), "FLRY3")
        self.assertEqual(MarketDataService.normalizar_ticker_b3("FLRY3"), "FLRY3")


class TestInvestimentosService(unittest.TestCase):
    """Testa o orquestrador InvestimentosService com mocks determinísticos."""

    def setUp(self) -> None:
        self.mock_client = MagicMock()
        self.mock_market = MagicMock()
        self.config = AppConfig(
            notion_token="fake-token",
            databases=NotionDatabaseIds(
                meses="db-meses",
                meus_investimentos="db-inv",
                aportes="db-aportes",
                historico_saldos="db-hist",
            ),
        )
        self.service = InvestimentosService(
            client=self.mock_client,
            config=self.config,
            market_data=self.mock_market,
        )

    def test_obter_resumo_carteira(self) -> None:
        self.mock_client.query_database.return_value = [
            {
                "id": "asset-1",
                "properties": {
                    "Nome": {"title": [{"plain_text": "ACAO3F"}]},
                    "Tipo": {"select": {"name": "Renda Variável - Ações"}},
                    "Valor Atual": {"number": 801.45},
                    "Valor Total": {"number": 910.75},
                    "Qtd": {"number": 45},
                },
            },
            {
                "id": "asset-2",
                "properties": {
                    "Nome": {"title": [{"plain_text": "Tesouro Selic 2029"}]},
                    "Tipo": {"select": {"name": "Renda Fixa - Pós-fixada (Selic/CDI)"}},
                    "Valor Atual": {"number": 10640.16},
                    "Valor Total": {"number": 9289.23},
                    "Qtd": {"number": 1},
                },
            },
        ]

        resumo = self.service.obter_resumo_carteira()
        self.assertEqual(resumo["total_ativos"], 2)
        self.assertEqual(resumo["patrimonio_total"], Decimal("11441.61"))
        self.assertEqual(resumo["total_investido"], Decimal("10199.98"))

    def test_atualizar_cotacoes_dry_run(self) -> None:
        self.mock_client.query_database.return_value = [
            {
                "id": "asset-1",
                "properties": {
                    "Nome": {"title": [{"plain_text": "ACAO3F"}]},
                    "Tipo": {"select": {"name": "Renda Variável - Ações"}},
                    "Valor Atual": {"number": 801.45},
                    "Valor Total": {"number": 910.75},
                    "Qtd": {"number": 45},
                },
            }
        ]
        self.mock_market.obter_cotacao_acao.return_value = CotacaoMercado(
            identificador="ACAO3F",
            preco_unitario=Decimal("18.00"),
            data_referencia="2026-11-03",
            fonte="Mock B3",
        )

        atualizacoes = self.service.atualizar_cotacoes_carteira(dry_run=True)
        self.assertEqual(len(atualizacoes), 1)
        self.assertEqual(atualizacoes[0]["novo"], Decimal("810.00"))
        self.mock_client.update_page.assert_not_called()

    def test_atualizar_cotacoes_tesouro_com_fracao_titulos(self) -> None:
        self.mock_client.query_database.return_value = [
            {
                "id": "asset-td",
                "properties": {
                    "Nome": {"title": [{"plain_text": "Tesouro Selic 2031"}]},
                    "Tipo": {"select": {"name": "Renda Fixa - Pós-fixada (Selic/CDI)"}},
                    "Valor Atual": {"number": 10500.00},
                    "Valor Total": {"formula": {"type": "number", "number": 9289.23}},
                    "Qtd": {"number": 1},
                    "Fração de Títulos": {"number": 0.529315},
                },
            }
        ]
        self.mock_market.obter_pu_tesouro_direto.return_value = CotacaoMercado(
            identificador="Tesouro Selic 01/03/2031",
            preco_unitario=Decimal("20101.76"),
            data_referencia="30/10/2026",
            fonte="Tesouro Transparente Oficial",
        )

        atualizacoes = self.service.atualizar_cotacoes_carteira(dry_run=False)
        self.assertEqual(len(atualizacoes), 1)
        self.assertEqual(atualizacoes[0]["novo"], Decimal("10640.16"))
        self.mock_client.update_page.assert_called_once()

    def test_sincronizar_aportes_carteira_recalcula_preco_medio_e_qtd(self) -> None:
        self.mock_client.query_database.side_effect = [
            # 1. listar_ativos_na_carteira
            [
                {
                    "id": "asset-rv",
                    "properties": {
                        "Nome": {"title": [{"plain_text": "BBAS3F"}]},
                        "Tipo": {"select": {"name": "Renda Variável - Ações"}},
                        "Valor Atual": {"number": 801.45},
                        "Valor Total": {"formula": {"type": "number", "number": 910.75}},
                        "Qtd": {"number": 1},
                    },
                }
            ],
            # 2. listar_aportes
            [
                {
                    "id": "ap-1",
                    "properties": {
                        "Nome": {"title": [{"plain_text": "1ª Compra"}]},
                        "Data": {"date": {"start": "2026-06-15"}},
                        "Tipo": {"select": {"name": "Compra / Aporte"}},
                        "Investimento": {"relation": [{"id": "asset-rv"}]},
                        "Quantidade": {"number": 25},
                        "Preço Unitário": {"number": 20.91},
                        "Taxas / Custos": {"number": 0},
                    },
                },
                {
                    "id": "ap-2",
                    "properties": {
                        "Nome": {"title": [{"plain_text": "2ª Compra"}]},
                        "Data": {"date": {"start": "2026-06-20"}},
                        "Tipo": {"select": {"name": "Compra / Aporte"}},
                        "Investimento": {"relation": [{"id": "asset-rv"}]},
                        "Quantidade": {"number": 20},
                        "Preço Unitário": {"number": 19.40},
                        "Taxas / Custos": {"number": 0},
                    },
                },
            ],
        ]

        sincronizados = self.service.sincronizar_aportes_carteira(dry_run=False)
        self.assertEqual(len(sincronizados), 1)
        self.assertEqual(sincronizados[0]["qtd"], Decimal("45.00"))
        self.assertEqual(sincronizados[0]["preco_medio"], Decimal("20.24"))
        self.assertEqual(sincronizados[0]["total_investido"], Decimal("910.75"))
        self.mock_client.update_page.assert_called_once()

    def test_fechar_mes_snapshot_idempotente_upsert_e_calcula_rendimento(self) -> None:
        self.mock_client.query_database.side_effect = [
            # 1. MesesRepository.meses_por_nome (carregar_mapa_meses)
            [
                {
                    "id": "mes-out",
                    "properties": {"Name": {"title": [{"plain_text": "2026-Outubro"}]}},
                },
                {
                    "id": "mes-nov",
                    "properties": {"Name": {"title": [{"plain_text": "2026-Novembro"}]}},
                },
            ],
            # 2. buscar_snapshots_do_mes (já existe snapshot para asset-1 em Nov)
            [
                {
                    "id": "snap-existente-1",
                    "properties": {
                        "Investimento": {"relation": [{"id": "asset-1"}]},
                    },
                }
            ],
            # 3. listar_ativos_na_carteira
            [
                {
                    "id": "asset-1",
                    "properties": {
                        "Nome": {"title": [{"plain_text": "Tesouro Selic 2031"}]},
                        "Tipo": {"select": {"name": "Renda Fixa - Pós-fixada"}},
                        "Valor Atual": {"number": 10640.16},
                        "Valor Total": {"number": 9289.23},
                        "Qtd": {"number": 1},
                    },
                },
                {
                    "id": "asset-2",
                    "properties": {
                        "Nome": {"title": [{"plain_text": "IPCA+"}]},
                        "Tipo": {"select": {"name": "Renda Fixa - Inflação"}},
                        "Valor Atual": {"number": 3896.66},
                        "Valor Total": {"number": 3905.54},
                        "Qtd": {"number": 1},
                    },
                },
            ],
            # 4. sincronizar_aportes_do_mes (aporte de 2500 em 2026-11)
            [
                {
                    "id": "ap-nov",
                    "properties": {
                        "Data": {"date": {"start": "2026-11-03"}},
                        "Tipo": {"select": {"name": "Compra / Aporte"}},
                        "Quantidade": {"number": 1},
                        "Preço Unitário": {"number": 2500.00},
                        "Taxas / Custos": {"number": 0},
                        "Mês de Competência": {"relation": [{"id": "mes-nov"}]},
                    },
                }
            ],
            # 5. obter_patrimonio_do_mes anterior (mes-out = 11900.00)
            [
                {
                    "id": "snap-out-1",
                    "properties": {"Valor no Dia": {"number": 11900.00}},
                }
            ],
        ]
        self.mock_client.create_page.return_value = {"id": "snap-novo-2"}

        res = self.service.fechar_mes_snapshot(data_foto="2026-11-03", dry_run=False)
        self.assertEqual(res["mes"], "2026-Novembro")
        self.assertEqual(res["criados"], 1)
        self.assertEqual(res["atualizados"], 1)
        self.assertEqual(res["total_patrimonio"], Decimal("14536.82"))
        self.assertEqual(res["patrimonio_anterior"], Decimal("11900.00"))
        self.assertEqual(res["aportes_mes"], Decimal("2500.00"))
        # Rendimento = 14536.82 - 11900.00 - 2500.00 = +136.82
        self.assertEqual(res["rendimento_mes"], Decimal("136.82"))


if __name__ == "__main__":
    unittest.main()
