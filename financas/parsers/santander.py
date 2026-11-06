"""Parsers para documentos do Santander (Extrato CSV, Fatura Fechada PDF e Fatura Aberta Vision/TXT)."""

from __future__ import annotations

import base64
import csv
from datetime import date
from decimal import Decimal
import io
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
from typing import Any, Iterable

import requests

from financas.domain.billing import CalculadoraFatura, adicionar_meses, formatar_mes_competencia
from financas.domain.classification import ClassificadorCategoria, identificar_pessoa_reembolso
from financas.domain.models import (
    CATEGORIA_EMPRESTIMO,
    DEFAULT_CARTOES,
    CartaoConfig,
    LancamentoTransacao,
    Pessoa,
    Recorrencia,
    TipoTransacao,
    normalizar_texto_chave,
    para_decimal,
)
from financas.parsers.base import BaseDocumentoParser, BasePdfParser, exigir_decimal
from financas.parsers.sanitization import MascaradorDadosSensiveis

logger = logging.getLogger(__name__)


def _obter_cartao_config(
    cartoes: dict[str, CartaoConfig], nome_cartao: str
) -> CartaoConfig:
    if nome_cartao in cartoes:
        return cartoes[nome_cartao]
    alternativo = (
        nome_cartao.replace("Santander", "Santader")
        if "Santander" in nome_cartao
        else nome_cartao.replace("Santader", "Santander")
    )
    if alternativo in cartoes:
        return cartoes[alternativo]
    raise KeyError(f"Cartão não configurado: '{nome_cartao}'")


