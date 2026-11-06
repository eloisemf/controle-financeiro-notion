"""Gerador de relatórios e mensagens estruturadas para o Telegram."""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal
import logging
from typing import Any

from financas.domain.billing import formatar_mes_competencia
from financas.notion.builders import NotionParser
from financas.notion.client import NotionClient
from financas.notion.repositories import NotionRepositories
from financas.notion.schema import ColunasGastosFixos, ColunasTransacoes
from financas.services.investments import InvestimentosService

logger = logging.getLogger(__name__)


class TelegramReportBuilder:
    """Monta o resumo financeiro semanal e alertas formatados com emojis."""

    def __init__(self, client: NotionClient, repos: NotionRepositories, invest_service: InvestimentosService) -> None:
        self._client = client
        self._repos = repos
        self._invest = invest_service

    def gerar_resumo_semanal(self, data_ref: date | None = None) -> str:
        """Gera o painel consolidado da semana."""
        hoje = data_ref or date.today()
        mes_label = formatar_mes_competencia(hoje.year, hoje.month)

        # 1. Dados do Mês na tabela Meses
        dados_mes = self._obter_dados_mes(mes_label)

        # 2. Dados dos Cartões na tabela Contas
        cartoes = self._obter_faturas_cartoes()

        # 3. Próximos Gastos Fixos Previstos (próximos 10 dias)
        proximos_fixos = self._obter_proximos_fixos(mes_label, hoje, dias_a_frente=10)

        # 4. Investimentos
        resumo_inv = self._invest.obter_resumo_carteira()

        # Montagem do Texto Formatado
        linhas: list[str] = [
            f"📊 *PAINEL SEMANAL DE FINANÇAS* — _{mes_label}_",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]

        # Bloco Orçamento
        meus_gastos = dados_mes.get("meus_gastos", Decimal("0.00"))
        saldo_livre = dados_mes.get("saldo_livre", Decimal("0.00"))
        meta_mensal = dados_mes.get("meta_mensal", Decimal("0.00"))

        dias_no_mes = calendar.monthrange(hoje.year, hoje.month)[1]
        dias_restantes = max(1, dias_no_mes - hoje.day)

        linhas.append("💸 *Orçamento & Gastos:*")
        linhas.append(f"  • Meus Gastos: *R$ {meus_gastos:,.2f}*".replace(",", "X").replace(".", ",").replace("X", "."))
        if meta_mensal > 0:
            pct = (meus_gastos / meta_mensal) * 100
            linhas.append(f"  • Meta do Mês: R$ {meta_mensal:,.2f} ({pct:.1f}% consumido)".replace(",", "X").replace(".", ",").replace("X", "."))

        sinal_livre = "🟢" if saldo_livre >= 0 else "🔴"
        linhas.append(f"  • Saldo Livre: {sinal_livre} *R$ {saldo_livre:,.2f}*".replace(",", "X").replace(".", ",").replace("X", "."))
        if saldo_livre > 0:
            media_dia = saldo_livre / Decimal(dias_restantes)
            media_sem = media_dia * 7
            linhas.append(f"  • Limite diário seguro: ~R$ {media_dia:.2f}/dia (ou R$ {media_sem:.2f}/sem)")

        linhas.append("")
        linhas.append("💳 *Faturas dos Cartões:*")
        if not cartoes:
            linhas.append("  • Nenhuma fatura ativa encontrada.")
        for nome_cartao, val_fatura in cartoes.items():
            val_formatado = f"R$ {val_fatura:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            linhas.append(f"  • {nome_cartao}: *{val_formatado}*")

        if proximos_fixos:
            linhas.append("")
            linhas.append("📅 *Próximos Compromissos Previstos:*")
            for item in proximos_fixos:
                val_str = f"R$ {item['valor']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                dt_str = item["data"].strftime("%d/%m")
                linhas.append(f"  • {dt_str} — {item['nome']}: {val_str} ({item['conta']})")

        linhas.append("")
        linhas.append("📈 *Patrimônio & Investimentos:*")
        patrimonio = resumo_inv.patrimonio_total
        lucro = resumo_inv.lucro_total
        sinal_l = "+" if lucro >= 0 else ""
        pat_fmt = f"R$ {patrimonio:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        lucro_fmt = f"{sinal_l}R$ {lucro:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        linhas.append(f"  • Total Investido: *{pat_fmt}*")
        linhas.append(f"  • Rendimento Acumulado: *{lucro_fmt}*")

        linhas.append("━━━━━━━━━━━━━━━━━━━━━━")
        linhas.append("💡 _Para registrar um gasto rápido: envie 'valor descricao' (ex: 15 cafe)_")

        return "\n".join(linhas)

    def _obter_dados_mes(self, mes_label: str) -> dict[str, Decimal]:
        try:
            mes_id = self._repos.meses.id_mes(mes_label)
            pagina = self._client.request("GET", f"/pages/{mes_id}")
            props = pagina.get("properties", {})
            return {
                "meus_gastos": NotionParser.prop_decimal(pagina, "Meus Gastos") or Decimal("0.00"),
                "saldo_livre": NotionParser.prop_decimal(pagina, "Saldo Livre Real") or Decimal("0.00"),
                "meta_mensal": NotionParser.prop_decimal(pagina, "Meta Mensal") or Decimal("0.00"),
                "receita": NotionParser.prop_decimal(pagina, "Receita") or Decimal("0.00"),
            }
        except Exception as exc:
            logger.warning("Falha ao consultar dados do mês %s: %s", mes_label, exc)
            return {}

    def _obter_faturas_cartoes(self) -> dict[str, Decimal]:
        cartoes: dict[str, Decimal] = {}
        try:
            paginas = self._client.query_database(self._repos.catalogo._db_ids.contas)
            for p in paginas:
                tipo = NotionParser.prop_select(p, "Tipo")
                if tipo == "Cartão de crédito":
                    nome = NotionParser.page_title(p)
                    # Prioriza fatura realizada do mês ou limite utilizado
                    fatura_val = (
                        NotionParser.prop_decimal(p, "Fatura do Mês (Realizada)")
                        or abs(NotionParser.prop_decimal(p, "Saldo") or Decimal("0.00"))
                    )
                    cartoes[nome] = fatura_val
        except Exception as exc:
            logger.warning("Falha ao ler faturas dos cartões: %s", exc)
        return cartoes

    def _obter_proximos_fixos(
        self, mes_label: str, hoje: date, dias_a_frente: int = 10
    ) -> list[dict[str, Any]]:
        limite = hoje + timedelta(days=dias_a_frente)
        proximos: list[dict[str, Any]] = []
        try:
            mes_id = self._repos.meses.id_mes(mes_label)
            paginas = self._client.query_database(
                self._repos.transacoes._database_id,
                filter_payload={
                    "and": [
                        {"property": ColunasTransacoes.MES, "relation": {"contains": mes_id}},
                        {"property": ColunasTransacoes.STATUS, "select": {"equals": "Previsto"}},
                    ]
                },
            )
            for p in paginas:
                dt_str = NotionParser.prop_date_start(p, ColunasTransacoes.CRIADO_EM)
                if not dt_str:
                    continue
                dt = date.fromisoformat(dt_str)
                if hoje <= dt <= limite:
                    nome = NotionParser.page_title(p)
                    valor = NotionParser.prop_decimal(p, ColunasTransacoes.VALOR) or Decimal("0.00")
                    contas = (
                        NotionParser.prop_relation_ids(p, ColunasTransacoes.SAIDA_DE)
                        or NotionParser.prop_relation_ids(p, ColunasTransacoes.ENTRADA_EM)
                    )
                    conta_nome = (
                        self._repos.catalogo.nome_conta_por_id(contas[0]) if contas else "Conta"
                    )
                    proximos.append({"nome": nome, "valor": valor, "data": dt, "conta": conta_nome})
        except Exception as exc:
            logger.warning("Falha ao consultar previstos do mês: %s", exc)

        return sorted(proximos, key=lambda x: x["data"])
