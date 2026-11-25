"""Serviço orquestrador do bot do Telegram integrado ao Notion."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
import logging
from pathlib import Path
import tempfile
from typing import Any

from financas.config import AppConfig
from financas.domain.billing import (
    CalculadoraFatura,
    adicionar_meses,
    competencia_ciclo_atual,
    formatar_mes_competencia,
    parse_mes_competencia,
)
from financas.domain.classification import ClassificadorCategoria
from financas.domain.models import (
    LancamentoTransacao,
    Recorrencia,
    StatusTransacao,
    TipoTransacao,
    decimal_para_centavos,
    formatar_brl,
    para_decimal,
)
from financas.notion.client import NotionClient
from financas.notion.repositories import NotionRepositories
from financas.parsers.router import ExtratorDocumentosFinanceiros
from financas.services.ingestion import FaturaIngestionService
from financas.services.investments import InvestimentosService
from financas.services.market_data import MarketDataService
from financas.telegram.client import TelegramClient
from financas.telegram.parsers import MensagemGastoParser
from financas.telegram.reports import TelegramReportBuilder

logger = logging.getLogger(__name__)


class TelegramBotService:
    """Orquestra o recebimento de mensagens, lançamento de gastos e disparo de resumos."""

    def __init__(
        self,
        config: AppConfig,
        client: NotionClient,
        telegram_client: TelegramClient | None = None,
        repos: NotionRepositories | None = None,
        market_data: MarketDataService | None = None,
    ) -> None:
        if not config.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN não configurado no .env.")

        self.config = config
        self._notion = client
        self._owns_telegram = telegram_client is None
        self._telegram = telegram_client or TelegramClient(
            token=config.telegram_bot_token,
            chat_id=config.telegram_chat_id,
        )
        self._repos = repos or NotionRepositories.from_client(client, config)
        self._calc = CalculadoraFatura()
        self._owns_market = market_data is None
        self._market = market_data or MarketDataService()
        self._invest_service = InvestimentosService(
            client=self._notion,
            config=self.config,
            market_data=self._market,
            investimentos_repo=self._repos.investimentos,
            meses_repo=self._repos.meses,
        )
        self._parser = MensagemGastoParser(
            conta_padrao="Santander Credito",
            classificador=ClassificadorCategoria(),
            calculadora=self._calc,
            cartoes=self.config.cartoes,
        )
        self._reporter = TelegramReportBuilder(
            client=self._notion,
            repos=self._repos,
            invest_service=self._invest_service,
        )

    def close(self) -> None:
        """Fecha clientes HTTP criados internamente pelo serviço."""
        if self._owns_telegram and hasattr(self._telegram, "close"):
            self._telegram.close()
        if self._owns_market and hasattr(self._market, "close"):
            self._market.close()

    def __enter__(self) -> TelegramBotService:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def processar_mensagens_pendentes(self, dry_run: bool = False) -> int:
        """Lê mensagens novas do Telegram, lança no Notion e responde ao usuário."""
        updates = self._telegram.obter_atualizacoes(timeout=0)
        if not updates:
            logger.info("Nenhuma mensagem pendente no Telegram.")
            return 0

        processadas = 0
        maior_update_id = 0
        callbacks_vistos: set[str] = set()

        for up in updates:
            up_id = up.get("update_id", 0)
            if up_id > maior_update_id:
                maior_update_id = up_id

            # Tratamento de cliques em botões inline (callback_query)
            cb = up.get("callback_query")
            if cb:
                cb_id = str(cb.get("id", ""))
                cb_data = str(cb.get("data", ""))
                cb_from = str(cb.get("from", {}).get("id", ""))
                if not self.config.telegram_chat_id or cb_from == str(self.config.telegram_chat_id):
                    if cb_data and cb_data in callbacks_vistos:
                        logger.info("Callback duplicado ignorado no mesmo lote: %s", cb_data)
                        self._telegram.responder_callback_query(cb_id)
                        continue
                    if cb_data:
                        callbacks_vistos.add(cb_data)
                    self._tratar_callback(cb_id, cb_data, chat_id=cb_from or str(self.config.telegram_chat_id), dry_run=dry_run)
                    processadas += 1
                continue

            msg = up.get("message", {})
            if not msg:
                continue

            chat_id = str(msg.get("chat", {}).get("id", ""))
            # Segurança: só atende ao chat_id configurado do dono!
            if self.config.telegram_chat_id and chat_id != str(self.config.telegram_chat_id):
                logger.warning("Mensagem ignorada de chat não autorizado: %s", chat_id)
                continue

            texto = msg.get("text", "").strip()

            # 1. Comandos
            if texto.startswith("/"):
                self._tratar_comando(texto, chat_id, dry_run=dry_run)
                processadas += 1
                continue

            # 2. Documentos ou Fotos (Faturas / Extratos / Prints)
            if "photo" in msg or "document" in msg:
                self._tratar_anexo(msg, chat_id, dry_run=dry_run)
                processadas += 1
                continue

            # 3. Gasto Manual em Texto
            if texto:
                self._tratar_gasto_texto(texto, chat_id, dry_run=dry_run)
                processadas += 1

        # Confirma as mensagens processadas avançando o offset
        if maior_update_id > 0 and not dry_run:
            self._telegram.obter_atualizacoes(offset=maior_update_id + 1, timeout=0)

        return processadas

    def _obter_lista_categorias(self) -> list[str]:
        """Retorna a lista ordenada de categorias do Notion (ou fallback do JSON)."""
        try:
            cats_notion = list(self._repos.catalogo.categorias.keys())
            if cats_notion:
                return sorted(cats_notion)
        except Exception as exc:
            logger.debug("Fallback para categorias do parser: %s", exc)
        return self._parser.categorias_disponiveis()

    def _obter_lista_pessoas(self) -> list[str]:
        """Retorna a lista ordenada de pessoas cadastradas na tabela Pessoas do Notion."""
        try:
            if self._repos.pessoas is not None:
                nomes = list(self._repos.pessoas.pessoas_por_nome.keys())
                if nomes:
                    return sorted(nomes)
        except Exception as exc:
            logger.debug("Falha ao listar pessoas do Notion: %s", exc)
        return []

    def _tratar_comando(self, comando: str, chat_id: str, dry_run: bool = False) -> None:
        partes_cmd = comando.split()
        cmd = partes_cmd[0].lower()
        if cmd in ("/start", "/ajuda", "/help"):
            msg_ajuda = (
                "👋 *Olá! Sou seu assistente do Controle Financeiro.*\n\n"
                "Para lançar sem erros, usamos *2 formatos combinados* (não precisa colocar acento!):\n\n"
                "1️⃣ *Atalho Rápido (separado por `;`):*\n"
                "`Item ; Valor ; Categoria`\n"
                "• `filtro de cafe ; 20 ; casa`\n"
                "• `almoco ; 35,90 ; alimentacao ; debito`\n"
                "• `remedio ; 45 ; saude ; inter ; essencial`\n"
                "• `tenis ; 549,99 ; vestuario ; 5x` *(divide em 5 parcelas)*\n\n"
                "2️⃣ *Modelo Copiar & Colar (digite /modelo):*\n"
                "```text\n"
                "Item: filtro de café\n"
                "Valor: 20\n"
                "Categoria: Casa\n"
                "Conta: Santander Credito\n"
                "```\n"
                "ℹ️ *Padrões automáticos:* Conta = `Santander Credito` | Uso = `Nao essencial`.\n\n"
                "💜 *Reembolso de Amigos/Pessoas (Pix recebido):*\n"
                "• `/reembolso amigo ; 150,00`\n"
                "• `/reembolso mae ; 200`\n\n"
                "📸 Você também pode mandar *prints de faturas ou PDFs*!\n\n"
                "📌 *Comandos disponíveis:*\n"
                "/salario [valor] — Registra entrada do salário na conta\n"
                "/pagar_fatura [cartao] ; [valor] — Registra pagamento da fatura\n"
                "/investir [ativo] ; [valor] — Registra aporte saindo da conta\n"
                "/reembolso [nome] ; [valor] — Registra Pix recebido de terceiro\n"
                "/categorias — Lista todas as categorias e contas aceitas\n"
                "/modelo — Envia o molde pronto para copiar e colar\n"
                "/resumo — Relatório de gastos, saldo livre e faturas\n"
                "/analise — Top categorias, essencial vs não essencial e poupança\n"
                "/parcelas — Compras parceladas ativas e total restante\n"
                "/investimentos — Atualiza cotações e mostra patrimônio\n"
                "/confirmar [nome] — Confirma gasto fixo previsto como Realizado\n"
                "/ajuda — Exibe esta mensagem"
            )
            self._telegram.enviar_mensagem(msg_ajuda, chat_id=chat_id)

        elif cmd in ("/categorias", "/categoria", "/contas"):
            cats = self._obter_lista_categorias()
            linhas_cat = "\n".join(f"• `{c}`" for c in cats)
            msg_cats = (
                "📂 *CATEGORIAS DISPONÍVEIS NO NOTION*\n"
                "*(Você pode digitar sem acento e em minúsculas!)*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{linhas_cat}\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "💳 *CONTAS / CARTÕES ACEITOS:*\n"
                "• `santander` *(padrão: Santander Credito)*\n"
                "• `debito` ou `pix` *(Santander Conta)*\n"
                "• `inter` *(Inter Credito)* | `inter conta`\n"
                "• `bradesco` *(Bradesco Credito)* | `bradesco conta`\n"
                "• `amazon` *(Amazon bradescard)*\n\n"
                "🎯 *USO DO DINHEIRO:*\n"
                "• Padrão automático: `Nao essencial`\n"
                "• Para forçar essencial, adicione `; essencial` no fim."
            )
            self._telegram.enviar_mensagem(msg_cats, chat_id=chat_id)

        elif cmd in ("/modelo", "/template"):
            msg_modelo = (
                "📋 *MODELO PARA COPIAR E COLAR:*\n\n"
                "`Item: `\n"
                "`Valor: `\n"
                "`Categoria: `\n"
                "`Conta/Cartão: Santander Credito`\n"
                "`Parcelas: ` *(opcional, ex: 5x ou 01/05)*\n\n"
                "⚡ *OU ATALHO DE 1 LINHA (com `;`):*\n"
                "`filtro de cafe ; 20 ; casa`\n"
                "`tenis ; 549,99 ; vestuario ; 5x`"
            )
            self._telegram.enviar_mensagem(msg_modelo, chat_id=chat_id)

        elif cmd in ("/salario", "/receita", "/entrada"):
            self._tratar_salario(comando, chat_id, dry_run=dry_run)

        elif cmd in ("/pagar_fatura", "/pagarfatura", "/pagar"):
            self._tratar_pagar_fatura(comando, chat_id, dry_run=dry_run)

        elif cmd in ("/reembolso", "/recebi", "/pagou", "/pessoas"):
            self._tratar_reembolso(comando, chat_id, dry_run=dry_run)

        elif cmd in ("/investir", "/aporte"):
            self._tratar_aporte(comando, chat_id, dry_run=dry_run)

        elif cmd in ("/confirmar", "/confirmado", "/pago"):
            if len(partes_cmd) < 2:
                self._telegram.enviar_mensagem(
                    "💡 Informe o nome do gasto previsto. Exemplo:\n`/confirmar Aluguel` ou `/confirmar Internet`",
                    chat_id=chat_id,
                )
                return
            termo = comando.split(maxsplit=1)[1].strip()
            self._confirmar_previsto_por_termo(termo, chat_id, dry_run=dry_run)

        elif cmd == "/resumo":
            self._telegram.enviar_mensagem("⏳ Gerando resumo financeiro...", chat_id=chat_id)
            resumo = self._reporter.gerar_resumo_semanal()
            self._telegram.enviar_mensagem(resumo, chat_id=chat_id)

        elif cmd in ("/analise", "/insights"):
            self._telegram.enviar_mensagem("⏳ Analisando categorias, parcelas e poupança do ciclo...", chat_id=chat_id)
            analise = self._reporter.gerar_analise_mensal()
            self._telegram.enviar_mensagem(analise, chat_id=chat_id)

        elif cmd in ("/parcelas", "/parcelamentos"):
            relatorio_parc = self._reporter.gerar_relatorio_parcelas()
            self._telegram.enviar_mensagem(relatorio_parc, chat_id=chat_id)

        elif cmd == "/investimentos":
            self._telegram.enviar_mensagem("⏳ Buscando cotações de mercado (B3 e Tesouro)...", chat_id=chat_id)
            try:
                self._invest_service.atualizar_cotacoes_carteira(dry_run=dry_run)
            except Exception as e:
                logger.warning("Falha ao atualizar cotações: %s", e)
            resumo_inv = self._invest_service.obter_resumo_carteira()
            pat = resumo_inv.patrimonio_total
            lucro = resumo_inv.lucro_total
            sinal = "+" if lucro > 0 else ""
            cdi_linha = ""
            try:
                cdi_mes = self._market.obter_cdi_mes_atual()
                if cdi_mes is not None:
                    cdi_pct = (cdi_mes * Decimal("100")).quantize(Decimal("0.01"))
                    cdi_linha = f"• CDI Acumulado no Mês: *{str(cdi_pct).replace('.', ',')}%*\n"
            except Exception as exc:
                logger.debug("Falha ao obter benchmark CDI: %s", exc)
            txt = (
                "📈 *PAINEL DE INVESTIMENTOS*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• Patrimônio Total: *{formatar_brl(pat)}*\n"
                f"• Rentabilidade: *{sinal}{formatar_brl(lucro)}*\n"
                f"• Total de Ativos: *{resumo_inv.total_ativos} ativos*\n"
                f"{cdi_linha}"
                "━━━━━━━━━━━━━━━━━━━━━━"
            )
            self._telegram.enviar_mensagem(txt, chat_id=chat_id)

        else:
            self._telegram.enviar_mensagem(
                "❓ Comando não reconhecido. Digite /ajuda ou /categorias para ver as opções.",
                chat_id=chat_id,
            )

    def _tratar_callback(self, cb_id: str, data: str, chat_id: str, dry_run: bool = False) -> None:
        """Trata cliques em botões inline (confirmar gasto fixo ou excluir lançamento)."""
        if data.startswith("confirmar:"):
            page_id = data.split(":", maxsplit=1)[1]
            if not dry_run:
                self._repos.transacoes.marcar_como_realizado(page_id)
            self._telegram.responder_callback_query(cb_id, texto="Status alterado para Realizado!")
            self._telegram.enviar_mensagem(
                "✅ *Gasto Fixo Confirmado!*\nO status foi alterado para *Realizado* com sucesso no seu Notion.",
                chat_id=chat_id,
            )
        elif data.startswith("excluir:"):
            page_id = data.split(":", maxsplit=1)[1]
            if not dry_run:
                self._repos.transacoes.excluir_lancamento(page_id)
            self._telegram.responder_callback_query(cb_id, texto="Lançamento excluído do Notion!")
            self._telegram.enviar_mensagem(
                "🗑️ *Lançamento Excluído!*\nA despesa foi movida para a lixeira do Notion com sucesso.",
                chat_id=chat_id,
            )
        elif data.startswith("sal:"):
            partes_sal = data.split(":")
            if len(partes_sal) == 3:
                _, centavos_str, mes_comp = partes_sal
                valor_sal = (Decimal(centavos_str) / Decimal("100")).quantize(Decimal("0.01"))
                self._registrar_salario(
                    valor=valor_sal,
                    conta="Santander Conta",
                    mes_competencia=mes_comp,
                    chat_id=chat_id,
                    dry_run=dry_run,
                )
                self._telegram.responder_callback_query(
                    cb_id, texto=f"Salário {formatar_brl(valor_sal)} registrado!"
                )
            else:
                self._telegram.responder_callback_query(cb_id)
        elif data.startswith("pf:"):
            partes_pf = data.split(":")
            if len(partes_pf) == 4:
                _, cartao, centavos_str, mes_comp = partes_pf
                valor_fatura = (Decimal(centavos_str) / Decimal("100")).quantize(Decimal("0.01"))
                self._registrar_pagamento_fatura(
                    cartao=cartao,
                    valor=valor_fatura,
                    mes_competencia=mes_comp,
                    conta_origem="Santander Conta",
                    chat_id=chat_id,
                    dry_run=dry_run,
                )
                self._telegram.responder_callback_query(
                    cb_id, texto=f"Pagamento de {cartao} registrado!"
                )
            else:
                self._telegram.responder_callback_query(cb_id)
        else:
            self._telegram.responder_callback_query(cb_id)

    def _confirmar_previsto_por_termo(self, termo: str, chat_id: str, dry_run: bool = False) -> None:
        """Localiza um gasto previsto pelo nome digitado e atualiza para Realizado."""
        encontrado = self._repos.transacoes.buscar_previsto_por_nome(termo)
        if not encontrado:
            self._telegram.enviar_mensagem(
                f"⚠️ Não encontrei nenhum gasto pendente com status `Previsto` contendo '*{termo}*'.",
                chat_id=chat_id,
            )
            return

        desc = encontrado["descricao"]
        val_fmt = formatar_brl(encontrado["valor"])
        if not dry_run:
            self._repos.transacoes.marcar_como_realizado(encontrado["page_id"])

        msg = (
            "✅ *Gasto Confirmado com Sucesso!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• *Item:* {desc}\n"
            f"• *Valor:* *{val_fmt}*\n"
            "• *Novo Status:* *Realizado* 🟢\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "O saldo livre e as fórmulas do Notion já foram atualizados!"
        )
        self._telegram.enviar_mensagem(msg, chat_id=chat_id)

    def _tratar_reembolso(self, comando: str, chat_id: str, dry_run: bool = False) -> None:
        """Processa o comando `/reembolso Nome ; Valor [; Conta]` e registra `TipoTransacao.REEMBOLSO`."""
        pessoas_lista = self._obter_lista_pessoas()
        bloco_pessoas = (
            "\n".join(f"• `{p}`" for p in pessoas_lista)
            if pessoas_lista
            else "• *(Consulte a tabela Pessoas no Notion)*"
        )

        reembolso = self._parser.parse_reembolso(comando)
        if not reembolso:
            msg_instrucoes = (
                "💜 *COMO REGISTRAR UM REEMBOLSO (PIX RECEBIDO):*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Envie no formato:\n"
                "`/reembolso Nome ; Valor`\n\n"
                "💡 *Exemplos:*\n"
                "• `/reembolso amigo ; 150,00`\n"
                "• `/reembolso mae ; 200`\n"
                "• `/reembolso amigo ; 50 ; inter`\n\n"
                "👥 *Pessoas cadastradas no Notion:*\n"
                f"{bloco_pessoas}\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "ℹ️ *Padrões:* Tipo = `Reembolso` | Entrada em = `Santander Conta` | Mês = Mês atual."
            )
            self._telegram.enviar_mensagem(msg_instrucoes, chat_id=chat_id)
            return

        nome_oficial: str | None = None
        if self._repos.pessoas is not None:
            nome_oficial = self._repos.pessoas.resolver_nome_pessoa(reembolso.pessoa_informada)

        if not nome_oficial:
            msg_erro = (
                f"⚠️ Não encontrei `'{reembolso.pessoa_informada}'` na tabela *Pessoas* do Notion.\n\n"
                "👥 *Pessoas cadastradas:*\n"
                f"{bloco_pessoas}\n\n"
                "💡 *Exemplo:* `/reembolso nome ; valor`"
            )
            self._telegram.enviar_mensagem(msg_erro, chat_id=chat_id)
            return

        hoje = date.today()
        lancamento = self._parser.criar_lancamento_reembolso(
            reembolso=reembolso,
            nome_oficial_pessoa=nome_oficial,
            data_referencia=hoje,
        )

        page_id = "dry-run-id"
        if not dry_run:
            self._repos.meses.garantir_mes(hoje.year, hoje.month)
            page_id = self._repos.transacoes.criar_lancamento(lancamento)

        val_fmt = formatar_brl(lancamento.valor)
        msg_confirmacao = (
            "💜 *Reembolso Registrado no Notion!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• *Descrição:* {lancamento.descricao}\n"
            f"• *Pessoa:* *{nome_oficial}*\n"
            f"• *Valor Recebido:* *{val_fmt}*\n"
            f"• *Tipo Transação:* `Reembolso` 🟣\n"
            f"• *Entrada em:* {lancamento.conta_ou_cartao}\n"
            f"• *Categoria:* {lancamento.categoria}\n"
            f"• *Mês Competência:* {lancamento.mes_competencia}\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ O saldo devedor de *" + nome_oficial + "* já foi abatido!"
        )
        botoes = {
            "inline_keyboard": [
                [{"text": "🗑️ Desfazer / Excluir Lançamento", "callback_data": f"excluir:{page_id}"}]
            ]
        }
        self._telegram.enviar_mensagem(msg_confirmacao, chat_id=chat_id, reply_markup=botoes)

    def _tratar_aporte(self, comando: str, chat_id: str, dry_run: bool = False) -> None:
        """Processa `/investir Ativo ; Valor [; Conta] [; dd/mm]`, debita da conta e registra em Aportes."""
        if not self.config.databases.aportes or not self.config.databases.meus_investimentos:
            self._telegram.enviar_mensagem(
                "⚠️ O módulo de Investimentos não está configurado no `.env`.",
                chat_id=chat_id,
            )
            return

        aporte = self._parser.parse_aporte(comando)
        if not aporte:
            self._telegram.enviar_mensagem(
                "📈 *COMO REGISTRAR UM INVESTIMENTO (SAÍDA DA CONTA + APORTE):*\n\n"
                "Envie no formato:\n"
                "`/investir Ativo ; Valor [; Conta] [; dd/mm]`\n\n"
                "💡 *Exemplos:*\n"
                "• `/investir IPCA+ ; 375,13`\n"
                "• `/investir IPCA+ ; 375,13 ; santander conta ; 08/11`\n"
                "• `/investir BBAS3F ; 180,00 ; inter conta ; 10 cotas`\n\n"
                "ℹ️ *Padrões:* Sai de `Santander Conta` (`Movimentação Interna`) e atualiza `📦 Aportes` + `Meus Investimentos`.",
                chat_id=chat_id,
            )
            return

        ativo = self._invest_service.resolver_ativo(aporte.ativo_informado)
        if not ativo:
            self._telegram.enviar_mensagem(
                f"⚠️ Não encontrei o ativo `'{aporte.ativo_informado}'` em *Meus Investimentos*.\n"
                "Verifique o nome exato do ativo na sua carteira no Notion.",
                chat_id=chat_id,
            )
            return

        lancamento = self._parser.criar_lancamento_aporte(aporte, nome_oficial_ativo=ativo.nome)
        tx_page_id = "dry-run-tx-id"
        if not dry_run:
            ano_a, mes_a = parse_mes_competencia(lancamento.mes_competencia)
            self._repos.meses.garantir_mes(ano_a, mes_a)
            tx_page_id = self._repos.transacoes.criar_lancamento(lancamento)
            self._invest_service.registrar_aporte(
                ativo_id=ativo.id,
                ativo_nome=ativo.nome,
                valor_total=lancamento.valor,
                data_aporte=lancamento.data_compra,
                quantidade=aporte.quantidade,
                dry_run=False,
            )

        val_fmt = formatar_brl(lancamento.valor)
        dt_fmt = lancamento.data_compra.strftime("%d/%m/%Y")
        msg_confirmacao = (
            "📈 *Investimento Registrado no Notion!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• *Ativo:* *{ativo.nome}*\n"
            f"• *Valor Aportado:* *{val_fmt}*\n"
            f"• *Data:* {dt_fmt} ({lancamento.mes_competencia})\n"
            f"• *Saída da Conta:* {lancamento.conta_ou_cartao} (`Movimentação Interna`)\n"
            "• *Atualizado:* `Transações` + `📦 Aportes` + `Meus Investimentos`\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )
        botoes = {
            "inline_keyboard": [
                [{"text": "🗑️ Desfazer Saída da Conta", "callback_data": f"excluir:{tx_page_id}"}]
            ]
        }
        self._telegram.enviar_mensagem(msg_confirmacao, chat_id=chat_id, reply_markup=botoes)

    def _tratar_gasto_texto(self, texto: str, chat_id: str, dry_run: bool = False) -> None:
        gasto = self._parser.parse_mensagem(texto)
        if not gasto:
            self._telegram.enviar_mensagem(
                "⚠️ *Formato não reconhecido.*\n\n"
                "Para evitar erros no nome do item, envie em um dos *2 formatos combinados*:\n\n"
                "1️⃣ *Separado por ponto-e-vírgula (`;`):*\n"
                "• `filtro de cafe ; 20 ; casa`\n"
                "• `almoco ; 35,90 ; alimentacao ; debito`\n\n"
                "2️⃣ *Modelo Dicionário (ou digite /modelo):*\n"
                "`Item: filtro de cafe`\n"
                "`Valor: 20`\n"
                "`Categoria: casa`",
                chat_id=chat_id,
            )
            return

        # Tenta validar também no catálogo do Notion caso seja uma categoria nova criada lá
        if not gasto.categoria and gasto.categoria_informada:
            try:
                cat_id = self._repos.catalogo.id_categoria(gasto.categoria_informada)
                nome_oficial = self._repos.catalogo.nome_categoria_por_id(cat_id)
                if nome_oficial:
                    gasto = gasto._replace(categoria=nome_oficial, incerto=False)
            except KeyError:
                pass

        if not gasto.categoria:
            detalhe = (
                f"Não reconheci a categoria `'{gasto.categoria_informada}'`.\n\n"
                if gasto.categoria_informada
                else "Você esqueceu de informar a categoria!\n\n"
            )
            msg_alerta = (
                "⚠️ *Categoria obrigatória!*\n\n"
                f"{detalhe}"
                "💡 *Exemplo correto:*\n"
                f"• `{gasto.descricao} ; {gasto.valor} ; casa`\n\n"
                "👉 Digite /categorias para ver a lista completa (não precisa usar acentos)."
            )
            self._telegram.enviar_mensagem(msg_alerta, chat_id=chat_id)
            return

        lancamento = self._parser.criar_lancamento(gasto)
        futuras = self._calc.projetar_parcelas_futuras(lancamento) if lancamento.eh_parcelado else []
        bloco_comprometimento = ""
        if lancamento.eh_parcelado and lancamento.total_parcelas:
            try:
                bloco_comprometimento = self._reporter.gerar_comprometimento_futuro(
                    parcela_extra=lancamento.valor,
                    total_parcelas_extra=lancamento.total_parcelas,
                )
            except Exception as exc:
                logger.debug("Falha ao gerar comprometimento futuro: %s", exc)

        page_id = "dry-run-id"
        vinculado_parcelas = False
        if not dry_run:
            ano_l, mes_l = parse_mes_competencia(lancamento.mes_competencia)
            self._repos.meses.garantir_mes(ano_l, mes_l)
            if lancamento.eh_parcelado and self._repos.parcelas is not None:
                parcela_mestre_id = self._repos.parcelas.garantir_parcela(lancamento)
                if parcela_mestre_id:
                    lancamento = replace(lancamento, compra_parcelada_id=parcela_mestre_id)
                    futuras = [replace(f, compra_parcelada_id=parcela_mestre_id) for f in futuras]
                    vinculado_parcelas = True

            page_id = self._repos.transacoes.criar_lancamento(lancamento)
            for parc_futura in futuras:
                ano_f, mes_f = parse_mes_competencia(parc_futura.mes_competencia)
                self._repos.meses.garantir_mes(ano_f, mes_f)
                self._repos.transacoes.criar_lancamento(parc_futura)

        val_fmt = formatar_brl(lancamento.valor)
        nome_exibicao = lancamento.nome_fantasia or lancamento.descricao
        bloco_parcelas = ""
        if lancamento.eh_parcelado and lancamento.parcela_atual and lancamento.total_parcelas:
            extra_fut = f" (+{len(futuras)} futura(s) projetada(s))" if futuras else ""
            extra_base = " • vinculado em 📦 Parcelas" if vinculado_parcelas else ""
            bloco_parcelas = (
                f"• *Parcelamento:* {lancamento.parcela_atual:02d}/{lancamento.total_parcelas:02d}{extra_fut}{extra_base}\n"
            )
        msg_confirmacao = (
            "✅ *Despesa Registrada no Notion!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• *Item (Nome Fantasia):* {nome_exibicao}\n"
            f"• *Valor:* *{val_fmt}*\n"
            f"{bloco_parcelas}"
            f"• *Conta/Cartão:* {lancamento.conta_ou_cartao}\n"
            f"• *Categoria:* {lancamento.categoria} ({lancamento.uso_dinheiro})\n"
            f"• *Mês Competência:* {lancamento.mes_competencia}\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )
        if bloco_comprometimento:
            msg_confirmacao += f"\n\n{bloco_comprometimento}"
        botoes = {
            "inline_keyboard": [
                [{"text": "🗑️ Desfazer / Excluir Lançamento", "callback_data": f"excluir:{page_id}"}]
            ]
        }
        self._telegram.enviar_mensagem(msg_confirmacao, chat_id=chat_id, reply_markup=botoes)

    def _tratar_anexo(self, msg: dict[str, Any], chat_id: str, dry_run: bool = False) -> None:
        self._telegram.enviar_mensagem("📥 Recebi seu arquivo! Processando e conciliando...", chat_id=chat_id)

        file_id = ""
        nome_arquivo = "arquivo_telegram"

        if "photo" in msg:
            # Pega a foto com a maior resolução
            foto = msg["photo"][-1]
            file_id = foto.get("file_id", "")
            nome_arquivo = f"print_{foto.get('file_unique_id', 'img')}.jpg"
        elif "document" in msg:
            doc = msg["document"]
            file_id = doc.get("file_id", "")
            nome_arquivo = doc.get("file_name", "documento.pdf")

        if not file_id:
            self._telegram.enviar_mensagem("❌ Falha ao obter identificador do arquivo.", chat_id=chat_id)
            return

        with tempfile.TemporaryDirectory() as tmp_dir:
            caminho_local = Path(tmp_dir) / nome_arquivo
            try:
                self._telegram.baixar_arquivo(file_id, caminho_local)
                pessoas = self._repos.pessoas.listar_todas()
                extrator = ExtratorDocumentosFinanceiros(
                    gemini_api_key=self.config.gemini_api_key,
                    nome_titular=self.config.nome_titular,
                    pessoas=pessoas,
                )
                lancamentos = extrator.extrair_de_arquivo(caminho_local)
                ingest_servico = FaturaIngestionService(
                    client=self._notion,
                    config=self.config,
                    repos=self._repos,
                    pessoas_cadastradas=pessoas,
                )
                relatorio = ingest_servico.importar_lancamentos(lancamentos, dry_run=dry_run)

                resumo_txt = (
                    "🧾 *Importação Concluída!*\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• Itens extraídos: *{relatorio.extraidos_da_fatura}*\n"
                    f"• Parcelas projetadas: *{relatorio.parcelas_futuras_projetadas}*\n"
                    f"• Lançamentos criados: *{len(relatorio.gravados)}*\n"
                    f"• Gastos fixos conciliados: *{len(relatorio.conciliados_previsto_para_realizado)}*\n"
                    f"• Já existentes (ignorados): *{len(relatorio.ignorados_por_idempotencia)}*\n"
                    "━━━━━━━━━━━━━━━━━━━━━━"
                )
                if relatorio.alertas_variacao_fixos:
                    alertas_fmt = "\n".join(f"  • {a}" for a in relatorio.alertas_variacao_fixos)
                    resumo_txt += f"\n\n⚠️ *Variação acima de 15% em Gastos Fixos:*\n{alertas_fmt}"
                self._telegram.enviar_mensagem(resumo_txt, chat_id=chat_id)
            except Exception as exc:
                logger.error("Erro ao processar anexo do Telegram: %s", exc, exc_info=True)
                self._telegram.enviar_mensagem(f"❌ Erro ao processar arquivo: {exc}", chat_id=chat_id)

    def disparar_resumo_semanal(self, dry_run: bool = False) -> str:
        """Atualiza investimentos e dispara o resumo semanal no Telegram."""
        try:
            logger.info("Atualizando cotações semanais de investimentos...")
            self._invest_service.atualizar_cotacoes_carteira(dry_run=dry_run)
        except Exception as exc:
            logger.warning("Falha ao atualizar cotações de investimentos: %s", exc)

        logger.info("Gerando painel semanal%s...", " (dry-run)" if dry_run else " para o Telegram")
        resumo = self._reporter.gerar_resumo_semanal()
        if not dry_run:
            self._telegram.enviar_mensagem(resumo)
        return resumo

    def notificar(self, texto: str) -> None:
        """Envia uma mensagem direta de notificação para o chat do usuário."""
        if self.config.telegram_chat_id:
            self._telegram.enviar_mensagem(texto, chat_id=self.config.telegram_chat_id)
        else:
            self._telegram.enviar_mensagem(texto)

    def verificar_e_notificar_gastos_previstos(
        self,
        data_referencia: date | None = None,
        dry_run: bool = False,
    ) -> int:
        """Verifica se há gastos com status 'Previsto' com cobrança na data de hoje e pergunta ao usuário."""
        hoje = data_referencia or date.today()
        previstos = self._repos.transacoes.buscar_previstos_para_data(hoje)
        if not previstos:
            logger.info("Nenhum gasto previsto encontrado com vencimento em %s.", hoje.isoformat())
            return 0

        chat_id = self.config.telegram_chat_id
        if not chat_id:
            logger.warning("TELEGRAM_CHAT_ID não configurado. Não é possível enviar lembrete.")
            return 0

        for item in previstos:
            desc = item["descricao"]
            val_fmt = formatar_brl(item["valor"])
            conta = item["conta"]
            page_id = item["page_id"]

            msg = (
                "🔔 *Lembrete de Gasto Fixo Previsto Hoje:*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• *Item:* {desc}\n"
                f"• *Valor:* *{val_fmt}*\n"
                f"• *Meio de Pagamento:* {conta}\n"
                f"• *Data Prevista:* {hoje.strftime('%d/%m/%Y')}\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "👉 Esse valor caiu certinho na conta/cartão?\n\n"
                f"💡 Clique no botão abaixo para confirmar ou responda `/confirmar {desc}`."
            )
            botoes = {
                "inline_keyboard": [
                    [{"text": f"✅ Confirmar {desc}", "callback_data": f"confirmar:{page_id}"}]
                ]
            }
            if not dry_run:
                self._telegram.enviar_mensagem(msg, chat_id=chat_id, reply_markup=botoes)

        logger.info("Notificados %d gastos previstos para o chat %s.", len(previstos), chat_id)
        return len(previstos)

    def _tratar_salario(self, comando: str, chat_id: str, dry_run: bool = False) -> None:
        """Processa `/salario Valor [; Conta]` e registra `TipoTransacao.ENTRADA` no Notion."""
        corpo = comando.split(maxsplit=1)[1].strip() if len(comando.split(maxsplit=1)) > 1 else ""
        if not corpo:
            self._telegram.enviar_mensagem(
                "💰 *COMO REGISTRAR SEU SALÁRIO:*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Envie: `/salario Valor` *(ou `/salario Valor ; Conta`)*\n\n"
                "💡 *Exemplos:*\n"
                "• `/salario 5500`\n"
                "• `/salario 5500,50 ; santander conta`",
                chat_id=chat_id,
            )
            return

        partes = [p.strip() for p in corpo.split(";") if p.strip()]
        try:
            valor = para_decimal(partes[0])
        except Exception:
            valor = None
        if not valor or valor <= 0:
            self._telegram.enviar_mensagem(
                "⚠️ Valor inválido. Exemplo: `/salario 5500` ou `/salario 5500,50`.",
                chat_id=chat_id,
            )
            return

        conta = "Santander Conta"
        if len(partes) > 1:
            conta_resolvida = self._parser._resolver_conta(partes[1])
            if conta_resolvida:
                conta = conta_resolvida

        hoje = date.today()
        if hoje.day >= 25:
            ano_comp, mes_comp_num = adicionar_meses(hoje.year, hoje.month, 1)
        else:
            ano_comp, mes_comp_num = hoje.year, hoje.month
        mes_competencia = formatar_mes_competencia(ano_comp, mes_comp_num)

        self._registrar_salario(
            valor=valor,
            conta=conta,
            mes_competencia=mes_competencia,
            chat_id=chat_id,
            dry_run=dry_run,
        )

    def _registrar_salario(
        self,
        valor: Decimal,
        conta: str,
        mes_competencia: str,
        chat_id: str,
        dry_run: bool = False,
    ) -> str:
        """Cria lançamento `TipoTransacao.ENTRADA` (Salário) no Notion e envia confirmação."""
        hoje = date.today()
        lancamento = LancamentoTransacao(
            descricao="Salário",
            valor=valor,
            data_compra=hoje,
            mes_competencia=mes_competencia,
            tipo=TipoTransacao.ENTRADA,
            categoria="",
            conta_ou_cartao=conta,
            recorrencia=Recorrencia.FIXO,
            status=StatusTransacao.REALIZADO,
            eh_entrada_na_conta=True,
        )

        page_id = "dry-run-id"
        if not dry_run:
            ano_comp, mes_comp_num = parse_mes_competencia(mes_competencia)
            self._repos.meses.garantir_mes(ano_comp, mes_comp_num)
            page_id = self._repos.transacoes.criar_lancamento(lancamento)

        msg = (
            "💰 *Salário Registrado no Notion!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• *Valor:* *{formatar_brl(valor)}*\n"
            f"• *Entrada em:* {conta}\n"
            f"• *Mês Competência:* {mes_competencia}\n"
            "• *Status:* *Realizado* 🟢\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )
        botoes = {
            "inline_keyboard": [
                [{"text": "🗑️ Desfazer / Excluir Lançamento", "callback_data": f"excluir:{page_id}"}]
            ]
        }
        self._telegram.enviar_mensagem(msg, chat_id=chat_id, reply_markup=botoes)
        return page_id

    def _tratar_pagar_fatura(self, comando: str, chat_id: str, dry_run: bool = False) -> None:
        """Processa `/pagar_fatura Cartao ; Valor [; Conta Origem]`."""
        corpo = comando.split(maxsplit=1)[1].strip() if len(comando.split(maxsplit=1)) > 1 else ""
        partes = [p.strip() for p in corpo.split(";") if p.strip()]
        if len(partes) < 2:
            self._telegram.enviar_mensagem(
                "💳 *COMO REGISTRAR PAGAMENTO DE FATURA:*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Envie: `/pagar_fatura Cartao ; Valor [; Conta Origem]`\n\n"
                "💡 *Exemplos:*\n"
                "• `/pagar_fatura santander ; 1469,23`\n"
                "• `/pagar_fatura bradesco ; 449,05 ; santander conta`",
                chat_id=chat_id,
            )
            return

        cartao = self._parser._resolver_conta(partes[0])
        if not cartao:
            self._telegram.enviar_mensagem(
                f"⚠️ Cartão '{partes[0]}' não reconhecido. Use `santander`, `bradesco` ou `inter`.",
                chat_id=chat_id,
            )
            return

        try:
            valor = para_decimal(partes[1])
        except Exception:
            valor = None
        if not valor or valor <= 0:
            self._telegram.enviar_mensagem(
                "⚠️ Valor inválido. Exemplo: `/pagar_fatura santander ; 1469,23`.",
                chat_id=chat_id,
            )
            return

        conta_origem = "Santander Conta"
        if len(partes) > 2:
            conta_resolvida = self._parser._resolver_conta(partes[2])
            if conta_resolvida:
                conta_origem = conta_resolvida

        hoje = date.today()
        ano_ciclo, mes_ciclo = competencia_ciclo_atual(hoje)
        mes_comp = formatar_mes_competencia(ano_ciclo, mes_ciclo)

        self._registrar_pagamento_fatura(
            cartao=cartao,
            valor=valor,
            mes_competencia=mes_comp,
            conta_origem=conta_origem,
            chat_id=chat_id,
            dry_run=dry_run,
        )

    def _registrar_pagamento_fatura(
        self,
        cartao: str,
        valor: Decimal,
        mes_competencia: str,
        conta_origem: str,
        chat_id: str,
        dry_run: bool = False,
    ) -> str:
        """Cria lançamento `TipoTransacao.PAGAR_FATURA` debitando `conta_origem` e creditando `cartao`."""
        hoje = date.today()
        lancamento = LancamentoTransacao(
            descricao=f"Pagamento Fatura {cartao}",
            valor=valor,
            data_compra=hoje,
            mes_competencia=mes_competencia,
            tipo=TipoTransacao.PAGAR_FATURA,
            categoria="",
            conta_ou_cartao=conta_origem,
            conta_destino=cartao,
            recorrencia=Recorrencia.NAO_RECORRENTE,
            status=StatusTransacao.REALIZADO,
        )

        page_id = "dry-run-id"
        if not dry_run:
            ano_m, mes_m = parse_mes_competencia(mes_competencia)
            self._repos.meses.garantir_mes(ano_m, mes_m)
            page_id = self._repos.transacoes.criar_lancamento(lancamento)

        msg = (
            "💳 *Pagamento de Fatura Registrado!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• *Cartão:* {cartao}\n"
            f"• *Valor Pago:* *{formatar_brl(valor)}*\n"
            f"• *Saída de:* {conta_origem}\n"
            f"• *Competência:* {mes_competencia}\n"
            "• *Status:* *Realizado* 🟢\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "O saldo da conta e o limite do cartão já foram atualizados no Notion!"
        )
        botoes = {
            "inline_keyboard": [
                [{"text": "🗑️ Desfazer / Excluir Lançamento", "callback_data": f"excluir:{page_id}"}]
            ]
        }
        self._telegram.enviar_mensagem(msg, chat_id=chat_id, reply_markup=botoes)
        return page_id

    def notificar_lembrete_salario(
        self,
        data_referencia: date | None = None,
        dry_run: bool = False,
    ) -> bool:
        """Pergunta no dia 25 às 10h se o salário já caiu na conta."""
        hoje = data_referencia or date.today()
        chat_id = self.config.telegram_chat_id
        if not chat_id:
            logger.warning("TELEGRAM_CHAT_ID não configurado. Não é possível enviar lembrete de salário.")
            return False

        ano_comp, mes_comp_num = adicionar_meses(hoje.year, hoje.month, 1) if hoje.day >= 25 else (hoje.year, hoje.month)
        mes_comp = formatar_mes_competencia(ano_comp, mes_comp_num)

        sal_padrao = getattr(self.config, "salario_padrao", None)
        if isinstance(sal_padrao, Decimal) and sal_padrao > 0:
            val_fmt = formatar_brl(sal_padrao)
            val_cmd = f"{sal_padrao:.2f}".replace(".", ",")
            msg = (
                "💰 *Bom dia! Hoje é dia 25 — seu salário já caiu na conta?*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• *Valor habitual:* *{val_fmt}*\n\n"
                "👉 Clique no botão abaixo para confirmar o valor habitual, ou responda com:\n"
                f"`/salario {val_cmd}` *(altere o valor se caiu diferente este mês)*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"ℹ️ A entrada será registrada na competência *{mes_comp}* (Santander Conta por padrão)."
            )
            botoes = {
                "inline_keyboard": [
                    [
                        {
                            "text": f"✅ Sim, caiu {val_fmt}",
                            "callback_data": f"sal:{decimal_para_centavos(sal_padrao)}:{mes_comp}",
                        }
                    ]
                ]
            }
            if not dry_run:
                self._telegram.enviar_mensagem(msg, chat_id=chat_id, reply_markup=botoes)
        else:
            msg = (
                "💰 *Bom dia! Hoje é dia 25 — seu salário já caiu na conta?*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Para registrar a entrada no Notion, responda com:\n"
                "`/salario [valor]`\n\n"
                "💡 *Exemplos:*\n"
                "• `/salario 5000`\n"
                "• `/salario 5000,50 ; santander conta`\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"ℹ️ A entrada será registrada na competência *{mes_comp}* (Santander Conta por padrão)."
            )
            if not dry_run:
                self._telegram.enviar_mensagem(msg, chat_id=chat_id)
        logger.info("Lembrete de salário enviado para o chat %s.", chat_id)
        return True

    def notificar_lembrete_cartoes(
        self,
        data_referencia: date | None = None,
        dry_run: bool = False,
    ) -> int:
        """Pergunta no dia 25 à noite se os cartões de crédito já foram pagos, com botões inline."""
        hoje = data_referencia or date.today()
        chat_id = self.config.telegram_chat_id
        if not chat_id:
            logger.warning("TELEGRAM_CHAT_ID não configurado. Não é possível enviar lembrete de cartões.")
            return 0

        ano_ciclo, mes_ciclo = competencia_ciclo_atual(hoje)
        mes_comp = formatar_mes_competencia(ano_ciclo, mes_ciclo)
        resumo_mes = self._repos.meses.obter_resumo_mes(mes_comp)

        faturas_ativas: list[tuple[str, Decimal]] = []
        for nome_cartao, chave in (
            ("Santander Credito", "fatura_santander"),
            ("Bradesco Credito", "fatura_bradesco"),
            ("Inter Credito", "fatura_inter"),
        ):
            val = resumo_mes.get(chave, Decimal("0.00"))
            if val > 0:
                faturas_ativas.append((nome_cartao, val))

        if not faturas_ativas:
            msg = (
                f"💳 *Fechamento do Dia 25 ({mes_comp})*\n"
                "Você já pagou seus cartões de crédito hoje?\n\n"
                "💡 Caso queira registrar manualmente, use:\n"
                "`/pagar_fatura santander ; valor`"
            )
            if not dry_run:
                self._telegram.enviar_mensagem(msg, chat_id=chat_id)
            return 0

        linhas_faturas = "\n".join(
            f"• *{cartao}:* *{formatar_brl(valor)}*" for cartao, valor in faturas_ativas
        )
        botoes_rows = [
            [
                {
                    "text": f"✅ Paguei {cartao} ({formatar_brl(valor)})",
                    "callback_data": f"pf:{cartao}:{decimal_para_centavos(valor)}:{mes_comp}",
                }
            ]
            for cartao, valor in faturas_ativas
        ]

        msg = (
            f"💳 *Boa noite! Dia 25 — você já pagou os cartões de crédito ({mes_comp})?*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{linhas_faturas}\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "👉 Clique nos botões abaixo para registrar o pagamento (saindo da `Santander Conta`) "
            "ou use `/pagar_fatura cartao ; valor` se pagou outro valor."
        )
        if not dry_run:
            self._telegram.enviar_mensagem(
                msg,
                chat_id=chat_id,
                reply_markup={"inline_keyboard": botoes_rows},
            )
        logger.info("Lembrete de pagamento de cartões enviado (%d faturas) para o chat %s.", len(faturas_ativas), chat_id)
        return len(faturas_ativas)
