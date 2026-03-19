"""
Módulo de feed de precios de BTC en tiempo real via WebSocket de Binance.
Mantiene un historial de velas de 1 minuto y calcula métricas de la ventana actual.
"""

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from .logger import get_logger

logger = get_logger("price_feed")

# URL del WebSocket de Binance para velas de 1 minuto de BTC/USDT
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@kline_1m"

# Número de velas a mantener en el historial
MAX_CANDLES = 60


@dataclass
class Candle:
    """Representa una vela (candlestick) de 1 minuto."""

    timestamp: int       # Tiempo de apertura en ms
    open: float          # Precio de apertura
    high: float          # Precio máximo
    low: float           # Precio mínimo
    close: float         # Precio de cierre
    volume: float        # Volumen negociado
    is_closed: bool      # Si la vela está cerrada (completa)

    @property
    def mid(self) -> float:
        """Precio medio de la vela."""
        return (self.high + self.low) / 2

    @property
    def typical_price(self) -> float:
        """Precio típico (HLC/3), usado para VWAP."""
        return (self.high + self.low + self.close) / 3


class PriceFeed:
    """
    Feed de precios en tiempo real de BTC/USDT desde Binance.

    Mantiene:
    - Una deque de las últimas MAX_CANDLES velas cerradas
    - La vela actual en formación
    - Precio actual, precio de apertura de la ventana y delta
    """

    def __init__(self, ws_url: str = BINANCE_WS_URL) -> None:
        self.ws_url = ws_url

        # Historial de velas cerradas (hasta 60)
        self.candles: Deque[Candle] = deque(maxlen=MAX_CANDLES)

        # Vela actual en formación
        self.current_candle: Optional[Candle] = None

        # Precio del último tick recibido
        self.last_price: float = 0.0

        # Precio de apertura de la ventana de 5 minutos actual
        self.window_open_price: float = 0.0

        # Porcentaje de cambio desde la apertura de la ventana
        self.window_delta_pct: float = 0.0

        # Estado de conexión
        self.is_connected: bool = False
        self._running: bool = False

        # Callbacks registrados para nuevas velas cerradas
        self._on_candle_callbacks: list[Callable[[Candle], None]] = []

        # Control de reconexión
        self._reconnect_delay: float = 1.0
        self._max_reconnect_delay: float = 60.0

    def register_candle_callback(self, callback: Callable[[Candle], None]) -> None:
        """Registra un callback que se llama cuando se cierra una vela."""
        self._on_candle_callbacks.append(callback)

    def set_window_open_price(self, price: float) -> None:
        """
        Establece el precio de apertura de la ventana de 5 minutos.
        Llamado al inicio de cada nueva ventana.
        """
        self.window_open_price = price
        self.window_delta_pct = 0.0
        logger.info(f"Ventana abierta en ${price:,.2f}")

    def _update_window_delta(self) -> None:
        """Actualiza el delta porcentual de la ventana actual."""
        if self.window_open_price > 0 and self.last_price > 0:
            self.window_delta_pct = (
                (self.last_price - self.window_open_price) / self.window_open_price * 100
            )

    def _process_kline_message(self, data: dict) -> None:
        """
        Procesa un mensaje de kline (vela) de Binance.

        Args:
            data: Datos del mensaje WebSocket de Binance
        """
        try:
            kline = data.get("k", {})

            candle = Candle(
                timestamp=int(kline["t"]),
                open=float(kline["o"]),
                high=float(kline["h"]),
                low=float(kline["l"]),
                close=float(kline["c"]),
                volume=float(kline["v"]),
                is_closed=bool(kline["x"]),
            )

            # Actualizar precio actual
            self.last_price = candle.close
            self.current_candle = candle
            self._update_window_delta()

            # Si la vela está cerrada, agregarla al historial
            if candle.is_closed:
                self.candles.append(candle)
                logger.debug(
                    f"Vela cerrada: O={candle.open:.2f} H={candle.high:.2f} "
                    f"L={candle.low:.2f} C={candle.close:.2f} V={candle.volume:.2f}"
                )

                # Notificar a los callbacks registrados
                for callback in self._on_candle_callbacks:
                    try:
                        callback(candle)
                    except Exception as e:
                        logger.error(f"Error en callback de vela: {e}")

        except (KeyError, ValueError, TypeError) as e:
            logger.error(f"Error procesando mensaje kline: {e} | Data: {data}")

    async def _connect_and_listen(self) -> None:
        """
        Se conecta al WebSocket de Binance y escucha mensajes.
        Lanza excepción en caso de error de conexión.
        """
        logger.info(f"Conectando a Binance WebSocket: {self.ws_url}")

        async with websockets.connect(
            self.ws_url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self.is_connected = True
            self._reconnect_delay = 1.0  # Resetear delay en conexión exitosa
            logger.info("Conectado a Binance WebSocket exitosamente")

            async for message in ws:
                if not self._running:
                    break

                try:
                    data = json.loads(message)
                    if data.get("e") == "kline":
                        self._process_kline_message(data)
                except json.JSONDecodeError as e:
                    logger.error(f"Error decodificando mensaje JSON: {e}")
                except Exception as e:
                    logger.error(f"Error procesando mensaje: {e}", exc_info=True)

    async def run(self) -> None:
        """
        Ejecuta el feed de precios con reconexión automática exponencial.
        Este método corre indefinidamente hasta que se llame a stop().
        """
        self._running = True
        logger.info("Iniciando feed de precios BTC/USDT")

        while self._running:
            try:
                self.is_connected = False
                await self._connect_and_listen()

            except ConnectionClosed as e:
                logger.warning(f"Conexión WebSocket cerrada: {e}")
            except WebSocketException as e:
                logger.error(f"Error WebSocket: {e}")
            except Exception as e:
                logger.error(f"Error inesperado en feed: {e}", exc_info=True)
            finally:
                self.is_connected = False

            if not self._running:
                break

            # Reconexión con backoff exponencial
            logger.info(
                f"Reconectando en {self._reconnect_delay:.1f}s... "
                f"(historial: {len(self.candles)} velas)"
            )
            await asyncio.sleep(self._reconnect_delay)

            # Incrementar delay de reconexión (máximo 60 segundos)
            self._reconnect_delay = min(
                self._reconnect_delay * 2, self._max_reconnect_delay
            )

    async def stop(self) -> None:
        """Detiene el feed de precios."""
        self._running = False
        self.is_connected = False
        logger.info("Feed de precios detenido")

    def get_closes(self) -> list[float]:
        """Retorna lista de precios de cierre de las velas en historial."""
        return [c.close for c in self.candles]

    def get_volumes(self) -> list[float]:
        """Retorna lista de volúmenes de las velas en historial."""
        return [c.volume for c in self.candles]

    def get_highs(self) -> list[float]:
        """Retorna lista de precios máximos."""
        return [c.high for c in self.candles]

    def get_lows(self) -> list[float]:
        """Retorna lista de precios mínimos."""
        return [c.low for c in self.candles]

    def has_enough_data(self, min_candles: int = 26) -> bool:
        """
        Verifica si hay suficientes velas para calcular indicadores.

        Args:
            min_candles: Número mínimo de velas requeridas (MACD necesita 26)
        """
        return len(self.candles) >= min_candles

    @property
    def status(self) -> dict:
        """Retorna resumen del estado actual del feed."""
        return {
            "connected": self.is_connected,
            "last_price": self.last_price,
            "candles_count": len(self.candles),
            "window_open_price": self.window_open_price,
            "window_delta_pct": self.window_delta_pct,
        }
