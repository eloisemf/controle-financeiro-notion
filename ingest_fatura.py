"""Ponto de entrada CLI para importar faturas e extratos (PDF, CSV, TXT, PNG/JPG) para o Notion."""

from __future__ import annotations

from financas.cli import (
    EXTENSOES_SUPORTADAS,
    configurar_ambiente_cli,
    confirmar_acao,
    construir_parser_ingest as construir_parser_cli,
    executar_ingest,
    listar_arquivos_alvo,
)


def confirmar_envio(nome_arquivo: str) -> bool:
    return confirmar_acao(f"Deseja aplicar as alterações de '{nome_arquivo}' no Notion?")


def main(argv: list[str] | None = None) -> None:
    configurar_ambiente_cli()
    args = construir_parser_cli().parse_args(argv)
    executar_ingest(args)


__all__ = [
    "EXTENSOES_SUPORTADAS",
    "confirmar_envio",
    "construir_parser_cli",
    "listar_arquivos_alvo",
    "main",
]

if __name__ == "__main__":
    main()