class SantanderExtratoCsvParser(BaseDocumentoParser):
    """Parser determinístico para extrato CSV de conta corrente do Santander."""

    _PADROES_SALARIO_DEFAULT: tuple[str, ...] = ("liquido de vencimento", "pagamento de salario")
    _PADROES_INTERNA_DEFAULT: tuple[str, ...] = ("transferencia programada",)
    _MAPA_FATURAS_DEFAULT: tuple[tuple[str, str], ...] = (
        ("pagamento cartao credito", "Santander Credito"),
        ("banco bradesco s a", "Bradesco Credito"),
        ("bradescard", "Bradesco Credito"),
    )

    def __init__(
        self,
        nome_conta: str = "Santander Conta",
        nome_titular: str = "",
        pessoas_cadastradas: Iterable[Pessoa] | None = None,
        padroes_salario: tuple[str, ...] | None = None,
        padroes_transferencia_interna: tuple[str, ...] | None = None,
        mapa_pagamento_fatura: tuple[tuple[str, str], ...] | None = None,
        classificador: ClassificadorCategoria | None = None,
    ) -> None:
        self._nome_conta = nome_conta
        self._nome_titular_norm = normalizar_texto_chave(nome_titular) if nome_titular else ""
        self._pessoas_cadastradas = pessoas_cadastradas
        self._padroes_salario = padroes_salario or self._PADROES_SALARIO_DEFAULT
        self._padroes_interna = (
            padroes_transferencia_interna or self._PADROES_INTERNA_DEFAULT
        )
        self._mapa_faturas = mapa_pagamento_fatura or self._MAPA_FATURAS_DEFAULT
        self._classificador = classificador or ClassificadorCategoria()

    def pode_processar(self, arquivo: Path, texto: str = "") -> bool:
        return arquivo.suffix.lower() == ".csv"

    def parse_arquivo(self, arquivo: Path, texto_pre_extraido: str = "") -> list[LancamentoTransacao]:
        return self.parse_csv(arquivo)

    def parse_csv(self, csv_path: Path) -> list[LancamentoTransacao]:
        try:
            conteudo = csv_path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            conteudo = csv_path.read_text(encoding="latin-1")
        return self.parse_texto(MascaradorDadosSensiveis.mascarar(conteudo))

    def _identificar_pagamento_fatura(self, desc_norm: str, det_norm: str) -> str | None:
        combinado = f"{desc_norm} {det_norm}"
        for padrao, destino in self._mapa_faturas:
            if padrao in combinado:
                return destino
        return None

    def parse_texto(self, texto: str) -> list[LancamentoTransacao]:
        linhas = texto.splitlines()
        idx_cabecalho = -1
        for i, ln in enumerate(linhas):
            if ln.strip().lower().startswith("data;descri"):
                idx_cabecalho = i
                break
        if idx_cabecalho == -1:
            raise ValueError(
                "Cabeçalho 'Data;Descrição;Detalhe;Valor;Saldo' não encontrado no CSV Santander."
            )

        leitor = csv.reader(io.StringIO("\n".join(linhas[idx_cabecalho + 1 :])), delimiter=";")
        lancamentos: list[LancamentoTransacao] = []

        for row in leitor:
            if len(row) < 4:
                continue
            data_s, desc_bruta, detalhe_bruto, valor_bruto = (
                row[0].strip(),
                row[1].strip(),
                row[2].strip(),
                row[3].strip(),
            )
            if not re.match(r"^\d{2}/\d{2}/\d{4}$", data_s):
                continue

            d_s, m_s, a_s = data_s.split("/")
            data_op = date(int(a_s), int(m_s), int(d_s))
            mes_comp = formatar_mes_competencia(data_op.year, data_op.month)

            negativo = valor_bruto.startswith("-")
            valor_abs = para_decimal(valor_bruto.lstrip("-").strip())
            if valor_abs is None or valor_abs <= Decimal("0.00"):
                continue

            desc_norm = normalizar_texto_chave(desc_bruta)
            det_norm = normalizar_texto_chave(detalhe_bruto)

            if "remuneracao aplicacao automatica" in desc_norm and valor_abs < Decimal("1.00"):
                continue

            descricao_completa = (
                f"{desc_bruta} {detalhe_bruto}".strip() if detalhe_bruto else desc_bruta
            )

            # 1. Salário / Proventos
            if any(p in desc_norm for p in self._padroes_salario):
                lancamentos.append(
                    LancamentoTransacao(
                        descricao="Salário (Líquido de vencimento)",
                        valor=valor_abs,
                        data_compra=data_op,
                        mes_competencia=mes_comp,
                        tipo=TipoTransacao.ENTRADA,
                        categoria="",
                        conta_ou_cartao=self._nome_conta,
                        recorrencia=Recorrencia.FIXO,
                    )
                )
                continue

            # 2. Transferências próprias / reserva
            if (
                self._nome_titular_norm and self._nome_titular_norm in det_norm
            ) or any(p in desc_norm for p in self._padroes_interna):
                lancamentos.append(
                    LancamentoTransacao(
                        descricao=descricao_completa,
                        valor=valor_abs,
                        data_compra=data_op,
                        mes_competencia=mes_comp,
                        tipo=TipoTransacao.MOVIMENTACAO_INTERNA,
                        categoria="",
                        conta_ou_cartao=self._nome_conta,
                        eh_entrada_na_conta=not negativo,
                    )
                )
                continue

            # 3. Pagamentos de fatura de cartão de crédito
            cartao_destino = self._identificar_pagamento_fatura(desc_norm, det_norm)
            if cartao_destino:
                lancamentos.append(
                    LancamentoTransacao(
                        descricao=descricao_completa,
                        valor=valor_abs,
                        data_compra=data_op,
                        mes_competencia=mes_comp,
                        tipo=TipoTransacao.PAGAR_FATURA,
                        categoria="",
                        conta_ou_cartao=self._nome_conta,
                        conta_destino=cartao_destino,
                    )
                )
                continue

            # 4. Entradas / Reembolsos via Pix
            if not negativo:
                pessoa_reemb = identificar_pessoa_reembolso(
                    detalhe_bruto, pessoas_cadastradas=self._pessoas_cadastradas
                )
                if pessoa_reemb:
                    lancamentos.append(
                        LancamentoTransacao(
                            descricao=descricao_completa,
                            valor=valor_abs,
                            data_compra=data_op,
                            mes_competencia=mes_comp,
                            tipo=TipoTransacao.REEMBOLSO,
                            categoria=CATEGORIA_EMPRESTIMO,
                            conta_ou_cartao=self._nome_conta,
                            pessoa=pessoa_reemb,
                        )
                    )
                else:
                    lancamentos.append(
                        LancamentoTransacao(
                            descricao=descricao_completa,
                            valor=valor_abs,
                            data_compra=data_op,
                            mes_competencia=mes_comp,
                            tipo=TipoTransacao.ENTRADA,
                            categoria="",
                            conta_ou_cartao=self._nome_conta,
                        )
                    )
                continue

            # 5. Saídas (Pix enviado, Débito automático)
            categoria, incerto = self._classificador.classificar_descricao(descricao_completa)
            lancamentos.append(
                LancamentoTransacao.criar_saida_classificada(
                    descricao=descricao_completa,
                    valor=valor_abs,
                    data_compra=data_op,
                    mes_competencia=mes_comp,
                    conta_ou_cartao=self._nome_conta,
                    categoria=categoria,
                    incerto=incerto,
                )
            )

        return lancamentos


