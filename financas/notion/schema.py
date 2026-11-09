"""Constantes de nomes de propriedades e colunas do schema no Notion."""

from __future__ import annotations


class ColunasTransacoes:
    NOME = "Name"
    NOME_FANTASIA = "Nome Fantasia"
    VALOR = "Valor"
    VALOR_A_RECEBER = "Valor a receber"
    CRIADO_EM = "Criado em"
    TIPO_TRANSACAO = "Tipo Transação"
    RECORRENCIA = "Recorrência"
    STATUS = "Status"
    MES = "Mês"
    CATEGORIA = "Categoria"
    SAIDA_DE = "Saida de"
    ENTRADA_EM = "Entrada em"
    TIPO_SAIDA = "Tipo Saida"
    GASTO_FIXO = "Gasto Fixo"
    PESSOA = "Pessoa"


class ColunasMeses:
    NOME = "Name"
    MES_DATA = "MesData"
    AUTO_RELACAO = "this (Não alterar)"
    TRANSACOES = "Transactions"
    HISTORICO_SALDOS = "Histórico de Saldos"
    PATRIMONIO_INVESTIDO = "Patrimônio Investido"
    PATRIMONIO_ANTERIOR = "Patrimônio Mês Anterior"
    APORTES_MES = "Aportes no Mês"
    RENDIMENTO_MES = "Rendimento do Mês"


class ColunasGastosFixos:
    NOME = "Nome"
    NOME_NA_FATURA = "Nome na fatura"
    VALOR_MEU = "Valor meu"
    VALOR_COBRADO = "Valor cobrado"
    VALOR_A_RECEBER = "Valor a receber"
    PERIODICIDADE = "Periodicidade"
    CATEGORIA = "Categoria"
    USO_DINHEIRO = "Uso do dinheiro"
    PAGO_COM = "Pago com"
    DIA_COBRANCA = "Dia da cobrança"
    ATIVO = "Ativo"
    INICIO = "Início"
    FIM = "Fim"
    OBSERVACAO = "Observação"
    PESSOA = "Pessoa"
    LANCAMENTOS = "Lançamentos"


class ColunasPessoas:
    NOME = "Nome"
    NOME_PIX_FATURA = "Nome no Pix / Fatura"
    NOME_PIX_FATURA_ALT = "Nome no Pix/Fatura"
    SALDO_INICIAL = "Saldo Inicial"
    OBSERVACAO = "Observação"
    TRANSACOES = "Transações"


class ColunasInvestimentos:
    NOME = "Nome"
    TIPO = "Tipo"
    STATUS = "Status"
    STATUS_NA_CARTEIRA = "Na Carteira"
    VALOR_ATUAL = "Valor Atual"
    VALOR_INVESTIDO = "Valor Investido"
    VALOR_TOTAL = "Valor Total"
    PRECO_COMPRA = "Preço de Compra"
    PRECO_MEDIO_AUTO = "Preço Médio (Auto)"
    QTD = "Qtd"
    FRACAO_TITULOS = "Fração de Títulos"
    APORTES = "Aportes"


class ColunasAportes:
    NOME = "Nome"
    DATA = "Data"
    TIPO = "Tipo"
    INVESTIMENTO = "Investimento"
    QUANTIDADE = "Quantidade"
    PRECO_UNITARIO = "Preço Unitário"
    TAXAS = "Taxas / Custos"
    VALOR_TOTAL = "Valor Total"
    MES_COMPETENCIA = "Mês de Competência"
    OBSERVACAO = "Observação"


class ColunasHistoricoSaldos:
    REGISTRO = "Registro"
    DATA_FOTO = "Data da Foto"
    INVESTIMENTO = "Investimento"
    VALOR_NO_DIA = "Valor no Dia"
    MES_COMPETENCIA = "Mês de Competência"
    QTD_COTAS = "Qtd de Cotas"
    OBSERVACAO = "Observação"
