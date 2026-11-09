"""Cliente HTTP para a Bot API do Telegram."""

from __future__ import annotations

import logging
from pathlib import Path
import time
from typing import Any
import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramError(RuntimeError):
    """Exceção levantada em falhas da API do Telegram."""


class TelegramClient:
    """Cliente para envio de mensagens e leitura de atualizações do Telegram."""

    def __init__(
        self,
        token: str,
        chat_id: str | int | None = None,
        timeout_s: int = 30,
        max_retries: int = 3,
        backoff_s: float = 1.5,
    ) -> None:
        if not token or not token.strip():
            raise ValueError("Token do bot do Telegram não pode ser vazio.")
        self._token = token.strip()
        self._default_chat_id = str(chat_id).strip() if chat_id else None
        self._timeout_s = timeout_s
        self._max_retries = max(1, max_retries)
        self._backoff_s = backoff_s
        self._base_url = f"{TELEGRAM_API_BASE}/bot{self._token}"
        self._file_base_url = f"{TELEGRAM_API_BASE}/file/bot{self._token}"
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> TelegramClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        kwargs.setdefault("timeout", self._timeout_s)
        ultimo_erro: Exception | None = None

        for tentativa in range(self._max_retries):
            try:
                resp = self._session.request(method, url, **kwargs)
                if resp.status_code in (429, 500, 502, 503, 504) and tentativa + 1 < self._max_retries:
                    espera = self._backoff_s * (2 ** tentativa)
                    logger.warning(
                        "HTTP %s ao chamar %s (tentativa %d/%d). Aguardando %.1fs...",
                        resp.status_code,
                        endpoint,
                        tentativa + 1,
                        self._max_retries,
                        espera,
                    )
                    time.sleep(espera)
                    continue
                break
            except requests.RequestException as exc:
                ultimo_erro = exc
                if tentativa + 1 < self._max_retries:
                    espera = self._backoff_s * (2 ** tentativa)
                    logger.warning(
                        "Oscilação de rede ao chamar %s (tentativa %d/%d): %s. Aguardando %.1fs...",
                        endpoint,
                        tentativa + 1,
                        self._max_retries,
                        exc,
                        espera,
                    )
                    time.sleep(espera)
                else:
                    raise TelegramError(f"Falha de rede ao chamar {endpoint}: {exc}") from exc
        else:
            raise TelegramError(f"Falha de rede ao chamar {endpoint}: {ultimo_erro}")

        try:
            dados = resp.json()
        except ValueError as exc:
            raise TelegramError(f"Resposta inválida de {endpoint} (HTTP {resp.status_code})") from exc

        if not dados.get("ok"):
            descricao = dados.get("description", "Erro desconhecido")
            raise TelegramError(f"Erro na API do Telegram ({endpoint}): {descricao}")

        return dados

    def get_me(self) -> dict[str, Any]:
        """Retorna os dados do bot autenticado."""
        return self._request("GET", "getMe").get("result", {})

    def enviar_mensagem(
        self,
        texto: str,
        chat_id: str | int | None = None,
        parse_mode: str | None = "Markdown",
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Envia uma mensagem de texto para o chat especificado ou padrão."""
        alvo = str(chat_id).strip() if chat_id else self._default_chat_id
        if not alvo:
            raise ValueError("Chat ID do Telegram não informado.")

        payload: dict[str, Any] = {
            "chat_id": alvo,
            "text": texto,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup

        return self._request("POST", "sendMessage", json=payload).get("result", {})

    def responder_callback_query(
        self,
        callback_query_id: str,
        texto: str | None = None,
        show_alert: bool = False,
    ) -> dict[str, Any]:
        """Confirma o recebimento de um clique de botão inline no Telegram."""
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if texto:
            payload["text"] = texto
        try:
            return self._request("POST", "answerCallbackQuery", json=payload).get("result", {})
        except Exception as exc:
            logger.warning("Falha ao responder callback query %s: %s", callback_query_id, exc)
            return {}

    def obter_atualizacoes(
        self,
        offset: int | None = None,
        limit: int = 100,
        timeout: int = 0,
    ) -> list[dict[str, Any]]:
        """Busca mensagens e comandos pendentes via `getUpdates`."""
        params: dict[str, Any] = {"limit": limit, "timeout": timeout}
        if offset is not None:
            params["offset"] = offset

        return self._request("GET", "getUpdates", params=params).get("result", [])

    def obter_caminho_arquivo(self, file_id: str) -> str:
        """Obtém o file_path relativo do Telegram para download."""
        dados = self._request("GET", "getFile", params={"file_id": file_id})
        return dados.get("result", {}).get("file_path", "")

    def baixar_arquivo(self, file_id: str, destino: Path) -> Path:
        """Baixa uma foto/documento enviado pelo usuário para um caminho local."""
        caminho_rel = self.obter_caminho_arquivo(file_id)
        if not caminho_rel:
            raise TelegramError(f"Não foi possível obter caminho do arquivo {file_id}")

        url = f"{self._file_base_url}/{caminho_rel}"
        try:
            resp = self._session.get(url, timeout=self._timeout_s)
            resp.raise_for_status()
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(resp.content)
            return destino
        except Exception as exc:
            raise TelegramError(f"Falha ao baixar arquivo {file_id}: {exc}") from exc
