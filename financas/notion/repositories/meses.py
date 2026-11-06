"""Repositório de meses de competência e fórmulas do painel no Notion."""

from __future__ import annotations

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
