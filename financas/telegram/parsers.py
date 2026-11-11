"""Parser determinístico para mensagens manuais de gastos no Telegram.

Formatos pré-acordados (sem adivinhação destrutiva no nome do item):
1. Dicionário / Template (Chave: Valor em múltiplas linhas):
   • Item: filtro de café
   • Valor: 20
   • Categoria: Casa
   • Conta/Cartão: Santander Credito (opcional, padrão = Santader Credito)
   • Uso: Nao essencial (opcional, padrão = Nao essencial)
   • Mês Competência: 2026-Novembro (opcional, calculado automaticamente)

2. Atalho Rápido de 1 linha separado por `;` (ou `|`):
   Item ; Valor ; Categoria [; Conta] [; Uso]
   Ex: `filtro de café ; 20 ; casa`
"""

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
    CATEGORIA_EMPRESTIMO,
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


def _limpar_chave_busca(texto: str) -> str:
    """Remove acentos, pontuação/emojis e normaliza espaços em minúsculas."""
    norm = normalizar_texto_chave(texto)
    return re.sub(r"[^a-z0-9\s]", " ", norm).strip()


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
                        (_limpar_chave_busca(k), str(v).strip())
                        for k, v in m_contas.items()
                        if k and v
                    ]
                m_cats = conteudo.get("categorias", {})
                if isinstance(m_cats, dict):
                    categorias = [
                        (_limpar_chave_busca(k), str(v).strip())
                        for k, v in m_cats.items()
                        if k and v
                    ]
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Falha ao ler regras do Telegram (%s): %s", alvo, exc)

    contas.sort(key=lambda r: len(r[0]), reverse=True)
    categorias.sort(key=lambda r: len(r[0]), reverse=True)
    return tuple(contas), tuple(categorias)


class GastoManualExtraido(NamedTuple):
    """Dados estruturados extraídos de uma mensagem do Telegram."""

    descricao: str
    valor: Decimal
    conta_ou_cartao: str
    categoria: str | None
    uso_dinheiro: str
    incerto: bool
    mes_competencia: str | None = None
    categoria_informada: str | None = None


class ReembolsoExtraido(NamedTuple):
    """Dados estruturados extraídos de um comando de reembolso no Telegram."""

    pessoa_informada: str
    valor: Decimal
    conta_destino: str = "Santader Conta"


