import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from typing import Optional
import logging

# ──────────────────────────────────────────────
# CONFIGURACIÓN
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

TOKEN = os.getenv("DISCORD_TOKEN", "TU_TOKEN_AQUI")
DATA_FILE = "flights.json"
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL", "30"))  # cada 30 min por defecto

# ──────────────────────────────────────────────
# INTENTS & BOT
# ──────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ──────────────────────────────────────────────
# PERSISTENCIA DE DATOS
# ──────────────────────────────────────────────
def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"flights": {}, "user_settings": {}}

def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

data = load_data()

# ──────────────────────────────────────────────
# SCRAPER DE PRECIOS (Google Flights via SerpAPI / Aviationstack / fallback)
# ──────────────────────────────────────────────
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")    # opcional
AVIATIONSTACK_KEY = os.getenv("AVIATIONSTACK_KEY", "")  # opcional

async def fetch_price_serpapi(origin: str, dest: str, date: str) -> Optional[float]:
    """Busca precios usando SerpAPI (Google Flights)."""
    if not SERPAPI_KEY:
        return None
    url = "https://serpapi.com/search"
    params = {
        "engine": "google_flights",
        "departure_id": origin.upper(),
        "arrival_id": dest.upper(),
        "outbound_date": date,
        "currency": "MXN",
        "hl": "es",
        "api_key": SERPAPI_KEY
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    result = await r.json()
                    best = result.get("best_flights", [])
                    if best:
                        price = best[0].get("price")
                        if price:
                            return float(str(price).replace(",", "").replace("$", ""))
    except Exception as e:
        log.warning(f"SerpAPI error: {e}")
    return None

async def fetch_price_mock(origin: str, dest: str, date: str) -> float:
    """
    Precio simulado para demostración cuando no hay API keys.
    En producción reemplaza esto con tu proveedor favorito.
    """
    import hashlib, math, time
    seed = hashlib.md5(f"{origin}{dest}{date}".encode()).hexdigest()
    base = (int(seed[:4], 16) % 8000) + 1500   # precio base entre 1500 y 9500 MXN
    # pequeña variación aleatoria con el tiempo para simular cambios
    variation = math.sin(time.time() / 3600) * 200  # oscila ±200 por hora
    return round(base + variation, 2)

async def get_flight_price(origin: str, dest: str, date: str) -> Optional[float]:
    """Intenta obtener precio real; si falla usa mock."""
    price = await fetch_price_serpapi(origin, dest, date)
    if price is None:
        price = await fetch_price_mock(origin, dest, date)
    return price

# ──────────────────────────────────────────────
# HELPERS DE FORMATO
# ──────────────────────────────────────────────
IATA_NAMES = {
    "MEX": "Ciudad de México", "CUN": "Cancún", "GDL": "Guadalajara",
    "MTY": "Monterrey", "TIJ": "Tijuana", "LAX": "Los Ángeles",
    "JFK": "Nueva York (JFK)", "MIA": "Miami", "MAD": "Madrid",
    "BCN": "Barcelona", "BOG": "Bogotá", "LIM": "Lima",
    "SCL": "Santiago", "EZE": "Buenos Aires", "GRU": "São Paulo",
    "ORD": "Chicago", "DFW": "Dallas", "SFO": "San Francisco",
    "CDG": "París", "LHR": "Londres", "FRA": "Frankfurt",
    "NRT": "Tokio", "DXB": "Dubái", "SIN": "Singapur",
}

def airport_name(code: str) -> str:
    return IATA_NAMES.get(code.upper(), code.upper())

def format_price(price: float) -> str:
    return f"${price:,.0f} MXN"

def price_diff_emoji(old: float, new: float) -> str:
    if new < old:
        pct = ((old - new) / old) * 100
        return f"📉 **BAJÓ** {pct:.1f}%"
    elif new > old:
        pct = ((new - old) / old) * 100
        return f"📈 **SUBIÓ** {pct:.1f}%"
    return "➡️ Sin cambio"

def flight_id(origin: str, dest: str, date: str, user_id: int) -> str:
    return f"{user_id}_{origin.upper()}_{dest.upper()}_{date}"

# ──────────────────────────────────────────────
# EMBEDS REUTILIZABLES
# ──────────────────────────────────────────────
def embed_flight_card(
    flight: dict,
    title: str = "✈️ Vuelo Monitoreado",
    color: int = 0x5865F2
) -> discord.Embed:
    origin = flight["origin"]
    dest = flight["dest"]
    date = flight["date"]
    price = flight.get("last_price")
    min_price = flight.get("min_price")
    max_price = flight.get("max_price")
    checks = flight.get("checks", 0)
    alert = flight.get("alert_threshold")

    embed = discord.Embed(title=title, color=color)
    embed.add_field(
        name="🛫 Ruta",
        value=f"`{origin}` → `{dest}`\n{airport_name(origin)} → {airport_name(dest)}",
        inline=True
    )
    embed.add_field(name="📅 Fecha", value=date, inline=True)
    embed.add_field(
        name="💰 Precio Actual",
        value=format_price(price) if price else "—",
        inline=True
    )
    if min_price and max_price:
        embed.add_field(
            name="📊 Rango Histórico",
            value=f"Min: {format_price(min_price)}\nMax: {format_price(max_price)}",
            inline=True
        )
    if alert:
        embed.add_field(
            name="🔔 Alerta Activa",
            value=f"Notificar si baja de {format_price(alert)}",
            inline=True
        )
    embed.add_field(name="🔍 Revisiones", value=str(checks), inline=True)
    embed.set_footer(text=f"ID: {flight['id']} • Cada {CHECK_INTERVAL_MINUTES} min")
    return embed

# ──────────────────────────────────────────────
# TASK: MONITOR DE PRECIOS
# ──────────────────────────────────────────────
@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def price_monitor():
    if not data["flights"]:
        return
    log.info(f"[Monitor] Revisando {len(data['flights'])} vuelos...")
    for fid, flight in list(data["flights"].items()):
        try:
            new_price = await get_flight_price(flight["origin"], flight["dest"], flight["date"])
            if new_price is None:
                continue

            old_price = flight.get("last_price")
            flight["last_price"] = new_price
            flight["checks"] = flight.get("checks", 0) + 1
            flight["last_checked"] = datetime.utcnow().isoformat()

            if old_price is None:
                flight["min_price"] = new_price
                flight["max_price"] = new_price
                save_data(data)
                continue

            flight["min_price"] = min(flight.get("min_price", new_price), new_price)
            flight["max_price"] = max(flight.get("max_price", new_price), new_price)

            price_changed = abs(new_price - old_price) > 1  # umbral mínimo $1
            alert_threshold = flight.get("alert_threshold")
            alert_triggered = alert_threshold and new_price <= alert_threshold and old_price > alert_threshold

            channel_id = flight.get("channel_id")
            user_id = flight.get("user_id")

            if (price_changed or alert_triggered) and channel_id:
                channel = bot.get_channel(int(channel_id))
                if channel:
                    diff_text = price_diff_emoji(old_price, new_price)
                    color = 0x57F287 if new_price < old_price else 0xED4245

                    embed = discord.Embed(
                        title="🔔 Cambio de Precio Detectado",
                        description=f"**{airport_name(flight['origin'])} → {airport_name(flight['dest'])}**\n📅 {flight['date']}",
                        color=color,
                        timestamp=datetime.utcnow()
                    )
                    embed.add_field(name="Precio Anterior", value=format_price(old_price), inline=True)
                    embed.add_field(name="Precio Actual", value=format_price(new_price), inline=True)
                    embed.add_field(name="Cambio", value=diff_text, inline=True)
                    embed.add_field(
                        name="📊 Histórico",
                        value=f"Mín: {format_price(flight['min_price'])} | Máx: {format_price(flight['max_price'])}",
                        inline=False
                    )

                    if alert_triggered:
                        embed.add_field(
                            name="🚨 ¡ALERTA DE PRECIO!",
                            value=f"El precio bajó a {format_price(new_price)}, que es ≤ tu alerta de {format_price(alert_threshold)}",
                            inline=False
                        )

                    mention = f"<@{user_id}> " if user_id else ""
                    await channel.send(f"{mention}", embed=embed)
                    log.info(f"[Monitor] Notificado cambio en {fid}: {old_price} → {new_price}")

            save_data(data)

        except Exception as e:
            log.error(f"[Monitor] Error en {fid}: {e}")

@price_monitor.before_loop
async def before_monitor():
    await bot.wait_until_ready()

# ──────────────────────────────────────────────
# VIEWS: INTERFAZ INTERACTIVA
# ──────────────────────────────────────────────

class FlightListView(discord.ui.View):
    """Paginación y acciones para la lista de vuelos."""
    def __init__(self, flights: list, user_id: int):
        super().__init__(timeout=120)
        self.flights = flights
        self.user_id = user_id
        self.page = 0
        self.per_page = 3

    def get_embed(self) -> discord.Embed:
        start = self.page * self.per_page
        end = start + self.per_page
        page_flights = self.flights[start:end]
        total_pages = max(1, -(-len(self.flights) // self.per_page))

        embed = discord.Embed(
            title="✈️ Tus Vuelos Monitoreados",
            description=f"Página {self.page + 1}/{total_pages} • {len(self.flights)} vuelo(s) activo(s)",
            color=0x5865F2
        )

        for f in page_flights:
            price = f.get("last_price")
            min_p = f.get("min_price")
            val = f"🛫 {airport_name(f['origin'])} → {airport_name(f['dest'])}\n"
            val += f"📅 {f['date']}\n"
            val += f"💰 {format_price(price) if price else 'Pendiente'}\n"
            if min_p and f.get("max_price"):
                val += f"📊 Mín {format_price(min_p)} | Máx {format_price(f['max_price'])}\n"
            val += f"🔍 Revisiones: {f.get('checks', 0)}"
            embed.add_field(name=f"ID: `{f['id']}`", value=val, inline=False)

        embed.set_footer(text="Usa /vuelo_eliminar <id> para dejar de monitorear")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("Solo puedes controlar tu propia lista.", ephemeral=True)
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("Solo puedes controlar tu propia lista.", ephemeral=True)
        total_pages = max(1, -(-len(self.flights) // self.per_page))
        if self.page < total_pages - 1:
            self.page += 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="🔄 Actualizar Precios", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("Solo puedes controlar tu propia lista.", ephemeral=True)
        await interaction.response.defer(thinking=True)
        for f in self.flights:
            price = await get_flight_price(f["origin"], f["dest"], f["date"])
            if price:
                f["last_price"] = price
                f["checks"] = f.get("checks", 0) + 1
                f["min_price"] = min(f.get("min_price", price), price)
                f["max_price"] = max(f.get("max_price", price), price)
        save_data(data)
        await interaction.followup.edit_message(message_id=interaction.message.id, embed=self.get_embed(), view=self)


class AddFlightModal(discord.ui.Modal, title="➕ Agregar Vuelo a Monitorear"):
    origin = discord.ui.TextInput(
        label="Aeropuerto Origen (código IATA)",
        placeholder="Ej: MEX, GDL, MTY",
        max_length=3,
        min_length=3,
    )
    dest = discord.ui.TextInput(
        label="Aeropuerto Destino (código IATA)",
        placeholder="Ej: CUN, LAX, JFK",
        max_length=3,
        min_length=3,
    )
    date = discord.ui.TextInput(
        label="Fecha de vuelo (YYYY-MM-DD)",
        placeholder="Ej: 2025-12-15",
        max_length=10,
        min_length=10,
    )
    alert_threshold = discord.ui.TextInput(
        label="Alerta de precio (opcional, en MXN)",
        placeholder="Ej: 3500 (notifica si baja de este valor)",
        required=False,
        max_length=10,
    )

    async def on_submit(self, interaction: discord.Interaction):
        origin = self.origin.value.strip().upper()
        dest = self.dest.value.strip().upper()
        date_str = self.date.value.strip()
        threshold_str = self.alert_threshold.value.strip()

        # Validar fecha
        try:
            flight_date = datetime.strptime(date_str, "%Y-%m-%d")
            if flight_date.date() < datetime.utcnow().date():
                return await interaction.response.send_message(
                    "❌ La fecha no puede ser en el pasado.", ephemeral=True
                )
        except ValueError:
            return await interaction.response.send_message(
                "❌ Formato de fecha inválido. Usa YYYY-MM-DD (ej: 2025-12-15)", ephemeral=True
            )

        # Validar umbral
        threshold = None
        if threshold_str:
            try:
                threshold = float(threshold_str.replace(",", "").replace("$", ""))
            except ValueError:
                return await interaction.response.send_message(
                    "❌ El precio de alerta debe ser un número.", ephemeral=True
                )

        fid = flight_id(origin, dest, date_str, interaction.user.id)
        if fid in data["flights"]:
            return await interaction.response.send_message(
                "⚠️ Ya estás monitoreando este vuelo.", ephemeral=True
            )

        await interaction.response.defer(thinking=True)

        price = await get_flight_price(origin, dest, date_str)

        data["flights"][fid] = {
            "id": fid,
            "origin": origin,
            "dest": dest,
            "date": date_str,
            "user_id": interaction.user.id,
            "channel_id": interaction.channel_id,
            "last_price": price,
            "min_price": price,
            "max_price": price,
            "checks": 1 if price else 0,
            "alert_threshold": threshold,
            "created_at": datetime.utcnow().isoformat(),
            "last_checked": datetime.utcnow().isoformat(),
        }
        save_data(data)

        embed = embed_flight_card(
            data["flights"][fid],
            title="✅ Vuelo Agregado con Éxito",
            color=0x57F287
        )
        embed.set_author(
            name=interaction.user.display_name,
            icon_url=interaction.user.display_avatar.url
        )
        await interaction.followup.send(embed=embed)


class SetAlertModal(discord.ui.Modal, title="🔔 Configurar Alerta de Precio"):
    flight_id_input = discord.ui.TextInput(
        label="ID del Vuelo",
        placeholder="Ej: 123456789_MEX_CUN_2025-12-15",
    )
    threshold = discord.ui.TextInput(
        label="Precio de alerta (MXN)",
        placeholder="Notificaré cuando el precio baje de este valor",
        max_length=10,
    )

    async def on_submit(self, interaction: discord.Interaction):
        fid = self.flight_id_input.value.strip()
        if fid not in data["flights"]:
            return await interaction.response.send_message("❌ ID de vuelo no encontrado.", ephemeral=True)
        if data["flights"][fid]["user_id"] != interaction.user.id:
            return await interaction.response.send_message("❌ No tienes permiso para modificar este vuelo.", ephemeral=True)

        try:
            threshold = float(self.threshold.value.replace(",", "").replace("$", ""))
        except ValueError:
            return await interaction.response.send_message("❌ Precio inválido.", ephemeral=True)

        data["flights"][fid]["alert_threshold"] = threshold
        save_data(data)
        await interaction.response.send_message(
            f"✅ Alerta configurada: te avisaré cuando `{fid}` baje de **{format_price(threshold)}**",
            ephemeral=True
        )


class FlightDashboardView(discord.ui.View):
    """Panel principal de control."""
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id

    @discord.ui.button(label="➕ Agregar Vuelo", style=discord.ButtonStyle.success, emoji="✈️")
    async def add_flight(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddFlightModal())

    @discord.ui.button(label="📋 Ver Mis Vuelos", style=discord.ButtonStyle.primary, emoji="📋")
    async def list_flights(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_flights = [f for f in data["flights"].values() if f["user_id"] == interaction.user.id]
        if not user_flights:
            return await interaction.response.send_message(
                "No tienes vuelos monitoreados. ¡Agrega uno con ➕!", ephemeral=True
            )
        view = FlightListView(user_flights, interaction.user.id)
        await interaction.response.send_message(embed=view.get_embed(), view=view, ephemeral=True)

    @discord.ui.button(label="🔔 Configurar Alerta", style=discord.ButtonStyle.secondary, emoji="🔔")
    async def set_alert(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SetAlertModal())

    @discord.ui.button(label="❌ Eliminar Vuelo", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete_prompt(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_flights = [f for f in data["flights"].values() if f["user_id"] == interaction.user.id]
        if not user_flights:
            return await interaction.response.send_message("No tienes vuelos para eliminar.", ephemeral=True)
        options = [
            discord.SelectOption(
                label=f"{f['origin']}→{f['dest']} ({f['date']})",
                value=f["id"],
                description=f"Precio: {format_price(f['last_price']) if f.get('last_price') else 'N/A'}"
            )
            for f in user_flights[:25]  # Discord max 25 options
        ]
        select = DeleteFlightSelect(options)
        view = discord.ui.View(timeout=60)
        view.add_item(select)
        await interaction.response.send_message("Selecciona el vuelo a eliminar:", view=view, ephemeral=True)


class DeleteFlightSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="Selecciona el vuelo...", options=options)

    async def callback(self, interaction: discord.Interaction):
        fid = self.values[0]
        if fid in data["flights"]:
            f = data["flights"].pop(fid)
            save_data(data)
            await interaction.response.send_message(
                f"✅ Vuelo `{f['origin']} → {f['dest']}` ({f['date']}) eliminado del monitoreo.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message("❌ Vuelo no encontrado.", ephemeral=True)


# ──────────────────────────────────────────────
# SLASH COMMANDS
# ──────────────────────────────────────────────

@tree.command(name="vuelos", description="📊 Abre el panel de control de vuelos")
async def cmd_panel(interaction: discord.Interaction):
    user_flights = [f for f in data["flights"].values() if f["user_id"] == interaction.user.id]
    total = len(user_flights)
    prices = [f["last_price"] for f in user_flights if f.get("last_price")]
    avg = sum(prices) / len(prices) if prices else 0

    embed = discord.Embed(
        title="✈️ Panel de Control — Flight Monitor Bot",
        description="Monitorea precios de vuelos y recibe alertas cuando cambien.",
        color=0x5865F2,
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="👤 Tus Vuelos", value=f"{total} activo(s)", inline=True)
    embed.add_field(name="💰 Precio Promedio", value=format_price(avg) if avg else "—", inline=True)
    embed.add_field(name="⏱️ Intervalo de Revisión", value=f"Cada {CHECK_INTERVAL_MINUTES} min", inline=True)

    if user_flights:
        recientes = sorted(user_flights, key=lambda x: x.get("last_checked", ""), reverse=True)[:3]
        lista = "\n".join(
            f"• `{f['origin']}→{f['dest']}` {f['date']} — {format_price(f['last_price']) if f.get('last_price') else '...'}"
            for f in recientes
        )
        embed.add_field(name="🕐 Recientes", value=lista, inline=False)

    embed.set_footer(text=f"Flight Monitor Bot • {interaction.user.display_name}")
    embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)

    view = FlightDashboardView(interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view)


@tree.command(name="vuelo_agregar", description="➕ Agrega un vuelo para monitorear")
@app_commands.describe(
    origen="Código IATA del aeropuerto de origen (ej: MEX)",
    destino="Código IATA del aeropuerto de destino (ej: CUN)",
    fecha="Fecha del vuelo en formato YYYY-MM-DD",
    alerta="Precio en MXN para activar alerta (opcional)"
)
async def cmd_add(interaction: discord.Interaction, origen: str, destino: str, fecha: str, alerta: Optional[float] = None):
    origin = origen.upper().strip()
    dest = destino.upper().strip()

    try:
        flight_date = datetime.strptime(fecha, "%Y-%m-%d")
        if flight_date.date() < datetime.utcnow().date():
            return await interaction.response.send_message("❌ La fecha no puede ser en el pasado.", ephemeral=True)
    except ValueError:
        return await interaction.response.send_message("❌ Formato de fecha inválido. Usa YYYY-MM-DD", ephemeral=True)

    fid = flight_id(origin, dest, fecha, interaction.user.id)
    if fid in data["flights"]:
        return await interaction.response.send_message("⚠️ Ya estás monitoreando este vuelo.", ephemeral=True)

    await interaction.response.defer(thinking=True)
    price = await get_flight_price(origin, dest, fecha)

    data["flights"][fid] = {
        "id": fid,
        "origin": origin,
        "dest": dest,
        "date": fecha,
        "user_id": interaction.user.id,
        "channel_id": interaction.channel_id,
        "last_price": price,
        "min_price": price,
        "max_price": price,
        "checks": 1 if price else 0,
        "alert_threshold": alerta,
        "created_at": datetime.utcnow().isoformat(),
        "last_checked": datetime.utcnow().isoformat(),
    }
    save_data(data)

    embed = embed_flight_card(data["flights"][fid], title="✅ Vuelo Agregado", color=0x57F287)
    await interaction.followup.send(embed=embed)


@tree.command(name="vuelo_lista", description="📋 Ver todos tus vuelos monitoreados")
async def cmd_list(interaction: discord.Interaction):
    user_flights = [f for f in data["flights"].values() if f["user_id"] == interaction.user.id]
    if not user_flights:
        return await interaction.response.send_message(
            "No tienes vuelos monitoreados. Usa `/vuelo_agregar` o `/vuelos` para comenzar.", ephemeral=True
        )
    view = FlightListView(user_flights, interaction.user.id)
    await interaction.response.send_message(embed=view.get_embed(), view=view)


@tree.command(name="vuelo_precio", description="💰 Consulta el precio actual de un vuelo")
@app_commands.describe(
    origen="Código IATA origen",
    destino="Código IATA destino",
    fecha="Fecha en formato YYYY-MM-DD"
)
async def cmd_price(interaction: discord.Interaction, origen: str, destino: str, fecha: str):
    await interaction.response.defer(thinking=True)
    price = await get_flight_price(origen.upper(), destino.upper(), fecha)
    embed = discord.Embed(
        title=f"💰 Precio: {origen.upper()} → {destino.upper()}",
        description=f"📅 {fecha}",
        color=0xFEE75C,
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Precio", value=format_price(price) if price else "No disponible", inline=False)
    embed.add_field(name="Origen", value=airport_name(origen), inline=True)
    embed.add_field(name="Destino", value=airport_name(destino), inline=True)
    embed.set_footer(text="Fuente: Google Flights / Estimado")
    await interaction.followup.send(embed=embed)


@tree.command(name="vuelo_eliminar", description="🗑️ Deja de monitorear un vuelo")
@app_commands.describe(id_vuelo="ID del vuelo (usa /vuelo_lista para verlo)")
async def cmd_delete(interaction: discord.Interaction, id_vuelo: str):
    if id_vuelo not in data["flights"]:
        return await interaction.response.send_message("❌ Vuelo no encontrado.", ephemeral=True)
    f = data["flights"][id_vuelo]
    if f["user_id"] != interaction.user.id:
        return await interaction.response.send_message("❌ No puedes eliminar vuelos de otros usuarios.", ephemeral=True)
    data["flights"].pop(id_vuelo)
    save_data(data)
    await interaction.response.send_message(
        f"✅ Vuelo `{f['origin']} → {f['dest']}` ({f['date']}) eliminado.", ephemeral=True
    )


@tree.command(name="vuelo_alerta", description="🔔 Configura una alerta de precio para un vuelo")
@app_commands.describe(
    id_vuelo="ID del vuelo",
    precio="Notificar cuando el precio baje de este valor (MXN)"
)
async def cmd_alert(interaction: discord.Interaction, id_vuelo: str, precio: float):
    if id_vuelo not in data["flights"]:
        return await interaction.response.send_message("❌ Vuelo no encontrado.", ephemeral=True)
    if data["flights"][id_vuelo]["user_id"] != interaction.user.id:
        return await interaction.response.send_message("❌ No tienes permiso.", ephemeral=True)
    data["flights"][id_vuelo]["alert_threshold"] = precio
    save_data(data)
    await interaction.response.send_message(
        f"✅ Alerta configurada: te notificaré cuando el precio baje de **{format_price(precio)}**",
        ephemeral=True
    )


@tree.command(name="vuelo_stats", description="📊 Estadísticas globales del bot")
async def cmd_stats(interaction: discord.Interaction):
    all_flights = list(data["flights"].values())
    user_flights = [f for f in all_flights if f["user_id"] == interaction.user.id]
    total_checks = sum(f.get("checks", 0) for f in all_flights)

    embed = discord.Embed(title="📊 Estadísticas del Bot", color=0x5865F2, timestamp=datetime.utcnow())
    embed.add_field(name="✈️ Vuelos Monitoreados (Total)", value=len(all_flights), inline=True)
    embed.add_field(name="👤 Tus Vuelos", value=len(user_flights), inline=True)
    embed.add_field(name="🔍 Revisiones Totales", value=f"{total_checks:,}", inline=True)
    embed.add_field(name="⏱️ Intervalo", value=f"{CHECK_INTERVAL_MINUTES} minutos", inline=True)
    embed.add_field(name="📡 Estado Monitor", value="🟢 Activo" if price_monitor.is_running() else "🔴 Inactivo", inline=True)
    await interaction.response.send_message(embed=embed)


@tree.command(name="ayuda", description="❓ Guía de uso del bot")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="❓ Ayuda — Flight Monitor Bot",
        description="Monitoreo automático de precios de vuelos con alertas en tiempo real.",
        color=0x5865F2
    )
    commands_info = [
        ("/vuelos", "Panel principal de control (interfaz gráfica)"),
        ("/vuelo_agregar", "Agrega un vuelo para monitorear"),
        ("/vuelo_lista", "Lista todos tus vuelos activos"),
        ("/vuelo_precio", "Consulta el precio actual de un vuelo"),
        ("/vuelo_eliminar", "Elimina un vuelo del monitoreo"),
        ("/vuelo_alerta", "Configura alerta cuando el precio baje"),
        ("/vuelo_stats", "Estadísticas del bot"),
    ]
    for cmd, desc in commands_info:
        embed.add_field(name=cmd, value=desc, inline=False)
    embed.add_field(
        name="📌 Códigos IATA comunes",
        value="MEX, CUN, GDL, MTY, TIJ, LAX, JFK, MIA, MAD, BCN",
        inline=False
    )
    embed.set_footer(text="Revisiones cada 30 minutos • Alertas automáticas por DM o canal")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ──────────────────────────────────────────────
# EVENTOS
# ──────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info(f"Bot conectado como {bot.user} (ID: {bot.user.id})")
    try:
        synced = await tree.sync()
        log.info(f"Slash commands sincronizados: {len(synced)}")
    except Exception as e:
        log.error(f"Error sincronizando commands: {e}")
    price_monitor.start()
    log.info(f"Monitor de precios iniciado (cada {CHECK_INTERVAL_MINUTES} min)")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{len(data['flights'])} vuelos ✈️"
        )
    )

@bot.event
async def on_command_error(ctx, error):
    log.error(f"Error en comando: {error}")

# ──────────────────────────────────────────────
# ENTRADA PRINCIPAL
# ──────────────────────────────────────────────
if __name__ == "__main__":
    if TOKEN == "TU_TOKEN_AQUI":
        log.error("⚠️  Configura DISCORD_TOKEN en el archivo .env")
        exit(1)
    bot.run(TOKEN)
