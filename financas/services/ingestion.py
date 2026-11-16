"""Serviço de ingestão idempotente de faturas com projeção de parcelas, conciliação de Gastos Fixos e suporte a `dry_run`."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher
import json
import logging
from pathlib import Path
import re
from typing import Iterable

from financas.config import AppConfig
from financas.domain.billing import CalculadoraFatura, parse_mes_competencia
from financas.domain.classification import identificar_pessoa_reembolso
from financas.domain.models import (
    CATEGORIA_EMPRESTIMO,
    ChaveIdempotencia,
    ChaveParcialTransacao,
    GastoFixo,
    LancamentoTransacao,
    Pessoa,
    Recorrencia,
    RegraCompraTerceiro,
    StatusTransacao,
    UsoDinheiro,
    VinculoGastoFixo,
)
from financas.notion.client import NotionClient
from financas.notion.repositories import NotionRepositories

logger = logging.getLogger(__name__)

ARQUIVO_REGRAS_TERCEIROS = Path(__file__).resolve().parents[2] / "regras_terceiros.json"


def carregar_regras_terceiros(caminho: Path | str | None = None) -> tuple[RegraCompraTerceiro, ...]:
    """Carrega regras customizadas de compras de terceiros a partir de um JSON opcional."""
    alvo = Path(caminho) if caminho else ARQUIVO_REGRAS_TERCEIROS
    if not alvo.is_file():
        return ()
    try:
        conteudo = json.loads(alvo.read_text(encoding="utf-8"))
        if isinstance(conteudo, list):
            regras: list[RegraCompraTerceiro] = []
            for item in conteudo:
                regras.append(
                    RegraCompraTerceiro(
                        padrao_descricao=item["padrao_descricao"],
                        conta_ou_cartao=item.get("conta_ou_cartao", ""),
                        pessoa=item["pessoa"],
                        valor_a_receber=Decimal(str(item["valor_a_receber"])),
                        categoria=item.get("categoria", CATEGORIA_EMPRESTIMO),
                        uso_dinheiro=item.get("uso_dinheiro", UsoDinheiro.EMPRESTIMO.value),
                    )
                )
            return tuple(regras)
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("Falha ao carregar regras de terceiros do arquivo JSON: %s", exc)
    return ()


@dataclass
class IngestionReport:
    """Relatório de execução da importação de uma fatura."""

    dry_run: bool = False
    extraidos_da_fatura: int = 0
    parcelas_futuras_projetadas: int = 0
    gravados: list[str] = field(default_factory=list)
    conciliados_previsto_para_realizado: list[str] = field(default_factory=list)
    religados_ao_mes: list[str] = field(default_factory=list)
    ignorados_por_idempotencia: list[str] = field(default_factory=list)
    alertas_variacao_fixos: list[str] = field(default_factory=list)

    @property
    def tem_alteracoes(self) -> bool:
        return bool(
            self.gravados
            or self.conciliados_previsto_para_realizado
            or self.religados_ao_mes
        )

    def formatar_resumo(self) -> str:
        prefixo = "[DRY-RUN / SIMULAÇÃO] " if self.dry_run else ""
        verbo_criados = "Lançamentos que seriam criados" if self.dry_run else "Lançamentos criados no Notion"
        verbo_conciliados = (
            "Gastos fixos que seriam conciliados (Previsto -> Realizado)"
            if self.dry_run
            else "Gastos fixos conciliados (Previsto -> Realizado)"
        )
        verbo_religados = (
            "Lançamentos órfãos que seriam religados ao mês"
            if self.dry_run
            else "Lançamentos órfãos religados ao mês"
        )
        linhas = [
            f"{prefixo}Itens extraídos da fatura: {self.extraidos_da_fatura} | Parcelas futuras projetadas: {self.parcelas_futuras_projetadas}",
            f"[+] {verbo_criados} ({len(self.gravados)}): {self.gravados}",
            f"[✓] {verbo_conciliados} ({len(self.conciliados_previsto_para_realizado)}): {self.conciliados_previsto_para_realizado}",
            f"[~] {verbo_religados} ({len(self.religados_ao_mes)}): {self.religados_ao_mes}",
            (
                f"[=] Ignorados por idempotência (já existiam) ({len(self.ignorados_por_idempotencia)}): "
                f"{self.ignorados_por_idempotencia}"
            ),
        ]
        if self.alertas_variacao_fixos:
            linhas.append(
                f"[!] Alertas de variação em gastos fixos ({len(self.alertas_variacao_fixos)}): "
                f"{self.alertas_variacao_fixos}"
            )
        return "\n".join(linhas)


class FaturaIngestionService:
    """Orquestra a projeção de parcelas, conciliação de gastos fixos, checagem de idempotência e escrita."""

    def __init__(
        self,
        client: NotionClient | None,
        config: AppConfig,
        calculadora: CalculadoraFatura | None = None,
        regras_terceiros: tuple[RegraCompraTerceiro, ...] | None = None,
        competencia_minima: tuple[int, int] | None = None,
        repos: NotionRepositories | None = None,
        pessoas_cadastradas: Iterable[Pessoa] | None = None,
    ) -> None:
        self._calc = calculadora or CalculadoraFatura()
        self._regras_terceiros = (
            regras_terceiros if regras_terceiros is not None else carregar_regras_terceiros()
        )
        self._competencia_minima = competencia_minima
        if repos is None:
            if client is None:
                raise ValueError("Informe `client` ou `repos` ao instanciar FaturaIngestionService.")
            repos = NotionRepositories.from_client(client, config)
        self._repos = repos
        self._pessoas_override = list(pessoas_cadastradas) if pessoas_cadastradas is not None else None
        self._pessoas_cache: list[Pessoa] | None = None

    @property
    def _pessoas_cadastradas(self) -> list[Pessoa]:
        if self._pessoas_override is not None:
            return self._pessoas_override
        if self._pessoas_cache is None:
            if self._repos.pessoas is not None:
                try:
                    self._pessoas_cache = self._repos.pessoas.listar_todas()
                except Exception as exc:
                    logger.error("Não foi possível listar pessoas do Notion: %s", exc, exc_info=True)
                    self._pessoas_cache = []
            else:
                self._pessoas_cache = []
        return self._pessoas_cache

    @staticmethod
    def identificar_gasto_fixo(
        descricao_fatura: str, gastos_ativos: list[GastoFixo]
    ) -> GastoFixo | None:
        """Cruza a descrição da fatura/extrato com a coluna `Nome na fatura` (ou `Nome`) dos Gastos Fixos."""
        for fixo in gastos_ativos:
            if fixo.corresponde_a_descricao(descricao_fatura):
                return fixo
        return None

    def identificar_compra_terceiro(
        self, descricao: str, conta_ou_cartao: str
    ) -> RegraCompraTerceiro | None:
        """Verifica se uma compra parcelada pertence a um terceiro via regras configuradas."""
        for regra in self._regras_terceiros:
            if regra.corresponde(descricao, conta_ou_cartao):
                return regra
        return None

    def _enriquecer_metadados_terceiro(self, item: LancamentoTransacao) -> LancamentoTransacao:
        regra_terc = self.identificar_compra_terceiro(item.descricao, item.conta_ou_cartao)
        if regra_terc:
            return replace(
                item,
                pessoa=regra_terc.pessoa,
                valor_a_receber=regra_terc.valor_a_receber,
                categoria=regra_terc.categoria,
                uso_dinheiro=regra_terc.uso_dinheiro,
            )
        if not item.pessoa and self._pessoas_cadastradas:
            pessoa_match = identificar_pessoa_reembolso(item.descricao, self._pessoas_cadastradas)
            if pessoa_match:
                return replace(
                    item,
                    pessoa=pessoa_match,
                    valor_a_receber=item.valor,
                    categoria=CATEGORIA_EMPRESTIMO,
                    uso_dinheiro=item.uso_dinheiro or UsoDinheiro.EMPRESTIMO.value,
                )
        elif item.pessoa:
            return replace(
                item,
                categoria=CATEGORIA_EMPRESTIMO,
                uso_dinheiro=item.uso_dinheiro or UsoDinheiro.EMPRESTIMO.value,
            )
        return item

    def expandir_com_parcelas_futuras(
        self, lancamentos_base: list[LancamentoTransacao]
    ) -> tuple[list[LancamentoTransacao], int]:
        """Retorna a lista completa (base + parcelas futuras) e o total de parcelas projetadas."""
        todos: list[LancamentoTransacao] = []
        total_projetadas = 0
        for item in lancamentos_base:
            if item.eh_parcelado:
                item = replace(
                    item,
                    data_compra=self._calc.data_parcela_na_competencia(
                        item.data_compra, item.mes_competencia
                    ),
                )
            item = self._enriquecer_metadados_terceiro(item)
            todos.append(item)
            futuras = self._calc.projetar_parcelas_futuras(item)
            todos.extend(futuras)
            total_projetadas += len(futuras)
        return todos, total_projetadas

    def _tentar_conciliar_gasto_fixo(
        self,
        item: LancamentoTransacao,
        fixo_match: GastoFixo,
        ano: int,
        mes: int,
        chave_bruta: ChaveIdempotencia,
        chaves_mes: dict[ChaveIdempotencia, str],
        vinculos_mes: dict[str, VinculoGastoFixo],
        relatorio: IngestionReport,
        dry_run: bool,
    ) -> tuple[LancamentoTransacao, bool]:
        """Processa um lançamento que casou com um Gasto Fixo.

        Retorna `(item_atualizado, ja_resolvido)`.
        """
        assert fixo_match.page_id is not None
        val_receber_fixo = fixo_match.valor_a_receber_para_competencia(ano, mes)
        resumo_fixo = (
            f"{item.mes_competencia} | {fixo_match.nome} "
            f"('{item.descricao}') | R$ {item.valor}"
        )

        if fixo_match.page_id in vinculos_mes:
            transacao_id, status_atual, data_iso = vinculos_mes[fixo_match.page_id]
            if status_atual == StatusTransacao.PREVISTO.value:
                if not dry_run:
                    self._repos.transacoes.conciliar_previsto_para_realizado(
                        transacao_page_id=transacao_id,
                        valor_real=item.valor,
                        data_real=item.data_compra,
                        valor_a_receber=val_receber_fixo,
                        pessoa=fixo_match.pessoa,
                    )
                vinculos_mes[fixo_match.page_id] = VinculoGastoFixo(
                    transacao_id,
                    StatusTransacao.REALIZADO.value,
                    item.data_compra.isoformat(),
                )
                relatorio.conciliados_previsto_para_realizado.append(resumo_fixo)
                valor_previsto = fixo_match.valor_lancamento_para_competencia(ano, mes)
                if valor_previsto > Decimal("0.00"):
                    diferenca = item.valor - valor_previsto
                    variacao_pct = (diferenca / valor_previsto) * Decimal("100")
                    if diferenca >= Decimal("10.00") and variacao_pct >= Decimal("15"):
                        relatorio.alertas_variacao_fixos.append(
                            f"{fixo_match.nome}: previsto R$ {valor_previsto:.2f} -> veio R$ {item.valor:.2f} (+{variacao_pct:.1f}%)"
                        )
                return item, True
            if data_iso == item.data_compra.isoformat():
                relatorio.ignorados_por_idempotencia.append(resumo_fixo)
                return item, True

        item_enriquecido = replace(
            item,
            descricao=fixo_match.nome,
            categoria=fixo_match.categoria,
            recorrencia=Recorrencia.FIXO,
            status=StatusTransacao.REALIZADO,
            gasto_fixo_id=fixo_match.page_id,
            uso_dinheiro=fixo_match.uso_dinheiro or item.uso_dinheiro,
            pessoa=fixo_match.pessoa,
            valor_a_receber=val_receber_fixo,
        )

        if chave_bruta in chaves_mes:
            id_existente = chaves_mes.pop(chave_bruta)
            if not dry_run:
                self._repos.transacoes.vincular_gasto_fixo_existente(
                    id_existente, item_enriquecido
                )
            chaves_mes[item_enriquecido.chave_idempotencia] = id_existente
            vinculos_mes[fixo_match.page_id] = VinculoGastoFixo(
                id_existente,
                StatusTransacao.REALIZADO.value,
                item_enriquecido.data_compra.isoformat(),
            )
            relatorio.conciliados_previsto_para_realizado.append(resumo_fixo)
            return item_enriquecido, True

        return item_enriquecido, False

    @staticmethod
    def _contas_equivalentes(conta_a: str, conta_b: str) -> bool:
        """Compara nomes de conta normalizados aceitando a grafia histórica 'santader'."""
        if conta_a == conta_b:
            return True
        return conta_a.replace("santander", "santader") == conta_b.replace("santander", "santader")

    @classmethod
    def _buscar_chave_telegram_manual(
        cls,
        chave: ChaveIdempotencia,
        chaves_mes: dict[ChaveIdempotencia, str],
        tolerancia_dias: int = 2,
    ) -> ChaveIdempotencia | None:
        """Localiza lançamento manual do Telegram (`Name=''`) no mesmo mês/valor com até ±2 dias."""
        try:
            data_alvo = date.fromisoformat(chave.data_iso)
        except ValueError:
            return None

        sufixo_parcela_match = re.search(r"- parc \d{2}/\d{2}$", chave.descricao_norm)
        sufixo_parcela = sufixo_parcela_match.group(0) if sufixo_parcela_match else None

        melhor: ChaveIdempotencia | None = None
        menor_diff: int | None = None
        for k in chaves_mes:
            descricao_compativel = k.descricao_norm == "" or (
                sufixo_parcela is not None and k.descricao_norm.endswith(sufixo_parcela)
            )
            if (
                descricao_compativel
                and k.centavos == chave.centavos
                and k.mes_competencia == chave.mes_competencia
                and cls._contas_equivalentes(k.conta_norm, chave.conta_norm)
            ):
                try:
                    diff = abs((date.fromisoformat(k.data_iso) - data_alvo).days)
                except ValueError:
                    continue
                if diff <= tolerancia_dias and (menor_diff is None or diff < menor_diff):
                    melhor = k
                    menor_diff = diff
        return melhor

    @classmethod
    def _buscar_chave_ocr_equivalente(
        cls,
        chave: ChaveIdempotencia,
        chaves_mes: dict[ChaveIdempotencia, str],
        tolerancia_dias: int = 1,
    ) -> ChaveIdempotencia | None:
        """Detecta duplicidade quando o OCR/fatura varia texto, ±1 dia ou centavos de parcela."""
        re_parc = re.compile(r"\s*-\s*parc\s+(\d{2}/\d{2})$")
        m_alvo = re_parc.search(chave.descricao_norm)
        parc_alvo = m_alvo.group(1) if m_alvo else None
        base_alvo = re_parc.sub("", chave.descricao_norm)
        desc_alvo = re.sub(r"[^a-z0-9]", "", base_alvo).replace("0", "o")
        if len(desc_alvo) < 4:
            return None

        try:
            data_alvo = date.fromisoformat(chave.data_iso)
        except ValueError:
            return None

        for k in chaves_mes:
            if (
                not k.descricao_norm
                or k.mes_competencia != chave.mes_competencia
                or not cls._contas_equivalentes(k.conta_norm, chave.conta_norm)
            ):
                continue

            m_k = re_parc.search(k.descricao_norm)
            parc_k = m_k.group(1) if m_k else None
            if parc_alvo != parc_k:
                continue

            tol_centavos = 5 if parc_alvo is not None else 0
            if abs(k.centavos - chave.centavos) > tol_centavos:
                continue

            try:
                if abs((date.fromisoformat(k.data_iso) - data_alvo).days) > tolerancia_dias:
                    continue
            except ValueError:
                continue

            base_k = re_parc.sub("", k.descricao_norm)
            desc_k = re.sub(r"[^a-z0-9]", "", base_k).replace("0", "o")
            if len(desc_k) < 4:
                continue
            if desc_k.startswith(desc_alvo) or desc_alvo.startswith(desc_k):
                return k
            if len(desc_k) >= 7 and len(desc_alvo) >= 7 and desc_k[:7] == desc_alvo[:7]:
                return k
            if SequenceMatcher(None, desc_k, desc_alvo).ratio() >= 0.72:
                return k
        return None

    def _tentar_religar_ou_ignorar(
        self,
        item: LancamentoTransacao,
        chave: ChaveIdempotencia,
        chaves_mes: dict[ChaveIdempotencia, str],
        orfas: dict[ChaveParcialTransacao, str],
        parceladas_existentes: dict[ChaveParcialTransacao, str],
        relatorio: IngestionReport,
        dry_run: bool,
    ) -> bool:
        """Verifica idempotência, órfãs sem mês e compras parceladas existentes.

        Retorna `True` se o item foi tratado e não precisa ser criado.
        """
        chave_parcial = chave.chave_parcial
        resumo_item = f"{item.mes_competencia} | {item.descricao_com_parcela} | R$ {item.valor}"

        if chave in chaves_mes:
            if (
                not dry_run
                and item.eh_parcelado
                and item.pessoa
                and item.valor_a_receber
                and chave_parcial in parceladas_existentes
            ):
                self._repos.transacoes.atualizar_metadados_terceiro(
                    transacao_page_id=parceladas_existentes[chave_parcial],
                    pessoa=item.pessoa,
                    valor_a_receber=item.valor_a_receber,
                    categoria=item.categoria,
                    uso_dinheiro=item.uso_dinheiro,
                )
            relatorio.ignorados_por_idempotencia.append(resumo_item)
            return True

        # Conciliação com lançamento manual do Telegram (onde Name="" e Nome Fantasia já foi preenchido)
        # Aceita variação de até ±2 dias entre o envio no Telegram e a data na fatura.
        chave_manual = self._buscar_chave_telegram_manual(chave, chaves_mes)
        if chave_manual is not None:
            page_id_manual = chaves_mes.pop(chave_manual)
            if not dry_run:
                self._repos.transacoes.preencher_nome_fatura(
                    page_id_manual, item.descricao_com_parcela
                )
            chaves_mes[chave] = page_id_manual
            relatorio.conciliados_previsto_para_realizado.append(resumo_item)
            return True

        # Tolerância a pequenas variações de OCR no mesmo dia, conta e valor
        chave_ocr = self._buscar_chave_ocr_equivalente(chave, chaves_mes)
        if chave_ocr is not None:
            relatorio.ignorados_por_idempotencia.append(resumo_item)
            return True

        if chave_parcial in orfas:
            page_id_orfa = orfas.pop(chave_parcial)
            if not dry_run:
                self._repos.transacoes.vincular_mes(page_id_orfa, item.mes_competencia)
            chaves_mes[chave] = page_id_orfa
            relatorio.religados_ao_mes.append(resumo_item)
            return True

        if item.eh_parcelado and chave_parcial in parceladas_existentes:
            page_id_parc = parceladas_existentes.pop(chave_parcial)
            if not dry_run:
                if item.pessoa and item.valor_a_receber:
                    self._repos.transacoes.atualizar_metadados_terceiro(
                        transacao_page_id=page_id_parc,
                        pessoa=item.pessoa,
                        valor_a_receber=item.valor_a_receber,
                        categoria=item.categoria,
                        uso_dinheiro=item.uso_dinheiro,
                        mes_competencia=item.mes_competencia,
                    )
                else:
                    self._repos.transacoes.vincular_mes(page_id_parc, item.mes_competencia)
            chaves_mes[chave] = page_id_parc
            relatorio.religados_ao_mes.append(resumo_item)
            return True

        return False

    def importar_lancamentos(
        self, lancamentos_base: list[LancamentoTransacao], dry_run: bool = False
    ) -> IngestionReport:
        relatorio = IngestionReport(
            dry_run=dry_run, extraidos_da_fatura=len(lancamentos_base)
        )
        todos, total_projetadas = self.expandir_com_parcelas_futuras(lancamentos_base)
        relatorio.parcelas_futuras_projetadas = total_projetadas

        gastos_fixos_ativos = self._repos.gastos_fixos.listar_ativos()
        chaves_por_mes: dict[str, dict[ChaveIdempotencia, str]] = {}
        vinculos_fixos_por_mes: dict[str, dict[str, VinculoGastoFixo]] = {}
        orfas = dict(self._repos.transacoes.buscar_orfas_sem_mes())
        parceladas_existentes = dict(self._repos.transacoes.buscar_parceladas_por_chave_parcial())

        for item in todos:
            ano, mes = parse_mes_competencia(item.mes_competencia)
            if self._competencia_minima and (ano, mes) < self._competencia_minima:
                continue
            if not dry_run:
                self._repos.meses.garantir_mes(ano, mes)

            if item.mes_competencia not in chaves_por_mes:
                chaves_por_mes[item.mes_competencia] = dict(
                    self._repos.transacoes.chaves_existentes_no_mes(item.mes_competencia)
                )
            if item.mes_competencia not in vinculos_fixos_por_mes:
                vinculos_fixos_por_mes[item.mes_competencia] = dict(
                    self._repos.transacoes.buscar_vinculos_gasto_fixo_no_mes(item.mes_competencia)
                )

            chave_bruta = item.chave_idempotencia
            fixo_match = self.identificar_gasto_fixo(item.descricao, gastos_fixos_ativos)
            if fixo_match and fixo_match.page_id:
                item, resolvido = self._tentar_conciliar_gasto_fixo(
                    item=item,
                    fixo_match=fixo_match,
                    ano=ano,
                    mes=mes,
                    chave_bruta=chave_bruta,
                    chaves_mes=chaves_por_mes[item.mes_competencia],
                    vinculos_mes=vinculos_fixos_por_mes[item.mes_competencia],
                    relatorio=relatorio,
                    dry_run=dry_run,
                )
                if resolvido:
                    continue

            chave = item.chave_idempotencia
            if self._tentar_religar_ou_ignorar(
                item=item,
                chave=chave,
                chaves_mes=chaves_por_mes[item.mes_competencia],
                orfas=orfas,
                parceladas_existentes=parceladas_existentes,
                relatorio=relatorio,
                dry_run=dry_run,
            ):
                continue

            if (
                not dry_run
                and item.eh_parcelado
                and not item.compra_parcelada_id
                and self._repos.parcelas is not None
            ):
                parcela_mestre_id = self._repos.parcelas.garantir_parcela(item)
                if parcela_mestre_id:
                    item = replace(item, compra_parcelada_id=parcela_mestre_id)

            novo_id = (
                "dry_run_id"
                if dry_run
                else self._repos.transacoes.criar_lancamento(item)
            )
            chaves_por_mes[item.mes_competencia][chave] = novo_id
            if item.gasto_fixo_id:
                vinculos_fixos_por_mes[item.mes_competencia][item.gasto_fixo_id] = VinculoGastoFixo(
                    novo_id,
                    item.status.value,
                    item.data_compra.isoformat(),
                )
            relatorio.gravados.append(
                f"{item.mes_competencia} | {item.descricao_com_parcela} | R$ {item.valor}"
            )

        return relatorio
