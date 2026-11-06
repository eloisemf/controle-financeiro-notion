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
        updates = self._telegram.obter_atualizacoes(timeout=2)
        if not updates:
            logger.info("Nenhuma mensagem pendente no Telegram.")
            return 0

        processadas = 0
        maior_update_id = 0

        for up in updates:
            up_id = up.get("update_id", 0)
            if up_id > maior_update_id:
                maior_update_id = up_id

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
                self._tratar_comando(texto, chat_id)
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

    def _tratar_comando(self, comando: str, chat_id: str) -> None:
        cmd = comando.lower().split()[0]
        if cmd in ("/start", "/ajuda", "/help"):
            msg_ajuda = (
                "👋 *Olá! Sou seu assistente do Controle Financeiro.*\n\n"
                "Para registrar uma despesa rápida, envie o *valor, item e categoria*:\n"
                "• `5 bala lanche`\n"
                "• `12,50 cafe lanche`\n"
                "• `35 almoco alimentacao santander`\n"
                "• `uber 18.90 transporte inter`\n"
                "• `farmacia 45,90 saude debito`\n\n"
                "⚠️ *A categoria é obrigatória* para manter seu controle preciso!\n"
                "Se não informar a categoria, eu te aviso para reenviar.\n\n"
                "📸 Você também pode me mandar *prints de faturas ou PDFs* que eu leio e concilio!\n\n"
                "Comandos úteis:\n"
                "/resumo — Exibe o relatório de gastos, saldo livre e faturas\n"
                "/investimentos — Atualiza cotações e mostra seu patrimônio\n"
                "/ajuda — Exibe esta mensagem de ajuda"
            )
            self._telegram.enviar_mensagem(msg_ajuda, chat_id=chat_id)

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
                "❓ Comando não reconhecido. Digite /ajuda para ver as instruções.",
                chat_id=chat_id,
            )

    def _tratar_gasto_texto(self, texto: str, chat_id: str, dry_run: bool = False) -> None:
        gasto = self._parser.parse_mensagem(texto)
        if not gasto:
            self._telegram.enviar_mensagem(
                "⚠️ Não entendi o valor ou formato do gasto. Envie no formato:\n"
                "• `15 cafezinho lanche` ou `uber 20 transporte inter` ou `bala 5 lanche`",
                chat_id=chat_id,
            )
            return

        if not gasto.categoria:
            msg_alerta = (
                "⚠️ *Categoria obrigatória!*\n\n"
                "Para manter seu controle organizado, informe a categoria junto com o valor.\n\n"
                "💡 *Exemplos:*\n"
                "• `5 bala lanche`\n"
                "• `15 cafezinho lanche`\n"
                "• `uber 20 transporte privado inter`\n"
                "• `farmacia 45 saude debito`\n\n"
                "📌 *Categorias disponíveis:*\n"
                "• *Lanche, Alimentação, Saúde, Beleza, Roupa*\n"
                "• *Transporte Privado, Transporte Público, Lazer, Livro*\n"
                "• *Cursos, Casa, Utilidade, Academia, Viagem, Eletronicos*"
            )
            self._telegram.enviar_mensagem(msg_alerta, chat_id=chat_id)
            return

        lancamento = self._parser.criar_lancamento(gasto)

        if not dry_run:
            self._repos.transacoes.criar_lancamento(lancamento)

        val_fmt = f"R$ {lancamento.valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        msg_confirmacao = (
            "✅ *Despesa Registrada no Notion!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• *Item:* {lancamento.descricao}\n"
            f"• *Valor:* *{val_fmt}*\n"
            f"• *Conta/Cartão:* {lancamento.conta_ou_cartao}\n"
            f"• *Categoria:* {lancamento.categoria} ({lancamento.uso_dinheiro})\n"
            f"• *Mês Competência:* {lancamento.mes_competencia}\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )
        self._telegram.enviar_mensagem(msg_confirmacao, chat_id=chat_id)

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
