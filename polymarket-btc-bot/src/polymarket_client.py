"""
Cliente de Polymarket para interactuar con los mercados BTC Up/Down 5-minutos.
Soporta modo simulación (DRY_RUN) y modo producción con py-clob-client.
"""

import asyncio
import datetime
import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from .config import Config
from .logger import get_logger

logger = get_logger("polymarket")


@dataclass
class MarketInfo:
    """Información de un mercado de Polymarket."""

    market_id: str                    # ID interno del mercado
    slug: str                         # Slug del mercado
    question: str                     # Pregunta del mercado
    condition_id: str                 # ID de condición on-chain
    up_token_id: str                  # Token ID para "UP"
    down_token_id: str                # Token ID para "DOWN"
    up_price: float                   # Precio actual del token UP (0-1)
    down_price: float                 # Precio actual del token DOWN (0-1)
    is_active: bool                   # Si el mercado está activo
    end_date_iso: str = ""            # Fecha de cierre del mercado
    orderbook_price_available: bool = True  # False si el precio viene del fallback/default
    extra: dict = field(default_factory=dict)


@dataclass
class OrderResult:
    """Resultado de una operación de orden."""

    success: bool                     # Si la orden fue exitosa
    order_id: str                     # ID de la orden (simulado en DRY_RUN)
    direction: str                    # UP o DOWN
    token_id: str                     # Token comprado
    amount_usdc: float                # Monto en USDC
    token_price: float                # Precio pagado por token
    tokens_bought: float              # Cantidad de tokens comprados
    simulated: bool                   # Si fue simulada
    error: Optional[str] = None       # Error si hubo
    raw_response: Optional[dict] = None


def _get_window_slug(timestamp: Optional[int] = None) -> str:
    """
    Genera el slug del mercado BTC Up/Down para la ventana de 5 minutos actual.

    Args:
        timestamp: Unix timestamp (usa tiempo actual si no se proporciona)

    Returns:
        Slug del mercado, ej: 'btc-updown-5m-1710000000'
    """
    ts = timestamp or int(time.time())
    window_ts = ts - (ts % 300)  # Redondear al múltiplo de 5 minutos anterior
    return f"btc-updown-5m-{window_ts}"


def _get_next_window_slug() -> str:
    """Genera el slug de la PRÓXIMA ventana de 5 minutos."""
    ts = int(time.time())
    window_ts = ts - (ts % 300) + 300  # Siguiente múltiplo de 5 minutos
    return f"btc-updown-5m-{window_ts}"


