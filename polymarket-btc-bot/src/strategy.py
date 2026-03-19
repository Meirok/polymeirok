"""
Módulo de estrategia de trading.
Analiza el feed de precios usando 7 indicadores técnicos ponderados
para generar señales de compra (UP/DOWN) con nivel de confianza.
"""

from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np

from .logger import get_logger
from .price_feed import PriceFeed

logger = get_logger("strategy")

Direction = Literal["UP", "DOWN", "SKIP"]


@dataclass
class Signal:
    """Señal de trading generada por la estrategia."""

    direction: Direction           # Dirección: UP, DOWN o SKIP
    confidence: float              # Confianza 0.0 a 1.0
    breakdown: dict[str, float]    # Puntuación por indicador
    raw_score: float               # Puntuación bruta antes de normalizar
    last_price: float              # Precio al momento de la señal
    candles_used: int              # Número de velas usadas

    def __str__(self) -> str:
        bd_str = ", ".join(
            f"{k}={v:+.3f}" for k, v in self.breakdown.items()
        )
        return (
            f"Signal({self.direction}, confianza={self.confidence:.1%}, "
            f"precio=${self.last_price:,.2f}, [{bd_str}])"
        )


# ---------------------------------------------------------------------------
# Funciones de cálculo de indicadores técnicos
# ---------------------------------------------------------------------------

def _calc_rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """
    Calcula el RSI (Relative Strength Index).

    Args:
        closes: Lista de precios de cierre
        period: Período del RSI (default 14)

    Returns:
        Valor RSI entre 0 y 100, o None si no hay suficientes datos
    """
    if len(closes) < period + 1:
        return None

    prices = np.array(closes[-(period + 1):], dtype=float)
    deltas = np.diff(prices)

    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    # Promedio simple inicial
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    # Suavizado de Wilder para los períodos restantes
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _calc_ema(prices: list[float], period: int) -> Optional[float]:
    """
    Calcula la EMA (Exponential Moving Average).

    Args:
        prices: Lista de precios
        period: Período de la EMA

    Returns:
        Último valor de la EMA, o None si no hay suficientes datos
    """
    if len(prices) < period:
        return None

    arr = np.array(prices, dtype=float)
    k = 2.0 / (period + 1.0)

    ema = arr[0]
    for price in arr[1:]:
        ema = price * k + ema * (1 - k)

    return float(ema)


def _calc_macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[Optional[float], Optional[float]]:
    """
    Calcula el MACD y su línea de señal.

    Args:
        closes: Lista de precios de cierre
        fast: Período EMA rápida (default 12)
        slow: Período EMA lenta (default 26)
        signal: Período EMA de la señal (default 9)

    Returns:
        Tupla (macd_line, signal_line) o (None, None) si no hay datos
    """
    if len(closes) < slow + signal:
        return None, None

    # Calcular EMAs para la línea MACD
    macd_values: list[float] = []
    for i in range(slow - 1, len(closes)):
        window = closes[: i + 1]
        ema_fast = _calc_ema(window, fast)
        ema_slow = _calc_ema(window, slow)
        if ema_fast is not None and ema_slow is not None:
            macd_values.append(ema_fast - ema_slow)

    if len(macd_values) < signal:
        return None, None

    # Línea MACD actual
    macd_line = macd_values[-1]

    # Línea de señal (EMA del MACD)
    signal_line = _calc_ema(macd_values, signal)

    return macd_line, signal_line


