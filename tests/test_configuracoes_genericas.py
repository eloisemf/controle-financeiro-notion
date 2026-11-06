"""Testes para o carregamento de configurações genéricas, JSON e Notion dinâmico."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from financas.config import NotionDatabaseIds
from financas.domain.classification import ClassificadorCategoria, carregar_regras_categoria
from financas.domain.models import DEFAULT_CARTOES, RegraFechamento
from financas.notion.repositories import CatalogoRepository, PessoasRepository
from financas.services.ingestion import carregar_regras_terceiros


class FakeNotionClient:
    def __init__(self, paginas: list[dict] | None = None) -> None:
        self._paginas = paginas or []

    def query_database(self, database_id: str, filter_payload: dict | None = None) -> list[dict]:
        return self._paginas


class TestConfiguracoesGenericas(unittest.TestCase):
    def test_carregar_regras_categoria_json(self) -> None:
        dados = {
            "pizzaria": "Lanche",
            "livraria": "Educação",
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(dados, f)
            caminho_tmp = Path(f.name)

        try:
            regras = carregar_regras_categoria(caminho_tmp)
            self.assertEqual(len(regras), 2)
            classificador = ClassificadorCategoria(regras=regras)
            cat, incerto = classificador.classificar_descricao("Pizzaria do Bairro")
            self.assertEqual(cat, "Lanche")
            self.assertFalse(incerto)
        finally:
            caminho_tmp.unlink(missing_ok=True)

    def test_carregar_regras_terceiros_json(self) -> None:
        dados = [
            {
                "padrao_descricao": "COMPRA CURSO",
                "conta_ou_cartao": "Inter Credito",
                "pessoa": "Irmão",
                "valor_a_receber": "120.50",
                "categoria": "Educação",
                "uso_dinheiro": "Emprestimo",
            }
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(dados, f)
            caminho_tmp = Path(f.name)

        try:
            regras = carregar_regras_terceiros(caminho_tmp)
            self.assertEqual(len(regras), 1)
            self.assertEqual(regras[0].pessoa, "Irmão")
            self.assertEqual(regras[0].valor_a_receber, Decimal("120.50"))
            self.assertTrue(regras[0].corresponde("COMPRA CURSO PARC 01/03", "Inter Credito"))
        finally:
            caminho_tmp.unlink(missing_ok=True)

    def test_classificacao_transporte_privado_e_fronteira_palavras(self) -> None:
        classificador = ClassificadorCategoria()
        for termo in ["Uber", "DL*UberRides", "Dl Ub", "99app", "99 corrida"]:
            cat, incerto = classificador.classificar_descricao(termo)
            self.assertEqual(cat, "Transporte Privado")
            self.assertFalse(incerto)

        # Bug fix: "99" não deve casar com números que contenham 99 (ex: R$ 199 ou Loja 1990)
        cat_falso, incerto_falso = classificador.classificar_descricao("Loja 1990 Roupas")
        self.assertEqual(cat_falso, "Diversos")
        self.assertTrue(incerto_falso)

        # Bug fix: "luz" não deve casar com "Luzia"
        cat_luzia, incerto_luzia = classificador.classificar_descricao("Restaurante Santa Luzia")
        self.assertEqual(cat_luzia, "Diversos")
        self.assertTrue(incerto_luzia)

    def test_classificacao_contas_fixas_e_celular(self) -> None:
        classificador = ClassificadorCategoria()
        self.assertEqual(classificador.classificar_descricao("Conta de Luz Enel")[0], "Luz")
        self.assertEqual(classificador.classificar_descricao("Boleto Condomínio Edifício")[0], "Condominio")
        self.assertEqual(classificador.classificar_descricao("Pix Aluguel Imóvel")[0], "Aluguel")
        self.assertEqual(classificador.classificar_descricao("correio*Correios Cel")[0], "Internet")
        self.assertEqual(classificador.classificar_descricao("Recarga Celular")[0], "Internet")

    def test_catalogo_obter_cartoes_configurados_do_notion(self) -> None:
        fake_paginas = [
            {
                "id": "page-cartao-1",
                "properties": {
                    "Name": {"type": "title", "title": [{"plain_text": "Nubank"}]},
                    "Dia Vencimento": {"number": 15},
                    "Dias Fechamento": {"number": 7},
                    "Regra Fechamento": {"select": {"name": "Dias Corridos"}},
                    "Ativo": {"checkbox": True},
                },
            }
        ]
        client = FakeNotionClient(fake_paginas)
        catalogo = CatalogoRepository(
            client=client,  # type: ignore[arg-type]
            db_ids=NotionDatabaseIds(),
        )
        cartoes = catalogo.obter_cartoes_configurados()
        self.assertIn("Nubank", cartoes)
        self.assertEqual(cartoes["Nubank"].dia_vencimento, 15)
        self.assertEqual(cartoes["Nubank"].dias_antes_vencimento, 7)
        self.assertEqual(cartoes["Nubank"].regra_fechamento, RegraFechamento.DIAS_CORRIDOS)

        config_mesclada = catalogo.obter_configuracao_cartoes(DEFAULT_CARTOES)
        self.assertIn("Nubank", config_mesclada)
        self.assertIn("Inter Credito", config_mesclada)

    def test_pessoas_repository_listar_todas(self) -> None:
        fake_paginas = [
            {
                "id": "page-pessoa-1",
                "properties": {
                    "Nome": {"type": "title", "title": [{"plain_text": "Carlos"}]},
                    "Nome no Pix / Fatura": {"rich_text": [{"plain_text": "Carlos Silva; Carlinhos"}]},
                    "Saldo Inicial": {"number": 50.0},
                    "Observação": {"rich_text": [{"plain_text": "Amigo da faculdade"}]},
                },
            }
        ]
        client = FakeNotionClient(fake_paginas)
        repo = PessoasRepository(
            client=client,  # type: ignore[arg-type]
            page_ids=None,
            db_ids=NotionDatabaseIds(),
            database_id="db_pessoas",
        )
        pessoas = repo.listar_todas()
        self.assertEqual(len(pessoas), 1)
        self.assertEqual(pessoas[0].nome, "Carlos")
        self.assertEqual(pessoas[0].nome_no_pix_fatura, "Carlos Silva; Carlinhos")
        self.assertEqual(pessoas[0].saldo_inicial, Decimal("50.00"))


if __name__ == "__main__":
    unittest.main()
