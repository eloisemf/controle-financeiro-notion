"""Ponto de entrada CLI (somente leitura) para inspecionar as databases do Notion."""

from __future__ import annotations

from financas.cli import configurar_ambiente_cli, executar_inspect


def main() -> None:
    configurar_ambiente_cli()
    executar_inspect()


if __name__ == "__main__":
    main()
