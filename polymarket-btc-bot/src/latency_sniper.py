"""
Módulo de Latency Sniper para el bot de trading.
Detecta movimientos bruscos de precio de BTC en los últimos 10 segundos
y genera señales de entrada rápida cuando hay suficiente tiempo restante
en la ventana actual.
"""

from collections import deque
from typing import Optional

from .config import Config
from .logger import get_logger
from .price_feed import PriceFeed
from .strategy import Signal

logger = get_logger("sniper")

# Número de muestras de precio a mantener (1 por segundo = 10 segundos)
PRICE_HISTORY_SIZE = 10


class LatencySniper:
    """
    Estrategia de sniper por latencia: detecta movimientos bruscos de precio.

    Monitorea el precio de BTC cada segundo durante toda la ventana.
    Solo genera señal de entrada en los últimos segundos de la ventana
    (entre SNIPER_ENTRY_WINDOW_MAX y SNIPER_ENTRY_WINDOW_MIN segundos antes
    del cierre). Esto captura el edge de entrar cuando la dirección del precio
    ya está prácticamente definida y no hay tiempo para una reversión.

    El tipo de trade generado es "SNIPER" para diferenciarlo del
    tipo "DIRECTIONAL" de la estrategia técnica.
    """

    def __init__(self, feed: PriceFeed, config: Config) -> None:
        self.feed = feed
        self.config = config

        # Historial de precios: deque de tamaño fijo (1 precio por segundo)
        self._price_history: deque[float] = deque(maxlen=PRICE_HISTORY_SIZE)

        # Flag para evitar múltiples intentos por ventana
        self._attempted_this_window: bool = False

    def reset_window(self) -> None:
        """Resetea el estado al inicio de una nueva ventana."""
        self._attempted_this_window = False

    def update(self) -> None:
        """
        Registra el precio actual. Debe llamarse cada segundo en el bucle principal.
        """
        if self.feed.last_price > 0:
            self._price_history.append(self.feed.last_price)

    def check_signal(self, seconds_until_close: int) -> Optional[Signal]:
        """
        Evalúa si hay un movimiento brusco que justifique una entrada sniper.

        Args:
            seconds_until_close: Segundos restantes en la ventana actual.

        Returns:
            Signal con dirección y confianza si se detecta movimiento, None si no.
        """
        # No actuar si ya intentamos en esta ventana
        if self._attempted_this_window:
            return None

        # Solo actuar en la ventana de entrada final: entre T-MAX y T-MIN segundos
        if not (
            self.config.sniper_entry_window_min
            <= seconds_until_close
            <= self.config.sniper_entry_window_max
        ):
            return None

        # Necesitamos al menos 10 muestras para evaluar el movimiento
        if len(self._price_history) < PRICE_HISTORY_SIZE:
            return None

        oldest_price = self._price_history[0]
        current_price = self._price_history[-1]

        if oldest_price <= 0:
            return None

        # Calcular movimiento porcentual en los últimos 10 segundos
        move_pct = (current_price - oldest_price) / oldest_price * 100
        threshold = self.config.sniper_threshold

        if abs(move_pct) <= threshold:
            return None

        # Movimiento detectado — generar señal
        direction = "UP" if move_pct > 0 else "DOWN"

        # Confianza proporcional al tamaño del movimiento relativo al umbral,
        # con un techo razonable. Movimiento del doble del umbral → 0.80 confianza.
        confidence = min(0.95, abs(move_pct) / threshold * 0.5)

        logger.info(
            f"[SNIPER] Movimiento detectado: {move_pct:+.4f}% en 10s "
            f"(umbral: {threshold}%) | Dirección: {direction} | "
            f"Confianza: {confidence:.1%} | Cierre en: {seconds_until_close}s"
        )

        self._attempted_this_window = True

        return Signal(
            direction=direction,
            confidence=confidence,
            breakdown={"sniper_move_10s_pct": round(move_pct, 4)},
            raw_score=move_pct / 100,
            last_price=current_price,
            candles_used=0,
        )
