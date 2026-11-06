"""Parsers determinísticos para documentos em PDF do Bradesco (Cartões e Conta Corrente)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import re
from typing import Iterable

from financas.domain.billing import formatar_mes_competencia
from financas.domain.classification import ClassificadorCategoria, identificar_pessoa_reembolso
from financas.domain.models import (
    CATEGORIA_EMPRESTIMO,
    LancamentoTransacao,
    Pessoa,
    TipoTransacao,
    normalizar_texto_chave,
)
from financas.parsers.base import BasePdfParser, exigir_decimal


class BradescoCartoesPdfParser(BasePdfParser):
    """Parser determinístico para faturas PDF do cartão Bradesco Credito."""

    _RE_FECHAMENTO_DISPONIVEL = re.compile(
        r"Dispon[ií]vel\s+em\s*(\d{2})/(\d{2})/(\d{4})", re.IGNORECASE
    )
    _RE_FECHAMENTO_LIVELO = re.compile(
        r"Saldo\s+de\s+pontos\s+Livelo\s+em\s*(\d{2})/(\d{2})/(\d{2,4})", re.IGNORECASE
    )
    _RE_LINHA = re.compile(r"^(\d{2})/(\d{2})\s+(.+?)\s+([\d\.]+,\d{2})(?:\s+(-))?$")
    _RE_PARCELA_E_CIDADE = re.compile(r"^(.+?)\s*(\d{2})/(\d{2})\s+([A-Za-zÀ-ÿ\s\.]+)$")
    _CIDADES_PADRAO: tuple[str, ...] = (
        "SAO PAULO",
        "NEW YORK",
        "RIO DE JANEIR",
        "RIO DE JANEIRO",
        "CURITIBA",
        "BELO HORIZONT",
        "EXTREMA",
        "OSASCO",
        "BARUERI",
        "CAMPINAS",
    )

    def __init__(
        self,
        nome_cartao: str = "Bradesco Credito",
        cidades_sufixo: tuple[str, ...] | None = None,
        classificador: ClassificadorCategoria | None = None,
    ) -> None:
        self._nome_cartao = nome_cartao
        self._cidades_sufixo = cidades_sufixo if cidades_sufixo is not None else self._CIDADES_PADRAO
        self._classificador = classificador or ClassificadorCategoria()

    def pode_processar(self, arquivo: Path, texto: str = "") -> bool:
        if arquivo.suffix.lower() != ".pdf":
            return False
        t = texto.lower()
        return "bradesco cart" in t or "fatura mensal" in t

    def parse_texto(self, texto: str) -> list[LancamentoTransacao]:
        data_fechamento = self._extrair_data_fechamento(texto)
        mes_fatura = formatar_mes_competencia(data_fechamento.year, data_fechamento.month)
        lancamentos: list[LancamentoTransacao] = []

        for linha_bruta in texto.splitlines():
            linha = linha_bruta.strip()
            if not linha or linha.startswith("Total "):
                continue
            if "Total da fatura em real" in linha:
                break

            match = self._RE_LINHA.match(linha)
            if not match:
                continue

            dia_s, mes_s, meio_bruto, valor_s, sinal_cred = match.groups()
            if sinal_cred == "-" or meio_bruto.upper().startswith("PAGTO "):
                continue

            dia_c, mes_c = int(dia_s), int(mes_s)
            ano_c = data_fechamento.year
            try_date = date(ano_c, mes_c, dia_c)
            data_compra = date(ano_c - 1, mes_c, dia_c) if try_date > data_fechamento else try_date

            parcela_atual: int | None = None
            total_parcelas: int | None = None
            match_parc = self._RE_PARCELA_E_CIDADE.match(meio_bruto.strip())
            if match_parc:
                desc_base, p1_s, p2_s, _ = match_parc.groups()
                p1, p2 = int(p1_s), int(p2_s)
                if 1 <= p1 <= p2 <= 48:
                    parcela_atual = p1
                    total_parcelas = p2
                    descricao_limpa = re.sub(r"\s+", " ", desc_base).strip()
                else:
                    descricao_limpa = self._remover_cidade(meio_bruto)
            else:
                descricao_limpa = self._remover_cidade(meio_bruto)

            valor = exigir_decimal(valor_s, contexto=descricao_limpa)
            categoria, incerto = self._classificador.classificar_descricao(descricao_limpa)

            lancamentos.append(
                LancamentoTransacao.criar_saida_classificada(
                    descricao=descricao_limpa,
                    valor=valor,
                    data_compra=data_compra,
                    mes_competencia=mes_fatura,
                    conta_ou_cartao=self._nome_cartao,
                    categoria=categoria,
                    incerto=incerto,
                    parcela_atual=parcela_atual,
                    total_parcelas=total_parcelas,
                )
            )

        return lancamentos

    def _remover_cidade(self, texto_meio: str) -> str:
        limpo = re.sub(r"\s+", " ", texto_meio).strip()
        upper = limpo.upper()
        for cidade in self._cidades_sufixo:
            if upper.endswith(" " + cidade):
                limpo = limpo[: -(len(cidade) + 1)].strip()
                break
        return limpo

    @classmethod
    def _extrair_data_fechamento(cls, texto: str) -> date:
        m = cls._RE_FECHAMENTO_DISPONIVEL.search(texto)
        if m:
            d, mes, a = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return date(a, mes, d)
        m2 = cls._RE_FECHAMENTO_LIVELO.search(texto)
        if m2:
            d, mes, a_str = int(m2.group(1)), int(m2.group(2)), m2.group(3)
            a = int(a_str) + 2000 if len(a_str) == 2 else int(a_str)
            return date(a, mes, d)
        raise ValueError(
            "Não foi possível localizar a data de fechamento na fatura Bradesco Cartões."
        )


class BradescoContaPdfParser(BasePdfParser):
    """Parser determinístico para extrato PDF de conta corrente do Bradesco (`Bradesco Conta`)."""

    _RE_LINHA_COM_DATA = re.compile(
        r"^(\d{2})/(\d{2})/(\d{4})\s+(PIX\s+(?:RECEBIDO|ENVIADO))\s+(.+?)\s+\d{2}/\d{2}\s+\d+\s+([\d\.]+,\d{2})\s+[\d\.,\-]+$",
        re.IGNORECASE,
    )
    _RE_CABECALHO_SEM_DATA = re.compile(r"^(PIX\s+(?:RECEBIDO|ENVIADO))\s*$", re.IGNORECASE)
    _RE_DETALHE_SEM_DATA = re.compile(
        r"^(.+?)\s+(\d{2})/(\d{2})\s+\d+\s+([\d\.]+,\d{2})\s+[\d\.,\-]+$"
    )

    def __init__(
        self,
        nome_conta: str = "Bradesco Conta",
        nome_titular: str = "",
        pessoas_cadastradas: Iterable[Pessoa] | None = None,
        classificador: ClassificadorCategoria | None = None,
    ) -> None:
        self._nome_conta = nome_conta
        self._nome_titular_norm = normalizar_texto_chave(nome_titular) if nome_titular else ""
        self._pessoas_cadastradas = pessoas_cadastradas
        self._classificador = classificador or ClassificadorCategoria()

    def pode_processar(self, arquivo: Path, texto: str = "") -> bool:
        if arquivo.suffix.lower() != ".pdf":
            return False
        t = texto.lower()
        return "bradesco celular" in t or "extrato de: ag" in t

    def parse_texto(self, texto: str) -> list[LancamentoTransacao]:
        lancamentos: list[LancamentoTransacao] = []
        linhas = [ln.strip() for ln in texto.splitlines() if ln.strip()]
        ultima_data: date | None = None
        i = 0

        while i < len(linhas):
            ln = linhas[i]
            m1 = self._RE_LINHA_COM_DATA.match(ln)
            if m1:
                d_s, m_s, a_s, tipo_pix, detalhe, valor_s = m1.groups()
                ultima_data = date(int(a_s), int(m_s), int(d_s))
                lancamentos.append(
                    self._construir_lancamento(ultima_data, tipo_pix, detalhe, valor_s)
                )
                i += 1
                continue

            m_cab = self._RE_CABECALHO_SEM_DATA.match(ln)
            if m_cab and i + 1 < len(linhas) and ultima_data is not None:
                tipo_pix = m_cab.group(1)
                m_det = self._RE_DETALHE_SEM_DATA.match(linhas[i + 1])
                if m_det:
                    detalhe, d_s, m_s, valor_s = m_det.groups()
                    data_op = date(ultima_data.year, int(m_s), int(d_s))
                    lancamentos.append(
                        self._construir_lancamento(data_op, tipo_pix, detalhe, valor_s)
                    )
                    i += 2
                    continue

            i += 1

        return lancamentos

    def _construir_lancamento(
        self, data_op: date, tipo_pix: str, detalhe: str, valor_s: str
    ) -> LancamentoTransacao:
        valor = exigir_decimal(valor_s, contexto=detalhe)
        mes_comp = formatar_mes_competencia(data_op.year, data_op.month)
        descricao = f"{tipo_pix.strip()} {detalhe.strip()}"
        norm_det = normalizar_texto_chave(detalhe)

        if self._nome_titular_norm and self._nome_titular_norm in norm_det:
            return LancamentoTransacao(
                descricao=descricao,
                valor=valor,
                data_compra=data_op,
                mes_competencia=mes_comp,
                tipo=TipoTransacao.MOVIMENTACAO_INTERNA,
                categoria="",
                conta_ou_cartao=self._nome_conta,
                eh_entrada_na_conta="recebido" in tipo_pix.lower(),
            )

        if "recebido" in tipo_pix.lower():
            pessoa_reemb = identificar_pessoa_reembolso(
                detalhe, pessoas_cadastradas=self._pessoas_cadastradas
            )
            if pessoa_reemb:
                return LancamentoTransacao(
                    descricao=descricao,
                    valor=valor,
                    data_compra=data_op,
                    mes_competencia=mes_comp,
                    tipo=TipoTransacao.REEMBOLSO,
                    categoria=CATEGORIA_EMPRESTIMO,
                    conta_ou_cartao=self._nome_conta,
                    pessoa=pessoa_reemb,
                )
            return LancamentoTransacao(
                descricao=descricao,
                valor=valor,
                data_compra=data_op,
                mes_competencia=mes_comp,
                tipo=TipoTransacao.ENTRADA,
                categoria="",
                conta_ou_cartao=self._nome_conta,
            )

        categoria, incerto = self._classificador.classificar_descricao(descricao)
        return LancamentoTransacao.criar_saida_classificada(
            descricao=descricao,
            valor=valor,
            data_compra=data_op,
            mes_competencia=mes_comp,
            conta_ou_cartao=self._nome_conta,
            categoria=categoria,
            incerto=incerto,
        )
