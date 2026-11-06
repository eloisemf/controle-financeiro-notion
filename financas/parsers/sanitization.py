"""Sanitização (mascaramento) de dados pessoais e bancários sensíveis (PII)."""

from __future__ import annotations

import re


class MascaradorDadosSensiveis:
    """Mascara PII e dados bancários sensíveis em textos extraídos de faturas/extratos."""

    _RE_CPF = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")
    _RE_CARTAO = re.compile(r"\b\d{4}[\s\*•Xx-]+\d{4}(?:[\s\*•Xx-]+\d{4})?\b")
    _RE_CARTAO_FINAL = re.compile(r"(?i)(cart[aã]o\s*(?:final|n[uú]mero)?\s*[:\-]?\s*)\d{4}")
    _RE_CONTA = re.compile(r"\b\d{4,10}-\d\b")
    _RE_BOLETO = re.compile(r"\b\d{5}\.\d{5}\s+\d{5}\.\d{6}\s+\d{5}\.\d{6}\s+\d\s+\d{14}\b")

    @classmethod
    def mascarar(cls, texto: str) -> str:
        texto = cls._RE_CPF.sub("***.***.***-**", texto)
        texto = cls._RE_CARTAO.sub("**** **** **** ****", texto)
        texto = cls._RE_CARTAO_FINAL.sub(r"\1****", texto)
        texto = cls._RE_CONTA.sub("*****-*", texto)
        texto = cls._RE_BOLETO.sub("[LINHA_DIGITAVEL_MASCARADA]", texto)
        return texto
