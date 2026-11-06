"""Testes unitários para os extratores de documentos (`financas.parsers`) e `MascaradorDadosSensiveis`."""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from financas.domain.models import Pessoa, TipoTransacao
from financas.parsers import (
    BradescoCartoesPdfParser,
    BradescoContaPdfParser,
    InterFaturaPdfParser,
    MascaradorDadosSensiveis,
    SantanderCartaoVisionParser,
    SantanderCartoesPdfParser,
    SantanderExtratoCsvParser,
)


class TestDocumentParsers(unittest.TestCase):
    def test_mascarador_oculta_cartao_e_cpf(self) -> None:
        bruto = "Cliente 123.456.789-00 Cartão 5432 **** **** 9876"
        mascarado = MascaradorDadosSensiveis.mascarar(bruto)
        self.assertNotIn("123.456.789-00", mascarado)
        self.assertNotIn("5432", mascarado)
        self.assertNotIn("9876", mascarado)

    def test_parse_texto_inter_ignora_pagamento_e_extrai_parcela(self) -> None:
        amostra = (
            "Fatura atual R$ 132,90\n"
            "**** **** **** **** 07/10/2026 R$ 132, 90\n"
            "12 de jan. 2026 LOJA ONLINE (Parcela 09 de 10) - R$ 132,90\n"
            "08 de set. 2026 PAGTO DEBITO AUTOMATICO - + R$ 167,80\n"
        )
        parser = InterFaturaPdfParser(nome_cartao="Inter Credito")
        itens = parser.parse_texto(amostra)

        self.assertEqual(len(itens), 1)
        item = itens[0]
        self.assertEqual(item.descricao, "LOJA ONLINE")
        self.assertEqual(item.descricao_com_parcela, "LOJA ONLINE - Parc 09/10")
        self.assertEqual(item.valor, Decimal("132.90"))
        self.assertEqual(item.data_compra, date(2026, 1, 12))
        self.assertEqual(item.mes_competencia, "2026-Setembro")
        self.assertEqual(item.parcela_atual, 9)
        self.assertEqual(item.total_parcelas, 10)

    def test_parse_bradesco_cartoes_extrai_fixos_e_parcelas(self) -> None:
        amostra = (
            "Fatura Mensal\n"
            "26/09 PAGTO ANTECIPADO PIX 851,79 -\n"
            "16/09 correio*Correios Cel SAO PAULO 30,00\n"
            "28/09 DM     *HELP.HBOMAX.CO NEW YORK 22,45\n"
            "23/11 LOJA*ELETRONICOS11/12 SAO PAULO 95,66\n"
            "Total da fatura em real 565,77\n"
            "Total Utilizado Disponível em\n"
            "19/10/2026\n"
        )
        parser = BradescoCartoesPdfParser()
        itens = parser.parse_texto(amostra)
        self.assertEqual(len(itens), 3)
        self.assertEqual(itens[0].descricao, "correio*Correios Cel")
        self.assertEqual(itens[0].data_compra, date(2026, 9, 16))
        self.assertEqual(itens[0].mes_competencia, "2026-Outubro")
        self.assertEqual(itens[1].descricao, "DM *HELP.HBOMAX.CO")
        self.assertEqual(itens[2].descricao, "LOJA*ELETRONICOS")
        self.assertEqual(itens[2].parcela_atual, 11)
        self.assertEqual(itens[2].total_parcelas, 12)
        self.assertEqual(itens[2].data_compra, date(2025, 11, 23))

    def test_parse_bradesco_conta_reembolso_e_movimentacao_interna(self) -> None:
        amostra = (
            "14/10/2026 PIX RECEBIDO REM: Amigo Pix Um 14/10 1332585 379,00 379,00\n"
            "PIX ENVIADO \n"
            "DES: OUTRA PESSOA 14/10 1946358 200,00 179,00\n"
            "PIX ENVIADO \n"
            "DES: TITULAR DA CONTA 14/10 1947251 179,00 0,00\n"
        )
        pessoas_teste = [
            Pessoa(nome="Amigo 1", nome_no_pix_fatura="Amigo Pix Um"),
        ]
        parser = BradescoContaPdfParser(
            nome_titular="TITULAR DA CONTA",
            pessoas_cadastradas=pessoas_teste,
        )
        itens = parser.parse_texto(amostra)
        self.assertEqual(len(itens), 3)
        self.assertEqual(itens[0].tipo, TipoTransacao.REEMBOLSO)
        self.assertEqual(itens[0].pessoa, "Amigo 1")
        self.assertEqual(itens[0].valor, Decimal("379.00"))
        self.assertEqual(itens[1].tipo, TipoTransacao.SAIDA)
        self.assertEqual(itens[2].tipo, TipoTransacao.MOVIMENTACAO_INTERNA)
        self.assertFalse(itens[2].eh_entrada_na_conta)

    def test_parse_santander_csv_separa_salario_faturas_e_reembolsos(self) -> None:
        amostra = (
            "Extrato de conta corrente\n"
            "Data;Descrição;Detalhe;Valor;Saldo\n"
            "23/10/2026;Liquido de vencimento;Cnpj 000;R$ 4.000,00;R$ 4.100,00\n"
            "26/10/2026;Pix enviado;Banco Bradesco S A;-R$ 565,77;R$ 3.534,23\n"
            "05/10/2026;Pix enviado;Amigo Pix Dois;-R$ 576,56;R$ 74,66\n"
            "05/10/2026;Pix recebido;Amigo Pix Dois;R$ 137,50;R$ 212,16\n"
            "14/10/2026;Pix recebido;Titular da Conta;R$ 179,00;R$ 391,16\n"
            "26/10/2026;Pix enviado;Titular da Conta;-R$ 88,67;R$ 302,49\n"
        )
        pessoas_teste = [
            Pessoa(nome="Amigo 2", nome_no_pix_fatura="Amigo Pix Dois"),
        ]
        parser = SantanderExtratoCsvParser(
            nome_titular="Titular da Conta",
            pessoas_cadastradas=pessoas_teste,
        )
        itens = parser.parse_texto(amostra)
        self.assertEqual(len(itens), 6)
        self.assertEqual(itens[0].tipo, TipoTransacao.ENTRADA)
        self.assertEqual(itens[0].descricao, "Salário (Líquido de vencimento)")
        self.assertEqual(itens[1].tipo, TipoTransacao.PAGAR_FATURA)
        self.assertEqual(itens[1].conta_destino, "Bradesco Credito")
        self.assertEqual(itens[2].tipo, TipoTransacao.SAIDA)
        self.assertIsNone(itens[2].pessoa)
        self.assertEqual(itens[3].tipo, TipoTransacao.REEMBOLSO)
        self.assertEqual(itens[3].pessoa, "Amigo 2")
        self.assertEqual(itens[4].tipo, TipoTransacao.MOVIMENTACAO_INTERNA)
        self.assertTrue(itens[4].eh_entrada_na_conta)
        self.assertEqual(itens[5].tipo, TipoTransacao.MOVIMENTACAO_INTERNA)
        self.assertFalse(itens[5].eh_entrada_na_conta)

    def test_parse_santander_cartao_texto_colado(self) -> None:
        amostra = (
            "18/10 Assinatura Teste 1 R$ 137,50\n"
            "23/10 Assinatura Teste 2 R$ 149,99\n"
        )
        parser = SantanderCartaoVisionParser()
        itens = parser.parse_texto_colado(amostra, ano_referencia=2026)
        self.assertEqual(len(itens), 2)
        self.assertEqual(itens[0].mes_competencia, "2026-Outubro")
        self.assertEqual(itens[1].mes_competencia, "2026-Novembro")

    def test_parse_santander_cartoes_pdf_fechada(self) -> None:
        amostra = (
            "Detalhamento da Fatura\n"
            "Pagamento e Demais Créditos\n"
            "Compra Data Descrição Parcela R$ US$\n"
            " 25/08 PAGAMENTO DE FATURA-INTERNET -803,78\n"
            " 28/08 ESTORNO COMPRA -94,99\n"
            "Parcelamentos\n"
            "Compra Data Descrição Parcela R$ US$\n"
            "2 04/08 LOJA EXEMPLO 02/04 145,87\n"
            " 08/08 RENNER 02/03 66,61\n"
            "Despesas\n"
            "Compra Data Descrição Parcela R$ US$\n"
            " 19/08 ASSINATURA EXEMPLO 137,50\n"
            " 22/09 MP *CLUBEGIRO 239,74\n"
            "Resumo da Fatura\n"
        )
        parser = SantanderCartoesPdfParser()
        itens = parser.parse_texto(
            amostra, nome_arquivo="Fatura_102026_TESTE.PDF.pdf", ano_referencia=2026
        )
        self.assertEqual(len(itens), 4)
        self.assertEqual(itens[0].descricao, "LOJA EXEMPLO")
        self.assertEqual(itens[0].parcela_atual, 2)
        self.assertEqual(itens[0].total_parcelas, 4)
        self.assertEqual(itens[0].valor, Decimal("145.87"))
        self.assertEqual(itens[0].mes_competencia, "2026-Setembro")
        self.assertEqual(itens[1].descricao, "RENNER")
        self.assertEqual(itens[1].parcela_atual, 2)
        self.assertEqual(itens[1].total_parcelas, 3)


if __name__ == "__main__":
    unittest.main()
