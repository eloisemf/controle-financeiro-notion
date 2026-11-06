"""Serviços de domínio para classificação de categorias e identificação de reembolsos."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
from typing import Iterable

from financas.domain.models import CATEGORIA_DIVERSOS, Pessoa, normalizar_texto_chave

logger = logging.getLogger(__name__)

ARQUIVO_REGRAS_CATEGORIA = Path(__file__).resolve().parents[2] / "regras_categoria.json"

# Mapeamento básico padrão de palavras-chave para categorias do Notion (fallback = 'Diversos')
REGRAS_CATEGORIA_PADRAO: tuple[tuple[str, str], ...] = (
    ("spotify", "Streamings"),
    ("netflix", "Streamings"),
    ("hbo", "Streamings"),
    ("meli", "Streamings"),
    ("wellhub", "Saúde"),
    ("gympass", "Saúde"),
    ("drogaria", "Saúde"),
    ("farmacia", "Saúde"),
    ("uber", "Transporte Privado"),
    ("dl ub", "Transporte Privado"),
    ("99app", "Transporte Privado"),
    ("99 app", "Transporte Privado"),
    ("99", "Transporte Privado"),
    ("ifood", "Lanche"),
    ("supermercado", "Alimentação"),
    ("mercado", "Alimentação"),
    ("luz", "Luz"),
    ("energia", "Luz"),
    ("condominio", "Condominio"),
    ("condomínio", "Condominio"),
    ("aluguel", "Aluguel"),
    ("correio", "Internet"),
    ("correios", "Internet"),
    ("celular", "Internet"),
    ("internet", "Internet"),
)

_CACHE_REGRAS_PADRAO: tuple[tuple[str, str], ...] | None = None


def carregar_regras_categoria(caminho: Path | str | None = None) -> tuple[tuple[str, str], ...]:
    """Carrega as regras de palavras-chave para categorias a partir de um arquivo JSON.

    Se o arquivo não existir ou for inválido, retorna `REGRAS_CATEGORIA_PADRAO`.
    Mantém cache em memória quando chamado para o caminho padrão.
    """
    global _CACHE_REGRAS_PADRAO
    if caminho is None and _CACHE_REGRAS_PADRAO is not None:
        return _CACHE_REGRAS_PADRAO

    alvo = Path(caminho) if caminho else ARQUIVO_REGRAS_CATEGORIA
    resultado = REGRAS_CATEGORIA_PADRAO
    if alvo.is_file():
        try:
            conteudo = json.loads(alvo.read_text(encoding="utf-8"))
            if isinstance(conteudo, dict):
                regras = [
                    (termo.strip().lower(), cat.strip())
                    for termo, cat in conteudo.items()
                    if termo and cat
                ]
                if regras:
                    resultado = tuple(regras)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Falha ao ler arquivo de regras de categoria (%s): %s", alvo, exc)

    if caminho is None:
        _CACHE_REGRAS_PADRAO = resultado
    return resultado


def _termo_corresponde(termo: str, texto_lower: str) -> bool:
    """Verifica se `termo` aparece em `texto_lower` respeitando fronteiras de palavra."""
    if termo.isdigit():
        padrao = rf"(?<![a-z0-9]){re.escape(termo)}(?![0-9])"
        return bool(re.search(padrao, texto_lower))
    if len(termo) <= 3:
        padrao = rf"(?<![a-z0-9]){re.escape(termo)}(?![a-z0-9])"
        return bool(re.search(padrao, texto_lower))
    return termo in texto_lower


class ClassificadorCategoria:
    """Classifica descrições de compras nas categorias do Notion de forma determinística."""

    def __init__(self, regras: tuple[tuple[str, str], ...] | None = None) -> None:
        self._regras = regras if regras is not None else carregar_regras_categoria()

    def classificar_descricao(self, descricao: str) -> tuple[str, bool]:
        """Classifica uma descrição utilizando as regras configuradas na instância."""
        return self.classificar(descricao, regras=self._regras)

    @classmethod
    def classificar(
        cls,
        descricao: str,
        regras: tuple[tuple[str, str], ...] | None = None,
    ) -> tuple[str, bool]:
        """Retorna `(categoria, incerto)`. Se incerto, classifica como 'Diversos'."""
        regras_ativas = regras if regras is not None else carregar_regras_categoria()
        desc_lower = descricao.lower()
        ignorar_mercado = any(
            termo in desc_lower for termo in ("mercadolivre", "mercado livre", "mercado pago")
        )
        for termo, categoria in regras_ativas:
            if termo == "mercado" and ignorar_mercado:
                continue
            if _termo_corresponde(termo, desc_lower):
                return categoria, False
        return CATEGORIA_DIVERSOS, True


def identificar_pessoa_reembolso(
    texto_remetente: str,
    pessoas_cadastradas: Iterable[Pessoa] | None = None,
    padroes_fallback: tuple[tuple[str, str], ...] = (),
) -> str | None:
    """Identifica se um Pix recebido veio de uma das Pessoas cadastradas.

    Consulta primeiro os padrões dinâmicos (`nome_no_pix_fatura`) das entidades `Pessoa`
    cadastradas no Notion e utiliza `padroes_fallback` caso fornecido.
    """
    norm = normalizar_texto_chave(texto_remetente)
    if not norm:
        return None

    if pessoas_cadastradas:
        for pessoa in pessoas_cadastradas:
            bruto = pessoa.nome_no_pix_fatura or pessoa.nome
            padroes = [
                normalizar_texto_chave(p)
                for p in bruto.split(";")
                if normalizar_texto_chave(p)
            ]
            if any(padrao in norm for padrao in padroes):
                return pessoa.nome

    for padrao, nome_pessoa in padroes_fallback:
        if padrao in norm:
            return nome_pessoa
    return None
