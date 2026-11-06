"""Construtores (Builders) e Parsers para payloads da API do Notion.

Encapsula o formato aninhado da API do Notion (2022-06-28) e garante que a
conversão de `Decimal` para o formato numérico JSON ocorra em um único ponto.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from financas.domain.models import para_decimal


class NotionPropertyBuilder:
    """Constrói valores de propriedades para criação/atualização de páginas no Notion."""

    @staticmethod
    def title(texto: str) -> dict[str, Any]:
        return {"title": [{"type": "text", "text": {"content": texto}}]}

    @staticmethod
    def rich_text(texto: str) -> dict[str, Any]:
        if not texto:
            return {"rich_text": []}
        return {"rich_text": [{"type": "text", "text": {"content": texto}}]}

    @staticmethod
    def number_decimal(valor: Decimal | int | None) -> dict[str, Any]:
        """Converte `Decimal` quantizado (0.01) para o payload numérico da API REST."""
        if valor is None:
            return {"number": None}
        dec = para_decimal(valor)
        return {"number": float(dec) if dec is not None else None}

    @staticmethod
    def number_raw(valor: Decimal | int | None) -> dict[str, Any]:
        """Converte `Decimal` de alta precisão (ex: fração de títulos) sem truncar em 2 casas."""
        if valor is None:
            return {"number": None}
        return {"number": float(valor)}

    @staticmethod
    def number_int(valor: int | None) -> dict[str, Any]:
        """Converte inteiro opcional para o payload numérico da API REST."""
        return {"number": int(valor) if valor is not None else None}

    @staticmethod
    def select(opcao: str | None) -> dict[str, Any]:
        if not opcao:
            return {"select": None}
        return {"select": {"name": opcao}}

    @staticmethod
    def checkbox(ativo: bool) -> dict[str, Any]:
        return {"checkbox": bool(ativo)}

    @staticmethod
    def date_iso(data: date | str | None) -> dict[str, Any]:
        if data is None:
            return {"date": None}
        iso = data.isoformat() if isinstance(data, date) else str(data)
        return {"date": {"start": iso}}

    @staticmethod
    def relation(page_ids: str | Iterable[str]) -> dict[str, Any]:
        if isinstance(page_ids, str):
            ids = [page_ids]
        else:
            ids = list(page_ids)
        return {"relation": [{"id": pid} for pid in ids if pid]}


class NotionSchemaBuilder:
    """Constrói definições de colunas (schema) para criação/alteração de databases."""

    @staticmethod
    def title() -> dict[str, Any]:
        return {"title": {}}

    @staticmethod
    def number(formato: str = "real") -> dict[str, Any]:
        return {"number": {"format": formato}}

    @staticmethod
    def select(opcoes: list[tuple[str, str]]) -> dict[str, Any]:
        return {
            "select": {
                "options": [{"name": nome, "color": cor} for nome, cor in opcoes]
            }
        }

    @staticmethod
    def relation_single(database_id: str) -> dict[str, Any]:
        return {
            "relation": {
                "database_id": database_id,
                "type": "single_property",
                "single_property": {},
            }
        }

    @staticmethod
    def relation_dual(database_id: str) -> dict[str, Any]:
        return {
            "relation": {
                "database_id": database_id,
                "type": "dual_property",
                "dual_property": {},
            }
        }

    @staticmethod
    def checkbox() -> dict[str, Any]:
        return {"checkbox": {}}

    @staticmethod
    def date() -> dict[str, Any]:
        return {"date": {}}

    @staticmethod
    def rich_text() -> dict[str, Any]:
        return {"rich_text": {}}

    @staticmethod
    def formula(expression: str) -> dict[str, Any]:
        return {"formula": {"expression": expression}}


class NotionParser:
    """Extrai dados tipados de objetos de página e database retornados pela API."""

    @staticmethod
    def normalize_id(raw_id: str | None) -> str:
        return raw_id.replace("-", "") if raw_id else ""

    @staticmethod
    def plain_text(rich_text_list: list[dict[str, Any]]) -> str:
        return "".join(item.get("plain_text", "") for item in rich_text_list).strip()

    @classmethod
    def page_title(cls, page: dict[str, Any]) -> str:
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title" or "title" in prop:
                return cls.plain_text(prop.get("title", []))
        return ""

    @staticmethod
    def _extract_raw_number(prop: dict[str, Any]) -> Any:
        if "number" in prop and prop["number"] is not None:
            return prop["number"]
        if "formula" in prop and isinstance(prop["formula"], dict):
            return prop["formula"].get("number")
        if "rollup" in prop and isinstance(prop["rollup"], dict):
            return prop["rollup"].get("number")
        return None

    @classmethod
    def prop_decimal(cls, page: dict[str, Any], prop_name: str) -> Decimal | None:
        prop = page.get("properties", {}).get(prop_name, {})
        raw = cls._extract_raw_number(prop)
        if raw is None:
            return None
        return para_decimal(str(raw))

    @classmethod
    def prop_decimal_raw(cls, page: dict[str, Any], prop_name: str) -> Decimal | None:
        prop = page.get("properties", {}).get(prop_name, {})
        raw = cls._extract_raw_number(prop)
        if raw is None:
            return None
        return Decimal(str(raw))

    @staticmethod
    def prop_select(page: dict[str, Any], prop_name: str) -> str | None:
        prop = page.get("properties", {}).get(prop_name, {})
        sel = prop.get("select")
        return sel.get("name") if sel else None

    @classmethod
    def prop_relation_ids(cls, page: dict[str, Any], prop_name: str) -> list[str]:
        prop = page.get("properties", {}).get(prop_name, {})
        rels = prop.get("relation", [])
        return [cls.normalize_id(r.get("id")) for r in rels if r.get("id")]

    @classmethod
    def prop_rich_text(cls, page: dict[str, Any], prop_name: str) -> str:
        prop = page.get("properties", {}).get(prop_name, {})
        return cls.plain_text(prop.get("rich_text", []))

    @staticmethod
    def prop_checkbox(page: dict[str, Any], prop_name: str) -> bool:
        prop = page.get("properties", {}).get(prop_name, {})
        return bool(prop.get("checkbox", False))

    @staticmethod
    def prop_int(page: dict[str, Any], prop_name: str) -> int | None:
        prop = page.get("properties", {}).get(prop_name, {})
        raw = prop.get("number")
        return int(raw) if raw is not None else None

    @staticmethod
    def prop_date_start(page: dict[str, Any], prop_name: str) -> str:
        prop = page.get("properties", {}).get(prop_name, {})
        date_obj = prop.get("date") or {}
        return str(date_obj.get("start") or "")
