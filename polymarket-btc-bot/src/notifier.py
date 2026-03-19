"""
Módulo de notificaciones via Telegram.
Envía alertas de trades, resultados y resúmenes diarios.
Se desactiva automáticamente si no se configuran las credenciales.
"""

import asyncio
from typing import Optional

import aiohttp

from .config import Config
from .logger import get_logger
from .risk_manager import Trade

logger = get_logger("notifier")

# URL base de la API de Telegram
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


def _escape_mdv2(text: str) -> str:
    """
    Escapa caracteres especiales para Telegram MarkdownV2.
    Debe aplicarse a todo texto dinámico que no sea parte de la sintaxis de formato.
    """
    special = r'\_*[]()~`>#+-=|{}.!'
    return "".join(f"\\{c}" if c in special else c for c in str(text))


class Notifier:
    """
    Notificador via Telegram Bot API.

    Si TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no están configurados,
    todas las operaciones son no-ops silenciosos.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._enabled = config.is_telegram_configured()
        self._session: Optional[aiohttp.ClientSession] = None

        if self._enabled:
            logger.info("Notificador Telegram habilitado")
        else:
            logger.info("Notificador Telegram deshabilitado (sin credenciales)")

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

    async def _send(self, message: str) -> bool:
        """
        Envía un mensaje por Telegram.

        Args:
            message: Texto del mensaje (soporta Markdown)

        Returns:
            True si se envió correctamente, False en caso contrario
        """
        if not self._enabled:
            return True  # No-op silencioso

        try:
            url = TELEGRAM_API_BASE.format(token=self.config.telegram_bot_token)
            payload = {
                "chat_id": self.config.telegram_chat_id,
                "text": message,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }

            session = await self._get_session()
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    logger.debug("Notificación Telegram enviada exitosamente")
                    return True
                else:
                    resp_text = await response.text()
                    logger.warning(
                        f"Error Telegram API: {response.status} | {resp_text[:200]}"
                    )
                    return False

        except aiohttp.ClientError as e:
            logger.error(f"Error de red al enviar Telegram: {e}")
            return False
        except Exception as e:
            logger.error(f"Error inesperado al enviar Telegram: {e}", exc_info=True)
            return False

    async def notify_trade_entry(
        self,
        trade: Trade,
        btc_price: float,
        signal_breakdown: dict[str, float],
    ) -> None:
        """
        Notifica la entrada de un nuevo trade.

        Args:
            trade: Trade registrado
            btc_price: Precio actual de BTC
            signal_breakdown: Desglose de scores por indicador
        """
        mode_label = "🔵 SIMULACIÓN" if trade.simulated else "🟢 REAL"
        direction_emoji = "📈" if trade.direction == "UP" else "📉"
        type_label = "🎯 SNIPER" if trade.trade_type == "SNIPER" else "📊 DIRECTIONAL"

        # Formatear breakdown de indicadores
        breakdown_lines = "\n".join(
            f"  • {name.upper()}: `{score:+.3f}`"
            for name, score in signal_breakdown.items()
        )

        message = (
            f"*{direction_emoji} TRADE ENTRADA* — {mode_label} | {type_label}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"*ID:* `{trade.trade_id}`\n"
            f"*Mercado:* `{trade.window_slug}`\n"
            f"*Dirección:* *{trade.direction}*\n"
            f"*BTC:* `${btc_price:,.2f}`\n"
            f"*Apuesta:* `${trade.amount_usdc:.2f} USDC`\n"
            f"*Tokens:* `{trade.tokens_bought:.4f} @ ${trade.token_price:.4f}`\n"
            f"*Confianza:* `{trade.confidence:.1%}`\n"
            f"\n*Indicadores:*\n{breakdown_lines}"
        )

        await self._send(message)

    async def notify_trade_result(self, trade: Trade) -> None:
        """
        Notifica el resultado de un trade resuelto.

        Args:
            trade: Trade ya resuelto
        """
        if not trade.resolved:
            return

        result_emoji = "✅" if trade.won else "❌"
        mode_label = "SIMULACIÓN" if trade.simulated else "REAL"

        message = (
            f"*{result_emoji} TRADE RESULTADO* — {mode_label}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"*ID:* `{trade.trade_id}`\n"
            f"*Dirección:* {trade.direction} → `{'CORRECTO' if trade.won else 'INCORRECTO'}`\n"
            f"*BTC apertura:* `${trade.open_price:,.2f}`\n"
            f"*BTC cierre:* `${trade.close_price:,.2f}`\n"
            f"*PnL:* `${trade.pnl_usdc:+.2f} USDC` (`{trade.pnl_pct:+.1f}%`)\n"
        )

        await self._send(message)

    async def notify_daily_summary(self, stats: dict) -> None:
        """
        Envía resumen diario de trading.

        Args:
            stats: Diccionario con estadísticas del RiskManager
        """
        pnl = stats.get("daily_pnl_usdc", 0.0)
        pnl_emoji = "📈" if pnl >= 0 else "📉"

        message = (
            f"*📊 RESUMEN DIARIO* {pnl_emoji}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"*Trades:* `{stats.get('resolved', 0)}` resueltos\n"
            f"*Victorias:* `{stats.get('wins', 0)}`\n"
            f"*Derrotas:* `{stats.get('losses', 0)}`\n"
            f"*Win rate:* `{stats.get('win_rate', 0):.1%}`\n"
            f"*PnL hoy:* `${pnl:+.2f} USDC`\n"
            f"*PnL total:* `${stats.get('total_pnl_usdc', 0):+.2f} USDC`\n"
        )

        if stats.get("halted"):
            message += f"\n⚠️ *BOT DETENIDO*: {stats.get('halt_reason', '')}"

        await self._send(message)

    async def notify_error(self, error: str, context: str = "") -> None:
        """
        Notifica un error crítico.

        Args:
            error: Mensaje de error
            context: Contexto adicional del error
        """
        context_line = f"\n*Contexto:* `{context}`" if context else ""

        message = (
            f"*⚠️ ERROR DEL BOT*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"*Error:* `{error}`"
            f"{context_line}"
        )

        await self._send(message)

    async def notify_stop_loss(self, daily_pnl: float, limit: float) -> None:
        """
        Notifica la activación del stop-loss diario.

        Args:
            daily_pnl: PnL diario actual
            limit: Límite de stop-loss configurado
        """
        message = (
            f"*🛑 STOP-LOSS ACTIVADO*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"*PnL diario:* `${daily_pnl:+.2f} USDC`\n"
            f"*Límite:* `-${limit:.2f} USDC`\n"
            f"El bot ha detenido las operaciones por hoy."
        )

        await self._send(message)

    async def notify_bot_start(self, config_str: str, dry_run: bool) -> None:
        """
        Notifica el inicio del bot.

        Args:
            config_str: Descripción de la configuración
            dry_run: Si está en modo simulación
        """
        mode = "🔵 SIMULACIÓN" if dry_run else "🟢 PRODUCCIÓN"

        message = (
            f"*🤖 BOT INICIADO* — {mode}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"*Configuración:*\n`{config_str}`"
        )

        await self._send(message)

    async def _send_mdv2(self, message: str) -> bool:
        """
        Envía un mensaje con formato MarkdownV2 por Telegram.

        Args:
            message: Texto del mensaje en formato MarkdownV2

        Returns:
            True si se envió correctamente, False en caso contrario
        """
        if not self._enabled:
            return True  # No-op silencioso

        try:
            url = TELEGRAM_API_BASE.format(token=self.config.telegram_bot_token)
            payload = {
                "chat_id": self.config.telegram_chat_id,
                "text": message,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True,
            }

            session = await self._get_session()
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    logger.debug("Notificación MarkdownV2 Telegram enviada exitosamente")
                    return True
                else:
                    resp_text = await response.text()
                    logger.warning(
                        f"Error Telegram API (MDV2): {response.status} | {resp_text[:200]}"
                    )
                    return False

        except aiohttp.ClientError as e:
            logger.error(f"Error de red al enviar Telegram MDV2: {e}")
            return False
        except Exception as e:
            logger.error(f"Error inesperado al enviar Telegram MDV2: {e}", exc_info=True)
            return False

    async def notify_window_summary(
        self,
        window_slug: str,
        open_price: float,
        close_price: float,
        trade: Optional[Trade],
        skip_reason: str,
        current_btc_price: float,
        stats: dict,
        dry_run: bool,
    ) -> None:
        """
        Envía el resumen de la ventana de 5 minutos que acaba de cerrar.
        Se llama al inicio de cada nueva ventana.

        Args:
            window_slug: Slug de la ventana que cerró (ej: btc-updown-5m-1710000000)
            open_price: Precio BTC de apertura de esa ventana
            close_price: Precio BTC de cierre de esa ventana
            trade: Trade colocado en esa ventana, o None si no hubo
            skip_reason: Razón por la que no se operó (si trade is None)
            current_btc_price: Precio actual de BTC (inicio de nueva ventana)
            stats: Diccionario de estadísticas del RiskManager
            dry_run: True si el bot está en modo simulación
        """
        # Movimiento real de BTC en la ventana anterior
        actual_went_up = close_price > open_price
        actual_emoji = "⬆️" if actual_went_up else "⬇️"
        actual_dir = "UP" if actual_went_up else "DOWN"

        # Slug con guiones escapados para MarkdownV2
        slug_escaped = _escape_mdv2(window_slug)
        open_str = _escape_mdv2(f"${open_price:,.2f}")
        close_str = _escape_mdv2(f"${close_price:,.2f}")

        lines: list[str] = [
            "📊 *Resumen ventana anterior*",
            f"⏱ Ventana: `{slug_escaped}`",
            f"📈 BTC apertura: `{open_str}` → cierre: `{close_str}`",
            f"📉 Resultado real: {actual_emoji} {actual_dir}",
        ]

        if trade is not None and trade.resolved:
            result_emoji = "✅" if trade.won else "❌"
            win_str = "GANÓ" if trade.won else "PERDIÓ"
            pnl_sign = "\\+" if trade.pnl_usdc >= 0 else ""
            pnl_val = _escape_mdv2(f"${abs(trade.pnl_usdc):.2f}")
            pnl_display = f"{pnl_sign}{pnl_val}"
            price_str = _escape_mdv2(f"${trade.token_price:.2f}")

            lines.append(
                f"\n🤖 *Bot apostó:* {trade.direction} @ `{price_str}`"
            )

            if dry_run:
                lines.append(
                    f"🧪 \\[SIM\\] Resultado: {result_emoji} {win_str} "
                    f"\\({pnl_display}\\)"
                )
            else:
                lines.append(
                    f"💰 \\[REAL\\] Resultado: {result_emoji} {win_str} "
                    f"\\({pnl_display}\\)"
                )
        else:
            # No se apostó en esta ventana
            reason = skip_reason or "Sin señal en esta ventana"
            reason_escaped = _escape_mdv2(reason)
            lines.append(f"\n⏭️ No apostó esta ventana \\({reason_escaped}\\)")

        # Estado actual
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        win_rate = stats.get("win_rate", 0.0)
        daily_pnl = stats.get("daily_pnl_usdc", 0.0)

        pnl_sign = "\\+" if daily_pnl >= 0 else "\\-"
        pnl_abs = _escape_mdv2(f"${abs(daily_pnl):.2f}")
        winrate_pct = _escape_mdv2(f"{win_rate:.0%}")
        btc_now_str = _escape_mdv2(f"${current_btc_price:,.2f}")

        lines.extend([
            "\n📊 *Estado actual*",
            f"💵 BTC ahora: `{btc_now_str}`",
            f"📅 P&L hoy: `{pnl_sign}{pnl_abs}`",
            f"🏆 Winrate: `{winrate_pct}` \\({wins}W / {losses}L\\)",
        ])

        message = "\n".join(lines)
        await self._send_mdv2(message)

    async def notify_bot_stop(self, reason: str = "Manual") -> None:
        """
        Notifica la detención del bot.

        Args:
            reason: Motivo de detención
        """
        message = (
            f"*🛑 BOT DETENIDO*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"*Motivo:* `{reason}`"
        )

        await self._send(message)