class MensagemGastoParser:
    """Extrai lançamentos de despesas a partir do template Chave: Valor ou atalho com `;`."""

    _RE_VALOR_EXATO = re.compile(
        r"^\s*(?:R\$\s*)?(\d{1,6}(?:[,\.]\d{1,2})?)\s*$",
        re.IGNORECASE,
    )
    _RE_VALOR_BUSCA = re.compile(
        r"(?i)(?:R\$\s*)?(\d{1,6}(?:[,\.]\d{1,2})?)(?!\S)",
    )

    def __init__(
        self,
        conta_padrao: str = "Santader Credito",
        uso_padrao: str = UsoDinheiro.NAO_ESSENCIAL.value,
        classificador: ClassificadorCategoria | None = None,
        calculadora: CalculadoraFatura | None = None,
        cartoes: dict[str, CartaoConfig] | None = None,
        regras_contas: tuple[tuple[str, str], ...] | None = None,
        regras_categorias: tuple[tuple[str, str], ...] | None = None,
    ) -> None:
        self.conta_padrao = conta_padrao
        self.uso_padrao = uso_padrao
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
                sorted(
                    ((_limpar_chave_busca(k), v) for k, v in regras_contas),
                    key=lambda r: len(r[0]),
                    reverse=True,
                )
            )
            self._regras_categorias = tuple(
                sorted(
                    ((_limpar_chave_busca(k), v) for k, v in regras_categorias),
                    key=lambda r: len(r[0]),
                    reverse=True,
                )
            )

        self._mapa_contas: dict[str, str] = {}
        for termo, oficial in self._regras_contas:
            self._mapa_contas[termo] = oficial
            self._mapa_contas[_limpar_chave_busca(oficial)] = oficial

        self._mapa_categorias: dict[str, str] = {}
        for termo, oficial in self._regras_categorias:
            self._mapa_categorias[termo] = oficial
            self._mapa_categorias[_limpar_chave_busca(oficial)] = oficial

    def categorias_disponiveis(self) -> list[str]:
        """Retorna a lista única e ordenada de categorias oficiais configuradas."""
        return sorted({oficial for _, oficial in self._regras_categorias})

    def contas_disponiveis(self) -> list[str]:
        """Retorna a lista única e ordenada de contas/cartões oficiais configurados."""
        return sorted({oficial for _, oficial in self._regras_contas})

    def _extrair_decimal(self, texto: str) -> Decimal | None:
        """Converte string monetária ('20', '15,50', 'R$ 45.90') em Decimal positivo."""
        limpo = texto.strip()
        if not limpo:
            return None
        m = self._RE_VALOR_EXATO.match(limpo) or self._RE_VALOR_BUSCA.search(limpo)
        if not m:
            return None
        try:
            val = para_decimal(m.group(1))
            if val is not None and val > Decimal("0.00"):
                return val
        except Exception:
            return None
        return None

    def resolver_categoria(self, texto_cat: str | None) -> str | None:
        """Resolve o nome oficial da categoria ignorando acentos e maiúsculas/minúsculas."""
        if not texto_cat or not texto_cat.strip():
            return None
        chave = _limpar_chave_busca(texto_cat)
        if not chave:
            return None
        if chave in self._mapa_categorias:
            return self._mapa_categorias[chave]

        # Busca por palavra inteira dentro das regras (ex: "categoria casa" -> "Casa")
        for termo, oficial in self._regras_categorias:
            if re.search(rf"\b{re.escape(termo)}\b", chave):
                return oficial
        return None

    def resolver_conta(self, texto_conta: str | None) -> str:
        """Resolve o nome oficial da conta/cartão ignorando acentos (padrão = conta_padrao)."""
        if not texto_conta or not texto_conta.strip():
            return self.conta_padrao
        chave = _limpar_chave_busca(texto_conta)
        if not chave:
            return self.conta_padrao
        if chave in self._mapa_contas:
            return self._mapa_contas[chave]

        for termo, oficial in self._regras_contas:
            if re.search(rf"\b{re.escape(termo)}\b", chave):
                return oficial
        return self.conta_padrao

    def _eh_uso_explicito(self, texto: str) -> str | None:
        """Verifica se um campo corresponde explicitamente a um tipo de Uso do Dinheiro."""
        chave = _limpar_chave_busca(texto)
        if chave in ("nao essencial", "nao", "livre", "variavel", "supérfluo", "superfluo"):
            return UsoDinheiro.NAO_ESSENCIAL.value
        if chave in ("essencial", "fixo", "obrigatorio"):
            return UsoDinheiro.ESSENCIAL.value
        if chave in ("emprestimo", "reembolso", "terceiro"):
            return UsoDinheiro.EMPRESTIMO.value
        if chave in ("investimento", "aporte", "guardar dinheiro"):
            return UsoDinheiro.INVESTIMENTO.value
        return None

    def resolver_uso(self, texto_uso: str | None) -> str:
        """Resolve o Uso do Dinheiro (padrão estrito = Nao essencial)."""
        if not texto_uso or not texto_uso.strip():
            return self.uso_padrao
        explicito = self._eh_uso_explicito(texto_uso)
        return explicito if explicito is not None else self.uso_padrao

    def _parse_formato_dicionario(self, texto: str) -> GastoManualExtraido | None:
        """Lê mensagens no formato de dicionário (Chave: Valor em linhas)."""
        campos: dict[str, str] = {}
        linhas_validas = 0

        for linha in texto.splitlines():
            limpa = re.sub(r"^[\s•\-\*▪️▸👉]+", "", linha).strip()
            if not limpa or ":" not in limpa:
                continue
            chave_bruta, valor_bruto = limpa.split(":", maxsplit=1)
            chave_norm = _limpar_chave_busca(chave_bruta)
            if not chave_norm:
                continue
            campos[chave_norm] = valor_bruto.strip()
            linhas_validas += 1

        if linhas_validas == 0:
            return None

        descricao = (
            campos.get("item")
            or campos.get("descricao")
            or campos.get("nome")
            or campos.get("produto")
            or ""
        ).strip()

        valor_str = (
            campos.get("valor")
            or campos.get("preco")
            or campos.get("total")
            or ""
        ).strip()
        valor = self._extrair_decimal(valor_str)

        if not descricao or valor is None:
            return None

        cat_bruta = (
            campos.get("categoria")
            or campos.get("cat")
            or ""
        ).strip()
        # Remove sufixo entre parênteses caso a pessoa tenha copiado "Casa (Nao essencial)"
        cat_sem_parenteses = re.sub(r"\(.*?\)", "", cat_bruta).strip()
        categoria = self.resolver_categoria(cat_sem_parenteses)

        conta_bruta = (
            campos.get("conta cartao")
            or campos.get("conta")
            or campos.get("cartao")
            or campos.get("pagamento")
            or ""
        ).strip()
        conta = self.resolver_conta(conta_bruta)

        uso_bruto = (
            campos.get("uso")
            or campos.get("uso do dinheiro")
            or campos.get("tipo")
            or ""
        ).strip()
        uso = self.resolver_uso(uso_bruto)

        mes_bruto = (
            campos.get("mes competencia")
            or campos.get("mes")
            or campos.get("competencia")
            or ""
        ).strip()
        mes_comp = mes_bruto if mes_bruto else None

        return GastoManualExtraido(
            descricao=descricao,
            valor=valor,
            conta_ou_cartao=conta,
            categoria=categoria,
            uso_dinheiro=uso,
            incerto=categoria is None,
            mes_competencia=mes_comp,
            categoria_informada=cat_sem_parenteses or None,
        )

    def _parse_formato_separador(self, texto: str) -> GastoManualExtraido | None:
        """Lê mensagens de 1 linha separadas por `;` ou `|`.

        Formato: `Item ; Valor ; Categoria [; Conta] [; Uso]`
        (também aceita `Valor ; Item ; Categoria`).
        """
        partes = [p.strip() for p in re.split(r"[;|]", texto) if p.strip()]
        if len(partes) < 2:
            return None

        # Detecta em qual das 2 primeiras posições está o valor monetário exato
        val_0 = self._extrair_decimal(partes[0]) if self._RE_VALOR_EXATO.match(partes[0]) else None
        val_1 = self._extrair_decimal(partes[1])

        if val_1 is not None and not self._RE_VALOR_EXATO.match(partes[0]):
            descricao = partes[0].strip()
            valor = val_1
        elif val_0 is not None:
            valor = val_0
            descricao = partes[1].strip()
        else:
            return None

        if not descricao or valor is None:
            return None

        cat_bruta = partes[2].strip() if len(partes) >= 3 else ""
        categoria = self.resolver_categoria(cat_bruta) if cat_bruta else None

        conta = self.conta_padrao
        uso = self.uso_padrao

        for extra in partes[3:]:
            uso_teste = self._eh_uso_explicito(extra)
            if uso_teste is not None:
                uso = uso_teste
            else:
                conta = self.resolver_conta(extra)

        return GastoManualExtraido(
            descricao=descricao,
            valor=valor,
            conta_ou_cartao=conta,
            categoria=categoria,
            uso_dinheiro=uso,
            incerto=categoria is None,
            mes_competencia=None,
            categoria_informada=cat_bruta or None,
        )

    def parse_mensagem(self, texto: str) -> GastoManualExtraido | None:
        """Extrai um gasto a partir dos formatos estruturados pré-acordados.

        1. Formato Dicionário (`Item: ... / Valor: ... / Categoria: ...`)
        2. Formato Separador (`Item ; Valor ; Categoria [; Conta] [; Uso]`)
        """
        limpo = texto.strip()
        if not limpo or limpo.startswith("/"):
            return None

        if ":" in limpo and ("\n" in limpo or limpo.lower().startswith(("item:", "• item:"))):
            resultado_dict = self._parse_formato_dicionario(limpo)
            if resultado_dict is not None:
                return resultado_dict

        if ";" in limpo or "|" in limpo:
            return self._parse_formato_separador(limpo)

        return None

    def criar_lancamento(
        self,
        gasto: GastoManualExtraido,
        data_referencia: date | None = None,
    ) -> LancamentoTransacao:
        """Converte o gasto extraído em uma entidade LancamentoTransacao pronta para o Notion."""
        if not gasto.categoria:
            raise ValueError("Categoria obrigatória não informada ou não reconhecida.")

        hoje = data_referencia or date.today()
        if gasto.mes_competencia:
            mes_comp = gasto.mes_competencia
        else:
            cartao = self._cartoes.get(gasto.conta_ou_cartao)
            if cartao and cartao.ativo:
                ano_c, mes_c = self._calc.competencia_fatura(cartao, hoje)
                mes_comp = formatar_mes_competencia(ano_c, mes_c)
            else:
                mes_comp = formatar_mes_competencia(hoje.year, hoje.month)

        return LancamentoTransacao(
            descricao="",
            nome_fantasia=gasto.descricao,
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

    def parse_reembolso(
        self,
        texto: str,
        conta_padrao_reembolso: str = "Santader Conta",
    ) -> ReembolsoExtraido | None:
        """Extrai pessoa, valor e conta de destino a partir do comando `/reembolso`.

        Aceita:
        • `/reembolso michele ; 145,87`
        • `/reembolso michele ; 145,87 ; inter`
        • `/reembolso michele 145,87`
        """
        limpo = re.sub(r"^/(?:reembolso|recebi|pagou)\b", "", texto.strip(), flags=re.IGNORECASE).strip()
        if not limpo:
            return None

        conta_destino = conta_padrao_reembolso

        if ";" in limpo or "|" in limpo:
            partes = [p.strip() for p in re.split(r"[;|]", limpo) if p.strip()]
            if len(partes) < 2:
                return None

            val_0 = self._extrair_decimal(partes[0]) if self._RE_VALOR_EXATO.match(partes[0]) else None
            val_1 = self._extrair_decimal(partes[1])

            if val_1 is not None and not self._RE_VALOR_EXATO.match(partes[0]):
                pessoa_str = partes[0].strip()
                valor = val_1
            elif val_0 is not None:
                valor = val_0
                pessoa_str = partes[1].strip()
            else:
                return None

            if len(partes) >= 3:
                conta_resolvida = self.resolver_conta(partes[2])
                if conta_resolvida.endswith("Credito"):
                    conta_destino = conta_resolvida.replace("Credito", "Conta")
                else:
                    conta_destino = conta_resolvida
        else:
            m = self._RE_VALOR_BUSCA.search(limpo)
            if not m:
                return None
            valor = self._extrair_decimal(m.group(0))
            if valor is None:
                return None
            pessoa_str = (limpo[: m.start()] + " " + limpo[m.end() :]).strip()

        if not pessoa_str or valor is None:
            return None

        return ReembolsoExtraido(
            pessoa_informada=pessoa_str,
            valor=valor,
            conta_destino=conta_destino,
        )

    def criar_lancamento_reembolso(
        self,
        reembolso: ReembolsoExtraido,
        nome_oficial_pessoa: str,
        data_referencia: date | None = None,
    ) -> LancamentoTransacao:
        """Cria um `LancamentoTransacao` com `TipoTransacao.REEMBOLSO` para o mês atual."""
        hoje = data_referencia or date.today()
        mes_comp = formatar_mes_competencia(hoje.year, hoje.month)
        return LancamentoTransacao(
            descricao=f"Pix Reembolso {nome_oficial_pessoa}",
            valor=reembolso.valor,
            data_compra=hoje,
            mes_competencia=mes_comp,
            tipo=TipoTransacao.REEMBOLSO,
            categoria=CATEGORIA_EMPRESTIMO,
            conta_ou_cartao=reembolso.conta_destino,
            recorrencia=Recorrencia.NAO_RECORRENTE,
            status=StatusTransacao.REALIZADO,
            pessoa=nome_oficial_pessoa,
        )
