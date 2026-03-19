"""
Módulo de gestión de riesgo del bot de trading.
Controla el stop-loss diario, frecuencia de trades, filtros de odds y confianza.
Lleva registro completo del historial de trades y PnL.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional

from .config import Config
from .logger import get_logger

logger = get_logger("risk_mgr")


@dataclass
class Trade:
    """Registro completo de un trade individual."""

    trade_id: str                     # ID único del trade
    window_slug: str                  # Slug del mercado
    direction: str                    # UP o DOWN
    token_id: str                     # Token comprado
    order_id: str                     # ID de la orden en Polymarket
    amount_usdc: float                # Monto apostado en USDC
    token_price: float                # Precio pagado por token
    tokens_bought: float              # Cantidad de tokens comprados
    entry_time: float                 # Timestamp de entrada (Unix)
    confidence: float                 # Confianza de la señal
    simulated: bool                   # Si fue simulado

    trade_type: str = "DIRECTIONAL"   # Tipo de trade: DIRECTIONAL o SNIPER

    # Campos de resolución (se llenan después)
    resolved: bool = False            # Si el trade fue resuelto
    resolution_time: float = 0.0     # Timestamp de resolución
    open_price: float = 0.0          # Precio BTC a la apertura de la ventana
    close_price: float = 0.0         # Precio BTC al cierre de la ventana
    won: Optional[bool] = None       # True si ganó, False si perdió
    pnl_usdc: float = 0.0            # PnL en USDC (positivo = ganancia)
    pnl_pct: float = 0.0             # PnL en porcentaje

    @property
    def entry_dt(self) -> datetime:
        """Fecha y hora de entrada como datetime."""
        return datetime.fromtimestamp(self.entry_time)

    @property
    def result_str(self) -> str:
        """Representación del resultado del trade."""
        if not self.resolved:
            return "PENDIENTE"
        emoji = "✓" if self.won else "✗"
        return f"{emoji} {'GANÓ' if self.won else 'PERDIÓ'} ${self.pnl_usdc:+.2f}"

    def resolve(
        self,
        open_price: float,
        close_price: float,
        resolution_time: Optional[float] = None,
    ) -> None:
        """
        Resuelve el trade comparando precio de apertura vs cierre.

        Args:
            open_price: Precio BTC a la apertura de la ventana
            close_price: Precio BTC al cierre de la ventana
            resolution_time: Timestamp de resolución (usa tiempo actual si None)
        """
        self.open_price = open_price
        self.close_price = close_price
        self.resolution_time = resolution_time or time.time()
        self.resolved = True

        # Determinar si ganó
        price_went_up = close_price > open_price
        self.won = (
            (self.direction == "UP" and price_went_up)
            or (self.direction == "DOWN" and not price_went_up)
        )

        if self.won:
            # Ganancia: tokens_bought * $1.00 - amount_usdc
            self.pnl_usdc = self.tokens_bought - self.amount_usdc
        else:
            # Pérdida: perdemos el monto apostado
            self.pnl_usdc = -self.amount_usdc

        if self.amount_usdc > 0:
            self.pnl_pct = self.pnl_usdc / self.amount_usdc * 100

        logger.info(
            f"Trade resuelto: {self.direction} | "
            f"BTC: ${open_price:,.2f} → ${close_price:,.2f} | "
            f"{self.result_str}"
        )


class RiskManager:
    """
    Gestor de riesgo del bot de trading.

    Controla:
    - Stop-loss diario: detiene operaciones si las pérdidas superan el límite
    - Frecuencia: máximo N trades por hora
    - Filtro de odds: solo opera en rangos de precio favorables
    - Filtro de confianza: solo opera con señales suficientemente confiables
    - Historial completo de trades y PnL
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._trades: list[Trade] = []
        self._trade_counter: int = 0
        self._halted: bool = False
        self._halt_reason: str = ""

    # -----------------------------------------------------------------------
    # Validación de trades
    # -----------------------------------------------------------------------

    def can_trade(
        self,
        confidence: float,
        token_price: float,
        window_slug: str,
    ) -> tuple[bool, str]:
        """
        Verifica si se puede ejecutar un trade bajo las reglas de riesgo.

        Args:
            confidence: Confianza de la señal (0.0 a 1.0)
            token_price: Precio del token (0.0 a 1.0)
            window_slug: Slug de la ventana actual

        Returns:
            Tupla (puede_tradear, motivo_de_rechazo)
        """
        # 1. Verificar si el bot está detenido por stop-loss
        if self._halted:
            return False, f"Bot detenido: {self._halt_reason}"

        # 2. Verificar stop-loss diario
        daily_pnl = self.get_daily_pnl()
        if daily_pnl <= -self.config.stop_loss_daily_usd:
            reason = (
                f"Stop-loss diario alcanzado: ${daily_pnl:.2f} "
                f"(límite: -${self.config.stop_loss_daily_usd:.2f})"
            )
            self._halted = True
            self._halt_reason = reason
            logger.warning(f"STOP-LOSS ACTIVADO: {reason}")
            return False, reason

        # 3. Verificar si ya se operó en esta ventana
        if self._already_traded_this_window(window_slug):
            return False, f"Ya se operó en la ventana: {window_slug}"

        # 4. Verificar frecuencia máxima por hora
        hourly_count = self.get_trades_last_hour()
        if hourly_count >= self.config.max_trades_per_hour:
            return False, (
                f"Límite de frecuencia: {hourly_count}/{self.config.max_trades_per_hour} "
                f"trades por hora"
            )

        # 5. Filtro de confianza
        if confidence < self.config.min_confidence:
            return False, (
                f"Confianza insuficiente: {confidence:.1%} "
                f"(mín: {self.config.min_confidence:.1%})"
            )

        # 6. Filtro de odds (precio del token)
        if not (self.config.min_odds <= token_price <= self.config.max_odds):
            return False, (
                f"Odds fuera de rango: {token_price:.4f} "
                f"(rango: {self.config.min_odds}-{self.config.max_odds})"
            )

        return True, "OK"

    def _already_traded_this_window(self, window_slug: str) -> bool:
        """Verifica si ya se realizó un trade en la ventana actual."""
        return any(
            t.window_slug == window_slug and not t.resolved
            for t in self._trades
        )

    # -----------------------------------------------------------------------
    # Registro de trades
    # -----------------------------------------------------------------------

    def register_trade(
        self,
        window_slug: str,
        direction: str,
        token_id: str,
        order_id: str,
        amount_usdc: float,
        token_price: float,
        tokens_bought: float,
        confidence: float,
        simulated: bool,
        trade_type: str = "DIRECTIONAL",
    ) -> Trade:
        """
        Registra un nuevo trade en el historial.

        Returns:
            Trade registrado
        """
        self._trade_counter += 1
        trade_id = f"T{self._trade_counter:04d}"

        trade = Trade(
            trade_id=trade_id,
            window_slug=window_slug,
            direction=direction,
            token_id=token_id,
            order_id=order_id,
            amount_usdc=amount_usdc,
            token_price=token_price,
            tokens_bought=tokens_bought,
            entry_time=time.time(),
            confidence=confidence,
            simulated=simulated,
            trade_type=trade_type,
        )

        self._trades.append(trade)
        logger.info(
            f"Trade registrado [{trade_id}] [{trade_type}]: {direction} ${amount_usdc:.2f} USDC "
            f"@ {token_price:.4f} | confianza={confidence:.1%}"
        )
        return trade

    def resolve_trade(
        self,
        window_slug: str,
        open_price: float,
        close_price: float,
    ) -> Optional[Trade]:
        """
        Resuelve un trade pendiente de la ventana especificada.

        Args:
            window_slug: Slug de la ventana a resolver
            open_price: Precio BTC de apertura de la ventana
            close_price: Precio BTC de cierre de la ventana

        Returns:
            Trade resuelto, o None si no se encontró
        """
        for trade in self._trades:
            if trade.window_slug == window_slug and not trade.resolved:
                trade.resolve(open_price, close_price)
                return trade

        logger.debug(f"No hay trades pendientes para ventana: {window_slug}")
        return None

    # -----------------------------------------------------------------------
    # Estadísticas y PnL
    # -----------------------------------------------------------------------

    def get_daily_pnl(self) -> float:
        """Calcula el PnL total del día actual."""
        today = date.today()
        return sum(
            t.pnl_usdc
            for t in self._trades
            if t.resolved and datetime.fromtimestamp(t.entry_time).date() == today
        )

    def get_total_pnl(self) -> float:
        """Calcula el PnL total de todos los trades resueltos."""
        return sum(t.pnl_usdc for t in self._trades if t.resolved)

    def get_trades_last_hour(self) -> int:
        """Cuenta trades realizados en la última hora."""
        cutoff = time.time() - 3600
        return sum(1 for t in self._trades if t.entry_time >= cutoff)

    def get_stats(self) -> dict:
        """Retorna estadísticas completas del bot."""
        resolved = [t for t in self._trades if t.resolved]
        wins = [t for t in resolved if t.won]
        losses = [t for t in resolved if not t.won]

        total_trades = len(self._trades)
        resolved_count = len(resolved)
        win_count = len(wins)
        loss_count = len(losses)

        win_rate = win_count / resolved_count if resolved_count > 0 else 0.0
        total_pnl = self.get_total_pnl()
        daily_pnl = self.get_daily_pnl()

        avg_win = (
            sum(t.pnl_usdc for t in wins) / win_count
            if win_count > 0 else 0.0
        )
        avg_loss = (
            sum(t.pnl_usdc for t in losses) / loss_count
            if loss_count > 0 else 0.0
        )

        return {
            "total_trades": total_trades,
            "resolved": resolved_count,
            "pending": total_trades - resolved_count,
            "wins": win_count,
            "losses": loss_count,
            "win_rate": win_rate,
            "total_pnl_usdc": total_pnl,
            "daily_pnl_usdc": daily_pnl,
            "avg_win_usdc": avg_win,
            "avg_loss_usdc": avg_loss,
            "trades_last_hour": self.get_trades_last_hour(),
            "halted": self._halted,
            "halt_reason": self._halt_reason,
        }

    def print_summary(self) -> None:
        """Imprime un resumen del rendimiento en la terminal."""
        stats = self.get_stats()

        print("\n" + "=" * 60)
        print("  RESUMEN DE TRADING")
        print("=" * 60)
        print(f"  Trades totales:    {stats['total_trades']}")
        print(f"  Resueltos:         {stats['resolved']}")
        print(f"  Pendientes:        {stats['pending']}")
        print(f"  Victorias:         {stats['wins']}")
        print(f"  Derrotas:          {stats['losses']}")
        print(f"  Win rate:          {stats['win_rate']:.1%}")
        print(f"  PnL total:         ${stats['total_pnl_usdc']:+.2f} USDC")
        print(f"  PnL hoy:           ${stats['daily_pnl_usdc']:+.2f} USDC")
        print(f"  Ganancia media:    ${stats['avg_win_usdc']:+.2f} USDC")
        print(f"  Pérdida media:     ${stats['avg_loss_usdc']:+.2f} USDC")

        if stats["halted"]:
            print(f"\n  ⚠ BOT DETENIDO: {stats['halt_reason']}")

        print("=" * 60 + "\n")

    @property
    def is_halted(self) -> bool:
        """Retorna True si el bot está detenido por stop-loss."""
        return self._halted

    @property
    def all_trades(self) -> list[Trade]:
        """Retorna todos los trades registrados."""
        return self._trades.copy()
