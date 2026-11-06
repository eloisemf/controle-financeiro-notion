"""Ponto de entrada CLI para o bot do Telegram (gastos manuais, comandos e resumo semanal)."""

from __future__ import annotations

from financas.cli import (
    configurar_ambiente_cli,
    construir_parser_telegram as construir_parser_cli,
    executar_telegram,
)


def main(argv: list[str] | None = None) -> None:
    configurar_ambiente_cli()
    args = construir_parser_cli().parse_args(argv)
    executar_telegram(args)


__all__ = ["construir_parser_cli", "main"]

if __name__ == "__main__":
    main()