class SantanderCartoesPdfParser(BasePdfParser):
    """Parser determinístico para fatura fechada em PDF do cartão Santander."""

    _RE_FILENAME_VENC = re.compile(r"fatura_(\d{2})(\d{4})", re.IGNORECASE)
    _RE_TEXTO_VENC = re.compile(r"vencimento\s*:?\s*(\d{2})/(\d{2})/(\d{4})", re.IGNORECASE)
    _RE_LINHA_LANCAMENTO = re.compile(
        r"^(?:[1-9]\s+)?(\d{2})/(\d{2})\s+(.+?)(?:\s+(\d{2})/(\d{2}))?\s+(-?[\d\.]+,\d{2})(?:\s+[\d\.,\-]+)?$"
    )

    def __init__(
        self,
        nome_cartao: str = "Santander Credito",
        calculadora: CalculadoraFatura | None = None,
        cartoes: dict[str, CartaoConfig] | None = None,
        classificador: ClassificadorCategoria | None = None,
    ) -> None:
        self._nome_cartao = nome_cartao
        self._calc = calculadora or CalculadoraFatura()
        self._cartoes = cartoes if cartoes is not None else DEFAULT_CARTOES
        self._classificador = classificador or ClassificadorCategoria()

    def pode_processar(self, arquivo: Path, texto: str = "") -> bool:
        if arquivo.suffix.lower() != ".pdf":
            return False
        t = texto.lower()
        return (
            "esfera.com.vc" in t
            or "santander.com.br" in t
            or "detalhamento da fatura" in t
        )

    def parse_arquivo(self, arquivo: Path, texto_pre_extraido: str = "") -> list[LancamentoTransacao]:
        texto = texto_pre_extraido or self.extrair_texto_mascarado(arquivo)
        return self.parse_texto(texto, nome_arquivo=arquivo.name)

    def parse_pdf(
        self, pdf_path: Path, ano_referencia: int | None = None
    ) -> list[LancamentoTransacao]:
        texto = self.extrair_texto_mascarado(pdf_path)
        return self.parse_texto(texto, nome_arquivo=pdf_path.name, ano_referencia=ano_referencia)

    def _inferir_competencia(
        self, texto: str, nome_arquivo: str = "", ano_referencia: int | None = None
    ) -> tuple[int, int]:
        cartao = _obter_cartao_config(self._cartoes, self._nome_cartao)
        hoje = date.today()
        ano_ref = ano_referencia if ano_referencia is not None else hoje.year
        mes_arq: int | None = None

        m_arq = self._RE_FILENAME_VENC.search(nome_arquivo)
        if m_arq:
            mes_arq, ano_ref = int(m_arq.group(1)), int(m_arq.group(2))

        m_txt = self._RE_TEXTO_VENC.search(texto)
        if m_txt:
            mes_arq, ano_ref = int(m_txt.group(2)), int(m_txt.group(3))

        secao = ""
        meses_despesas: list[int] = []
        for ln in texto.splitlines():
            limpa = ln.strip()
            lower = limpa.lower()
            if lower.startswith("despesas"):
                secao = "despesas"
                continue
            if lower.startswith("resumo da fatura"):
                break
            if secao == "despesas":
                m_ln = self._RE_LINHA_LANCAMENTO.match(limpa)
                if m_ln:
                    meses_despesas.append(int(m_ln.group(2)))
        if meses_despesas:
            mes_fech = meses_despesas[-1]
            ano_fech = ano_ref - 1 if (mes_arq == 1 and mes_fech == 12) else ano_ref
            return (ano_fech, mes_fech)

        if mes_arq is not None:
            return self._calc.competencia_por_vencimento(cartao, ano_ref, mes_arq)
        return (ano_ref, hoje.month)

    def parse_texto(
        self, texto: str, nome_arquivo: str = "", ano_referencia: int | None = None
    ) -> list[LancamentoTransacao]:
        ano_comp, mes_comp = self._inferir_competencia(
            texto, nome_arquivo=nome_arquivo, ano_referencia=ano_referencia
        )
        mes_competencia_str = formatar_mes_competencia(ano_comp, mes_comp)

        lancamentos: list[LancamentoTransacao] = []
        secao = ""

        for ln in texto.splitlines():
            limpa = ln.strip()
            if not limpa:
                continue
            lower = limpa.lower()

            if lower.startswith("pagamento e demais"):
                secao = "creditos"
                continue
            if lower.startswith("parcelamentos"):
                secao = "parcelamentos"
                continue
            if lower.startswith("despesas"):
                secao = "despesas"
                continue
            if lower.startswith("resumo da fatura") or lower.startswith("saldo total consolidado"):
                break
            if lower.startswith("valor total") or lower.startswith("compra data"):
                continue

            if secao not in ("parcelamentos", "despesas"):
                continue

            m = self._RE_LINHA_LANCAMENTO.match(limpa)
            if not m:
                continue

            dia_s, mes_s, desc_bruta, parc_at_s, parc_tot_s, valor_s = m.groups()
            if valor_s.startswith("-"):
                continue
            valor = para_decimal(valor_s)
            if valor is None or valor <= Decimal("0.00"):
                continue

            dia_c, mes_c = int(dia_s), int(mes_s)
            p_atual = int(parc_at_s) if parc_at_s else None
            p_total = int(parc_tot_s) if parc_tot_s else None

            if p_atual and p_atual > 1:
                ano_orig, _ = adicionar_meses(ano_comp, mes_comp, -(p_atual - 1))
                ano_c = ano_orig
            else:
                ano_c = ano_comp - 1 if mes_c > mes_comp else ano_comp

            data_compra = date(ano_c, mes_c, dia_c)
            desc_limpa = re.sub(r"\s+", " ", desc_bruta).strip()
            cat, incerto = self._classificador.classificar_descricao(desc_limpa)

            lancamentos.append(
                LancamentoTransacao.criar_saida_classificada(
                    descricao=desc_limpa,
                    valor=valor,
                    data_compra=data_compra,
                    mes_competencia=mes_competencia_str,
                    conta_ou_cartao=self._nome_cartao,
                    categoria=cat,
                    incerto=incerto,
                    parcela_atual=p_atual,
                    total_parcelas=p_total,
                )
            )

        return lancamentos


