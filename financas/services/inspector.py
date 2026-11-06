"""Serviço de inspeção somente leitura das databases do Notion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from financas.config import NotionDatabaseIds
from financas.notion.builders import NotionParser
from financas.notion.client import NotionClient


@dataclass(frozen=True)
class PropriedadeResumo:
    nome: str
    tipo: str
    detalhes: str = ""

    def formatar(self) -> str:
        sufixo = f" | {self.detalhes}" if self.detalhes else ""
        return f"  - {self.nome}: [{self.tipo}]{sufixo}"


@dataclass(frozen=True)
class DatabaseResumo:
    database_id: str
    titulo: str
    propriedades: tuple[PropriedadeResumo, ...]


class WorkspaceInspectorService:
    """Inspeciona todas as databases visíveis pela integração no Notion."""

    def __init__(self, client: NotionClient, db_ids: NotionDatabaseIds) -> None:
        self._client = client
        self._db_ids = db_ids

    def inspecionar_tudo(self) -> list[DatabaseResumo]:
        raw_dbs = self._client.search(filter_object="database")
        id_para_titulo = self._db_ids.as_name_map()

        for db in raw_dbs:
            db_id = NotionParser.normalize_id(db.get("id"))
            titulo = NotionParser.plain_text(db.get("title", [])) or "(Sem título)"
            id_para_titulo[db_id] = titulo

        resumos: list[DatabaseResumo] = []
        for db in raw_dbs:
            db_id = NotionParser.normalize_id(db.get("id"))
            titulo = id_para_titulo.get(db_id, "(Sem título)")
            props = db.get("properties", {})
            lista_props = tuple(
                self._descrever_propriedade(nome, props[nome], id_para_titulo)
                for nome in sorted(props.keys())
            )
            resumos.append(
                DatabaseResumo(
                    database_id=db_id,
                    titulo=titulo,
                    propriedades=lista_props,
                )
            )
        return resumos

    @staticmethod
    def _descrever_propriedade(
        nome: str, prop_data: dict[str, Any], id_para_titulo: dict[str, str]
    ) -> PropriedadeResumo:
        tipo = prop_data.get("type", "unknown")
        detalhes: list[str] = []

        if tipo == "relation":
            rel = prop_data.get("relation", {})
            alvo_id = NotionParser.normalize_id(rel.get("database_id"))
            alvo_nome = id_para_titulo.get(alvo_id, "Desconhecida")
            rel_tipo = rel.get("type", "")
            synced = rel.get("dual_property", {}).get("synced_property_name", "")
            sync_str = f", synced_prop='{synced}'" if synced else ""
            detalhes.append(f"-> '{alvo_nome}' ({alvo_id}) [{rel_tipo}{sync_str}]")
        elif tipo in ("select", "multi_select"):
            opcoes = [o.get("name", "") for o in prop_data.get(tipo, {}).get("options", [])]
            detalhes.append(f"opções={opcoes}")
        elif tipo == "status":
            opcoes = [o.get("name", "") for o in prop_data.get("status", {}).get("options", [])]
            detalhes.append(f"status={opcoes}")
        elif tipo == "formula":
            expr = " ".join(prop_data.get("formula", {}).get("expression", "").split())
            detalhes.append(f"expr=`{expr}`")
        elif tipo == "rollup":
            r = prop_data.get("rollup", {})
            detalhes.append(
                f"via '{r.get('relation_property_name')}'.'{r.get('rollup_property_name')}' ({r.get('function')})"
            )
        elif tipo == "number":
            detalhes.append(f"formato={prop_data.get('number', {}).get('format', '')}")

        return PropriedadeResumo(nome=nome, tipo=tipo, detalhes=" ".join(detalhes))