class PolymarketClient:
    """
    Cliente asíncrono para interactuar con Polymarket.

    En modo DRY_RUN, simula todas las operaciones sin ejecutarlas.
    En modo PRODUCTION, usa py-clob-client para operaciones reales.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._clob_client = None  # Cliente CLOB (inicializado lazy en producción)
        # Cache: window_ts -> MarketInfo (or None if not found), cleared on new window
        self._market_cache: dict[int, Optional["MarketInfo"]] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        """Obtiene o crea la sesión HTTP."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        """Cierra la sesión HTTP."""
        if self._session and not self._session.closed:
            await self._session.close()

    def _init_clob_client(self) -> bool:
        """
        Inicializa el cliente CLOB de py-clob-client.
        Solo aplica en modo producción.

        Returns:
            True si se inicializó correctamente, False en caso contrario
        """
        if self._clob_client is not None:
            return True

        try:
            from py_clob_client.client import ClobClient

            clob_kwargs = {
                "host": self.config.clob_api_base,
                "chain_id": 137,  # Polygon Mainnet
                "key": self.config.private_key,
                "signature_type": self.config.signature_type,
            }

            # Only pass funder if signature_type != 0 or proxy address is set
            if self.config.signature_type != 0 or self.config.polymarket_proxy_address:
                clob_kwargs["funder"] = self.config.polymarket_proxy_address

            self._clob_client = ClobClient(**clob_kwargs)

            # Derivar credenciales desde la clave privada
            self._clob_client.set_api_creds(self._clob_client.create_or_derive_api_creds())
            logger.info("Cliente CLOB inicializado en modo producción")
            return True

        except ImportError:
            logger.error("py-clob-client no está instalado. Instalar con: pip install py-clob-client")
            return False
        except Exception as e:
            logger.error(f"Error inicializando cliente CLOB: {e}", exc_info=True)
            return False

    @staticmethod
    def _is_valid_token_id(token_id: str) -> bool:
        """Returns True if token_id is a 64-character lowercase hex string."""
        return bool(token_id and re.fullmatch(r"[0-9a-fA-F]{64}", token_id))

    async def _fetch_gamma_markets(
        self, session: aiohttp.ClientSession, params: dict, label: str
    ) -> Optional[list]:
        """
        Fetches markets from Gamma API with the given query params.
        Logs the full raw response at DEBUG level.
        Returns the list of markets or None on failure.
        """
        url = f"{self.config.gamma_api_base}/markets"
        logger.debug(f"Gamma API request [{label}]: GET {url} params={params}")
        try:
            async with session.get(url, params=params) as response:
                raw_text = await response.text()
                logger.debug(
                    f"Gamma API raw response [{label}] HTTP {response.status}:\n{raw_text}"
                )
                if response.status != 200:
                    logger.warning(
                        f"Gamma API respondió {response.status} para {label}"
                    )
                    return None
                try:
                    data = json.loads(raw_text)
                except json.JSONDecodeError as exc:
                    logger.error(
                        f"Gamma API respuesta no es JSON válido [{label}]: {exc}\n"
                        f"Body: {raw_text[:500]}"
                    )
                    return None
                markets = data if isinstance(data, list) else data.get("data", [])
                return markets if markets else None
        except aiohttp.ClientError as e:
            logger.error(f"Error HTTP al consultar Gamma API [{label}]: {e}")
            return None

    def _extract_tokens_from_market(self, market: dict) -> tuple[str, str]:
        """
        Parses the tokens array from a Gamma API market object.

        Expected structure:
            market["tokens"] = [
                {"token_id": "abc...", "outcome": "Up"},
                {"token_id": "def...", "outcome": "Down"},
            ]

        Returns (up_token_id, down_token_id) — empty strings if not found.
        """
        up_token_id = ""
        down_token_id = ""

        tokens = market.get("tokens", [])
        if tokens:
            for token in tokens:
                outcome = str(token.get("outcome", "")).lower()
                tid = str(token.get("token_id", ""))
                if "up" in outcome:
                    up_token_id = tid
                elif "down" in outcome:
                    down_token_id = tid

        # Fallback: clobTokenIds in positional order (UP=0, DOWN=1)
        if not up_token_id or not down_token_id:
            clob_ids = market.get("clobTokenIds", [])
            if len(clob_ids) >= 2:
                logger.debug(
                    "tokens array missing/incomplete — falling back to clobTokenIds positional order"
                )
                up_token_id = up_token_id or str(clob_ids[0])
                down_token_id = down_token_id or str(clob_ids[1])

        return up_token_id, down_token_id

    async def _find_market_by_tag(
        self, session: aiohttp.ClientSession, window_close_ts: int
    ) -> Optional[dict]:
        """
        Search for a BTC 5-minute market using tag=btc and filter by question text
        and end_date proximity to window_close_ts (±120 s).

        Returns the raw market dict or None if not found.
        """
        markets = await self._fetch_gamma_markets(
            session,
            {"tag": "btc", "active": "true", "limit": "10"},
            label="tag=btc",
        )
        if not markets:
            return None

        tolerance = 120  # seconds
        for m in markets:
            question = m.get("question", "").lower()
            btc_match = "btc" in question or "bitcoin" in question
            five_match = any(tok in question for tok in ("5m", " 5 ", "five", "5-min", "5 min"))
            if not (btc_match and five_match):
                continue

            end_date = m.get("endDate", "")
            if end_date:
                try:
                    end_dt = datetime.datetime.fromisoformat(
                        end_date.replace("Z", "+00:00")
                    )
                    end_ts = int(end_dt.timestamp())
                    if abs(end_ts - window_close_ts) <= tolerance:
                        return m
                except (ValueError, AttributeError):
                    pass

        return None

    async def get_market(self, slug: Optional[str] = None) -> Optional[MarketInfo]:
        """
        Obtiene información del mercado activo por su slug.

        Discovery order:
          1. Log raw crypto-tag listing (debug only).
          2. Try multiple slug formats for the current window.
          3. If none match, search by tag=btc and filter by question + end_date.
          4. Cache the result for the current window to avoid redundant API calls.

        Args:
            slug: Slug del mercado (usa el de la ventana actual si es None)

        Returns:
            MarketInfo si el mercado existe, None en caso contrario
        """
        ts = int(time.time())
        window_ts = ts - (ts % 300)
        window_close_ts = window_ts + 300

        # --- Cache check ---
        if window_ts in self._market_cache:
            logger.debug(f"Retornando mercado en caché para ventana {window_ts}")
            return self._market_cache[window_ts]

        # Evict stale cache entries (keep only current window)
        stale_keys = [k for k in self._market_cache if k != window_ts]
        for k in stale_keys:
            del self._market_cache[k]

        session = await self._get_session()

        # --- Step 1: Log raw crypto listing for visibility ---
        logger.debug("Solicitando listado raw de Gamma API (tag=crypto) para diagnóstico…")
        await self._fetch_gamma_markets(
            session,
            {"tag": "crypto", "active": "true", "limit": "50"},
            label="debug:tag=crypto",
        )

        # --- Step 2: Try multiple slug formats ---
        slug_candidates: list[str] = []
        if slug:
            slug_candidates.append(slug)
        slug_candidates += [
            f"btc-updown-5m-{window_ts}",
            f"btc-up-or-down-5m-{window_ts}",
            f"bitcoin-up-or-down-5m-{window_ts}",
        ]
        # Deduplicate while preserving order
        seen: set[str] = set()
        slugs_to_try = [s for s in slug_candidates if not (s in seen or seen.add(s))]  # type: ignore[func-returns-value]

        raw_market: Optional[dict] = None
        matched_slug = ""
        for candidate in slugs_to_try:
            markets = await self._fetch_gamma_markets(
                session,
                {"slug": candidate, "active": "true"},
                label=f"slug={candidate}",
            )
            if markets:
                raw_market = markets[0]
                matched_slug = candidate
                logger.info(f"Mercado encontrado con slug={candidate}")
                break

        # --- Step 3: Tag-based fallback ---
        if raw_market is None:
            logger.info(
                f"Ningún slug coincidió para ventana {window_ts} — "
                "buscando por tag=btc con filtro de pregunta y fecha…"
            )
            raw_market = await self._find_market_by_tag(session, window_close_ts)
            if raw_market:
                matched_slug = raw_market.get("slug", "")
                logger.info(
                    f"Mercado encontrado por tag=btc — slug={matched_slug}"
                )

        if raw_market is None:
            logger.warning(
                f"Mercado no encontrado para ventana {window_ts} "
                f"(slugs probados: {slugs_to_try})"
            )
            self._market_cache[window_ts] = None
            return None

        # --- Step 4: Log full market data ---
        logger.info(
            f"Mercado confirmado — slug={raw_market.get('slug')} | "
            f"question={raw_market.get('question')} | "
            f"id={raw_market.get('id')} | "
            f"endDate={raw_market.get('endDate')}"
        )
        logger.debug(
            f"Datos completos del mercado:\n"
            f"{json.dumps(raw_market, indent=2, default=str)}"
        )

        try:
            market = raw_market
            logger.debug(
                f"Mercado seleccionado: id={market.get('id')} "
                f"slug={market.get('slug')} active={market.get('active')}"
            )

            # Extract UP/DOWN token IDs from tokens array
            up_token_id, down_token_id = self._extract_tokens_from_market(market)

            logger.debug(
                f"Token IDs extraídos — UP: '{up_token_id}' | DOWN: '{down_token_id}'"
            )

            # Validate token IDs — abort if either is malformed
            for tid, label in ((up_token_id, "UP"), (down_token_id, "DOWN")):
                if not self._is_valid_token_id(tid):
                    logger.error(
                        f"Token ID inválido para {label}: '{tid}' "
                        f"(se esperaba hex de 64 chars). "
                        f"Respuesta completa del mercado:\n"
                        f"{json.dumps(market, indent=2, default=str)}"
                    )
                    self._market_cache[window_ts] = None
                    return None

            up_price = 0.5
            down_price = 0.5
            orderbook_price_available = False

            prices = await self._get_token_prices(
                up_token_id, down_token_id, gamma_market_data=market
            )
            if prices is not None:
                up_price, down_price, orderbook_price_available = prices

            if not orderbook_price_available:
                logger.warning(
                    "Precio de orderbook no disponible — omitiendo filtro de odds"
                )

            # --- Step 5: Build and cache result ---
            result = MarketInfo(
                market_id=str(market.get("id", "")),
                slug=market.get("slug", matched_slug),
                question=market.get("question", ""),
                condition_id=market.get("conditionId", ""),
                up_token_id=up_token_id,
                down_token_id=down_token_id,
                up_price=up_price,
                down_price=down_price,
                is_active=market.get("active", False),
                end_date_iso=market.get("endDate", ""),
                orderbook_price_available=orderbook_price_available,
                extra=market,
            )
            self._market_cache[window_ts] = result
            return result

        except Exception as e:
            logger.error(f"Error inesperado al procesar mercado: {e}", exc_info=True)
            return None

    async def _get_token_prices(
        self, up_token_id: str, down_token_id: str, gamma_market_data: Optional[dict] = None
    ) -> Optional[tuple[float, float, bool]]:
        """
        Obtiene los precios actuales de los tokens UP y DOWN.

        Intenta primero el CLOB orderbook. Si falla, intenta extraer precios
        del Gamma API (outcomePrices). Retorna None sólo si ambas fuentes fallan.

        Args:
            up_token_id: ID del token UP
            down_token_id: ID del token DOWN
            gamma_market_data: Datos de mercado ya obtenidos del Gamma API (fallback)

        Returns:
            Tupla (up_price, down_price, prices_available) o None si ambas fuentes fallan.
            prices_available=False indica precios del fallback Gamma, no del CLOB.
        """
        up_price = 0.5
        down_price = 0.5
        up_obtained = False
        down_obtained = False

        try:
            session = await self._get_session()
            url = f"{self.config.clob_api_base}/book"

            # Obtener precio del token UP desde el CLOB
            try:
                async with session.get(url, params={"token_id": up_token_id}) as resp:
                    if resp.status == 200:
                        book = await resp.json()
                        asks = book.get("asks", [])
                        if asks:
                            up_price = float(asks[0].get("price", 0.5))
                            up_obtained = True
                        else:
                            logger.warning(
                                f"CLOB orderbook UP ({up_token_id[:12]}…) sin asks — "
                                f"respuesta vacía: {book}"
                            )
                    else:
                        body = await resp.text()
                        logger.warning(
                            f"CLOB orderbook UP falló: HTTP {resp.status} | "
                            f"token={up_token_id[:12]}… | body={body[:200]}"
                        )
            except Exception as e:
                logger.error(f"Error en petición CLOB orderbook UP: {e}", exc_info=True)

            # Obtener precio del token DOWN desde el CLOB
            try:
                async with session.get(url, params={"token_id": down_token_id}) as resp:
                    if resp.status == 200:
                        book = await resp.json()
                        asks = book.get("asks", [])
                        if asks:
                            down_price = float(asks[0].get("price", 0.5))
                            down_obtained = True
                        else:
                            logger.warning(
                                f"CLOB orderbook DOWN ({down_token_id[:12]}…) sin asks — "
                                f"respuesta vacía: {book}"
                            )
                    else:
                        body = await resp.text()
                        logger.warning(
                            f"CLOB orderbook DOWN falló: HTTP {resp.status} | "
                            f"token={down_token_id[:12]}… | body={body[:200]}"
                        )
            except Exception as e:
                logger.error(f"Error en petición CLOB orderbook DOWN: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Error general obteniendo precios del CLOB: {e}", exc_info=True)

        # Si CLOB devolvió precios reales, retornar
        if up_obtained and down_obtained:
            return up_price, down_price, True

        # --- Fallback: intentar obtener precios del Gamma API ---
        if gamma_market_data is not None:
            gamma_prices = self._extract_gamma_prices(gamma_market_data)
            if gamma_prices:
                gup, gdown = gamma_prices
                logger.info(
                    f"Precios obtenidos del Gamma API como fallback — "
                    f"UP: {gup:.4f} | DOWN: {gdown:.4f}"
                )
                return gup, gdown, False

        # Ambas fuentes fallaron
        if not up_obtained or not down_obtained:
            logger.warning(
                "No se pudo obtener precio del CLOB ni del Gamma API — "
                "precio de orderbook no disponible"
            )
        return None

    def _extract_gamma_prices(self, market_data: dict) -> Optional[tuple[float, float]]:
        """
        Extrae precios UP/DOWN de los datos del Gamma API.

        Intenta los campos: outcomePrices, bestBid/bestAsk por token.

        Args:
            market_data: Diccionario de mercado retornado por el Gamma API

        Returns:
            Tupla (up_price, down_price) o None si no se pueden extraer
        """
        try:
            # Campo outcomePrices: lista de strings ["0.55", "0.45"]
            outcome_prices = market_data.get("outcomePrices")
            if outcome_prices and len(outcome_prices) >= 2:
                up = float(outcome_prices[0])
                down = float(outcome_prices[1])
                if 0.0 < up < 1.0 and 0.0 < down < 1.0:
                    return up, down

            # Campo tokens con price embebido
            tokens = market_data.get("tokens", [])
            if len(tokens) >= 2:
                up = float(tokens[0].get("price", 0.0))
                down = float(tokens[1].get("price", 0.0))
                if 0.0 < up < 1.0 and 0.0 < down < 1.0:
                    return up, down

        except (ValueError, TypeError, KeyError) as e:
            logger.debug(f"Error extrayendo precios del Gamma API: {e}")

        return None

    async def place_order(
        self,
        direction: str,
        token_id: str,
        token_price: float,
        amount_usdc: float,
    ) -> OrderResult:
        """
        Coloca una orden de compra de tokens.

        En DRY_RUN: simula la orden y la loguea.
        En PRODUCTION: ejecuta la orden real via py-clob-client.

        Args:
            direction: "UP" o "DOWN"
            token_id: ID del token a comprar
            token_price: Precio actual del token (0-1)
            amount_usdc: Monto en USDC a invertir

        Returns:
            OrderResult con el resultado de la operación
        """
        # Calcular tokens a comprar
        if token_price <= 0:
            return OrderResult(
                success=False,
                order_id="",
                direction=direction,
                token_id=token_id,
                amount_usdc=amount_usdc,
                token_price=token_price,
                tokens_bought=0.0,
                simulated=True,
                error="Precio de token inválido (0 o negativo)",
            )

        tokens_to_buy = amount_usdc / token_price

        # --- MODO SIMULACIÓN ---
        if self.config.dry_run or not self.config.production:
            simulated_id = f"SIM-{direction}-{int(time.time())}"
            logger.info(
                f"[SIMULACIÓN] Orden {direction}: {tokens_to_buy:.4f} tokens @ "
                f"${token_price:.4f} = ${amount_usdc:.2f} USDC | ID: {simulated_id}"
            )
            return OrderResult(
                success=True,
                order_id=simulated_id,
                direction=direction,
                token_id=token_id,
                amount_usdc=amount_usdc,
                token_price=token_price,
                tokens_bought=tokens_to_buy,
                simulated=True,
            )

        # --- MODO PRODUCCIÓN ---
        if not self._init_clob_client():
            return OrderResult(
                success=False,
                order_id="",
                direction=direction,
                token_id=token_id,
                amount_usdc=amount_usdc,
                token_price=token_price,
                tokens_bought=0.0,
                simulated=False,
                error="No se pudo inicializar el cliente CLOB",
            )

        try:
            # Ejecutar en thread pool para no bloquear el event loop
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                self._place_order_sync,
                token_id,
                token_price,
                tokens_to_buy,
            )

            if result.get("success"):
                order_id = result.get("orderID", result.get("id", "unknown"))
                logger.info(
                    f"[PRODUCCIÓN] Orden ejecutada: {direction} {tokens_to_buy:.4f} tokens "
                    f"@ ${token_price:.4f} | ID: {order_id}"
                )
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    direction=direction,
                    token_id=token_id,
                    amount_usdc=amount_usdc,
                    token_price=token_price,
                    tokens_bought=tokens_to_buy,
                    simulated=False,
                    raw_response=result,
                )
            else:
                error_msg = result.get("errorMsg", "Error desconocido del CLOB")
                logger.error(f"Error al colocar orden en CLOB: {error_msg}")
                return OrderResult(
                    success=False,
                    order_id="",
                    direction=direction,
                    token_id=token_id,
                    amount_usdc=amount_usdc,
                    token_price=token_price,
                    tokens_bought=0.0,
                    simulated=False,
                    error=error_msg,
                    raw_response=result,
                )

        except Exception as e:
            logger.error(f"Excepción al colocar orden: {e}", exc_info=True)
            return OrderResult(
                success=False,
                order_id="",
                direction=direction,
                token_id=token_id,
                amount_usdc=amount_usdc,
                token_price=token_price,
                tokens_bought=0.0,
                simulated=False,
                error=str(e),
            )

    def _place_order_sync(
        self, token_id: str, price: float, size: float
    ) -> dict:
        """
        Coloca una orden de mercado de forma síncrona via py-clob-client.
        Este método se ejecuta en un thread separado.

        Args:
            token_id: ID del token
            price: Precio límite de la orden
            size: Cantidad de tokens

        Returns:
            Respuesta del cliente CLOB
        """
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side="BUY",
            )

            # Crear y firmar la orden límite GTC
            signed_order = self._clob_client.create_order(order_args)
            response = self._clob_client.post_order(signed_order, OrderType.GTC)
            return response

        except Exception as e:
            logger.error(f"Error en _place_order_sync: {e}")
            return {"success": False, "errorMsg": str(e)}

    def get_current_window_slug(self) -> str:
        """Retorna el slug de la ventana de 5 minutos actual."""
        return _get_window_slug()

    def get_next_window_slug(self) -> str:
        """Retorna el slug de la próxima ventana de 5 minutos."""
        return _get_next_window_slug()

    def seconds_until_next_window(self) -> int:
        """Calcula cuántos segundos faltan para la próxima ventana."""
        ts = int(time.time())
        return 300 - (ts % 300)

    def seconds_in_current_window(self) -> int:
        """Calcula cuántos segundos han pasado en la ventana actual."""
        ts = int(time.time())
        return ts % 300
