# 🤖 Polymarket BTC Up/Down 5m Trading Bot

Bot de trading automático para los mercados de predicción **"¿Subirá o bajará el BTC en los próximos 5 minutos?"** en [Polymarket](https://polymarket.com).

> ⚠️ **ADVERTENCIA DE RIESGO**: Este bot opera con activos de predicción. Existe un riesgo real de pérdida de capital. Úsalo bajo tu propia responsabilidad y solo con dinero que puedas permitirte perder.

---

## ¿Qué hace este bot?

Cada 5 minutos, Polymarket abre un mercado binario donde puedes apostar si el precio de Bitcoin será **más alto (UP)** o **más bajo (DOWN)** que el precio de apertura al cerrarse la ventana de 5 minutos.

Este bot:
1. **Recibe precios en tiempo real** de BTC/USDT vía WebSocket de Binance
2. **Analiza 7 indicadores técnicos** para generar una señal de trading (UP/DOWN/SKIP)
3. **Aplica filtros de riesgo** (confianza, odds, stop-loss, frecuencia)
4. **Coloca órdenes automáticamente** en Polymarket usando la API CLOB
5. **Resuelve y registra** el resultado de cada trade
6. **Envía notificaciones** a Telegram con cada entrada, resultado y resumen diario

---

## Arquitectura del proyecto

```
polymarket-btc-bot/
├── main.py                 # Punto de entrada, CLI y gestión de señales
├── requirements.txt        # Dependencias Python
├── Dockerfile              # Imagen Docker para despliegue
├── docker-compose.yml      # Configuración de contenedor
├── .env.example            # Plantilla de variables de entorno
├── .gitignore
├── README.md
└── src/
    ├── config.py           # Carga y validación de configuración desde .env
    ├── logger.py           # Logging colorizado en consola y archivos diarios
    ├── price_feed.py       # WebSocket de Binance con reconexión automática
    ├── strategy.py         # 7 indicadores técnicos ponderados
    ├── polymarket_client.py # API de Polymarket (Gamma + CLOB)
    ├── risk_manager.py     # Stop-loss, filtros, historial de trades y PnL
    ├── bot.py              # Bucle principal de trading
    └── notifier.py         # Notificaciones por Telegram
```

---

## Requisitos

- Python 3.11 o superior
- Conexión a Internet estable
- Cuenta en Polymarket con USDC en Polygon (solo para modo producción)
- Bot de Telegram (opcional, para notificaciones)

---

## Instalación

### 1. Clonar el repositorio

```bash
git clone https://github.com/tu-usuario/polymarket-btc-bot.git
cd polymarket-btc-bot
```

### 2. Crear entorno virtual

```bash
python -m venv .venv
source .venv/bin/activate        # Linux / macOS
# .venv\Scripts\activate         # Windows
```

### 3. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 4. Configurar variables de entorno

```bash
cp .env.example .env
nano .env   # Editar con tus valores
```

---

## Configuración del archivo `.env`

| Variable | Descripción | Valor por defecto |
|---|---|---|
| `PRIVATE_KEY` | Clave privada de tu wallet Polygon (sin 0x) | *(vacío)* |
| `POLYMARKET_PROXY_ADDRESS` | Dirección del proxy de Polymarket | *(vacío)* |
| `SIGNATURE_TYPE` | Tipo de firma (0=EOA, 1=EIP-1271, 2=proxy) | `0` |
| `PRODUCTION` | `true` para ejecutar órdenes reales | `false` |
| `DRY_RUN` | `true` para simular sin enviar órdenes | `true` |
| `BET_AMOUNT_USDC` | USDC a apostar por trade | `5.0` |
| `MIN_CONFIDENCE` | Confianza mínima de señal (0 a 1) | `0.65` |
| `MIN_ODDS` | Precio mínimo de token para operar | `0.55` |
| `MAX_ODDS` | Precio máximo de token para operar | `0.92` |
| `ENTRY_SECONDS_BEFORE` | Segundos antes del cierre para entrar | `25` |
| `MAX_TRADES_PER_HOUR` | Máximo de trades por hora | `12` |
| `STOP_LOSS_DAILY_USD` | Stop-loss diario en USD | `20.0` |
| `TELEGRAM_BOT_TOKEN` | Token del bot de Telegram | *(vacío)* |
| `TELEGRAM_CHAT_ID` | ID del chat de Telegram | *(vacío)* |

### Obtener credenciales de Polymarket

1. Ve a [polymarket.com](https://polymarket.com) y conecta tu wallet de Polygon
2. Tu `POLYMARKET_PROXY_ADDRESS` es la dirección del proxy creado por Polymarket al registrarte
3. Tu `PRIVATE_KEY` es la clave privada de la wallet que controla ese proxy
4. Deposita USDC en Polygon en tu cuenta de Polymarket

### Configurar bot de Telegram (opcional)

1. Habla con [@BotFather](https://t.me/BotFather) en Telegram
2. Crea un nuevo bot con `/newbot` y guarda el token
3. Habla con [@userinfobot](https://t.me/userinfobot) para obtener tu `chat_id`
4. Agrega ambos valores a tu `.env`

---

## Ejecución

### Modo simulación (recomendado para empezar)

No requiere credenciales. Las órdenes se simulan y se loguean, pero **no se ejecutan** en Polymarket.

```bash
python main.py
```

### Modo producción (dinero real)

Requiere `PRIVATE_KEY`, `POLYMARKET_PROXY_ADDRESS` y `PRODUCTION=true` en `.env`.

```bash
python main.py --live
```

El bot pedirá confirmación antes de iniciar en producción.

### Ver configuración actual

```bash
python main.py --summary
```

### Detener el bot

Presiona `Ctrl+C`. El bot guardará el resumen de la sesión antes de salir.

---

## Despliegue con Docker en un VPS

### 1. Preparar el VPS

```bash
# Instalar Docker en Ubuntu/Debian
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```

### 2. Clonar el proyecto en el VPS

```bash
git clone https://github.com/tu-usuario/polymarket-btc-bot.git
cd polymarket-btc-bot
cp .env.example .env
nano .env   # Configurar variables de producción
```

### 3. Construir e iniciar el contenedor

```bash
# Construir la imagen
docker-compose build

# Iniciar en background
docker-compose up -d

# Ver logs en tiempo real
docker-compose logs -f polybot
```

### 4. Gestión del contenedor

```bash
# Detener el bot
docker-compose down

# Reiniciar el bot
docker-compose restart polybot

# Ver estado
docker-compose ps

# Ver logs de hoy
cat logs/bot.log
```

### 5. Acceder a logs persistentes

Los logs se guardan en `./logs/` en el host, rotando diariamente. El contenedor conserva 30 días de historial.

---

## Los 7 indicadores técnicos

El bot analiza el mercado usando una combinación ponderada de 7 indicadores técnicos. Cada indicador produce un score en el rango **[-1, +1]** donde +1 = señal alcista y -1 = señal bajista.

### 1. RSI — Relative Strength Index (peso: 20%)

Mide la velocidad y magnitud de los movimientos de precio recientes para identificar condiciones de sobrecompra o sobreventa.

- **RSI < 30** (sobreventa) → señal de compra fuerte (+1)
- **RSI > 70** (sobrecompra) → señal de venta fuerte (-1)
- **RSI entre 30-70** → señal débil proporcional

*Parámetros: período 14 velas de 1 minuto*

### 2. MACD — Moving Average Convergence Divergence (peso: 20%)

Mide la diferencia entre dos medias móviles exponenciales para detectar cambios de tendencia y momentum.

- **MACD > línea de señal** → tendencia alcista (+)
- **MACD < línea de señal** → tendencia bajista (-)

*Parámetros: EMA rápida 12, EMA lenta 26, señal 9*

### 3. Cruce de EMAs — Exponential Moving Averages (peso: 15%)

Detecta cruces entre la EMA corta (9 períodos) y la EMA larga (21 períodos).

- **EMA9 > EMA21** → tendencia alcista de corto plazo (+)
- **EMA9 < EMA21** → tendencia bajista de corto plazo (-)

*Parámetros: EMA rápida 9, EMA lenta 21*

### 4. Bandas de Bollinger (peso: 15%)

Mide la volatilidad usando una media móvil central y bandas superior/inferior a 2 desviaciones estándar.

- **Precio cerca de banda inferior** → posible rebote alcista (+)
- **Precio cerca de banda superior** → posible reversión bajista (-)

*Parámetros: período 20, 2 desviaciones estándar*

### 5. Momentum (peso: 15%)

Mide el cambio porcentual del precio a lo largo de N períodos para evaluar la fuerza de la tendencia actual.

- **Momentum positivo** → precio subiendo con fuerza (+)
- **Momentum negativo** → precio cayendo con fuerza (-)

*Parámetros: 10 períodos (10 minutos de datos de 1m)*

### 6. VWAP Proxy — Volume Weighted Average Price (peso: 10%)

Calcula un precio promedio ponderado por volumen como referencia de valor justo.

- **Precio > VWAP** → mercado alcista, compradores dominantes (+)
- **Precio < VWAP** → mercado bajista, vendedores dominantes (-)

*Parámetros: últimas 20 velas*

### 7. Delta de ventana (peso: 5%)

Mide el cambio porcentual del precio de BTC desde la apertura de la ventana de 5 minutos actual.

- **Delta positivo** → precio subiendo en la ventana actual (+)
- **Delta negativo** → precio cayendo en la ventana actual (-)

*Calcula momentum intra-ventana de los últimos 5 minutos*

### Score combinado y decisión

El score final es la suma ponderada de los 7 indicadores. La dirección se elige así:

| Score | Dirección | Acción |
|---|---|---|
| `> +0.10` | **UP** | Comprar tokens UP |
| `< -0.10` | **DOWN** | Comprar tokens DOWN |
| Entre `-0.10` y `+0.10` | **SKIP** | No operar |

La **confianza** es el valor absoluto del score: a más extremo, más confianza.

---

## Gestión de riesgo

El bot implementa múltiples capas de protección:

| Filtro | Descripción |
|---|---|
| **Stop-loss diario** | Detiene el bot si las pérdidas superan `STOP_LOSS_DAILY_USD` |
| **Confianza mínima** | Solo opera si la señal supera `MIN_CONFIDENCE` |
| **Filtro de odds** | Solo opera si el precio del token está entre `MIN_ODDS` y `MAX_ODDS` |
| **Frecuencia máxima** | Máximo `MAX_TRADES_PER_HOUR` trades por hora |
| **Una operación por ventana** | Solo un trade por ventana de 5 minutos |

---

## Advertencias importantes

1. **Este bot no garantiza ganancias**. Los mercados de predicción son impredecibles y los indicadores técnicos no son perfectos.

2. **Los mercados de 5 minutos son muy aleatorios**. En ventanas tan cortas, el ruido supera a la señal en la mayoría de los casos.

3. **Empieza siempre en simulación** (`DRY_RUN=true`) y analiza los resultados durante al menos varios días antes de operar con dinero real.

4. **Nunca arriesgues dinero que no puedas perder**. Configura un stop-loss adecuado y nunca deposits más de lo que estás dispuesto a perder completamente.

5. **Las comisiones de Polymarket** afectan la rentabilidad. Considera el spread del orderbook al configurar `MIN_ODDS` y `MAX_ODDS`.

6. **Mantén las credenciales seguras**. Nunca subas el archivo `.env` a GitHub ni compartas tu `PRIVATE_KEY`.

---

## Licencia

MIT License. Úsalo bajo tu propia responsabilidad.
