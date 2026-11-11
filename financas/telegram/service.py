"""Serviço orquestrador do bot do Telegram integrado ao Notion."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import logging
from pathlib import Path
import tempfile
from typing import Any

from financas.config import AppConfig
from financas.domain.billing import CalculadoraFatura
from financas.domain.classification import ClassificadorCategoria
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
        self._telegram = telegram_client or TelegramClient(
            token=config.telegram_bot_token,
            chat_id=config.telegram_chat_id,
        )
        self._repos = repos or NotionRepositories.from_client(client, config)
        self._calc = CalculadoraFatura()
        self._market = market_data or MarketDataService()
        self._invest_service = InvestimentosService(
            client=self._notion,
            config=self.config,
            market_data=self._market,
            investimentos_repo=self._repos.investimentos,
            meses_repo=self._repos.meses,
        )
        self._parser = MensagemGastoParser(
            conta_padrao="Santader Credito",
            classificador=ClassificadorCategoria(),
            calculadora=self._calc,
            cartoes=self.config.cartoes,
        )
        self._reporter = TelegramReportBuilder(
            client=self._notion,
            repos=self._repos,
            invest_service=self._invest_service,
        )

    def processar_mensagens_pendentes(self, dry_run: bool = False) -> int:
        """Lê mensagens novas do Telegram, lança no Notion e responde ao usuário."""
        updates = self._telegram.obter_atualizacoes(timeout=0)
        if not updates:
            logger.info("Nenhuma mensagem pendente no Telegram.")
            return 0

        processadas = 0
        maior_update_id = 0

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
        if maior_update_id > 0:
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
                "• `remedio ; 45 ; saude ; inter ; essencial`\n\n"
                "2️⃣ *Modelo Copiar & Colar (digite /modelo):*\n"
                "```text\n"
                "Item: filtro de café\n"
                "Valor: 20\n"
                "Categoria: Casa\n"
                "Conta: Santander Credito\n"
                "```\n"
                "ℹ️ *Padrões automáticos:* Conta = `Santader Credito` | Uso = `Nao essencial`.\n\n"
                "💜 *Reembolso de Amigos/Pessoas (Pix recebido):*\n"
                "• `/reembolso michele ; 145,87`\n"
                "• `/reembolso mae ; 200`\n\n"
                "📸 Você também pode mandar *prints de faturas ou PDFs*!\n\n"
                "📌 *Comandos disponíveis:*\n"
                "/reembolso [nome] ; [valor] — Registra Pix recebido de terceiro\n"
                "/categorias — Lista todas as categorias e contas aceitas\n"
                "/modelo — Envia o molde pronto para copiar e colar\n"
                "/resumo — Relatório de gastos, saldo livre e faturas\n"
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
                "• `santander` *(padrão: Santader Credito)*\n"
                "• `debito` ou `pix` *(Santader Conta)*\n"
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
                "`Conta/Cartão: Santander Credito`\n\n"
                "⚡ *OU ATALHO DE 1 LINHA (com `;`):*\n"
                "`filtro de cafe ; 20 ; casa`"
            )
            self._telegram.enviar_mensagem(msg_modelo, chat_id=chat_id)

        elif cmd in ("/reembolso", "/recebi", "/pagou", "/pessoas"):
            self._tratar_reembolso(comando, chat_id, dry_run=dry_run)

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

        elif cmd == "/investimentos":
            self._telegram.enviar_mensagem("⏳ Buscando cotações de mercado (B3 e Tesouro)...", chat_id=chat_id)
            try:
                self._invest_service.atualizar_cotacoes_carteira()
            except Exception as e:
                logger.warning("Falha ao atualizar cotações: %s", e)
            resumo_inv = self._invest_service.obter_resumo_carteira()
            pat = resumo_inv.patrimonio_total
            lucro = resumo_inv.lucro_total
            sinal = "+" if lucro >= 0 else ""
            txt = (
                "📈 *PAINEL DE INVESTIMENTOS*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• Patrimônio Total: *R$ {pat:,.2f}*\n".replace(",", "X").replace(".", ",").replace("X", ".") +
                f"• Rentabilidade: *{sinal}R$ {lucro:,.2f}*\n".replace(",", "X").replace(".", ",").replace("X", ".") +
                f"• Total de Ativos: *{resumo_inv.total_ativos} ativos*\n"
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
        val_fmt = f"R$ {encontrado['valor']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
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
                "• `/reembolso michele ; 145,87`\n"
                "• `/reembolso mae ; 200`\n"
                "• `/reembolso michele ; 50 ; inter`\n\n"
                "👥 *Pessoas cadastradas no Notion:*\n"
                f"{bloco_pessoas}\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "ℹ️ *Padrões:* Tipo = `Reembolso` | Entrada em = `Santader Conta` | Mês = Mês atual."
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

        val_fmt = f"R$ {lancamento.valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
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

        page_id = "dry-run-id"
        if not dry_run:
            page_id = self._repos.transacoes.criar_lancamento(lancamento)

        val_fmt = f"R$ {lancamento.valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        nome_exibicao = lancamento.nome_fantasia or lancamento.descricao
        msg_confirmacao = (
            "✅ *Despesa Registrada no Notion!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• *Item (Nome Fantasia):* {nome_exibicao}\n"
            f"• *Valor:* *{val_fmt}*\n"
            f"• *Conta/Cartão:* {lancamento.conta_ou_cartao}\n"
            f"• *Categoria:* {lancamento.categoria} ({lancamento.uso_dinheiro})\n"
            f"• *Mês Competência:* {lancamento.mes_competencia}\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )
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
                self._telegram.enviar_mensagem(resumo_txt, chat_id=chat_id)
            except Exception as exc:
                logger.error("Erro ao processar anexo do Telegram: %s", exc, exc_info=True)
                self._telegram.enviar_mensagem(f"❌ Erro ao processar arquivo: {exc}", chat_id=chat_id)

    def disparar_resumo_semanal(self) -> None:
        """Atualiza investimentos e dispara o resumo semanal no Telegram."""
        try:
            logger.info("Atualizando cotações semanais de investimentos...")
            self._invest_service.atualizar_cotacoes_carteira()
        except Exception as exc:
            logger.warning("Falha ao atualizar cotações de investimentos: %s", exc)

        logger.info("Enviando painel semanal para o Telegram...")
        resumo = self._reporter.gerar_resumo_semanal()
        self._telegram.enviar_mensagem(resumo)

    def notificar(self, texto: str) -> None:
        """Envia uma mensagem direta de notificação para o chat do usuário."""
        if self.config.telegram_chat_id:
            self._telegram.enviar_mensagem(texto, chat_id=self.config.telegram_chat_id)
        else:
            self._telegram.enviar_mensagem(texto)

    def verificar_e_notificar_gastos_previstos(self, data_referencia: date | None = None) -> int:
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
            val_fmt = f"R$ {item['valor']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
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
            self._telegram.enviar_mensagem(msg, chat_id=chat_id, reply_markup=botoes)

        logger.info("Notificados %d gastos previstos para o chat %s.", len(previstos), chat_id)
        return len(previstos)
