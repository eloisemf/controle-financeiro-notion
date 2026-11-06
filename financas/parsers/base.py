"""Classes base, contratos e utilitários compartilhados pelos parsers de documentos financeiros."""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from pathlib import Path

from pypdf import PdfReader

from financas.domain.models import LancamentoTransacao, para_decimal
from financas.parsers.sanitization import MascaradorDadosSensiveis

MESES_ABREV_PT: dict[str, int] = {
    "jan": 1,
    "fev": 2,
    "mar": 3,
    "abr": 4,
    "mai": 5,
    "jun": 6,
    "jul": 7,
    "ago": 8,
    "set": 9,
    "out": 10,
    "nov": 11,
    "dez": 12,
}


def exigir_decimal(valor_str: str, contexto: str = "") -> Decimal:
    """Converte string monetária em `Decimal` levantando `ValueError` explícito se vazio/inválido."""
    dec = para_decimal(valor_str)
    if dec is None:
        msg = f"Valor monetário inválido ou vazio: {valor_str!r}"
        if contexto:
            msg += f" ({contexto})"
        raise ValueError(msg)
    return dec


def ler_pdf_mascarado(pdf_path: Path) -> str:
    """Extrai o texto de todas as páginas de um arquivo PDF e aplica o mascaramento de PII."""
    reader = PdfReader(pdf_path)
    paginas = [page.extract_text() or "" for page in reader.pages]
    return MascaradorDadosSensiveis.mascarar("\n".join(paginas))


class BaseDocumentoParser(ABC):
    """Interface base (Strategy) para extratores de documentos financeiros."""

    def pode_processar(self, arquivo: Path, texto: str = "") -> bool:
        """Retorna True se este parser suporta o arquivo/conteúdo informado."""
        return False

    @abstractmethod
    def parse_arquivo(self, arquivo: Path, texto_pre_extraido: str = "") -> list[LancamentoTransacao]:
        """Extrai lançamentos a partir de um arquivo financeiro."""


class BasePdfParser(BaseDocumentoParser):
    """Classe base abstrata para extratores determinísticos de documentos em PDF."""

    def extrair_texto_mascarado(self, pdf_path: Path) -> str:
        return ler_pdf_mascarado(pdf_path)

    def parse_pdf(self, pdf_path: Path) -> list[LancamentoTransacao]:
        texto = self.extrair_texto_mascarado(pdf_path)
        return self.parse_texto(texto)

    def parse_arquivo(self, arquivo: Path, texto_pre_extraido: str = "") -> list[LancamentoTransacao]:
        texto = texto_pre_extraido or self.extrair_texto_mascarado(arquivo)
        return self.parse_texto(texto)

    @abstractmethod
    def parse_texto(self, texto: str) -> list[LancamentoTransacao]:
        """Extrai lançamentos a partir do texto já sanitizado do PDF."""