def _calc_bollinger_bands(
    closes: list[float],
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Calcula las Bandas de Bollinger.

    Args:
        closes: Lista de precios de cierre
        period: Período de la SMA (default 20)
        std_dev: Multiplicador de desviación estándar (default 2.0)

    Returns:
        Tupla (upper_band, middle_band, lower_band) o (None, None, None)
    """
    if len(closes) < period:
        return None, None, None

    window = np.array(closes[-period:], dtype=float)
    middle = float(np.mean(window))
    std = float(np.std(window, ddof=1))

    upper = middle + std_dev * std
    lower = middle - std_dev * std

    return upper, middle, lower


def _calc_momentum(closes: list[float], period: int = 10) -> Optional[float]:
    """
    Calcula el momentum de precio como cambio porcentual en N períodos.

    Args:
        closes: Lista de precios de cierre
        period: Número de períodos (default 10)

    Returns:
        Cambio porcentual, o None si no hay suficientes datos
    """
    if len(closes) < period + 1:
        return None

    current = closes[-1]
    past = closes[-(period + 1)]

    if past == 0:
        return None

    return (current - past) / past * 100.0


def _calc_vwap_proxy(
    closes: list[float],
    volumes: list[float],
    highs: list[float],
    lows: list[float],
    period: int = 20,
) -> Optional[float]:
    """
    Calcula un proxy del VWAP (Volume Weighted Average Price).
    Usa precio típico * volumen para aproximar el VWAP real.

    Args:
        closes: Precios de cierre
        volumes: Volúmenes
        highs: Precios máximos
        lows: Precios mínimos
        period: Períodos a considerar (default 20)

    Returns:
        VWAP del período, o None si no hay datos
    """
    n = min(period, len(closes), len(volumes), len(highs), len(lows))
    if n < 1:
        return None

    closes_w = np.array(closes[-n:], dtype=float)
    volumes_w = np.array(volumes[-n:], dtype=float)
    highs_w = np.array(highs[-n:], dtype=float)
    lows_w = np.array(lows[-n:], dtype=float)

    # Precio típico = (high + low + close) / 3
    typical = (highs_w + lows_w + closes_w) / 3.0
    total_volume = np.sum(volumes_w)

    if total_volume == 0:
        return None

    return float(np.sum(typical * volumes_w) / total_volume)


# ---------------------------------------------------------------------------
# Clase principal de estrategia
# ---------------------------------------------------------------------------

class Strategy:
    """
    Estrategia de trading basada en 7 indicadores técnicos ponderados.

    Pesos de indicadores:
    - RSI(14):           0.20
    - MACD(12,26,9):     0.20
    - EMA Cross(9,21):   0.15
    - Bollinger(20,2):   0.15
    - Momentum(10):      0.15
    - VWAP proxy:        0.10
    - Window delta:      0.05

    Cada indicador produce un score en [-1, +1]:
    - +1 = señal alcista (UP)
    - -1 = señal bajista (DOWN)
    - 0  = neutro
    """

    # Pesos de los indicadores (deben sumar 1.0)
    WEIGHTS = {
        "rsi": 0.20,
        "macd": 0.20,
        "ema_cross": 0.15,
        "bollinger": 0.15,
        "momentum": 0.15,
        "vwap": 0.10,
        "window_delta": 0.05,
    }

    # Umbral mínimo de score para generar señal (no SKIP)
    MIN_SCORE_THRESHOLD = 0.10

    def __init__(self, price_feed: PriceFeed) -> None:
        self.feed = price_feed

    def _score_rsi(self, closes: list[float]) -> float:
        """RSI: sobreventa (<30) = +1, sobrecompra (>70) = -1."""
        rsi = _calc_rsi(closes)
        if rsi is None:
            return 0.0

        if rsi < 30:
            # Muy sobrevendido = señal de compra fuerte
            return min(1.0, (30 - rsi) / 30)
        elif rsi > 70:
            # Muy sobrecomprado = señal de venta fuerte
            return -min(1.0, (rsi - 70) / 30)
        else:
            # Zona neutral: leve sesgo proporcional
            return -(rsi - 50) / 50 * 0.3

    def _score_macd(self, closes: list[float]) -> float:
        """MACD: cruce MACD vs señal. MACD > señal = alcista."""
        macd_line, signal_line = _calc_macd(closes)
        if macd_line is None or signal_line is None:
            return 0.0

        diff = macd_line - signal_line

        # Normalizar diferencia relativa al precio actual
        if self.feed.last_price > 0:
            norm_diff = diff / self.feed.last_price * 10000  # En puntos básicos
            return float(np.clip(norm_diff, -1.0, 1.0))

        return float(np.sign(diff)) * 0.5

    def _score_ema_cross(self, closes: list[float]) -> float:
        """EMA Cross: EMA9 > EMA21 = alcista."""
        ema_fast = _calc_ema(closes, 9)
        ema_slow = _calc_ema(closes, 21)

        if ema_fast is None or ema_slow is None:
            return 0.0

        if ema_slow == 0:
            return 0.0

        # Diferencia relativa entre EMAs
        rel_diff = (ema_fast - ema_slow) / ema_slow * 100
        return float(np.clip(rel_diff * 10, -1.0, 1.0))

    def _score_bollinger(self, closes: list[float]) -> float:
        """Bollinger: precio cerca de banda inferior = alcista."""
        upper, middle, lower = _calc_bollinger_bands(closes)

        if upper is None or lower is None or middle is None:
            return 0.0

        band_width = upper - lower
        if band_width == 0:
            return 0.0

        current = closes[-1]

        # Posición relativa dentro de las bandas (-1 a +1)
        # -1 = en la banda inferior (alcista), +1 = en la banda superior (bajista)
        relative_pos = (current - middle) / (band_width / 2)

        # Invertir: cerca de banda inferior = señal alcista (+1)
        return float(np.clip(-relative_pos, -1.0, 1.0))

    def _score_momentum(self, closes: list[float]) -> float:
        """Momentum: cambio porcentual positivo en 10 períodos = alcista."""
        mom = _calc_momentum(closes)
        if mom is None:
            return 0.0

        # Normalizar momentum (asumiendo rango típico de ±2% como saturación)
        return float(np.clip(mom / 2.0, -1.0, 1.0))

    def _score_vwap(
        self,
        closes: list[float],
        volumes: list[float],
        highs: list[float],
        lows: list[float],
    ) -> float:
        """VWAP: precio por encima del VWAP = bajista, debajo = alcista."""
        vwap = _calc_vwap_proxy(closes, volumes, highs, lows)
        if vwap is None or vwap == 0:
            return 0.0

        current = closes[-1]
        rel_diff = (current - vwap) / vwap * 100

        # Por encima del VWAP es alcista (momentum alcista sostenido)
        return float(np.clip(rel_diff * 5, -1.0, 1.0))

    def _score_window_delta(self) -> float:
        """Window delta: % de movimiento desde apertura de ventana."""
        delta = self.feed.window_delta_pct
        # Normalizar (asumiendo ±1% como saturación en 5 minutos)
        return float(np.clip(delta / 1.0, -1.0, 1.0))

    def analyze(self) -> Signal:
        """
        Analiza el feed de precios y genera una señal de trading.

        Returns:
            Signal con dirección, confianza y breakdown por indicador
        """
        closes = self.feed.get_closes()
        volumes = self.feed.get_volumes()
        highs = self.feed.get_highs()
        lows = self.feed.get_lows()
        last_price = self.feed.last_price

        # Verificar datos suficientes (MACD necesita al menos 35 velas)
        if not self.feed.has_enough_data(35):
            logger.debug(
                f"Datos insuficientes: {len(closes)}/35 velas. Generando SKIP."
            )
            return Signal(
                direction="SKIP",
                confidence=0.0,
                breakdown={k: 0.0 for k in self.WEIGHTS},
                raw_score=0.0,
                last_price=last_price,
                candles_used=len(closes),
            )

        # Calcular score de cada indicador
        breakdown = {
            "rsi": self._score_rsi(closes),
            "macd": self._score_macd(closes),
            "ema_cross": self._score_ema_cross(closes),
            "bollinger": self._score_bollinger(closes),
            "momentum": self._score_momentum(closes),
            "vwap": self._score_vwap(closes, volumes, highs, lows),
            "window_delta": self._score_window_delta(),
        }

        # Score ponderado total [-1, +1]
        raw_score = sum(
            score * self.WEIGHTS[name]
            for name, score in breakdown.items()
        )

        # Confianza como distancia desde el centro (0 = sin confianza, 1 = máxima)
        confidence = abs(raw_score)

        # Determinar dirección
        if abs(raw_score) < self.MIN_SCORE_THRESHOLD:
            direction: Direction = "SKIP"
        elif raw_score > 0:
            direction = "UP"
        else:
            direction = "DOWN"

        signal = Signal(
            direction=direction,
            confidence=confidence,
            breakdown=breakdown,
            raw_score=raw_score,
            last_price=last_price,
            candles_used=len(closes),
        )

        logger.debug(str(signal))
        return signal
