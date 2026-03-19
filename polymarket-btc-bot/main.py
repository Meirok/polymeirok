"""
Punto de entrada principal del bot de trading Polymarket BTC Up/Down 5m.

Uso:
    python main.py              # Modo simulación (DRY_RUN=true)
    python main.py --live       # Modo producción (requiere credenciales)
    python main.py --summary    # Mostrar solo resumen de PnL y salir

Flags:
    --live      Activa PRODUCTION=true y DRY_RUN=false
    --summary   Muestra resumen de la sesión y sale (sin trading)
    --help      Muestra esta ayuda
"""

import argparse
import asyncio
import signal
import sys
from typing import Optional

from src.bot import TradingBot
from src.config import Config
from src.logger import get_logger, setup_logger

# Inicializar logger raíz
setup_logger("polybot")
logger = get_logger("main")


def parse_args() -> argparse.Namespace:
    """Parsea los argumentos de línea de comando."""
    parser = argparse.ArgumentParser(
        description="Bot de Trading Polymarket BTC Up/Down 5 minutos",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--live",
        action="store_true",
        help="Activar modo producción (requiere PRIVATE_KEY y POLYMARKET_PROXY_ADDRESS)",
    )

    parser.add_argument(
        "--summary",
        action="store_true",
        help="Mostrar solo resumen de PnL y salir sin iniciar trading",
    )

    return parser.parse_args()


def validate_and_build_config(args: argparse.Namespace) -> Optional[Config]:
    """
    Construye y valida la configuración.

    Args:
        args: Argumentos de línea de comando

    Returns:
        Config válida, o None si hay errores críticos
    """
    config = Config()

    # Aplicar flags de CLI sobre la configuración del .env
    if args.live:
        config.production = True
        config.dry_run = False
        logger.info("Modo PRODUCCIÓN activado por --live")
    else:
        # Por defecto, forzar simulación si no se usa --live
        if not config.production:
            config.dry_run = True

    # Validar configuración
    errors = config.validate()
    if errors:
        logger.error("Errores de configuración:")
        for err in errors:
            logger.error(f"  • {err}")

        if config.production and not config.dry_run:
            logger.error(
                "Corrija los errores en el archivo .env antes de continuar."
            )
            return None
        else:
            logger.warning(
                "Errores detectados pero continuando en modo simulación."
            )

    return config


def print_config_summary(config: Config) -> None:
    """Imprime resumen de configuración al iniciar."""
    mode = "SIMULACIÓN (DRY_RUN)" if config.dry_run or not config.production else "PRODUCCIÓN REAL"

    print("\n" + "=" * 60)
    print("  POLYMARKET BTC UP/DOWN 5M TRADING BOT")
    print("=" * 60)
    print(f"  Modo:              {mode}")
    print(f"  Apuesta por trade: ${config.bet_amount_usdc:.2f} USDC")
    print(f"  Confianza mínima:  {config.min_confidence:.0%}")
    print(f"  Rango de odds:     {config.min_odds} - {config.max_odds}")
    print(f"  Max trades/hora:   {config.max_trades_per_hour}")
    print(f"  Stop-loss diario:  ${config.stop_loss_daily_usd:.2f} USDC")
    print(f"  Entrada:           {config.entry_seconds_before}s antes del cierre")
    print(f"  Telegram:          {'✓ Activo' if config.is_telegram_configured() else '✗ No configurado'}")
    print("=" * 60 + "\n")

    if config.dry_run or not config.production:
        print("  ⚠  MODO SIMULACIÓN: Las órdenes NO se ejecutarán en Polymarket.")
        print("  ⚠  Para operar en real, usa: python main.py --live")
        print()


async def run_bot(config: Config) -> None:
    """
    Ejecuta el bot de trading con manejo de señales del sistema.

    Args:
        config: Configuración validada del bot
    """
    bot = TradingBot(config)

    # Configurar manejo de señales para parada elegante
    loop = asyncio.get_event_loop()

    def handle_shutdown(sig_name: str) -> None:
        logger.info(f"Señal {sig_name} recibida. Deteniendo bot...")
        asyncio.create_task(shutdown(bot, sig_name))

    async def shutdown(bot: TradingBot, reason: str) -> None:
        """Parada elegante del bot."""
        await bot.stop()
        logger.info("Bot detenido. Generando resumen final...")
        await bot.send_daily_summary()

    # Registrar handlers de señales
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig,
                lambda s=sig: handle_shutdown(s.name),
            )
        except (NotImplementedError, RuntimeError):
            # Windows no soporta add_signal_handler
            pass

    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Interrupción de teclado. Deteniendo...")
        await bot.stop()
    finally:
        # Mostrar resumen final al salir
        bot.risk_manager.print_summary()


def show_summary_only() -> None:
    """
    Muestra solo información de configuración y sale.
    Útil para verificar la configuración sin iniciar el bot.
    """
    config = Config()
    print_config_summary(config)

    print("  Para iniciar el bot en simulación:")
    print("    python main.py")
    print()
    print("  Para iniciar el bot en producción:")
    print("    python main.py --live")
    print()


def main() -> int:
    """
    Función principal del programa.

    Returns:
        Código de salida (0 = éxito, 1 = error)
    """
    args = parse_args()

    # Modo solo-resumen: mostrar config y salir
    if args.summary:
        show_summary_only()
        return 0

    # Construir y validar configuración
    config = validate_and_build_config(args)
    if config is None:
        return 1

    # Mostrar resumen de configuración
    print_config_summary(config)

    # Advertencia de producción
    if config.production and not config.dry_run:
        print("  ⚠  ADVERTENCIA: Vas a operar con DINERO REAL en Polymarket.")
        print("  ⚠  Asegúrate de entender los riesgos antes de continuar.")
        print()
        try:
            response = input("  ¿Confirmar inicio en modo PRODUCCIÓN? (escribe 'SI' para continuar): ")
            if response.strip().upper() != "SI":
                print("  Inicio cancelado por el usuario.")
                return 0
        except (KeyboardInterrupt, EOFError):
            print("\n  Inicio cancelado.")
            return 0

    logger.info("Iniciando bot de trading...")

    # Ejecutar bot
    try:
        asyncio.run(run_bot(config))
    except KeyboardInterrupt:
        logger.info("Bot detenido por el usuario")
    except Exception as e:
        logger.error(f"Error fatal: {e}", exc_info=True)
        return 1

    logger.info("Bot finalizado exitosamente")
    return 0


if __name__ == "__main__":
    sys.exit(main())
