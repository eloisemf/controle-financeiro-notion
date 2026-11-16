"""Repositório de meses de competência e fórmulas do painel no Notion."""

from __future__ import annotations

from decimal import Decimal
import logging

from financas.domain.billing import formatar_mes_competencia
from financas.domain.models import CartaoConfig, normalizar_texto_chave
from financas.notion.builders import NotionParser, NotionPropertyBuilder as Prop
from financas.notion.client import NotionClient
from financas.notion.repositories.base import BaseNotionRepository
from financas.notion.schema import ColunasMeses, ColunasTransacoes

logger = logging.getLogger(__name__)


class MesesRepository(BaseNotionRepository):
    """Gerencia páginas e propriedades da tabela `Meses`."""

    FORMULAS_BASE: dict[str, str] = {
        "Pix/Débito": (
            f'prop("{ColunasMeses.TRANSACOES}").filter(current.prop("{ColunasTransacoes.TIPO_TRANSACAO}") == "Saida" '
            f'and current.prop("{ColunasTransacoes.SAIDA_DE}").first().prop("Tipo") == "Conta (Débito e Pix)")'
            f'.map(current.prop("{ColunasTransacoes.VALOR}")).sum()'
        ),
        "Gastos fixos": (
            f'prop("{ColunasMeses.TRANSACOES}").filter(current.prop("{ColunasTransacoes.TIPO_TRANSACAO}") == "Saida" '
            f'and current.prop("{ColunasTransacoes.RECORRENCIA}") == "Fixo")'
            f'.map(current.prop("{ColunasTransacoes.VALOR}")).sum()'
        ),
        "Fixos previstos": (
            f'prop("{ColunasMeses.TRANSACOES}").filter(current.prop("{ColunasTransacoes.TIPO_TRANSACAO}") == "Saida" '
            f'and current.prop("{ColunasTransacoes.STATUS}") == "Previsto")'
            f'.map(current.prop("{ColunasTransacoes.VALOR}")).sum()'
        ),
    }

    FORMULAS_ANALITICAS: dict[str, str] = {
        "Total Faturas Cartões": (
            'prop("Fatura Santander") + prop("Fatura Bradesco") + prop("Fatura Inter")'
        ),
        "Taxa de Poupança (%)": (
            'let(salario, if(empty(prop("Salário Base")), 0, prop("Salário Base")), '
            'let(bonus, if(empty(prop("Bônus")), 0, prop("Bônus")), '
            'let(rendaTotal, (if(prop("Receita") > 0, prop("Receita"), salario)) + bonus, '
            'if(rendaTotal <= 0, toNumber(""), round(((rendaTotal - prop("Meus Gastos")) / rendaTotal) * 10000) / 10000))))'
        ),
    }

    def __init__(self, client: NotionClient, database_id: str) -> None:
        super().__init__(client)
        self._database_id = database_id
        self._mapa_meses: dict[str, str] | None = None

    @classmethod
    def gerar_formulas_painel(
        cls, cartoes: dict[str, CartaoConfig] | None = None
    ) -> dict[str, str]:
        """Gera dinamicamente o mapa de fórmulas do painel a partir dos cartões ativos."""
        formulas: dict[str, str] = {}
        if cartoes:
            for nome_cartao, cfg in cartoes.items():
                if not cfg.ativo:
                    continue
                curto = nome_cartao.split()[0]
                chave = f"Fatura {curto}"
                if chave in formulas:
                    continue
                formulas[chave] = (
                    f'prop("{ColunasMeses.TRANSACOES}").filter(current.prop("{ColunasTransacoes.TIPO_TRANSACAO}") == "Saida" '
                    f'and current.prop("{ColunasTransacoes.SAIDA_DE}").first().prop("Name") == "{nome_cartao}")'
                    f'.map(current.prop("{ColunasTransacoes.VALOR}")).sum()'
                )
        formulas.update(cls.FORMULAS_BASE)
        return formulas

    @property
    def meses_por_nome(self) -> dict[str, str]:
        if self._mapa_meses is None:
            self._mapa_meses = self._carregar_mapa_titulo_id(self._database_id)
        return self._mapa_meses

    def id_mes(self, label: str) -> str:
        chave = label.strip()
        if chave not in self.meses_por_nome:
            raise KeyError(f"Mês '{chave}' não cadastrado na tabela Meses.")
        return self.meses_por_nome[chave]

    def obter_resumo_mes(self, mes_label: str) -> dict[str, Decimal]:
        """Consulta os valores consolidados e faturas de um mês na tabela `Meses`."""
        mes_id = self.id_mes(mes_label)
        pagina = self._client.request("GET", f"/pages/{mes_id}")
        zero = Decimal("0.00")
        return {
            "meus_gastos": NotionParser.prop_decimal(pagina, "Meus Gastos") or zero,
            "saldo_livre": NotionParser.prop_decimal(pagina, "Saldo Livre Real") or zero,
            "meta_mensal": NotionParser.prop_decimal(pagina, "Meta Mensal") or zero,
            "receita": NotionParser.prop_decimal(pagina, "Receita") or zero,
            "salario_base": NotionParser.prop_decimal(pagina, "Salário Base") or zero,
            "bonus": NotionParser.prop_decimal(pagina, "Bônus") or zero,
            "fatura_santander": NotionParser.prop_decimal(pagina, "Fatura Santander") or zero,
            "fatura_bradesco": NotionParser.prop_decimal(pagina, "Fatura Bradesco") or zero,
            "fatura_inter": NotionParser.prop_decimal(pagina, "Fatura Inter") or zero,
            "total_faturas_cartoes": NotionParser.prop_decimal(pagina, "Total Faturas Cartões") or zero,
            "total_parcelado_mes": NotionParser.prop_decimal(pagina, "Total Parcelado no Mês") or zero,
            "aportes_mes": NotionParser.prop_decimal(pagina, ColunasMeses.APORTES_MES) or zero,
            "taxa_poupanca": NotionParser.prop_decimal_raw(pagina, "Taxa de Poupança (%)") or zero,
        }

    def garantir_mes(self, ano: int, mes: int) -> tuple[str, bool]:
        """Garante que o mês `(ano, mes)` exista na tabela Meses.

        Retorna `(page_id, criado_agora)`.
        """
        label = formatar_mes_competencia(ano, mes)
        if label in self.meses_por_nome:
            return self.meses_por_nome[label], False

        nova_pagina = self._client.create_page(
            database_id=self._database_id,
            properties={
                ColunasMeses.NOME: Prop.title(label),
                ColunasMeses.MES_DATA: Prop.date_iso(f"{ano:04d}-{mes:02d}-01"),
            },
        )
        page_id = NotionParser.normalize_id(nova_pagina["id"])
        self._client.update_page(
            page_id=nova_pagina["id"],
            properties={ColunasMeses.AUTO_RELACAO: Prop.relation(nova_pagina["id"])},
        )
        self.meses_por_nome[label] = page_id
        logger.info("Criado mês de competência '%s' (id=%s) no Notion.", label, page_id)
        return page_id, True

    def verificar_formulas_faltantes(
        self, cartoes: dict[str, CartaoConfig] | None = None
    ) -> list[str]:
        db_schema = self._client.get_database(self._database_id)
        existentes_norm = {
            normalizar_texto_chave(nome) for nome in db_schema.get("properties", {}).keys()
        }
        formulas = self.gerar_formulas_painel(cartoes)
        return [
            nome
            for nome in formulas
            if normalizar_texto_chave(nome) not in existentes_norm
        ]

    def garantir_formulas_analiticas(self) -> list[str]:
        """Cria na tabela `Meses` as fórmulas analíticas que ainda não existam."""
        db_schema = self._client.get_database(self._database_id)
        existentes_norm = {
            normalizar_texto_chave(nome) for nome in db_schema.get("properties", {}).keys()
        }
        novas: dict[str, dict[str, dict[str, str]]] = {}
        criadas: list[str] = []
        for nome, expr in self.FORMULAS_ANALITICAS.items():
            if normalizar_texto_chave(nome) not in existentes_norm:
                novas[nome] = {"formula": {"expression": expr}}
                criadas.append(nome)
        if novas:
            self._client.update_database(self._database_id, {"properties": novas})
        return criadas
