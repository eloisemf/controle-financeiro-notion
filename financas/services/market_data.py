"""Serviço de captura de cotações de mercado (B3, Tesouro Nacional e Índices)."""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
import io
import logging
import re
from types import TracebackType

import requests

from financas.domain.investments import CotacaoMercado

logger = logging.getLogger(__name__)

TESOURO_CSV_URL = (
    "https://www.tesourotransparente.gov.br/ckan/dataset/"
    "df56aa42-484a-4a59-8184-7676580c81e3/resource/"
    "796d2059-14e9-44e3-80c9-2d9e30b405c1/download/precotaxatesourodireto.csv"
)


class MarketDataService:
    """Cliente resiliente para APIs públicas de cotações do mercado financeiro brasileiro."""

    def __init__(self, timeout_s: int = 15) -> None:
        self._timeout_s = timeout_s
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "ControleFinanceiro/2.0"})
        self._tesouro_cache: dict[str, CotacaoMercado] | None = None

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> MarketDataService:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    @staticmethod
    def normalizar_ticker_b3(ticker: str) -> str:
        """Remove apenas o sufixo fracionário 'F' de tickers da B3 (ex: 'PETR4F' -> 'PETR4', 'FLRY3F' -> 'FLRY3')."""
        limpo = ticker.upper().strip()
        return re.sub(r"^([A-Z]{4}\d{1,2})F$", r"\1", limpo)

    def obter_cotacao_acao(self, ticker: str) -> CotacaoMercado | None:
        """Obtém cotação de ação na B3 (ex: PETR4 / PETR4F) via Brapi ou Yahoo Finance."""
        clean_ticker = self.normalizar_ticker_b3(ticker)

        # 1. Tentativa via Brapi (API gratuita para B3)
        try:
            url = f"https://brapi.dev/api/quote/{clean_ticker}"
            resp = self._session.get(url, timeout=self._timeout_s)
            if resp.status_code == 200:
                dados = resp.json()
                results = dados.get("results", [])
                if results and "regularMarketPrice" in results[0]:
                    preco = Decimal(str(results[0]["regularMarketPrice"]))
                    data_ref = (
                        results[0].get("regularMarketTime", "")[:10]
                        or date.today().isoformat()
                    )
                    logger.info("Cotação de %s obtida via Brapi: R$ %s", clean_ticker, preco)
                    return CotacaoMercado(
                        identificador=ticker,
                        preco_unitario=preco,
                        data_referencia=data_ref,
                        fonte="Brapi / B3",
                    )
        except (requests.RequestException, ValueError, KeyError) as e:
            logger.warning("Falha ao consultar Brapi para %s: %s", clean_ticker, e)

        # 2. Tentativa via Yahoo Finance API
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{clean_ticker}.SA"
            resp = self._session.get(url, timeout=self._timeout_s)
            if resp.status_code == 200:
                dados = resp.json()
                meta = dados.get("chart", {}).get("result", [{}])[0].get("meta", {})
                preco_val = meta.get("regularMarketPrice")
                if preco_val:
                    preco = Decimal(str(round(preco_val, 2)))
                    logger.info("Cotação de %s obtida via Yahoo Finance: R$ %s", clean_ticker, preco)
                    return CotacaoMercado(
                        identificador=ticker,
                        preco_unitario=preco,
                        data_referencia=date.today().isoformat(),
                        fonte="Yahoo Finance / B3",
                    )
        except (requests.RequestException, ValueError, KeyError, IndexError) as e:
            logger.warning("Falha ao consultar Yahoo Finance para %s: %s", clean_ticker, e)

        return None

    def _parse_dma(self, date_str: str) -> tuple[int, int, int]:
        """Converte 'DD/MM/AAAA' em tupla comparável (AAAA, MM, DD)."""
        parts = date_str.strip().split("/")
        if len(parts) == 3:
            try:
                return (int(parts[2]), int(parts[1]), int(parts[0]))
            except ValueError:
                pass
        return (0, 0, 0)

    def _carregar_mapa_tesouro(self) -> dict[str, CotacaoMercado]:
        """Baixa o CSV oficial do Tesouro Transparente uma única vez e armazena o PU mais recente de cada título."""
        if self._tesouro_cache is not None:
            return self._tesouro_cache

        mapa: dict[str, CotacaoMercado] = {}
        ultimas_datas: dict[str, tuple[int, int, int]] = {}

        try:
            logger.info("Baixando tabela de preços do Tesouro Transparente...")
            resp = self._session.get(TESOURO_CSV_URL, stream=True, timeout=self._timeout_s)
            if resp.status_code != 200:
                logger.warning("HTTP %d ao acessar Tesouro Transparente.", resp.status_code)
                self._tesouro_cache = {}
                return self._tesouro_cache

            reader = csv.DictReader(
                io.TextIOWrapper(resp.raw, encoding="latin1"),
                delimiter=";",
            )

            for row in reader:
                tipo = row.get("Tipo Titulo", "").strip()
                venc = row.get("Data Vencimento", "").strip()
                if not tipo or not venc:
                    continue
                row_key = f"{tipo.lower()} {venc}"
                dt_base = row.get("Data Base", "").strip()
                dt_tuple = self._parse_dma(dt_base)

                if row_key not in ultimas_datas or dt_tuple > ultimas_datas[row_key]:
                    pu_str = row.get("PU Base Manha") or row.get("PU Venda Manha") or "0"
                    pu_clean = pu_str.replace(".", "").replace(",", ".")
                    try:
                        preco = Decimal(pu_clean)
                    except ValueError:
                        continue
                    ultimas_datas[row_key] = dt_tuple
                    mapa[row_key] = CotacaoMercado(
                        identificador=f"{tipo} {venc}",
                        preco_unitario=preco,
                        data_referencia=dt_base,
                        fonte="Tesouro Transparente Oficial",
                    )
        except (requests.RequestException, OSError) as e:
            logger.warning("Falha ao carregar CSV do Tesouro Direto: %s", e)

        self._tesouro_cache = mapa
        return self._tesouro_cache

    def obter_pu_tesouro_direto(self, titulo: str, vencimento: str) -> CotacaoMercado | None:
        """Obtém o Preço Unitário (PU) oficial de compra/venda do Tesouro Transparente."""
        target_key = f"{titulo.strip().lower()} {vencimento.strip()}"
        mapa = self._carregar_mapa_tesouro()
        if target_key in mapa:
            cotacao = mapa[target_key]
            logger.info(
                "PU Tesouro '%s %s' obtido: R$ %s (Data Base: %s)",
                titulo,
                vencimento,
                cotacao.preco_unitario,
                cotacao.data_referencia,
            )
            return cotacao

        # Fallback por ano de vencimento quando apenas o ano (ex: "2031") foi extraído do nome do ativo
        if len(vencimento.strip()) == 4 and vencimento.strip().isdigit():
            ano_alvo = vencimento.strip()
            prefixo = titulo.strip().lower()
            for chave, cotacao in mapa.items():
                if chave.startswith(prefixo) and chave.endswith(f"/{ano_alvo}"):
                    return cotacao
        return None
