"""Módulo de integração com o Telegram (bot de gastos manuais e resumo semanal)."""

from financas.telegram.client import TelegramClient, TelegramError
from financas.telegram.parsers import GastoManualExtraido, MensagemGastoParser
from financas.telegram.reports import TelegramReportBuilder
from financas.telegram.service import TelegramBotService

__all__ = [
    "GastoManualExtraido",
    "MensagemGastoParser",
    "TelegramBotService",
    "TelegramClient",
    "TelegramError",
    "TelegramReportBuilder",
]
