"""Repositório de lançamentos e idempotência da tabela `Transações` no Notion."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import logging
from typing import Any

from financas.domain.models import (
    ChaveIdempotencia,
    ChaveParcialTransacao,
    LancamentoTransacao,
    StatusTransacao,
    TipoTransacao,
    VinculoGastoFixo,
    decimal_para_centavos,
    normalizar_texto_chave,
)
from financas.notion.builders import (
    NotionParser,
    NotionPropertyBuilder as Prop,
    NotionSchemaBuilder as Schema,
)
from financas.notion.client import NotionClient
from financas.notion.repositories.base import BaseNotionRepository
from financas.notion.repositories.catalogo import CatalogoRepository
from financas.notion.repositories.meses import MesesRepository
from financas.notion.repositories.pessoas import PessoasRepository
from financas.notion.schema import ColunasTransacoes

logger = logging.getLogger(__name__)


class TransacoesRepository(BaseNotionRepository):
    """Gerencia o schema, a idempotência e os lançamentos da tabela `Transações`."""

    def __init__(
        self,
        client: NotionClient,
        database_id: str,
        catalogo: CatalogoRepository,
        meses_repo: MesesRepository,
        pessoas_repo: PessoasRepository | None = None,
    ) -> None:
        super().__init__(client)
        self._database_id = database_id
        self._catalogo = catalogo
        self._meses = meses_repo
        self._pessoas = pessoas_repo

    def garantir_colunas_conciliacao(
        self, gastos_fixos_db_id: str, pessoas_db_id: str | None = None
    ) -> list[str]:
        """Garante que `Status`, `Gasto Fixo`, `Pessoa`, `Valor a receber` e a opção `Reembolso` existam em `Transações`."""
        schema = self._client.get_database(self._database_id)
        existentes = schema.get("properties", {})
        novas: dict[str, Any] = {}
        alteradas: list[str] = []

        if ColunasTransacoes.STATUS not in existentes:
            novas[ColunasTransacoes.STATUS] = Schema.select([("Previsto", "yellow"), ("Realizado", "green")])
            alteradas.append(ColunasTransacoes.STATUS)
        if ColunasTransacoes.GASTO_FIXO not in existentes:
            novas[ColunasTransacoes.GASTO_FIXO] = Schema.relation_dual(gastos_fixos_db_id)
            alteradas.append(ColunasTransacoes.GASTO_FIXO)
        if ColunasTransacoes.VALOR_A_RECEBER not in existentes:
            novas[ColunasTransacoes.VALOR_A_RECEBER] = Schema.number("real")
            alteradas.append(ColunasTransacoes.VALOR_A_RECEBER)
        if pessoas_db_id and ColunasTransacoes.PESSOA not in existentes:
            novas[ColunasTransacoes.PESSOA] = Schema.relation_dual(pessoas_db_id)
            alteradas.append(ColunasTransacoes.PESSOA)

        prop_tipo = existentes.get(ColunasTransacoes.TIPO_TRANSACAO, {})
        if prop_tipo.get("type") == "select":
            opcoes_atuais = prop_tipo.get("select", {}).get("options", [])
            nomes_opcoes = {o.get("name") for o in opcoes_atuais}
            if TipoTransacao.REEMBOLSO.value not in nomes_opcoes:
                opcoes_preservadas = [
                    {"name": o["name"], "color": o.get("color", "default")}
                    for o in opcoes_atuais
                    if o.get("name")
                ]
                opcoes_preservadas.append(
                    {"name": TipoTransacao.REEMBOLSO.value, "color": "purple"}
                )
                novas[ColunasTransacoes.TIPO_TRANSACAO] = {"select": {"options": opcoes_preservadas}}
                alteradas.append(f"{ColunasTransacoes.TIPO_TRANSACAO} (Reembolso)")

        if novas:
            self._client.update_database(self._database_id, {"properties": novas})
        return alteradas

    def _extrair_chave_parcial(self, pagina: dict[str, Any]) -> ChaveParcialTransacao:
        desc = normalizar_texto_chave(NotionParser.page_title(pagina))
        val = NotionParser.prop_decimal(pagina, ColunasTransacoes.VALOR)
        centavos = decimal_para_centavos(val)
        contas_ids = (
            NotionParser.prop_relation_ids(pagina, ColunasTransacoes.SAIDA_DE)
            or NotionParser.prop_relation_ids(pagina, ColunasTransacoes.ENTRADA_EM)
        )
        nome_conta = (
            self._catalogo.nome_conta_por_id(contas_ids[0]) if contas_ids else ""
        )
        return ChaveParcialTransacao(desc, centavos, normalizar_texto_chave(nome_conta))

    def chaves_existentes_no_mes(
        self, mes_competencia: str
    ) -> dict[ChaveIdempotencia, str]:
        """Retorna o mapa `chave_idempotencia -> page_id` já gravado para um mês."""
        chave_mes = mes_competencia.strip()
        if chave_mes not in self._meses.meses_por_nome:
            return {}
        mes_id = self._meses.id_mes(chave_mes)
        paginas = self._client.query_database(
            self._database_id,
            filter_payload={"property": ColunasTransacoes.MES, "relation": {"contains": mes_id}},
        )
        chaves: dict[ChaveIdempotencia, str] = {}
        for p in paginas:
            desc, centavos, conta_norm = self._extrair_chave_parcial(p)
            data_iso = NotionParser.prop_date_start(p, ColunasTransacoes.CRIADO_EM)
            chave = ChaveIdempotencia(desc, centavos, chave_mes, conta_norm, data_iso)
            chaves[chave] = NotionParser.normalize_id(p["id"])
        return chaves

    def vincular_gasto_fixo_existente(
        self, transacao_page_id: str, item: LancamentoTransacao
    ) -> None:
        """Atualiza uma transação já existente que passou a casar com um Gasto Fixo."""
        props: dict[str, Any] = {
            ColunasTransacoes.NOME: Prop.title(item.descricao),
            ColunasTransacoes.RECORRENCIA: Prop.select(item.recorrencia.value),
            ColunasTransacoes.STATUS: Prop.select(item.status.value),
        }
        if item.gasto_fixo_id:
            props[ColunasTransacoes.GASTO_FIXO] = Prop.relation(item.gasto_fixo_id)
        if item.categoria:
            props[ColunasTransacoes.CATEGORIA] = Prop.relation(self._catalogo.id_categoria(item.categoria))
        if item.uso_dinheiro:
            props[ColunasTransacoes.TIPO_SAIDA] = Prop.relation(self._catalogo.id_uso_dinheiro(item.uso_dinheiro))
        if item.valor_a_receber is not None:
            props[ColunasTransacoes.VALOR_A_RECEBER] = Prop.number_decimal(item.valor_a_receber)
        if item.pessoa and self._pessoas is not None:
            props[ColunasTransacoes.PESSOA] = Prop.relation(self._pessoas.id_pessoa(item.pessoa))
        self._client.update_page(page_id=transacao_page_id, properties=props)

    def buscar_orfas_sem_mes(self) -> dict[ChaveParcialTransacao, str]:
        """Retorna mapa `(desc_norm, centavos, conta_norm) -> page_id` de transações cujo `Mês` ficou vazio."""
        paginas = self._client.query_database(
            self._database_id,
            filter_payload={"property": ColunasTransacoes.MES, "relation": {"is_empty": True}},
        )
        return {
            self._extrair_chave_parcial(p): NotionParser.normalize_id(p["id"])
            for p in paginas
        }

    def buscar_parceladas_por_chave_parcial(self) -> dict[ChaveParcialTransacao, str]:
        """Retorna mapa `(desc_norm, centavos, conta_norm) -> page_id` para compras parceladas (`- parc XX/YY`)."""
        paginas = self._client.query_database(
            self._database_id,
            filter_payload={"property": ColunasTransacoes.NOME, "title": {"contains": "- Parc "}},
        )
        return {
            self._extrair_chave_parcial(p): NotionParser.normalize_id(p["id"])
            for p in paginas
        }

    def vincular_mes(self, page_id: str, mes_competencia: str) -> None:
        """Restaura o vínculo `Mês` de uma transação órfã."""
        mes_id = self._meses.id_mes(mes_competencia)
        self._client.update_page(page_id=page_id, properties={ColunasTransacoes.MES: Prop.relation(mes_id)})

    def buscar_vinculos_gasto_fixo_no_mes(
        self, mes_competencia: str
    ) -> dict[str, VinculoGastoFixo]:
        """Retorna mapa `gasto_fixo_page_id -> VinculoGastoFixo(transacao_page_id, status, data_iso)` no mês."""
        chave_mes = mes_competencia.strip()
        if chave_mes not in self._meses.meses_por_nome:
            return {}
        mes_id = self._meses.id_mes(chave_mes)
        paginas = self._client.query_database(
            self._database_id,
            filter_payload={
                "and": [
                    {"property": ColunasTransacoes.MES, "relation": {"contains": mes_id}},
                    {"property": ColunasTransacoes.GASTO_FIXO, "relation": {"is_not_empty": True}},
                ]
            },
        )
        vinculos: dict[str, VinculoGastoFixo] = {}
        for p in paginas:
            gf_ids = NotionParser.prop_relation_ids(p, ColunasTransacoes.GASTO_FIXO)
            if gf_ids:
                status = NotionParser.prop_select(p, ColunasTransacoes.STATUS) or ""
                data_iso = NotionParser.prop_date_start(p, ColunasTransacoes.CRIADO_EM)
                vinculos[gf_ids[0]] = VinculoGastoFixo(
                    NotionParser.normalize_id(p["id"]), status, data_iso
                )
        return vinculos

    def atualizar_data_previsto(self, transacao_page_id: str, nova_data: date) -> None:
        """Atualiza a data prevista (`Criado em`) de um lançamento que ainda está `Previsto`."""
        self._client.update_page(
            page_id=transacao_page_id,
            properties={ColunasTransacoes.CRIADO_EM: Prop.date_iso(nova_data)},
        )

    def marcar_como_realizado(self, transacao_page_id: str) -> None:
        """Altera o status de um lançamento de 'Previsto' para 'Realizado'."""
        self._client.update_page(
            page_id=transacao_page_id,
            properties={ColunasTransacoes.STATUS: Prop.select(StatusTransacao.REALIZADO.value)},
        )

    def buscar_previstos_para_data(self, data_referencia: date) -> list[dict[str, Any]]:
        """Busca todas as transações com Status='Previsto' agendadas para uma data específica."""
        filtro = {
            "and": [
                {
                    "property": ColunasTransacoes.STATUS,
                    "select": {"equals": StatusTransacao.PREVISTO.value},
                },
                {
                    "property": ColunasTransacoes.CRIADO_EM,
                    "date": {"equals": data_referencia.isoformat()},
                },
            ]
        }
        paginas = self._client.query_database_all(self._database_id, filter_payload=filtro)
        itens: list[dict[str, Any]] = []
        for p in paginas:
            nome = NotionParser.page_title(p)
            val = NotionParser.prop_decimal(p, ColunasTransacoes.VALOR)
            contas = (
                NotionParser.prop_relation_ids(p, ColunasTransacoes.SAIDA_DE)
                or NotionParser.prop_relation_ids(p, ColunasTransacoes.ENTRADA_EM)
            )
            nome_conta = "Conta/Cartão"
            if contas:
                nome_conta = self._catalogo.nome_conta(contas[0])
            itens.append({
                "page_id": NotionParser.normalize_id(p["id"]),
                "descricao": nome,
                "valor": val,
                "conta": nome_conta,
                "data": data_referencia,
            })
        return itens

    def buscar_previsto_por_nome(self, termo: str) -> dict[str, Any] | None:
        """Busca um lançamento pendente com Status='Previsto' que contenha o termo no título."""
        termo_norm = normalizar_texto_chave(termo)
        filtro = {
            "property": ColunasTransacoes.STATUS,
            "select": {"equals": StatusTransacao.PREVISTO.value},
        }
        paginas = self._client.query_database_all(self._database_id, filter_payload=filtro)
        for p in paginas:
            nome = NotionParser.page_title(p)
            if termo_norm in normalizar_texto_chave(nome):
                val = NotionParser.prop_decimal(p, ColunasTransacoes.VALOR)
                return {
                    "page_id": NotionParser.normalize_id(p["id"]),
                    "descricao": nome,
                    "valor": val,
                }
        return None

    def conciliar_previsto_para_realizado(
        self,
        transacao_page_id: str,
        valor_real: Decimal,
        data_real: date,
        valor_a_receber: Decimal | None = None,
        pessoa: str | None = None,
    ) -> None:
        """Atualiza uma transação `Previsto` para `Realizado` com o valor e a data reais da fatura/extrato."""
        props: dict[str, Any] = {
            ColunasTransacoes.STATUS: Prop.select(StatusTransacao.REALIZADO.value),
            ColunasTransacoes.VALOR: Prop.number_decimal(valor_real),
            ColunasTransacoes.CRIADO_EM: Prop.date_iso(data_real),
        }
        if valor_a_receber is not None:
            props[ColunasTransacoes.VALOR_A_RECEBER] = Prop.number_decimal(valor_a_receber)
        if pessoa and self._pessoas is not None:
            props[ColunasTransacoes.PESSOA] = Prop.relation(self._pessoas.id_pessoa(pessoa))
        self._client.update_page(page_id=transacao_page_id, properties=props)

    def atualizar_metadados_terceiro(
        self,
        transacao_page_id: str,
        pessoa: str,
        valor_a_receber: Decimal,
        categoria: str | None = None,
        uso_dinheiro: str | None = None,
        mes_competencia: str | None = None,
    ) -> None:
        """Vincula `Pessoa`, `Valor a receber` e categoria/uso em lançamentos de terceiros já existentes."""
        props: dict[str, Any] = {
            ColunasTransacoes.VALOR_A_RECEBER: Prop.number_decimal(valor_a_receber),
        }
        if self._pessoas is not None:
            props[ColunasTransacoes.PESSOA] = Prop.relation(self._pessoas.id_pessoa(pessoa))
        if categoria:
            props[ColunasTransacoes.CATEGORIA] = Prop.relation(self._catalogo.id_categoria(categoria))
        if uso_dinheiro:
            props[ColunasTransacoes.TIPO_SAIDA] = Prop.relation(self._catalogo.id_uso_dinheiro(uso_dinheiro))
        if mes_competencia:
            props[ColunasTransacoes.MES] = Prop.relation(self._meses.id_mes(mes_competencia))
        self._client.update_page(page_id=transacao_page_id, properties=props)

    def criar_lancamento(self, lancamento: LancamentoTransacao) -> str:
        """Grava um `LancamentoTransacao` na tabela Transações e retorna o page_id."""
        mes_id = self._meses.id_mes(lancamento.mes_competencia)
        conta_id = self._catalogo.id_conta(lancamento.conta_ou_cartao)

        props: dict[str, Any] = {
            ColunasTransacoes.NOME: Prop.title(lancamento.descricao_com_parcela),
            ColunasTransacoes.VALOR: Prop.number_decimal(lancamento.valor),
            ColunasTransacoes.CRIADO_EM: Prop.date_iso(lancamento.data_compra),
            ColunasTransacoes.TIPO_TRANSACAO: Prop.select(lancamento.tipo.value),
            ColunasTransacoes.RECORRENCIA: Prop.select(lancamento.recorrencia.value),
            ColunasTransacoes.STATUS: Prop.select(lancamento.status.value),
            ColunasTransacoes.MES: Prop.relation(mes_id),
        }
        if lancamento.categoria:
            cat_id = self._catalogo.id_categoria(lancamento.categoria)
            props[ColunasTransacoes.CATEGORIA] = Prop.relation(cat_id)
        if (
            lancamento.tipo in (TipoTransacao.ENTRADA, TipoTransacao.REEMBOLSO)
            or lancamento.eh_entrada_na_conta
        ):
            props[ColunasTransacoes.ENTRADA_EM] = Prop.relation(conta_id)
        else:
            props[ColunasTransacoes.SAIDA_DE] = Prop.relation(conta_id)
            if lancamento.conta_destino:
                destino_id = self._catalogo.id_conta(lancamento.conta_destino)
                props[ColunasTransacoes.ENTRADA_EM] = Prop.relation(destino_id)

        if lancamento.gasto_fixo_id:
            props[ColunasTransacoes.GASTO_FIXO] = Prop.relation(lancamento.gasto_fixo_id)
        if lancamento.uso_dinheiro:
            props[ColunasTransacoes.TIPO_SAIDA] = Prop.relation(self._catalogo.id_uso_dinheiro(lancamento.uso_dinheiro))
        if lancamento.valor_a_receber is not None:
            props[ColunasTransacoes.VALOR_A_RECEBER] = Prop.number_decimal(lancamento.valor_a_receber)
        if lancamento.pessoa and self._pessoas is not None:
            props[ColunasTransacoes.PESSOA] = Prop.relation(self._pessoas.id_pessoa(lancamento.pessoa))

        pagina = self._client.create_page(database_id=self._database_id, properties=props)
        novo_id = NotionParser.normalize_id(pagina["id"])
        logger.debug("Criado lançamento '%s' (id=%s) no Notion.", lancamento.descricao_com_parcela, novo_id)
        return novo_id
