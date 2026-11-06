"""Ponto de entrada CLI para lançar os Gastos Fixos (`Previsto`) de um mês no Notion."""

from __future__ import annotations

from financas.cli import (
    configurar_ambiente_cli,
    construir_parser_fixos as construir_parser_cli,
    executar_fixos,
)


def main(argv: list[str] | None = None) -> None:
    configurar_ambiente_cli()
    args = construir_parser_cli().parse_args(argv)
    executar_fixos(args)


__all__ = ["construir_parser_cli", "main"]

if __name__ == "__main__":
    main()
