"""Cliente HTTP resiliente para a API REST do Notion.

Implementa:
- Controle de taxa de requisições (~3 req/s documentado pela Notion API).
- Retry automático com respeito ao header `Retry-After` (HTTP 429) e
  exponential backoff para falhas transitórias de servidor (HTTP 5xx) e de rede.
- Paginação transparente para queries de databases, busca e blocos filhos.
"""

from __future__ import annotations

import logging
import time
from types import TracebackType
from typing import Any

import requests

from financas.config import NOTION_API_VERSION, NOTION_BASE_URL

logger = logging.getLogger(__name__)


class NotionApiError(RuntimeError):
    """Exceção levantada quando a API do Notion retorna erro não recuperável ou esgota tentativas."""

    def __init__(self, method: str, path: str, status_code: int | None, code: str, message: str) -> None:
        status_str = f"HTTP {status_code}" if status_code is not None else "NETWORK/RETRY"
        super().__init__(f"{method} {path} -> {status_str} ({code}): {message}")
        self.method = method
        self.path = path
        self.status_code = status_code
        self.code = code
        self.message = message


class NotionClient:
    """Cliente da API do Notion com gerenciamento de sessão, rate limit e paginação."""

    def __init__(
        self,
        token: str,
        base_url: str = NOTION_BASE_URL,
        notion_version: str = NOTION_API_VERSION,
        min_interval_s: float = 0.35,
        max_retries: int = 5,
        timeout_s: int = 30,
    ) -> None:
        if not token or not token.strip():
            raise ValueError("Token do Notion não pode ser vazio.")
        self._base_url = base_url.rstrip("/")
        self._min_interval_s = min_interval_s
        self._max_retries = max_retries
        self._timeout_s = timeout_s
        self._last_request_ts = 0.0

        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token.strip()}",
                "Notion-Version": notion_version,
                "Content-Type": "application/json",
            }
        )

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> NotionClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def _throttle(self) -> None:
        decorrido = time.monotonic() - self._last_request_ts
        espera = self._min_interval_s - decorrido
        if espera > 0:
            time.sleep(espera)
        self._last_request_ts = time.monotonic()

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        ultimo_status: int | None = None

        for tentativa in range(self._max_retries):
            self._throttle()
            try:
                resp = self._session.request(method, url, timeout=self._timeout_s, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                backoff = 2**tentativa
                logger.warning(
                    "Falha de conexão/timeout em %s %s (%s). Tentativa %d/%d em %ds...",
                    method,
                    path,
                    exc,
                    tentativa + 1,
                    self._max_retries,
                    backoff,
                )
                time.sleep(backoff)
                continue

            ultimo_status = resp.status_code

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "1.0"))
                logger.warning("Rate limit (HTTP 429) em %s %s. Aguardando %.1fs...", method, path, retry_after)
                time.sleep(retry_after)
                continue

            if resp.status_code >= 500:
                backoff = 2**tentativa
                logger.warning(
                    "Erro transitório HTTP %d em %s %s. Tentativa %d/%d em %ds...",
                    resp.status_code,
                    method,
                    path,
                    tentativa + 1,
                    self._max_retries,
                    backoff,
                )
                time.sleep(backoff)
                continue

            try:
                dados = resp.json()
            except ValueError:
                dados = {}

            if resp.status_code >= 400:
                raise NotionApiError(
                    method=method,
                    path=path,
                    status_code=resp.status_code,
                    code=dados.get("code", "unknown_error"),
                    message=dados.get("message", resp.text),
                )
            return dados

        raise NotionApiError(
            method=method,
            path=path,
            status_code=ultimo_status,
            code="max_retries_exceeded",
            message=f"Excedeu o limite de {self._max_retries} tentativas.",
        )

    def _paginate(
        self,
        method: str,
        path: str,
        base_payload: dict[str, Any] | None = None,
        use_query_params: bool = False,
    ) -> list[dict[str, Any]]:
        """Executa requisições paginadas (page_size=100) acumulando `results` até `has_more=False`."""
        resultados: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            params_or_body: dict[str, Any] = {"page_size": 100}
            if base_payload:
                params_or_body.update(base_payload)
            if cursor:
                params_or_body["start_cursor"] = cursor

            if use_query_params:
                dados = self.request(method, path, params=params_or_body)
            else:
                dados = self.request(method, path, json=params_or_body)

            resultados.extend(dados.get("results", []))
            if not dados.get("has_more"):
                return resultados
            cursor = dados.get("next_cursor")

    # --- Endpoints de Usuário / Bot ---
    def get_me(self) -> dict[str, Any]:
        return self.request("GET", "/users/me")

    # --- Endpoints de Busca ---
    def search(self, filter_object: str | None = None, query: str | None = None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {}
        if filter_object:
            payload["filter"] = {"value": filter_object, "property": "object"}
        if query:
            payload["query"] = query
        return self._paginate("POST", "/search", base_payload=payload)

    # --- Endpoints de Databases ---
    def get_database(self, database_id: str) -> dict[str, Any]:
        return self.request("GET", f"/databases/{database_id}")

    def create_database(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/databases", json=payload)

    def update_database(self, database_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", f"/databases/{database_id}", json=payload)

    def query_database(
        self,
        database_id: str,
        filter_payload: dict[str, Any] | None = None,
        sorts: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {}
        if filter_payload:
            payload["filter"] = filter_payload
        if sorts:
            payload["sorts"] = sorts
        return self._paginate("POST", f"/databases/{database_id}/query", base_payload=payload)

    # --- Endpoints de Páginas e Blocos ---
    def create_page(self, database_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self.request(
            "POST",
            "/pages",
            json={"parent": {"database_id": database_id}, "properties": properties},
        )

    def update_page(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", f"/pages/{page_id}", json={"properties": properties})

    def delete_page(self, page_id: str) -> dict[str, Any]:
        """Exclui (move para a lixeira) uma página. A API do Notion não faz exclusão definitiva."""
        return self.request("PATCH", f"/pages/{page_id}", json={"archived": True})

    def get_block(self, block_id: str) -> dict[str, Any]:
        return self.request("GET", f"/blocks/{block_id}")

    def get_block_children(self, block_id: str) -> list[dict[str, Any]]:
        return self._paginate("GET", f"/blocks/{block_id}/children", use_query_params=True)
