"""Parser inteligente para mensagens manuais de gastos e comandos no Telegram."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
import logging
from pathlib import Path
import re
from typing import NamedTuple

from financas.domain.billing import CalculadoraFatura, formatar_mes_competencia
from financas.domain.classification import ClassificadorCategoria
from financas.domain.models import (
    CartaoConfig,
    LancamentoTransacao,
    Recorrencia,
    StatusTransacao,
    TipoTransacao,
    UsoDinheiro,
    normalizar_texto_chave,
    para_decimal,
)

logger = logging.getLogger(__name__)

ARQUIVO_REGRAS_TELEGRAM = Path(__file__).resolve().parents[2] / "regras_telegram.json"
ARQUIVO_REGRAS_TELEGRAM_EXAMPLE = Path(__file__).resolve().parents[2] / "regras_telegram.example.json"


def carregar_regras_telegram(
    caminho: Path | str | None = None,
) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    """Carrega as regras de mapeamento de contas e categorias do Telegram a partir do JSON.

    Retorna `(regras_contas, regras_categorias)`, ambas ordenadas pelo tamanho decrescente do termo.
    """
    alvo = Path(caminho) if caminho else ARQUIVO_REGRAS_TELEGRAM
    if not alvo.is_file() and not caminho and ARQUIVO_REGRAS_TELEGRAM_EXAMPLE.is_file():
        alvo = ARQUIVO_REGRAS_TELEGRAM_EXAMPLE

    contas: list[tuple[str, str]] = []
    categorias: list[tuple[str, str]] = []

    if alvo.is_file():
        try:
            conteudo = json.loads(alvo.read_text(encoding="utf-8"))
            if isinstance(conteudo, dict):
                m_contas = conteudo.get("contas", {})
                if isinstance(m_contas, dict):
                    contas = [
                        (normalizar_texto_chave(k), str(v).strip())
                        for k, v in m_contas.items()
                        if k and v
                    ]
                m_cats = conteudo.get("categorias", {})
                if isinstance(m_cats, dict):
                    categorias = [
                        (normalizar_texto_chave(k), str(v).strip())
                        for k, v in m_cats.items()
                        if k and v
                    ]
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Falha ao ler regras do Telegram (%s): %s", alvo, exc)

    contas.sort(key=lambda r: len(r[0]), reverse=True)
    categorias.sort(key=lambda r: len(r[0]), reverse=True)
    return tuple(contas), tuple(categorias)


class GastoManualExtraido(NamedTuple):
    """Dados brutos extraídos de uma mensagem de texto simples."""

    descricao: str
    valor: Decimal
    conta_ou_cartao: str
    categoria: str | None
    uso_dinheiro: str
    incerto: bool


class MensagemGastoParser:
    """Extrai lançamentos de despesas a partir de texto livre informal do Telegram."""

    # Padrão para valores monetários: "5", "5.00", "12,50", "R$ 45,90"
    _RE_VALOR = re.compile(
        r"(?i)(?:R\$\s*)?(\d{1,6}(?:[,\.]\d{1,2})?)(?!\S)",
    )

    def __init__(
        self,
        conta_padrao: str = "Santader Credito",
        classificador: ClassificadorCategoria | None = None,
        calculadora: CalculadoraFatura | None = None,
        cartoes: dict[str, CartaoConfig] | None = None,
        regras_contas: tuple[tuple[str, str], ...] | None = None,
        regras_categorias: tuple[tuple[str, str], ...] | None = None,
    ) -> None:
        self.conta_padrao = conta_padrao
        self._classificador = classificador or ClassificadorCategoria()
        self._calc = calculadora or CalculadoraFatura()
        self._cartoes = cartoes or {}

        if regras_contas is None or regras_categorias is None:
            c_padrao, cat_padrao = carregar_regras_telegram()
            self._regras_contas = regras_contas if regras_contas is not None else c_padrao
            self._regras_categorias = (
                regras_categorias if regras_categorias is not None else cat_padrao
            )
        else:
            self._regras_contas = tuple(
                sorted(regras_contas, key=lambda r: len(r[0]), reverse=True)
            )
            self._regras_categorias = tuple(
                sorted(regras_categorias, key=lambda r: len(r[0]), reverse=True)
            )

    def _identificar_conta(self, texto: str) -> tuple[str, str]:
        """Identifica se uma conta/cartão foi mencionada no texto e a remove da descrição."""
        texto_norm = normalizar_texto_chave(texto)
        for termo, nome_oficial in self._regras_contas:
            padrao = rf"\b{re.escape(termo)}\b"
            if re.search(padrao, texto_norm):
                texto_limpo = re.sub(padrao, "", texto, flags=re.IGNORECASE).strip()
                texto_limpo = re.sub(r"\s+", " ", texto_limpo).strip(" -:,")
                return nome_oficial, texto_limpo

        return self.conta_padrao, texto.strip()

    def _identificar_categoria(self, texto: str) -> tuple[str | None, str]:
        """Extrai a categoria mencionada explicitamente no texto ou infere via regras do JSON.

        Retorna `(categoria_oficial_ou_none, texto_sem_categoria)`.
        """
        texto_norm = normalizar_texto_chave(texto)

        for termo, nome_cat in self._regras_categorias:
            termo_norm = normalizar_texto_chave(termo)
            padrao = rf"\b{re.escape(termo_norm)}\b"
            if re.search(padrao, texto_norm):
                # Se o texto for exatamente a palavra da categoria (ex: "cafe" ou "uber"), mantém a descrição
                if texto_norm == termo_norm:
                    return nome_cat, texto.strip()
                # Caso contrário, remove a categoria da descrição para deixá-la limpa
                texto_limpo = re.sub(padrao, "", texto, flags=re.IGNORECASE).strip()
                texto_limpo = re.sub(r"\s+", " ", texto_limpo).strip(" -:,")
                return nome_cat, texto_limpo or texto.strip()

        # Fallback para o classificador baseado em regras de JSON
        cat_classif, incerto = self._classificador.classificar_descricao(texto)
        if not incerto and cat_classif != "Diversos":
            return cat_classif, texto.strip()

        # Não foi informada categoria válida e o classificador ficou incerto
        return None, texto.strip()

    def parse_mensagem(self, texto: str) -> GastoManualExtraido | None:
        """Tenta extrair um gasto a partir de uma mensagem de texto simples.

        Exemplos suportados:
        - "5 bala lanche"
        - "15 cafezinho lanche"
        - "uber 22,50 transporte inter"
        - "49,90 farmacia saude debito"
        """
        linha = texto.strip()
        if not linha or linha.startswith("/"):
            return None

        # 1. Localizar o valor monetário
        match_valor = self._RE_VALOR.search(linha)
        if not match_valor:
            return None

        valor_str = match_valor.group(1).replace(",", ".")
        try:
            valor = para_decimal(Decimal(valor_str))
            if valor is None or valor <= Decimal("0.00"):
                return None
        except Exception:
            return None

        # 2. Separar a descrição sem o número do valor
        inicio, fim = match_valor.span()
        texto_sem_valor = (linha[:inicio] + " " + linha[fim:]).strip()
        texto_sem_valor = re.sub(r"\s+", " ", texto_sem_valor).strip(" -:,")

        if not texto_sem_valor:
            return None

        # 3. Identificar conta ou cartão mencionado
        conta, texto_sem_conta = self._identificar_conta(texto_sem_valor)

        # 4. Identificar e extrair categoria
        categoria, descricao_final = self._identificar_categoria(texto_sem_conta)

        incerto = categoria is None
        uso = UsoDinheiro.por_incerteza(incerto)

        return GastoManualExtraido(
            descricao=descricao_final.strip() or "Despesa avulsa",
            valor=valor,
            conta_ou_cartao=conta,
            categoria=categoria,
            uso_dinheiro=uso,
            incerto=incerto,
        )

    def criar_lancamento(
        self,
        gasto: GastoManualExtraido,
        data_referencia: date | None = None,
    ) -> LancamentoTransacao:
        """Converte o gasto extraído em uma entidade LancamentoTransacao pronta para o Notion."""
        if not gasto.categoria:
            raise ValueError("Categoria obrigatória não informada.")

        hoje = data_referencia or date.today()
        cartao = self._cartoes.get(gasto.conta_ou_cartao)
        if cartao and cartao.ativo:
            ano_c, mes_c = self._calc.competencia_fatura(cartao, hoje)
            mes_comp = formatar_mes_competencia(ano_c, mes_c)
        else:
            mes_comp = formatar_mes_competencia(hoje.year, hoje.month)

        return LancamentoTransacao(
            descricao=gasto.descricao,
            valor=gasto.valor,
            data_compra=hoje,
            mes_competencia=mes_comp,
            tipo=TipoTransacao.SAIDA,
            categoria=gasto.categoria,
            conta_ou_cartao=gasto.conta_ou_cartao,
            recorrencia=Recorrencia.NAO_RECORRENTE,
            status=StatusTransacao.REALIZADO,
            uso_dinheiro=gasto.uso_dinheiro,
        )
