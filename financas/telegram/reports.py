"""Gerador de relatórios e mensagens estruturadas para o Telegram."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import logging
from typing import Any

from financas.domain.billing import (
    adicionar_meses,
    competencia_ciclo_atual,
    dias_restantes_ciclo,
    formatar_mes_competencia,
)
from financas.domain.models import Periodicidade, formatar_brl
from financas.notion.client import NotionClient
from financas.notion.repositories import NotionRepositories
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
        ano_ciclo, mes_ciclo = competencia_ciclo_atual(hoje)
        mes_label = formatar_mes_competencia(ano_ciclo, mes_ciclo)

        # 1. Dados do Mês na tabela Meses
        dados_mes = self._obter_dados_mes(mes_label)

        # 2. Dados dos Cartões (fórmulas vivas da tabela Meses, com fallback para Contas)
        cartoes = self._obter_faturas_cartoes(dados_mes)

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

        dias_restantes = dias_restantes_ciclo(hoje)

        linhas.append("💸 *Orçamento & Gastos:*")
        linhas.append(f"  • Meus Gastos: *{formatar_brl(meus_gastos)}*")
        if meta_mensal > 0:
            pct = (meus_gastos / meta_mensal) * 100
            linhas.append(f"  • Meta do Mês: {formatar_brl(meta_mensal)} ({pct:.1f}% consumido)")

        sinal_livre = "🟢" if saldo_livre >= 0 else "🔴"
        linhas.append(f"  • Saldo Livre: {sinal_livre} *{formatar_brl(saldo_livre)}*")
        if saldo_livre > 0:
            media_dia = saldo_livre / Decimal(dias_restantes)
            media_sem = media_dia * 7
            linhas.append(
                f"  • Limite diário seguro: ~{formatar_brl(media_dia)}/dia (ou {formatar_brl(media_sem)}/sem)"
            )

        linhas.append("")
        linhas.append("💳 *Faturas dos Cartões:*")
        if not cartoes:
            linhas.append("  • Nenhuma fatura ativa encontrada.")
        for nome_cartao, val_fatura in cartoes.items():
            linhas.append(f"  • {nome_cartao}: *{formatar_brl(val_fatura)}*")

        if proximos_fixos:
            linhas.append("")
            linhas.append("📅 *Próximos Compromissos Previstos:*")
            for item in proximos_fixos:
                val_str = formatar_brl(item["valor"])
                dt_str = item["data"].strftime("%d/%m")
                linhas.append(f"  • {dt_str} — {item['nome']}: {val_str} ({item['conta']})")

        linhas.append("")
        linhas.append("📈 *Patrimônio & Investimentos:*")
        patrimonio = resumo_inv.patrimonio_total
        lucro = resumo_inv.lucro_total
        sinal_l = "+" if lucro > 0 else ""
        pat_fmt = formatar_brl(patrimonio)
        lucro_fmt = f"{sinal_l}{formatar_brl(lucro)}"
        linhas.append(f"  • Total Investido: *{pat_fmt}*")
        linhas.append(f"  • Rendimento Acumulado: *{lucro_fmt}*")

        linhas.append("━━━━━━━━━━━━━━━━━━━━━━")
        linhas.append("💡 _Para registrar um gasto: envie 'item ; valor ; categoria' (ex: filtro de cafe ; 20 ; casa)_")

        return "\n".join(linhas)

    def _obter_dados_mes(self, mes_label: str) -> dict[str, Decimal]:
        try:
            dados = self._repos.meses.obter_resumo_mes(mes_label)
            return dados if isinstance(dados, dict) else {}
        except Exception as exc:
            logger.warning("Falha ao consultar dados do mês %s: %s", mes_label, exc)
            return {}

    def _obter_faturas_cartoes(self, dados_mes: dict[str, Decimal] | None = None) -> dict[str, Decimal]:
        if dados_mes and any(k in dados_mes for k in ("fatura_santander", "fatura_bradesco", "fatura_inter")):
            mapa = (
                ("Santander Credito", dados_mes.get("fatura_santander", Decimal("0.00"))),
                ("Bradesco Credito", dados_mes.get("fatura_bradesco", Decimal("0.00"))),
                ("Inter Credito", dados_mes.get("fatura_inter", Decimal("0.00"))),
            )
            return {nome: val for nome, val in mapa if val > Decimal("0.00")}

        try:
            return self._repos.catalogo.listar_faturas_cartoes_fallback()
        except Exception as exc:
            logger.warning("Falha ao ler faturas dos cartões: %s", exc)
            return {}

    def _obter_proximos_fixos(
        self, mes_label: str, hoje: date, dias_a_frente: int = 10
    ) -> list[dict[str, Any]]:
        limite = hoje + timedelta(days=dias_a_frente)
        proximos: list[dict[str, Any]] = []
        try:
            mes_id = self._repos.meses.id_mes(mes_label)
            proximos = self._repos.transacoes.listar_previstos_periodo(mes_id, hoje, limite)
        except Exception as exc:
            logger.warning("Falha ao consultar previstos do mês: %s", exc)

        return sorted(proximos, key=lambda x: x["data"])

    def gerar_analise_mensal(self, data_ref: date | None = None) -> str:
        """Gera painel analítico do mês vigente: Top categorias, Essencial vs Não essencial e Poupança."""
        hoje = data_ref or date.today()
        ano_ciclo, mes_ciclo = competencia_ciclo_atual(hoje)
        mes_label = formatar_mes_competencia(ano_ciclo, mes_ciclo)

        dados_mes = self._obter_dados_mes(mes_label)
        gastos = self._listar_analise_mes(mes_label)

        total_pessoal = sum((item["valor_meu"] for item in gastos), Decimal("0.00"))
        if total_pessoal <= Decimal("0.00"):
            total_pessoal = dados_mes.get("meus_gastos", Decimal("0.00"))

        por_categoria: dict[str, Decimal] = {}
        por_uso: dict[str, Decimal] = {}
        total_parcelado = Decimal("0.00")
        for item in gastos:
            cat = item["categoria"]
            uso = item["uso_dinheiro"]
            val = item["valor_meu"]
            por_categoria[cat] = por_categoria.get(cat, Decimal("0.00")) + val
            por_uso[uso] = por_uso.get(uso, Decimal("0.00")) + val
            if item.get("parcelado"):
                total_parcelado += val

        if total_parcelado <= Decimal("0.00"):
            total_parcelado = dados_mes.get("total_parcelado_mes", Decimal("0.00"))

        top_cats = sorted(por_categoria.items(), key=lambda x: x[1], reverse=True)[:5]
        usos_ord = sorted(por_uso.items(), key=lambda x: x[1], reverse=True)

        taxa_poupanca_pct = (dados_mes.get("taxa_poupanca", Decimal("0.00")) * Decimal("100")).quantize(
            Decimal("0.1")
        )
        aportes_mes = dados_mes.get("aportes_mes", Decimal("0.00"))
        meta_mensal = dados_mes.get("meta_mensal", Decimal("0.00"))

        linhas = [
            f"🧠 *ANÁLISE FINANCEIRA* — _{mes_label}_",
            "━━━━━━━━━━━━━━━━━━━━━━",
            "📌 *Indicadores do Ciclo:*",
            f"  • Meus Gastos: *{formatar_brl(total_pessoal)}*",
            f"  • Comprometido em Parcelas: *{formatar_brl(total_parcelado)}*",
        ]
        if meta_mensal > Decimal("0.00") and total_parcelado > Decimal("0.00"):
            pct_parc = (total_parcelado / meta_mensal) * Decimal("100")
            linhas.append(f"  • Peso das Parcelas na Meta: *{pct_parc:.1f}%*")
        if aportes_mes > Decimal("0.00"):
            linhas.append(f"  • Aportes no Mês: *{formatar_brl(aportes_mes)}*")
        if taxa_poupanca_pct != Decimal("0.0"):
            linhas.append(f"  • Taxa de Poupança: *{taxa_poupanca_pct:.1f}%*")

        linhas.append("")
        linhas.append("🏷️ *Top Categorias (Meus Gastos):*")
        if not top_cats:
            linhas.append("  • Sem gastos lançados neste ciclo.")
        for cat, val in top_cats:
            pct = (val / total_pessoal * Decimal("100")) if total_pessoal > 0 else Decimal("0")
            linhas.append(f"  • {cat}: *{formatar_brl(val)}* ({pct:.1f}%)")

        if usos_ord:
            linhas.append("")
            linhas.append("⚖️ *Essencial vs Não Essencial:*")
            for uso, val in usos_ord:
                pct = (val / total_pessoal * Decimal("100")) if total_pessoal > 0 else Decimal("0")
                linhas.append(f"  • {uso}: *{formatar_brl(val)}* ({pct:.1f}%)")

        linhas.append("━━━━━━━━━━━━━━━━━━━━━━")
        return "\n".join(linhas)

    def gerar_relatorio_parcelas(self) -> str:
        """Gera resumo das compras parceladas ativas em `📦 Parcelas`."""
        try:
            ativas = self._repos.parcelas.listar_parcelas_ativas()
        except Exception as exc:
            logger.warning("Falha ao listar parcelas ativas: %s", exc)
            ativas = []

        if not ativas:
            return (
                "📦 *COMPRAS PARCELADAS ATIVAS*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Nenhuma compra parcelada em andamento encontrada."
            )

        meus = [p for p in ativas if p.get("de_quem") == "Meu"]
        mensal_meu = sum((p["valor_parcela"] for p in meus), Decimal("0.00"))
        restante_meu = sum((p["falta_pagar"] for p in meus), Decimal("0.00"))

        linhas = [
            "📦 *COMPRAS PARCELADAS ATIVAS*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"  • Comprometimento Mensal (Meu): *{formatar_brl(mensal_meu)}/mês*",
            f"  • Total Restante a Pagar (Meu): *{formatar_brl(restante_meu)}*",
            "",
        ]
        for p in ativas[:12]:
            tag = " _(terceiro)_" if p.get("de_quem") != "Meu" else ""
            fim_txt = f" • até {p['fim']}" if p.get("fim") else ""
            linhas.append(
                f"  • *{p['gasto']}*{tag}: {p['parcela_atual']:02d}/{p['total_parcelas']:02d} • "
                f"{formatar_brl(p['valor_parcela'])}/mês • Falta *{formatar_brl(p['falta_pagar'])}* "
                f"({p['cartao']}{fim_txt})"
            )
        if len(ativas) > 12:
            linhas.append(f"  _... e mais {len(ativas) - 12} parcelamento(s) em 📦 Parcelas._")
        bloco_futuro = self.gerar_comprometimento_futuro()
        if bloco_futuro:
            linhas.append("")
            linhas.append(bloco_futuro)
        linhas.append("━━━━━━━━━━━━━━━━━━━━━━")
        return "\n".join(linhas)

    def gerar_comprometimento_futuro(
        self,
        data_ref: date | None = None,
        meses_a_frente: int = 3,
        parcela_extra: Decimal = Decimal("0.00"),
        total_parcelas_extra: int = 0,
    ) -> str:
        """Projeta Fixos + Parcelas nos próximos meses e alerta se passar de 50-55% da renda."""
        hoje = data_ref or date.today()
        ano_base, mes_base = competencia_ciclo_atual(hoje)
        mes_atual_label = formatar_mes_competencia(ano_base, mes_base)
        dados_mes = self._obter_dados_mes(mes_atual_label)
        renda_base = (
            dados_mes.get("salario_base", Decimal("0.00"))
            + dados_mes.get("bonus", Decimal("0.00"))
        )
        if renda_base <= Decimal("0.00"):
            renda_base = dados_mes.get("receita", Decimal("0.00"))

        try:
            fixos = list(self._repos.gastos_fixos.listar_ativos())
        except Exception as exc:
            logger.warning("Falha ao listar gastos fixos ativos: %s", exc)
            fixos = []

        try:
            parcelas = list(self._repos.parcelas.listar_parcelas_ativas())
        except Exception as exc:
            logger.warning("Falha ao listar parcelas ativas para projeção: %s", exc)
            parcelas = []

        if not fixos and not parcelas and parcela_extra <= Decimal("0.00"):
            return ""

        linhas = ["📆 *Comprometimento Próximos 3 Meses (Fixos + Parcelas):*"]
        alerta_alto = False
        for offset in range(1, meses_a_frente + 1):
            ano_f, mes_f = adicionar_meses(ano_base, mes_base, offset)
            label_f = formatar_mes_competencia(ano_f, mes_f)

            fixos_mes = Decimal("0.00")
            for g in fixos:
                if g.periodicidade == Periodicidade.ANUAL and (
                    not g.inicio or g.inicio.month != mes_f
                ):
                    continue
                bruto = g.valor_lancamento_para_competencia(ano_f, mes_f)
                a_receber = g.valor_a_receber_para_competencia(ano_f, mes_f) or Decimal("0.00")
                fixos_mes += max(Decimal("0.00"), bruto - a_receber)

            parcelas_mes = sum(
                (
                    p["valor_parcela"]
                    for p in parcelas
                    if p.get("de_quem") == "Meu"
                    and offset < (p["total_parcelas"] - p["parcela_atual"] + 1)
                ),
                Decimal("0.00"),
            )
            if parcela_extra > Decimal("0.00") and offset < total_parcelas_extra:
                parcelas_mes += parcela_extra

            total_comp = fixos_mes + parcelas_mes
            if renda_base > Decimal("0.00"):
                pct = (total_comp / renda_base) * Decimal("100")
                if pct >= Decimal("55"):
                    icone = "🔴"
                    alerta_alto = True
                elif pct >= Decimal("50"):
                    icone = "🟡"
                    alerta_alto = True
                else:
                    icone = "🟢"
                linhas.append(
                    f"  • {icone} *{label_f}*: *{formatar_brl(total_comp)}* "
                    f"({pct:.1f}% da renda | Fixos {formatar_brl(fixos_mes)} + Parc {formatar_brl(parcelas_mes)})"
                )
            else:
                linhas.append(
                    f"  • *{label_f}*: *{formatar_brl(total_comp)}* "
                    f"(Fixos {formatar_brl(fixos_mes)} + Parc {formatar_brl(parcelas_mes)})"
                )

        if alerta_alto:
            linhas.append("  ⚠️ _Atenção: comprometimento acima de 50% da renda antes dos gastos variáveis!_")
        return "\n".join(linhas)

    def _listar_analise_mes(self, mes_label: str) -> list[dict[str, Any]]:
        try:
            mes_id = self._repos.meses.id_mes(mes_label)
            return self._repos.transacoes.listar_analise_gastos_mes(mes_id)
        except Exception as exc:
            logger.warning("Falha ao consultar análise do mês %s: %s", mes_label, exc)
            return []
