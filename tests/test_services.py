"""Testes unitários para `FaturaIngestionService`, `GastosFixosService`, `NotionClient` e CLIs."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import unittest
from unittest.mock import MagicMock, patch

from financas.config import AppConfig
from financas.domain.classification import ClassificadorCategoria, identificar_pessoa_reembolso
from financas.domain.models import (
    ChaveIdempotencia,
    ChaveParcialTransacao,
    GastoFixo,
    LancamentoTransacao,
    Pessoa,
    RegraCompraTerceiro,
    StatusTransacao,
    TipoTransacao,
    VinculoGastoFixo,
    decimal_para_centavos,
)
from financas.notion.client import NotionApiError, NotionClient
from financas.notion.repositories import NotionRepositories
from financas.parsers.base import exigir_decimal
from financas.services.fixed_expenses import GastosFixosService
from financas.services.ingestion import FaturaIngestionService
import ingest_fatura
import lancar_fixos


class FakeMesesRepo:
    def __init__(self) -> None:
        self.meses_por_nome: dict[str, str] = {"2026-Novembro": "mes_nov_id"}
        self.garantir_chamadas: list[tuple[int, int]] = []

    def garantir_mes(self, ano: int, mes: int) -> tuple[str, bool]:
        self.garantir_chamadas.append((ano, mes))
        return "mes_id", False


class FakeGastosFixosRepo:
    def __init__(self, ativos: list[GastoFixo]) -> None:
        self._ativos = ativos

    def listar_ativos(self) -> list[GastoFixo]:
        return list(self._ativos)


class FakeTransacoesRepo:
    def __init__(self) -> None:
        self.criados: list[LancamentoTransacao] = []
        self.conciliados: list[tuple[str, Decimal, date]] = []
        self.datas_ajustadas: list[tuple[str, date]] = []
        self.vinculos_mes: dict[str, dict[str, VinculoGastoFixo]] = {}
        self.chaves_mes: dict[str, dict[ChaveIdempotencia, str]] = {}

    def buscar_vinculos_gasto_fixo_no_mes(
        self, mes_competencia: str
    ) -> dict[str, VinculoGastoFixo]:
        return dict(self.vinculos_mes.get(mes_competencia, {}))

    def chaves_existentes_no_mes(
        self, mes_competencia: str
    ) -> dict[ChaveIdempotencia, str]:
        return dict(self.chaves_mes.get(mes_competencia, {}))

    def buscar_orfas_sem_mes(self) -> dict[ChaveParcialTransacao, str]:
        return {}

    def buscar_parceladas_por_chave_parcial(self) -> dict[ChaveParcialTransacao, str]:
        return {}

    def criar_lancamento(self, lancamento: LancamentoTransacao) -> str:
        self.criados.append(lancamento)
        return f"page_{len(self.criados)}"

    def conciliar_previsto_para_realizado(
        self,
        transacao_page_id: str,
        valor_real: Decimal,
        data_real: date,
        valor_a_receber: Decimal | None = None,
        pessoa: str | None = None,
    ) -> None:
        self.conciliados.append((transacao_page_id, valor_real, data_real))

    def atualizar_data_previsto(self, transacao_page_id: str, nova_data: date) -> None:
        self.datas_ajustadas.append((transacao_page_id, nova_data))


def _criar_fake_repos(
    ativos: list[GastoFixo],
    transacoes_repo: FakeTransacoesRepo | None = None,
) -> tuple[NotionRepositories, FakeMesesRepo, FakeTransacoesRepo]:
    meses_repo = FakeMesesRepo()
    tx_repo = transacoes_repo or FakeTransacoesRepo()
    gf_repo = FakeGastosFixosRepo(ativos)
    repos = NotionRepositories(
        catalogo=None,  # type: ignore[arg-type]
        meses=meses_repo,  # type: ignore[arg-type]
        pessoas=None,  # type: ignore[arg-type]
        gastos_fixos=gf_repo,  # type: ignore[arg-type]
        transacoes=tx_repo,  # type: ignore[arg-type]
    )
    return repos, meses_repo, tx_repo


class TestServicesAndDryRun(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig(notion_token="fake_token")
        self.spotify = GastoFixo(
            page_id="gf_spotify",
            nome="Spotify",
            nome_na_fatura="ASAAS *Kotas",
            valor_meu=Decimal("10.89"),
            valor_cobrado=Decimal("10.89"),
            categoria="Streamings",
            uso_dinheiro="Nao essencial",
            pago_com="Santander Credito",
            dia_cobranca=17,
            ativo=True,
        )

    def test_gastos_fixos_service_dry_run_nao_escreve_no_notion(self) -> None:
        repos, meses_repo, tx_repo = _criar_fake_repos([self.spotify])
        service = GastosFixosService(client=None, config=self.config, repos=repos)

        relatorio_dry = service.lancar_previstos_do_mes(2026, 11, dry_run=True)
        self.assertTrue(relatorio_dry.dry_run)
        self.assertTrue(relatorio_dry.tem_alteracoes)
        self.assertEqual(len(relatorio_dry.previstos_criados), 1)
        self.assertEqual(len(tx_repo.criados), 0)
        self.assertEqual(len(meses_repo.garantir_chamadas), 0)
        self.assertIn("[DRY-RUN / SIMULAÇÃO]", relatorio_dry.formatar_resumo())

        relatorio_real = service.lancar_previstos_do_mes(2026, 11, dry_run=False)
        self.assertFalse(relatorio_real.dry_run)
        self.assertEqual(len(tx_repo.criados), 1)
        self.assertEqual(len(meses_repo.garantir_chamadas), 1)

    def test_ingestion_service_dry_run_e_conciliacao_previsto_para_realizado(self) -> None:
        tx_repo = FakeTransacoesRepo()
        tx_repo.vinculos_mes["2026-Novembro"] = {
            "gf_spotify": VinculoGastoFixo("tx_previsto_1", StatusTransacao.PREVISTO.value, "2026-11-17")
        }
        repos, meses_repo, _ = _criar_fake_repos([self.spotify], tx_repo)
        regras_terceiros = (
            RegraCompraTerceiro(
                padrao_descricao="LOJA ONLINE",
                conta_ou_cartao="Inter Credito",
                pessoa="Amigo Teste",
                valor_a_receber=Decimal("132.90"),
            ),
        )
        service = FaturaIngestionService(
            client=None, config=self.config, repos=repos, regras_terceiros=regras_terceiros
        )

        lancamentos = [
            LancamentoTransacao(
                descricao="ASAAS *Kotas",
                valor=Decimal("10.89"),
                data_compra=date(2026, 11, 17),
                mes_competencia="2026-Novembro",
                tipo=TipoTransacao.SAIDA,
                categoria="Diversos",
                conta_ou_cartao="Santander Credito",
            ),
            LancamentoTransacao(
                descricao="LOJA ONLINE",
                valor=Decimal("132.90"),
                data_compra=date(2026, 11, 12),
                mes_competencia="2026-Novembro",
                tipo=TipoTransacao.SAIDA,
                categoria="Diversos",
                conta_ou_cartao="Inter Credito",
                parcela_atual=9,
                total_parcelas=10,
            ),
        ]

        rel_dry = service.importar_lancamentos(lancamentos, dry_run=True)
        self.assertTrue(rel_dry.dry_run)
        self.assertTrue(rel_dry.tem_alteracoes)
        self.assertEqual(rel_dry.parcelas_futuras_projetadas, 1)
        self.assertEqual(len(rel_dry.conciliados_previsto_para_realizado), 1)
        self.assertEqual(len(rel_dry.gravados), 2)
        self.assertEqual(len(tx_repo.criados), 0)
        self.assertEqual(len(tx_repo.conciliados), 0)
        self.assertEqual(len(meses_repo.garantir_chamadas), 0)

        rel_real = service.importar_lancamentos(lancamentos, dry_run=False)
        self.assertFalse(rel_real.dry_run)
        self.assertEqual(len(tx_repo.conciliados), 1)
        self.assertEqual(tx_repo.conciliados[0][0], "tx_previsto_1")
        self.assertEqual(len(tx_repo.criados), 2)
        self.assertEqual(tx_repo.criados[0].pessoa, "Amigo Teste")
        self.assertEqual(tx_repo.criados[0].valor_a_receber, Decimal("132.90"))
        self.assertEqual(tx_repo.criados[1].mes_competencia, "2026-Dezembro")
        self.assertEqual(tx_repo.criados[1].parcela_atual, 10)

    def test_classificacao_e_helpers_dominio(self) -> None:
        self.assertEqual(decimal_para_centavos(Decimal("10.89")), 1089)
        self.assertEqual(decimal_para_centavos(None), 0)
        self.assertEqual(exigir_decimal("R$ 45,90"), Decimal("45.90"))
        with self.assertRaises(ValueError):
            exigir_decimal("")

        cat, incerto = ClassificadorCategoria.classificar("IFOOD *RESTAURANTE")
        self.assertEqual((cat, incerto), ("Lanche", False))

        pessoas_custom = [
            Pessoa(nome="Prima", nome_no_pix_fatura="Maria Clara Silva; M C Silva")
        ]
        self.assertEqual(
            identificar_pessoa_reembolso("PIX RECEBIDO Maria Clara Silva", pessoas_custom),
            "Prima",
        )

    def test_cli_flags_modos_execucao(self) -> None:
        parser_ing = ingest_fatura.construir_parser_cli()
        args_default = parser_ing.parse_args([])
        self.assertFalse(args_default.dry_run)
        self.assertFalse(args_default.interactive)

        args_dry = parser_ing.parse_args(["--dry-run", "faturas/inter.pdf"])
        self.assertTrue(args_dry.dry_run)
        self.assertEqual(args_dry.caminhos, ["faturas/inter.pdf"])

        args_int = parser_ing.parse_args(["-i"])
        self.assertTrue(args_int.interactive)

        parser_fix = lancar_fixos.construir_parser_cli()
        args_fix = parser_fix.parse_args(["2026-Dezembro", "--dry-run"])
        self.assertEqual(args_fix.mes_competencia, "2026-Dezembro")
        self.assertTrue(args_fix.dry_run)

    def test_notion_client_paginacao_e_erro_nao_json(self) -> None:
        with NotionClient(token="fake-token", min_interval_s=0.0, max_retries=2) as client:
            resp_p1 = MagicMock(
                status_code=200,
                json=MagicMock(return_value={"results": [{"id": "1"}], "has_more": True, "next_cursor": "cur_2"}),
            )
            resp_p2 = MagicMock(
                status_code=200,
                json=MagicMock(return_value={"results": [{"id": "2"}], "has_more": False}),
            )
            with patch.object(client._session, "request", side_effect=[resp_p1, resp_p2]):
                itens = client.query_database("db_1")
                self.assertEqual(len(itens), 2)

            resp_erro = MagicMock(
                status_code=400,
                text="Bad Request Text",
                json=MagicMock(side_effect=ValueError("No JSON")),
            )
            with patch.object(client._session, "request", return_value=resp_erro):
                with self.assertRaises(NotionApiError) as ctx:
                    client.get_me()
                self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
