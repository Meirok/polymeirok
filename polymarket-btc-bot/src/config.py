"""
Módulo de configuración del bot.
Carga variables de entorno desde el archivo .env y provee valores por defecto.
"""

import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


def _get_bool(key: str, default: bool = False) -> bool:
    """Convierte variable de entorno a booleano."""
    val = os.getenv(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")


def _get_float(key: str, default: float) -> float:
    """Convierte variable de entorno a float."""
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _get_int(key: str, default: int) -> int:
    """Convierte variable de entorno a int."""
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


@dataclass
class Config:
    """Configuración central del bot de trading."""

    # --- Credenciales de Polymarket ---
    private_key: str = field(default_factory=lambda: os.getenv("PRIVATE_KEY", ""))
    polymarket_proxy_address: str = field(
        default_factory=lambda: os.getenv("POLYMARKET_PROXY_ADDRESS", "")
    )
    signature_type: int = field(
        default_factory=lambda: _get_int("SIGNATURE_TYPE", 0)
    )

    # --- Modo de operación ---
    production: bool = field(default_factory=lambda: _get_bool("PRODUCTION", False))
    dry_run: bool = field(default_factory=lambda: _get_bool("DRY_RUN", True))

    # --- Parámetros de trading ---
    bet_amount_usdc: float = field(
        default_factory=lambda: _get_float("BET_AMOUNT_USDC", 1.0)
    )
    min_confidence: float = field(
        default_factory=lambda: _get_float("MIN_CONFIDENCE", 0.55)
    )
    min_odds: float = field(default_factory=lambda: _get_float("MIN_ODDS", 0.55))
    max_odds: float = field(default_factory=lambda: _get_float("MAX_ODDS", 0.92))
    entry_seconds_before: int = field(
        default_factory=lambda: _get_int("ENTRY_SECONDS_BEFORE", 25)
    )

    # --- Gestión de riesgo ---
    max_trades_per_hour: int = field(
        default_factory=lambda: _get_int("MAX_TRADES_PER_HOUR", 12)
    )
    stop_loss_daily_usd: float = field(
        default_factory=lambda: _get_float("STOP_LOSS_DAILY_USD", 5.0)
    )

    # --- Latency Sniper ---
    sniper_threshold: float = field(
        default_factory=lambda: _get_float("SNIPER_THRESHOLD", 0.05)
    )
    sniper_entry_window_max: int = field(
        default_factory=lambda: _get_int("SNIPER_ENTRY_WINDOW_MAX", 30)
    )
    sniper_entry_window_min: int = field(
        default_factory=lambda: _get_int("SNIPER_ENTRY_WINDOW_MIN", 5)
    )

    # --- Notificaciones Telegram ---
    telegram_bot_token: Optional[str] = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN")
    )
    telegram_chat_id: Optional[str] = field(
        default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID")
    )

    # --- URLs de APIs ---
    gamma_api_base: str = "https://gamma-api.polymarket.com"
    clob_api_base: str = "https://clob.polymarket.com"
    binance_ws_url: str = "wss://stream.binance.com:9443/ws/btcusdt@kline_1m"

    def validate(self) -> list[str]:
        """
        Valida la configuración y retorna lista de errores.
        En modo DRY_RUN no se requieren credenciales reales.
        """
        errors: list[str] = []

        if not self.dry_run and self.production:
            # Solo requerimos credenciales en modo producción real
            if not self.private_key:
                errors.append("PRIVATE_KEY es requerida en modo PRODUCTION")
            if not self.polymarket_proxy_address:
                errors.append("POLYMARKET_PROXY_ADDRESS es requerida en modo PRODUCTION")

        if self.bet_amount_usdc <= 0:
            errors.append("BET_AMOUNT_USDC debe ser mayor a 0")

        if not (0.0 < self.min_confidence <= 1.0):
            errors.append("MIN_CONFIDENCE debe estar entre 0 y 1")

        if not (0.0 < self.min_odds < self.max_odds < 1.0):
            errors.append("MIN_ODDS y MAX_ODDS deben estar entre 0 y 1, con MIN < MAX")

        if self.max_trades_per_hour <= 0:
            errors.append("MAX_TRADES_PER_HOUR debe ser mayor a 0")

        if self.stop_loss_daily_usd <= 0:
            errors.append("STOP_LOSS_DAILY_USD debe ser mayor a 0")

        return errors

    def is_telegram_configured(self) -> bool:
        """Verifica si Telegram está configurado correctamente."""
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    def __str__(self) -> str:
        mode = "PRODUCCIÓN" if self.production and not self.dry_run else "SIMULACIÓN"
        return (
            f"Config[modo={mode}, apuesta={self.bet_amount_usdc} USDC, "
            f"confianza_min={self.min_confidence:.0%}, "
            f"odds={self.min_odds}-{self.max_odds}, "
            f"stop_loss={self.stop_loss_daily_usd} USD]"
        )
