"""Fachada roteadora (`ExtratorDocumentosFinanceiros`) para arquivos financeiros multi-banco."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from financas.domain.billing import CalculadoraFatura
from financas.domain.models import LancamentoTransacao, Pessoa
from financas.parsers.base import BaseDocumentoParser, ler_pdf_mascarado
from financas.parsers.bradesco import BradescoCartoesPdfParser, BradescoContaPdfParser
from financas.parsers.inter import InterFaturaPdfParser
from financas.parsers.santander import (
    SantanderCartaoVisionParser,
    SantanderCartoesPdfParser,
    SantanderExtratoCsvParser,
)


class ExtratorDocumentosFinanceiros:
    """Identifica automaticamente o banco e o formato do arquivo usando um registro de estratégias (`BaseDocumentoParser`)."""

    def __init__(
        self,
        gemini_api_key: str | None = None,
        calculadora: CalculadoraFatura | None = None,
        nome_titular: str = "",
        pessoas: Iterable[Pessoa] | None = None,
        parsers: Sequence[BaseDocumentoParser] | None = None,
    ) -> None:
        self._calc = calculadora or CalculadoraFatura()
        if parsers is not None:
            self._parsers = list(parsers)
        else:
            self._parsers = [
                SantanderExtratoCsvParser(
                    nome_titular=nome_titular, pessoas_cadastradas=pessoas
                ),
                SantanderCartaoVisionParser(
                    gemini_api_key=gemini_api_key, calculadora=self._calc
                ),
                BradescoCartoesPdfParser(),
                BradescoContaPdfParser(
                    nome_titular=nome_titular, pessoas_cadastradas=pessoas
                ),
                SantanderCartoesPdfParser(calculadora=self._calc),
                InterFaturaPdfParser(calculadora=self._calc),
            ]

    def registrar_parser(self, parser: BaseDocumentoParser, inicio: bool = True) -> None:
        """Registra um novo parser customizado na cadeia de resolução."""
        if inicio:
            self._parsers.insert(0, parser)
        else:
            self._parsers.append(parser)

    def extrair_de_arquivo(self, arquivo: Path) -> list[LancamentoTransacao]:
        sufixo = arquivo.suffix.lower()
        texto_pdf = ler_pdf_mascarado(arquivo) if sufixo == ".pdf" else ""

        for parser in self._parsers:
            if parser.pode_processar(arquivo, texto_pdf):
                return parser.parse_arquivo(arquivo, texto_pre_extraido=texto_pdf)

        raise ValueError(f"Formato ou banco de arquivo não suportado: {arquivo.name}")
