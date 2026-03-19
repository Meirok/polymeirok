"""
Bucle principal del bot de trading.
Coordina el feed de precios, la estrategia, el cliente de Polymarket,
la gestión de riesgo y las notificaciones.
"""

import asyncio
import time
from typing import Optional

from .config import Config
from .latency_sniper import LatencySniper
from .logger import get_logger
from .notifier import Notifier
from .polymarket_client import PolymarketClient
from .price_feed import PriceFeed
from .risk_manager import RiskManager, Trade

logger = get_logger("bot")

# Intervalo de polling del bucle principal (segundos)
POLL_INTERVAL = 1.0

# Segundos antes del cierre para intentar la entrada
# (usa config.entry_seconds_before, este es el valor por defecto)
DEFAULT_ENTRY_SECONDS = 25


class TradingBot:
    """
    Bot de trading principal para mercados BTC Up/Down de 5 minutos de Polymarket.

    Opera EXCLUSIVAMENTE en modo Latency Sniper.

    Flujo por ventana de 5 minutos:
    1. Detectar nueva ventana via aritmética de timestamps
    2. Registrar precio de apertura de la ventana
    3. Cada segundo: verificar si el Latency Sniper detecta un movimiento brusco
    4. Si sniper dispara señal y reglas de riesgo pasan: colocar orden
    5. Al cerrar la ventana: resolver trade y actualizar PnL
    6. Enviar resumen de ventana por Telegram al inicio de la nueva ventana
    """

    def __init__(self, config: Config) -> None:
        self.config = config

        # Componentes del bot
        self.feed = PriceFeed(config.binance_ws_url)
        self.sniper = LatencySniper(self.feed, config)
        self.polymarket = PolymarketClient(config)
        self.risk_manager = RiskManager(config)
        self.notifier = Notifier(config)

        # Estado de la ventana actual
        self._current_window_ts: int = 0
        self._current_window_slug: str = ""
        self._window_open_price: float = 0.0
        self._traded_this_window: bool = False

        # Razón por la que no se operó esta ventana (para el resumen)
        self._window_skip_reason: str = ""

        # Seguimiento del mayor movimiento detectado por el sniper en la ventana actual
        self._sniper_peak_move_pct: float = 0.0
        self._sniper_peak_move_second: int = 0

        # Control del bot
        self._running: bool = False

    async def start(self) -> None:
        """
        Inicia todos los componentes del bot.
        El feed de precios corre en background mientras el bucle principal opera.
        """
        self._running = True

        # Notificar inicio
        await self.notifier.notify_bot_start(
            config_str=str(self.config),
            dry_run=self.config.dry_run or not self.config.production,
        )

        logger.info("=" * 60)
        logger.info("Bot de Trading Polymarket BTC Up/Down 5m")
        logger.info(f"Modo: {'SIMULACIÓN' if self.config.dry_run else 'PRODUCCIÓN'}")
        logger.info(f"Apuesta: ${self.config.bet_amount_usdc:.2f} USDC")
        logger.info(f"Confianza mínima: {self.config.min_confidence:.0%}")
        logger.info(f"Odds: {self.config.min_odds} - {self.config.max_odds}")
        logger.info(f"Stop-loss diario: ${self.config.stop_loss_daily_usd:.2f}")
        logger.info(
            f"Sniper: umbral={self.config.sniper_threshold}% | "
            f"mín. segundos restantes={self.config.sniper_min_seconds_left}s"
        )
        logger.info("=" * 60)

        # Iniciar feed de precios y bucle principal en paralelo
        try:
            await asyncio.gather(
                self.feed.run(),
                self._main_loop(),
                return_exceptions=False,
            )
        except asyncio.CancelledError:
            logger.info("Bot cancelado por señal externa")
        except Exception as e:
            logger.error(f"Error crítico en el bot: {e}", exc_info=True)
            await self.notifier.notify_error(str(e), context="main loop")
        finally:
            await self._cleanup()

    async def stop(self) -> None:
        """Detiene el bot graciosamente."""
        logger.info("Iniciando parada del bot...")
        self._running = False
        await self.feed.stop()

    async def _cleanup(self) -> None:
        """Limpieza al terminar el bot."""
        await self.polymarket.close()
        await self.notifier.close()
        logger.info("Recursos liberados")

    async def _main_loop(self) -> None:
        """
        Bucle principal que maneja la lógica de trading por ventana de 5 minutos.

        Espera hasta que el feed tenga datos antes de iniciar.
        """
        logger.info("Esperando datos del feed de precios...")

        # Esperar a que el feed se conecte y tenga datos iniciales
        while self._running:
            if self.feed.is_connected and self.feed.last_price > 0:
                break
            await asyncio.sleep(1.0)

        logger.info(f"Feed activo. Precio BTC: ${self.feed.last_price:,.2f}")

        # Inicializar ventana actual
        self._init_current_window()

        # Bucle principal
        while self._running:
            try:
                await self._process_tick()
            except Exception as e:
                logger.error(f"Error en tick del bucle principal: {e}", exc_info=True)
                await self.notifier.notify_error(str(e), context="process_tick")

            await asyncio.sleep(POLL_INTERVAL)

    def _get_window_timestamp(self) -> int:
        """Obtiene el timestamp de la ventana actual de 5 minutos."""
        ts = int(time.time())
        return ts - (ts % 300)

    def _seconds_in_window(self) -> int:
        """Segundos transcurridos en la ventana actual."""
        return int(time.time()) % 300

    def _seconds_until_close(self) -> int:
        """Segundos hasta el cierre de la ventana actual."""
        return 300 - self._seconds_in_window()

    def _init_current_window(self) -> None:
        """Inicializa el estado para la ventana actual."""
        self._current_window_ts = self._get_window_timestamp()
        self._current_window_slug = f"btc-updown-5m-{self._current_window_ts}"
        self._traded_this_window = False
        self._window_skip_reason = ""
        self._sniper_peak_move_pct = 0.0
        self._sniper_peak_move_second = 0
        self.sniper.reset_window()

        if self.feed.last_price > 0:
            self._window_open_price = self.feed.last_price
            self.feed.set_window_open_price(self._window_open_price)
            logger.info(
                f"Ventana iniciada: {self._current_window_slug} | "
                f"Apertura BTC: ${self._window_open_price:,.2f} | "
                f"Cierra en: {self._seconds_until_close()}s"
            )

    async def _process_tick(self) -> None:
        """
        Procesa un tick del bucle principal.
        Detecta cambios de ventana y maneja la lógica de entrada y resolución.
        """
        current_window_ts = self._get_window_timestamp()
        seconds_in_window = self._seconds_in_window()
        seconds_until_close = self._seconds_until_close()

        # --- Detectar nueva ventana ---
        if current_window_ts != self._current_window_ts:
            await self._on_new_window(current_window_ts)
            return

        # --- Actualizar sniper con precio actual cada segundo ---
        self.sniper.update()

        # --- Rastrear el mayor movimiento del sniper en la ventana ---
        price_history = self.sniper._price_history
        if len(price_history) >= 10:
            oldest = price_history[0]
            current = price_history[-1]
            if oldest > 0:
                move_pct = (current - oldest) / oldest * 100
                if abs(move_pct) > abs(self._sniper_peak_move_pct):
                    self._sniper_peak_move_pct = move_pct
                    self._sniper_peak_move_second = seconds_in_window

        # --- Verificar señal del Latency Sniper ---
        # El sniper puede actuar en cualquier momento cuando quedan > MIN segundos
        if not self._traded_this_window and self.feed.last_price > 0:
            sniper_signal = self.sniper.check_signal(seconds_until_close)
            if sniper_signal is not None:
                logger.info(
                    f"[SNIPER] Señal detectada: {sniper_signal.direction} | "
                    f"Movimiento: {sniper_signal.breakdown.get('sniper_move_10s_pct', 0):+.4f}% | "
                    f"Cierre en: {seconds_until_close}s"
                )
                await self._try_enter_trade(
                    trade_type="SNIPER", forced_signal=sniper_signal
                )
                return

        # Log periódico del estado (cada 30 segundos)
        if seconds_in_window % 30 == 0 and seconds_in_window > 0:
            self._log_status(seconds_until_close)

    async def _on_new_window(self, new_window_ts: int) -> None:
        """
        Maneja la transición a una nueva ventana de 5 minutos.
        Resuelve trades pendientes, envía resumen de ventana, e inicializa la nueva.

        Args:
            new_window_ts: Timestamp de la nueva ventana
        """
        logger.info(f"Nueva ventana detectada: {new_window_ts}")

        # Capturar datos de la ventana que cierra para el resumen
        prev_slug = self._current_window_slug
        prev_open = self._window_open_price
        prev_close = self.feed.last_price  # Precio al cierre = precio al inicio de la nueva
        prev_traded = self._traded_this_window
        prev_skip_reason = self._window_skip_reason
        prev_sniper_peak_move_pct = self._sniper_peak_move_pct
        prev_sniper_peak_move_second = self._sniper_peak_move_second
        new_window_slug = f"btc-updown-5m-{new_window_ts}"

        # Resolver trades pendientes de la ventana anterior
        resolved_trade: Optional[Trade] = None
        if prev_slug and prev_traded:
            resolved_trade = await self._resolve_pending_trades()

        # Enviar resumen de la ventana que acaba de cerrar
        if prev_slug and prev_open > 0:
            stats = self.risk_manager.get_stats()
            await self.notifier.notify_window_summary(
                new_window_slug=new_window_slug,
                window_slug=prev_slug,
                open_price=prev_open,
                close_price=prev_close,
                trade=resolved_trade,
                skip_reason=prev_skip_reason,
                current_btc_price=self.feed.last_price,
                stats=stats,
                dry_run=self.config.dry_run,
                sniper_peak_move_pct=prev_sniper_peak_move_pct,
                sniper_peak_move_second=prev_sniper_peak_move_second,
            )

        # Inicializar nueva ventana
        self._current_window_ts = new_window_ts
        self._current_window_slug = new_window_slug
        self._traded_this_window = False
        self._window_skip_reason = ""
        self._sniper_peak_move_pct = 0.0
        self._sniper_peak_move_second = 0
        self.sniper.reset_window()

        # Precio de apertura de la nueva ventana
        self._window_open_price = self.feed.last_price
        self.feed.set_window_open_price(self._window_open_price)

        logger.info(
            f"Nueva ventana: {self._current_window_slug} | "
            f"BTC apertura: ${self._window_open_price:,.2f}"
        )

        # Verificar stop-loss diario
        if self.risk_manager.is_halted:
            daily_pnl = self.risk_manager.get_daily_pnl()
            logger.warning(
                f"Bot detenido por stop-loss. PnL diario: ${daily_pnl:+.2f} USDC"
            )

    async def _try_enter_trade(
        self,
        trade_type: str = "SNIPER",
        forced_signal=None,
    ) -> None:
        """
        Intenta entrar en un trade para la ventana actual.

        Args:
            trade_type: Tipo de trade (siempre "SNIPER" en este modo)
            forced_signal: Signal generada por el sniper.

        Flujo:
        1. Usa señal del sniper
        2. Verifica reglas de riesgo
        3. Obtiene mercado de Polymarket
        4. Coloca la orden
        5. Registra el trade
        6. Notifica entrada
        """
        if forced_signal is None:
            return

        seconds_until_close = self._seconds_until_close()
        logger.info(
            f"[{trade_type}] Analizando señal para {self._current_window_slug} | "
            f"Cierra en {seconds_until_close}s | "
            f"BTC: ${self.feed.last_price:,.2f}"
        )

        # 1. Usar señal del sniper
        signal = forced_signal

        logger.info(f"[{trade_type}] Señal generada: {signal}")

        if signal.direction == "SKIP":
            logger.info(f"[{trade_type}] Señal SKIP — no se opera en esta ventana")
            self._window_skip_reason = "Señal SKIP"
            return

        # 2. Obtener mercado de Polymarket para conocer precios de tokens
        market = await self.polymarket.get_market(self._current_window_slug)

        # En modo DRY_RUN sin mercado activo, usar precios simulados
        if market is None:
            if self.config.dry_run:
                # Simular precios de mercado en DRY_RUN
                token_price = 0.65  # Precio simulado
                token_id = f"SIM-TOKEN-{signal.direction}"
                logger.info(
                    f"[{trade_type}] [SIMULACIÓN] Mercado no encontrado. "
                    f"Usando precio simulado: {token_price}"
                )
            else:
                logger.warning(
                    f"[{trade_type}] Mercado no encontrado para {self._current_window_slug}. "
                    f"Saltando trade."
                )
                self._window_skip_reason = "Mercado no encontrado"
                return
        else:
            # Seleccionar token según dirección
            if signal.direction == "UP":
                token_id = market.up_token_id
                token_price = market.up_price
            else:
                token_id = market.down_token_id
                token_price = market.down_price

            if not market.is_active:
                logger.warning(f"[{trade_type}] Mercado inactivo — saltando trade")
                self._window_skip_reason = "Mercado inactivo"
                return

        # 3. Verificar reglas de riesgo
        can_trade, reason = self.risk_manager.can_trade(
            confidence=signal.confidence,
            token_price=token_price,
            window_slug=self._current_window_slug,
        )

        if not can_trade:
            logger.info(f"[{trade_type}] Trade bloqueado por riesgo: {reason}")
            self._window_skip_reason = reason
            return

        # 4. Colocar orden
        logger.info(
            f"[{trade_type}] Colocando orden {signal.direction} | "
            f"Token: {token_price:.4f} | "
            f"Monto: ${self.config.bet_amount_usdc:.2f} USDC | "
            f"Confianza: {signal.confidence:.1%}"
        )

        order_result = await self.polymarket.place_order(
            direction=signal.direction,
            token_id=token_id,
            token_price=token_price,
            amount_usdc=self.config.bet_amount_usdc,
        )

        if not order_result.success:
            logger.error(f"[{trade_type}] Error al colocar orden: {order_result.error}")
            await self.notifier.notify_error(
                error=f"Error de orden: {order_result.error}",
                context=self._current_window_slug,
            )
            self._window_skip_reason = f"Error de orden: {order_result.error}"
            return

        # 5. Registrar trade en el gestor de riesgo
        trade = self.risk_manager.register_trade(
            window_slug=self._current_window_slug,
            direction=order_result.direction,
            token_id=order_result.token_id,
            order_id=order_result.order_id,
            amount_usdc=order_result.amount_usdc,
            token_price=order_result.token_price,
            tokens_bought=order_result.tokens_bought,
            confidence=signal.confidence,
            simulated=order_result.simulated,
            trade_type=trade_type,
        )

        self._traded_this_window = True

        # Guardar precio de apertura en el trade para resolución posterior
        trade.open_price = self._window_open_price

        # 6. Notificar entrada
        await self.notifier.notify_trade_entry(
            trade=trade,
            btc_price=self.feed.last_price,
            signal_breakdown=signal.breakdown,
        )

    async def _resolve_pending_trades(self) -> Optional[Trade]:
        """
        Resuelve trades pendientes de la ventana anterior.
        Usa el precio actual de BTC como precio de cierre.

        Returns:
            Trade resuelto, o None si no había trades pendientes.
        """
        close_price = self.feed.last_price

        if close_price <= 0:
            logger.warning("Precio de cierre inválido — no se puede resolver trade")
            return None

        logger.info(
            f"Resolviendo trades de {self._current_window_slug} | "
            f"Cierre BTC: ${close_price:,.2f} | "
            f"Apertura: ${self._window_open_price:,.2f}"
        )

        resolved_trade = self.risk_manager.resolve_trade(
            window_slug=self._current_window_slug,
            open_price=self._window_open_price,
            close_price=close_price,
        )

        if resolved_trade:
            # Notificar resultado
            await self.notifier.notify_trade_result(resolved_trade)

            # Si el stop-loss se activó con este resultado, notificar
            if self.risk_manager.is_halted:
                daily_pnl = self.risk_manager.get_daily_pnl()
                await self.notifier.notify_stop_loss(
                    daily_pnl=daily_pnl,
                    limit=self.config.stop_loss_daily_usd,
                )

        return resolved_trade

    def _log_status(self, seconds_until_close: int) -> None:
        """Loguea el estado actual del bot de forma periódica."""
        stats = self.risk_manager.get_stats()
        feed_status = self.feed.status

        logger.info(
            f"Estado | BTC: ${feed_status['last_price']:,.2f} | "
            f"Δventana: {feed_status['window_delta_pct']:+.3f}% | "
            f"Cierre en: {seconds_until_close}s | "
            f"PnL día: ${stats['daily_pnl_usdc']:+.2f} | "
            f"Trades: {stats['total_trades']} | "
            f"WR: {stats['win_rate']:.0%} | "
            f"Velas: {feed_status['candles_count']}"
        )

    async def send_daily_summary(self) -> None:
        """Envía el resumen diario de trading por Telegram."""
        stats = self.risk_manager.get_stats()
        await self.notifier.notify_daily_summary(stats)
        self.risk_manager.print_summary()
