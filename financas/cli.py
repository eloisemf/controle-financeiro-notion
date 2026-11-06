"""Interface de linha de comando (CLI) unificada e utilitários compartilhados."""

from __future__ import annotations

import argparse
from datetime import date
import logging
import os
from pathlib import Path
import sys
from typing import Callable, Protocol

from financas.config import AppConfig
from financas.domain.billing import formatar_mes_competencia, parse_mes_competencia
from financas.domain.investments import ResumoCarteira
from financas.notion.client import NotionClient
from financas.notion.repositories import NotionRepositories
from financas.parsers import ExtratorDocumentosFinanceiros
from financas.services.fixed_expenses import GastosFixosService
from financas.services.ingestion import FaturaIngestionService
from financas.services.inspector import WorkspaceInspectorService
from financas.services.investments import InvestimentosService
from financas.services.market_data import MarketDataService
from financas.services.setup import WorkspaceSetupService

logger = logging.getLogger(__name__)

EXTENSOES_SUPORTADAS = {".pdf", ".csv", ".txt", ".png", ".jpg", ".jpeg", ".webp"}


class RelatorioExecucao(Protocol):
    @property
    def tem_alteracoes(self) -> bool: ...

    def formatar_resumo(self) -> str: ...


def configurar_ambiente_cli(nivel_log: int | None = None) -> None:
    """Configura encoding UTF-8 no stdout e inicializa o logging padrão."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if nivel_log is None:
        env_level = os.getenv("LOG_LEVEL", "").upper().strip()
        nivel_log = getattr(logging, env_level, logging.INFO)
    logging.basicConfig(
        level=nivel_log,
        format="%(levelname)s [%(name)s]: %(message)s",
        force=True,
    )


def adicionar_argumentos_modo_execucao(parser: argparse.ArgumentParser) -> None:
    """Adiciona o grupo mutuamente exclusivo `--dry-run` e `--interactive`."""
    grupo_modo = parser.add_mutually_exclusive_group()
    grupo_modo.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Apenas simula a operação e exibe o relatório sem gravar nada no Notion.",
    )
    grupo_modo.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Exibe a simulação (dry-run) e pede confirmação [s/N] antes de gravar.",
    )


def confirmar_acao(mensagem: str) -> bool:
    resposta = input(f"\n{mensagem} [s/N]: ")
    return resposta.strip().lower() in ("s", "sim", "y", "yes")


def executar_com_modo(
    acao: Callable[[bool], RelatorioExecucao],
    dry_run: bool,
    interactive: bool,
    mensagem_confirmacao: str,
) -> None:
    """Executa uma operação de serviço respeitando os modos direto, `--dry-run` e `--interactive`."""
    if dry_run:
        relatorio = acao(True)
        print(relatorio.formatar_resumo())
        return

    if interactive:
        previa = acao(True)
        print(previa.formatar_resumo())
        if not previa.tem_alteracoes:
            print("-> Nenhuma alteração pendente para enviar.")
            return
        if not confirmar_acao(mensagem_confirmacao):
            print("-> Operação cancelada pelo usuário.")
            return

    relatorio = acao(False)
    print(relatorio.formatar_resumo())


def listar_arquivos_alvo(alvo: Path) -> list[Path]:
    if alvo.is_file():
        return [alvo]
    if alvo.is_dir():
        return sorted(
            p for p in alvo.iterdir() if p.is_file() and p.suffix.lower() in EXTENSOES_SUPORTADAS
        )
    raise FileNotFoundError(f"Caminho não encontrado: {alvo}")


def construir_parser_ingest(
    subparser: argparse.ArgumentParser | None = None,
) -> argparse.ArgumentParser:
    parser = subparser or argparse.ArgumentParser(
        description="Importa faturas e extratos financeiros para o Notion de forma idempotente."
    )
    parser.add_argument(
        "caminhos",
        nargs="*",
        help="Arquivos ou pastas a processar (padrão: faturas/).",
    )
    adicionar_argumentos_modo_execucao(parser)
    return parser


def construir_parser_fixos(
    subparser: argparse.ArgumentParser | None = None,
) -> argparse.ArgumentParser:
    hoje = date.today()
    mes_atual_default = formatar_mes_competencia(hoje.year, hoje.month)
    parser = subparser or argparse.ArgumentParser(
        description="Lança os Gastos Fixos ativos como 'Previsto' para um mês de competência."
    )
    parser.add_argument(
        "mes_competencia",
        nargs="?",
        default=mes_atual_default,
        help=f"Mês de competência no formato YYYY-Mes (padrão: {mes_atual_default}).",
    )
    parser.add_argument(
        "--notificar",
        action="store_true",
        help="Envia confirmação de lançamento dos gastos fixos para o Telegram.",
    )
    adicionar_argumentos_modo_execucao(parser)
    return parser


def construir_parser_investimentos(
    subparser: argparse.ArgumentParser | None = None,
) -> argparse.ArgumentParser:
    parser = subparser or argparse.ArgumentParser(
        description="Automação de Investimentos: Cotações B3/Tesouro e Fechamento Mensal."
    )
    parser.add_argument(
        "--cotacao",
        action="store_true",
        help="Atualiza cotações de mercado (B3 e Tesouro) automaticamente.",
    )
    parser.add_argument(
        "--sincronizar-aportes",
        action="store_true",
        help="Recalcula Qtd e Preço Médio em Meus Investimentos a partir da base de Aportes.",
    )
    parser.add_argument(
        "--fechar-mes",
        action="store_true",
        help="Gera o snapshot patrimonial de fechamento mensal no Notion (idempotente).",
    )
    parser.add_argument(
        "--data",
        type=str,
        default=None,
        help="Data personalizada para a foto de fechamento (formato: AAAA-MM-DD).",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Executa em modo simulação sem alterar dados no Notion.",
    )
    return parser


def executar_ingest(args: argparse.Namespace) -> None:
    pasta_padrao = Path(__file__).resolve().parents[1] / "faturas"
    alvos = [Path(p) for p in args.caminhos] if args.caminhos else [pasta_padrao]

    arquivos: list[Path] = []
    for alvo in alvos:
        arquivos.extend(listar_arquivos_alvo(alvo))

    config = AppConfig.from_env()
    with NotionClient(token=config.notion_token) as client:
        repos = NotionRepositories.from_client(client, config)
        try:
            pessoas = repos.pessoas.listar_todas()
        except Exception as exc:
            logger.error("Não foi possível carregar lista de pessoas do Notion: %s", exc, exc_info=True)
            pessoas = []

        extrator = ExtratorDocumentosFinanceiros(
            gemini_api_key=config.gemini_api_key,
            nome_titular=config.nome_titular,
            pessoas=pessoas,
        )
        servico = FaturaIngestionService(
            client=client, config=config, repos=repos, pessoas_cadastradas=pessoas
        )
        for arq in arquivos:
            logger.info("Processando arquivo: %s", arq.name)
            lancamentos = extrator.extrair_de_arquivo(arq)
            executar_com_modo(
                acao=lambda dry: servico.importar_lancamentos(lancamentos, dry_run=dry),
                dry_run=args.dry_run,
                interactive=args.interactive,
                mensagem_confirmacao=f"Deseja aplicar as alterações de '{arq.name}' no Notion?",
            )


def executar_fixos(args: argparse.Namespace) -> None:
    ano, mes = parse_mes_competencia(args.mes_competencia)
    config = AppConfig.from_env()
    with NotionClient(token=config.notion_token) as client:
        servico = GastosFixosService(client=client, config=config)
        executar_com_modo(
            acao=lambda dry: servico.lancar_previstos_do_mes(ano=ano, mes=mes, dry_run=dry),
            dry_run=args.dry_run,
            interactive=args.interactive,
            mensagem_confirmacao=(
                f"Deseja aplicar os lançamentos previstos de '{args.mes_competencia}' no Notion?"
            ),
        )

        if getattr(args, "notificar", False) and config.telegram_bot_token and not args.dry_run:
            try:
                from financas.telegram.client import TelegramClient

                tel = TelegramClient(token=config.telegram_bot_token, chat_id=config.telegram_chat_id)
                msg = (
                    f"📌 *Gastos Fixos lançados com sucesso!*\n\n"
                    f"Os gastos fixos recorrentes do mês *{args.mes_competencia}* "
                    f"foram gerados como `Previsto` no seu Notion."
                )
                tel.enviar_mensagem(msg)
                logger.info("Notificação de gastos fixos enviada para o Telegram.")
            except Exception as exc:
                logger.warning("Não foi possível enviar confirmação ao Telegram: %s", exc)


def exibir_resumo_investimentos(resumo: ResumoCarteira | dict) -> None:
    """Exibe o resumo patrimonial consolidado em tabela formatada."""
    print("=" * 75)
    print("📊 PAINEL CONSOLIDADO DE INVESTIMENTOS (NOTION)")
    print("=" * 75)
    print(f"{'ATIVO':<28} | {'TIPO':<32} | {'SALDO ATUAL':>12}")
    print("-" * 75)
    for a in resumo["ativos"]:
        nome = str(a["nome"])[:28]
        tipo = str(a["tipo"])[:32]
        val = a["valor_atual"]
        print(f"{nome:<28} | {tipo:<32} | R$ {val:>9,.2f}")
    print("-" * 75)
    total = resumo["patrimonio_total"]
    lucro = resumo["lucro_total"]
    sinal = "+" if lucro >= 0 else ""
    print(f"{'PATRIMÔNIO TOTAL ATIVO':<63} : R$ {total:>9,.2f}")
    print(f"{'LUCRO/VALORIZAÇÃO ESTIMADA':<63} : {sinal}R$ {lucro:>9,.2f}")
    print("=" * 75)


def executar_investimentos(args: argparse.Namespace) -> None:
    config = AppConfig.from_env()
    with NotionClient(token=config.notion_token) as client, MarketDataService() as market_data:
        service = InvestimentosService(
            client=client,
            config=config,
            market_data=market_data,
        )

        deve_sincronizar = (
            getattr(args, "sincronizar_aportes", False)
            or args.cotacao
            or args.fechar_mes
        )
        if deve_sincronizar:
            logger.info("Sincronizando Aportes com a Carteira (Qtd e Preço Médio)...")
            sincronizados = service.sincronizar_aportes_carteira(dry_run=args.dry_run)
            print("-" * 75)
            for s in sincronizados:
                modo = "[SIMULAÇÃO]" if args.dry_run else "[SINCRONIZADO]"
                print(
                    f"  {modo} {s['ativo']}: Qtd={s['qtd']} | PM=R$ {s['preco_medio']:.2f} "
                    f"| Total Investido=R$ {s['total_investido']:.2f}"
                )
            print("-" * 75)

        if args.cotacao:
            logger.info("Buscando cotações de mercado (B3 / Tesouro Transparente)...")
            atualizacoes = service.atualizar_cotacoes_carteira(dry_run=args.dry_run)
            print("-" * 75)
            if not atualizacoes:
                print("Nenhuma cotação precisou ser atualizada no momento.")
            for at in atualizacoes:
                modo = "[SIMULAÇÃO]" if args.dry_run else "[ATUALIZADO]"
                if "novo" in at:
                    print(
                        f"  {modo} {at['ativo']}: R$ {at['anterior']:.2f} -> R$ {at['novo']:.2f} "
                        f"(PU: R$ {at['pu']:.2f} via {at['fonte']})"
                    )
                else:
                    print(
                        f"  {modo} {at['ativo']}: PU R$ {at['pu']:.2f} obtido "
                        f"({at['fonte']} ref: {at.get('data_base')})"
                    )
            print("-" * 75)

        if args.fechar_mes:
            modo_str = " (MODO SIMULAÇÃO)" if args.dry_run else ""
            logger.info("Executando Fechamento Mensal de Investimentos%s...", modo_str)
            resultado = service.fechar_mes_snapshot(data_foto=args.data, dry_run=args.dry_run)
            print("-" * 75)
            print(f"Mês de Fechamento   : {resultado['mes']}")
            print(f"Data da Foto        : {resultado['data_foto']}")
            print(
                f"Total de Ativos     : {resultado['total_ativos']} "
                f"(Criados: {resultado.get('criados', 0)}, Atualizados: {resultado.get('atualizados', 0)})"
            )
            print(f"Patrimônio Total    : R$ {resultado['total_patrimonio']:,.2f}")
            if resultado.get("patrimonio_anterior") is not None:
                print(f"Patrimônio Anterior : R$ {resultado['patrimonio_anterior']:,.2f}")
            if resultado.get("aportes_mes") is not None:
                print(f"Aportes no Mês      : R$ {resultado['aportes_mes']:,.2f}")
            if resultado.get("rendimento_mes") is not None:
                print(f"Rendimento Puro Mês : R$ {resultado['rendimento_mes']:+,.2f}")
            print("-" * 75)
            if args.dry_run:
                print("Simulação concluída! Nenhum dado foi gravado no Notion.")
            else:
                print("Snapshots registrados/atualizados com sucesso no Notion!")

        resumo = service.obter_resumo_carteira()
        exibir_resumo_investimentos(resumo)


def executar_inspect() -> None:
    config = AppConfig.from_env()
    with NotionClient(token=config.notion_token) as client:
        me = client.get_me()
        print(f"Autenticado na API do Notion como: '{me.get('name')}' (type={me.get('type')})\n")

        inspector = WorkspaceInspectorService(client=client, db_ids=config.databases)
        databases = inspector.inspecionar_tudo()

        print(f"Total de databases encontradas: {len(databases)}")
        print("=" * 80)
        print(f"{'TÍTULO DA DATABASE':<35} | {'ID (SEM HÍFENS)':<32}")
        print("-" * 80)
        for db in databases:
            print(f"{db.titulo:<35} | {db.database_id:<32}")
        print("=" * 80)
        print()

        for db in databases:
            print(f"### Tabela: {db.titulo} (ID: `{db.database_id}`)")
            print(f"Total de colunas: {len(db.propriedades)}")
            for prop in db.propriedades:
                print(prop.formatar())
            print("-" * 80)


def executar_setup() -> None:
    config = AppConfig.from_env()
    with NotionClient(token=config.notion_token) as client:
        servico = WorkspaceSetupService(client=client, config=config)
        relatorio = servico.executar()
        print(relatorio.formatar_resumo())


def construir_parser_telegram(
    subparser: argparse.ArgumentParser | None = None,
) -> argparse.ArgumentParser:
    parser = subparser or argparse.ArgumentParser(
        description="Integração Telegram: processa mensagens pendentes ou dispara resumo semanal."
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Lê e processa mensagens pendentes (gastos manuais, comandos ou arquivos).",
    )
    parser.add_argument(
        "--semanal",
        action="store_true",
        help="Atualiza cotações de investimentos e dispara o resumo semanal no Telegram.",
    )
    parser.add_argument(
        "--checar-previstos",
        action="store_true",
        help="Verifica se há gastos fixos previstos para a data de hoje e envia lembrete no Telegram.",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Simula o processamento sem gravar no Notion.",
    )
    return parser


def executar_telegram(args: argparse.Namespace) -> None:
    from financas.telegram.service import TelegramBotService

    config = AppConfig.from_env()
    with NotionClient(token=config.notion_token) as client:
        bot_service = TelegramBotService(config=config, client=client)

        if getattr(args, "semanal", False):
            logger.info("Executando rotina semanal do Telegram...")
            bot_service.disparar_resumo_semanal()
            print("✅ Resumo semanal enviado com sucesso para o Telegram!")

        if getattr(args, "checar_previstos", False):
            logger.info("Verificando gastos fixos previstos para a data de hoje...")
            notificados = bot_service.verificar_e_notificar_gastos_previstos()
            print(f"✅ Verificação de previstos: {notificados} lembrete(s) enviado(s).")

        if getattr(args, "sync", False) or (
            not getattr(args, "semanal", False) and not getattr(args, "checar_previstos", False)
        ):
            logger.info("Verificando mensagens pendentes no Telegram...")
            qtd = bot_service.processar_mensagens_pendentes(dry_run=args.dry_run)
            print(f"✅ Processamento concluído: {qtd} mensagem(ns) tratada(s).")


def construir_parser_principal() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="financas",
        description="CLI unificada de automação e conciliação de controle financeiro no Notion.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Exibe logs detalhados (nível DEBUG).",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Silencia logs informativos (apenas WARNING e ERROR).",
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    p_ingest = sub.add_parser("ingest", help="Importa faturas e extratos para o Notion.")
    construir_parser_ingest(p_ingest)

    p_fixos = sub.add_parser("fixos", help="Lança Gastos Fixos previstos para um mês.")
    construir_parser_fixos(p_fixos)

    p_inv = sub.add_parser("investimentos", help="Gerencia cotações e snapshots de investimentos.")
    construir_parser_investimentos(p_inv)

    p_tel = sub.add_parser("telegram", help="Processa mensagens e resumos do Telegram.")
    construir_parser_telegram(p_tel)

    sub.add_parser("inspect", help="Inspeciona as databases visíveis no Notion.")
    sub.add_parser("setup", help="Provisiona e sincroniza databases e dados iniciais no Notion.")

    return parser


def main(argv: list[str] | None = None) -> None:
    args = construir_parser_principal().parse_args(argv)
    if getattr(args, "verbose", False):
        configurar_ambiente_cli(logging.DEBUG)
    elif getattr(args, "quiet", False):
        configurar_ambiente_cli(logging.WARNING)
    else:
        configurar_ambiente_cli()

    if args.comando == "ingest":
        executar_ingest(args)
    elif args.comando == "fixos":
        executar_fixos(args)
    elif args.comando == "investimentos":
        executar_investimentos(args)
    elif args.comando == "telegram":
        executar_telegram(args)
    elif args.comando == "inspect":
        executar_inspect()
    elif args.comando == "setup":
        executar_setup()


if __name__ == "__main__":
    main()
