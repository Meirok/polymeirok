"""
Módulo de logging con salida colorizada y archivos de log diarios.
Proporciona un logger centralizado para todo el bot.
"""

import logging
import os
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from typing import Optional

from colorama import Fore, Style, init

# Inicializar colorama para soporte multiplataforma
init(autoreset=True)

# Directorio de logs
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


class ColorizedFormatter(logging.Formatter):
    """Formateador de logs con colores para la terminal."""

    # Colores por nivel de log
    LEVEL_COLORS = {
        logging.DEBUG: Fore.CYAN,
        logging.INFO: Fore.GREEN,
        logging.WARNING: Fore.YELLOW,
        logging.ERROR: Fore.RED,
        logging.CRITICAL: Fore.RED + Style.BRIGHT,
    }

    # Colores para las partes del mensaje
    TIME_COLOR = Fore.WHITE + Style.DIM
    NAME_COLOR = Fore.BLUE
    RESET = Style.RESET_ALL

    def format(self, record: logging.LogRecord) -> str:
        """Formatea el registro con colores."""
        level_color = self.LEVEL_COLORS.get(record.levelno, Fore.WHITE)

        # Timestamp formateado
        timestamp = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")

        # Nombre del logger (recortado si es muy largo)
        name = record.name.split(".")[-1][:12].ljust(12)

        # Nivel del log
        level = record.levelname[:7].ljust(7)

        # Mensaje formateado con colores
        formatted = (
            f"{self.TIME_COLOR}[{timestamp}]{self.RESET} "
            f"{self.NAME_COLOR}[{name}]{self.RESET} "
            f"{level_color}[{level}]{self.RESET} "
            f"{record.getMessage()}"
        )

        # Agregar información de excepción si existe
        if record.exc_info:
            formatted += f"\n{Fore.RED}{self.formatException(record.exc_info)}{self.RESET}"

        return formatted


class PlainFormatter(logging.Formatter):
    """Formateador sin colores para archivos de log."""

    def format(self, record: logging.LogRecord) -> str:
        """Formatea el registro sin colores."""
        timestamp = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        name = record.name.split(".")[-1][:12].ljust(12)
        level = record.levelname[:7].ljust(7)

        formatted = f"[{timestamp}] [{name}] [{level}] {record.getMessage()}"

        if record.exc_info:
            formatted += f"\n{self.formatException(record.exc_info)}"

        return formatted


def setup_logger(
    name: str,
    level: int = logging.DEBUG,
    log_dir: Optional[str] = None,
) -> logging.Logger:
    """
    Configura y retorna un logger con salida a consola (colorizada)
    y archivo de log rotativo diario.

    Args:
        name: Nombre del logger
        level: Nivel mínimo de logging
        log_dir: Directorio para archivos de log (usa LOGS_DIR por defecto)

    Returns:
        Logger configurado
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Evitar duplicar handlers si el logger ya fue configurado
    if logger.handlers:
        return logger

    # --- Handler de consola con colores ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(ColorizedFormatter())
    logger.addHandler(console_handler)

    # --- Handler de archivo con rotación diaria ---
    log_directory = log_dir or LOGS_DIR
    os.makedirs(log_directory, exist_ok=True)

    log_file = os.path.join(log_directory, "bot.log")
    file_handler = TimedRotatingFileHandler(
        filename=log_file,
        when="midnight",       # Rotar a medianoche
        interval=1,            # Cada día
        backupCount=30,        # Conservar 30 días de logs
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(PlainFormatter())
    file_handler.suffix = "%Y-%m-%d"
    logger.addHandler(file_handler)

    # Evitar propagación al logger raíz
    logger.propagate = False

    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Obtiene un logger configurado para el módulo especificado.
    Si el logger raíz del bot no está configurado, lo inicializa.

    Args:
        name: Nombre del módulo (ej: 'price_feed', 'strategy')

    Returns:
        Logger listo para usar
    """
    root_name = "polybot"
    full_name = f"{root_name}.{name}"

    # Asegurar que el logger raíz está configurado
    setup_logger(root_name)

    logger = logging.getLogger(full_name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = True  # Propagar al logger raíz

    return logger
