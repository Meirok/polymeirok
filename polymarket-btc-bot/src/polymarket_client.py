"""
Cliente de Polymarket para interactuar con los mercados BTC Up/Down 5-minutos.
Soporta modo simulación (DRY_RUN) y modo producción con py-clob-client.
"""

import asyncio
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
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key="",
                api_secret="",
                api_passphrase="",
            )

            self._clob_client = ClobClient(
                host=self.config.clob_api_base,
                chain_id=137,  # Polygon Mainnet
                private_key=self.config.private_key,
                creds=creds,
                signature_type=self.config.signature_type,
                funder=self.config.polymarket_proxy_address,
            )

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

    async def get_market(self, slug: Optional[str] = None) -> Optional[MarketInfo]:
        """
        Obtiene información del mercado activo por su slug.

        Args:
            slug: Slug del mercado (usa el de la ventana actual si es None)

        Returns:
            MarketInfo si el mercado existe, None en caso contrario
        """
        market_slug = slug or _get_window_slug()

        try:
            session = await self._get_session()
            url = f"{self.config.gamma_api_base}/markets"
            params = {"slug": market_slug}

            logger.debug(f"Buscando mercado: {market_slug}")

            async with session.get(url, params=params) as response:
                if response.status != 200:
                    logger.warning(
                        f"API Gamma respondió {response.status} para slug={market_slug}"
                    )
                    return None

                data = await response.json()

                # La API retorna una lista de mercados
                markets = data if isinstance(data, list) else data.get("data", [])

                if not markets:
                    logger.debug(f"No se encontró mercado para slug: {market_slug}")
                    return None

                market = markets[0]

                # Extraer tokens (outcomes) del mercado
                tokens = market.get("tokens", []) or market.get("clobTokenIds", [])

                # Intentar identificar UP y DOWN tokens
                up_token_id = ""
                down_token_id = ""
                up_price = 0.5
                down_price = 0.5

                outcomes = market.get("outcomes", ["UP", "DOWN"])
                clob_token_ids = market.get("clobTokenIds", [])

                if len(clob_token_ids) >= 2:
                    # Asumir que el orden es UP=0, DOWN=1
                    up_token_id = str(clob_token_ids[0])
                    down_token_id = str(clob_token_ids[1])

                # Intentar obtener precios actuales del orderbook
                if up_token_id:
                    prices = await self._get_token_prices(up_token_id, down_token_id)
                    if prices:
                        up_price, down_price = prices

                return MarketInfo(
                    market_id=str(market.get("id", "")),
                    slug=market_slug,
                    question=market.get("question", ""),
                    condition_id=market.get("conditionId", ""),
                    up_token_id=up_token_id,
                    down_token_id=down_token_id,
                    up_price=up_price,
                    down_price=down_price,
                    is_active=market.get("active", False),
                    end_date_iso=market.get("endDate", ""),
                    extra=market,
                )

        except aiohttp.ClientError as e:
            logger.error(f"Error HTTP al obtener mercado {market_slug}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error inesperado al obtener mercado: {e}", exc_info=True)
            return None

    async def _get_token_prices(
        self, up_token_id: str, down_token_id: str
    ) -> Optional[tuple[float, float]]:
        """
        Obtiene los precios actuales de los tokens UP y DOWN desde el CLOB.

        Args:
            up_token_id: ID del token UP
            down_token_id: ID del token DOWN

        Returns:
            Tupla (up_price, down_price) o None si hay error
        """
        try:
            session = await self._get_session()
            url = f"{self.config.clob_api_base}/book"

            # Obtener precio del token UP
            up_price = 0.5
            down_price = 0.5

            async with session.get(url, params={"token_id": up_token_id}) as resp:
                if resp.status == 200:
                    book = await resp.json()
                    # El mejor ask (precio de compra más barato disponible)
                    asks = book.get("asks", [])
                    if asks:
                        up_price = float(asks[0].get("price", 0.5))

            async with session.get(url, params={"token_id": down_token_id}) as resp:
                if resp.status == 200:
                    book = await resp.json()
                    asks = book.get("asks", [])
                    if asks:
                        down_price = float(asks[0].get("price", 0.5))

            return up_price, down_price

        except Exception as e:
            logger.debug(f"Error obteniendo precios del CLOB: {e}")
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
            from py_clob_client.clob_types import MarketOrderArgs, OrderType

            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=size,
            )

            # Crear y firmar la orden de mercado
            signed_order = self._clob_client.create_market_order(order_args)
            response = self._clob_client.post_order(signed_order, OrderType.FOK)
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
