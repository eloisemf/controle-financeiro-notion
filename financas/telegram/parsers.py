"""Parser inteligente para mensagens manuais de gastos e comandos no Telegram."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
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

    # Aliases rápidos para mapear termos informais para os nomes exatos no Notion
    _ALIASES_CONTAS: tuple[tuple[tuple[str, ...], str], ...] = (
        (("santander conta", "santader conta", "debito santander", "pix santander"), "Santader Conta"),
        (("inter conta", "debito inter", "pix inter"), "Inter Conta"),
        (("bradesco conta", "debito bradesco", "pix bradesco"), "Bradesco Conta"),
        (("santander", "santader", "santander credito", "santader credito"), "Santader Credito"),
        (("inter", "inter credito"), "Inter Credito"),
        (("bradesco", "bradesco credito"), "Bradesco Credito"),
        (("amazon", "bradescard"), "Amazon bradescard"),
        (("debito", "pix", "conta"), "Santader Conta"),
    )

    # Mapeamento para extração ou exigência de categoria obrigatória
    _ALIASES_CATEGORIAS: tuple[tuple[tuple[str, ...], str], ...] = (
        (("lanche", "lanches", "cafe", "café", "cafezinho", "padaria", "mcdonalds", "burger king", "snack"), "Lanche"),
        (("alimentacao", "alimentação", "mercado", "supermercado", "hortifruti", "feira", "almoco", "almoço", "jantar", "restaurante"), "Alimentação"),
        (("transporte privado", "transporte", "uber", "99", "taxi", "combustivel", "gasolina"), "Transporte Privado"),
        (("transporte publico", "transporte público", "metro", "metrô", "onibus", "ônibus", "passagem"), "Transporte Público"),
        (("saude", "saúde", "farmacia", "drogaria", "remedio", "remédio", "consulta", "medico"), "Saúde"),
        (("plano de saude", "plano de saúde", "convenio"), "Plano de saúde"),
        (("beleza", "salao", "salão", "cabeleireiro", "manicure", "corte", "estetica"), "Beleza"),
        (("roupa", "roupas", "vestuario", "sapato", "tenis", "tênis", "calcado"), "Roupa"),
        (("lazer", "cinema", "show", "passeio", "parque", "diversao", "diversão"), "Lazer"),
        (("livro", "livros", "leitura", "kindle"), "Livro"),
        (("cursos", "curso", "educacao", "educação", "faculdade"), "Cursos"),
        (("casa", "moveis", "móveis", "decoracao", "decoração", "utensilio"), "Casa"),
        (("utilidade", "utilidades", "ferramenta"), "Utilidade"),
        (("academia", "gym", "treino", "suplemento"), "Academia"),
        (("viagem", "hotel", "hospedagem", "voo"), "Viagem"),
        (("eletronicos", "eletrônicos", "celular", "gadget"), "Eletronicos"),
        (("streamings", "streaming", "netflix", "spotify", "hbo", "prime"), "Streamings"),
        (("ajuda familiar", "ajuda mae", "ajuda mãe", "mesada"), "Ajuda Familiar"),
    )

    def __init__(
        self,
        conta_padrao: str = "Santader Credito",
        classificador: ClassificadorCategoria | None = None,
        calculadora: CalculadoraFatura | None = None,
        cartoes: dict[str, CartaoConfig] | None = None,
    ) -> None:
        self.conta_padrao = conta_padrao
        self._classificador = classificador or ClassificadorCategoria()
        self._calc = calculadora or CalculadoraFatura()
        self._cartoes = cartoes or {}

    def _identificar_conta(self, texto: str) -> tuple[str, str]:
        """Identifica se uma conta/cartão foi mencionada no texto e a remove da descrição."""
        texto_norm = normalizar_texto_chave(texto)
        for termos, nome_oficial in self._ALIASES_CONTAS:
            for termo in sorted(termos, key=len, reverse=True):
                padrao = rf"\b{re.escape(termo)}\b"
                if re.search(padrao, texto_norm):
                    texto_limpo = re.sub(padrao, "", texto, flags=re.IGNORECASE).strip()
                    texto_limpo = re.sub(r"\s+", " ", texto_limpo).strip(" -:,")
                    return nome_oficial, texto_limpo

        return self.conta_padrao, texto.strip()

    def _identificar_categoria(self, texto: str) -> tuple[str | None, str]:
        """Extrai a categoria mencionada explicitamente no texto ou infere via palavras-chave.

        Retorna `(categoria_oficial_ou_none, texto_sem_categoria)`.
        """
        texto_norm = normalizar_texto_chave(texto)

        for termos, nome_cat in self._ALIASES_CATEGORIAS:
            for termo in sorted(termos, key=len, reverse=True):
                padrao = rf"\b{re.escape(termo)}\b"
                if re.search(padrao, texto_norm):
                    # Se o texto for exatamente a palavra da categoria (ex: "cafe" ou "uber"), mantém a descrição
                    if normalizar_texto_chave(texto) == termo:
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
        - "49,90 farmacia santander conta"
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