class SantanderCartaoVisionParser(BaseDocumentoParser):
    """Extrator para fatura aberta de cartão (via print `.png`/`.jpg` com Gemini Flash ou `.txt`)."""

    _MODELS_FALLBACK_DEFAULT = (
        "gemini-2.5-flash",
        "gemini-2.0-flash",
    )
    _EXTENSOES_IMAGEM = (".png", ".jpg", ".jpeg", ".webp")
    _RE_TXT_LINHA = re.compile(
        r"^(\d{2})/(\d{2})(?:/(\d{4}))?\s+(.+?)\s+(?:R\$\s*)?([\d\.]+,\d{2})$"
    )
    _RE_PARC = re.compile(r"\b(\d{1,2})/(\d{1,2})\b")

    def __init__(
        self,
        gemini_api_key: str | None = None,
        nome_cartao: str = "Santander Credito",
        calculadora: CalculadoraFatura | None = None,
        cartoes: dict[str, CartaoConfig] | None = None,
        models_fallback: tuple[str, ...] | None = None,
        classificador: ClassificadorCategoria | None = None,
    ) -> None:
        self._api_key = gemini_api_key
        self._nome_cartao = nome_cartao
        self._calc = calculadora or CalculadoraFatura()
        self._cartoes = cartoes if cartoes is not None else DEFAULT_CARTOES
        self._classificador = classificador or ClassificadorCategoria()
        env_models = os.getenv("GEMINI_MODELS", "").strip()
        if models_fallback:
            self._models_fallback = models_fallback
        elif env_models:
            self._models_fallback = tuple(m.strip() for m in env_models.split(",") if m.strip())
        else:
            self._models_fallback = self._MODELS_FALLBACK_DEFAULT

    def pode_processar(self, arquivo: Path, texto: str = "") -> bool:
        return arquivo.suffix.lower() in (".txt", *self._EXTENSOES_IMAGEM)

    def parse_arquivo(
        self,
        path: Path,
        texto_pre_extraido: str = "",
        ano_referencia: int | None = None,
    ) -> list[LancamentoTransacao]:
        sufixo = path.suffix.lower()
        if sufixo == ".txt":
            texto = MascaradorDadosSensiveis.mascarar(path.read_text(encoding="utf-8"))
            return self.parse_texto_colado(texto, ano_referencia=ano_referencia)
        if sufixo in self._EXTENSOES_IMAGEM:
            sidecar_txt = path.with_suffix(".txt")
            if sidecar_txt.is_file():
                logger.info("Usando transcrição local sidecar: %s", sidecar_txt.name)
                texto = MascaradorDadosSensiveis.mascarar(sidecar_txt.read_text(encoding="utf-8"))
                return self.parse_texto_colado(texto, ano_referencia=ano_referencia)
            return self.parse_imagem_gemini(path, ano_referencia=ano_referencia)
        raise ValueError(f"Formato não suportado para fatura aberta: {sufixo}")

    def parse_texto_colado(
        self, texto: str, ano_referencia: int | None = None
    ) -> list[LancamentoTransacao]:
        ano_ref = ano_referencia if ano_referencia is not None else date.today().year
        cartao = _obter_cartao_config(self._cartoes, self._nome_cartao)
        lancamentos: list[LancamentoTransacao] = []

        for ln in texto.splitlines():
            linha = ln.strip()
            m = self._RE_TXT_LINHA.match(linha)
            if not m:
                continue
            d_s, m_s, a_s, desc_bruta, val_s = m.groups()
            ano = int(a_s) if a_s else ano_ref
            data_compra = date(ano, int(m_s), int(d_s))
            ano_comp, mes_comp = self._calc.competencia_fatura(cartao, data_compra)

            p_atual: int | None = None
            p_total: int | None = None
            m_parc = self._RE_PARC.search(desc_bruta)
            if m_parc:
                p1, p2 = int(m_parc.group(1)), int(m_parc.group(2))
                if 1 <= p1 <= p2 <= 48:
                    p_atual, p_total = p1, p2
                    desc_limpa = self._RE_PARC.sub("", desc_bruta).strip(" -")
                else:
                    desc_limpa = desc_bruta.strip()
            else:
                desc_limpa = desc_bruta.strip()

            valor = exigir_decimal(val_s, contexto=desc_limpa)
            cat, incerto = self._classificador.classificar_descricao(desc_limpa)
            lancamentos.append(
                LancamentoTransacao.criar_saida_classificada(
                    descricao=desc_limpa,
                    valor=valor,
                    data_compra=data_compra,
                    mes_competencia=formatar_mes_competencia(ano_comp, mes_comp),
                    conta_ou_cartao=self._nome_cartao,
                    categoria=cat,
                    incerto=incerto,
                    parcela_atual=p_atual,
                    total_parcelas=p_total,
                )
            )
        return lancamentos

    def parse_imagem_gemini(
        self, image_path: Path, ano_referencia: int | None = None
    ) -> list[LancamentoTransacao]:
        if not self._api_key:
            raise RuntimeError(
                "GEMINI_API_KEY não configurada no .env para leitura de print de fatura."
            )
        ano_ref = ano_referencia if ano_referencia is not None else date.today().year
        logger.warning(
            "Enviando imagem '%s' para extração externa via API Gemini Flash Vision...",
            image_path.name,
        )
        mime_type = mimetypes.guess_type(image_path.name)[0] or "image/png"
        b64_img = base64.b64encode(image_path.read_bytes()).decode("ascii")
        prompt = (
            "Extraia apenas as compras válidas (Autorizadas ou Em processamento) desta fatura de cartão "
            "em JSON estrito (lista de objetos). "
            "Ignore compras com status 'Cancelada', valores riscados, pagamentos de fatura ou limites. "
            "Para cada item retorne: "
            '{"data": "DD/MM/YYYY", "descricao": "texto", "valor": "0.00", '
            '"parcela_atual": null ou int, "total_parcelas": null ou int}. '
            f"Se o ano não aparecer na imagem, use {ano_ref}."
        )
        payload: dict[str, Any] = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": mime_type, "data": b64_img}},
                    ]
                }
            ],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.0},
        }
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self._api_key,
        }

        itens_json: list[dict[str, Any]] | None = None
        last_error: Exception | None = None
        for model in self._models_fallback:
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            try:
                resp = requests.post(endpoint, headers=headers, json=payload, timeout=45)
                if resp.status_code == 200:
                    body = resp.json()
                    raw_json = body["candidates"][0]["content"]["parts"][0]["text"]
                    itens_json = json.loads(raw_json)
                    break
                else:
                    last_error = RuntimeError(f"Modelo {model} retornou HTTP {resp.status_code}: {resp.text[:200]}")
                    logger.warning("Modelo %s retornou HTTP %d. Tentando próximo...", model, resp.status_code)
            except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
                last_error = e
                logger.warning("Falha ao consultar ou decodificar resposta de %s: %s. Tentando próximo...", model, e)

        if itens_json is None:
            raise last_error or RuntimeError("Nenhum modelo Gemini respondeu com sucesso.")

        cartao = _obter_cartao_config(self._cartoes, self._nome_cartao)
        lancamentos: list[LancamentoTransacao] = []
        for obj in itens_json:
            parts = str(obj.get("data", "")).strip().split("/")
            if len(parts) == 3:
                d_s, m_s, a_s = parts
                ano = int(a_s) if len(a_s) == 4 else (2000 + int(a_s))
            elif len(parts) == 2:
                d_s, m_s = parts
                ano = ano_ref
            else:
                continue
            data_c = date(ano, int(m_s), int(d_s))
            ano_comp, mes_comp = self._calc.competencia_fatura(cartao, data_c)
            desc = MascaradorDadosSensiveis.mascarar(str(obj["descricao"]).strip())
            valor = exigir_decimal(str(obj["valor"]), contexto=desc)
            cat, incerto = self._classificador.classificar_descricao(desc)
            lancamentos.append(
                LancamentoTransacao.criar_saida_classificada(
                    descricao=desc,
                    valor=valor,
                    data_compra=data_c,
                    mes_competencia=formatar_mes_competencia(ano_comp, mes_comp),
                    conta_ou_cartao=self._nome_cartao,
                    categoria=cat,
                    incerto=incerto,
                    parcela_atual=obj.get("parcela_atual"),
                    total_parcelas=obj.get("total_parcelas"),
                )
            )
        return lancamentos
