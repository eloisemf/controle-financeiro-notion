"""Ponto de entrada CLI para provisionamento e migração idempotente das databases no Notion."""

from __future__ import annotations

from financas.cli import configurar_ambiente_cli, executar_setup


def main() -> None:
    configurar_ambiente_cli()
    executar_setup()


if __name__ == "__main__":
    main()
