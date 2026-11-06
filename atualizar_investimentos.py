"""Ponto de entrada CLI para gestão, cotações e fechamento de investimentos."""

from __future__ import annotations

from financas.cli import (
    configurar_ambiente_cli,
    construir_parser_investimentos as construir_parser_cli,
    executar_investimentos,
    exibir_resumo_investimentos as exibir_resumo,
)


def main(argv: list[str] | None = None) -> None:
    configurar_ambiente_cli()
    args = construir_parser_cli().parse_args(argv)
    executar_investimentos(args)


__all__ = ["construir_parser_cli", "exibir_resumo", "main"]

if __name__ == "__main__":
    main()
