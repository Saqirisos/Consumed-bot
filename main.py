import os
import sqlite3
import itertools
import asyncio
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import discord
from discord.ext import commands, tasks
from discord import app_commands

# =========================================================
# CONFIG
# =========================================================

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "/data/bot.db")

if not TOKEN:
    raise ValueError("A variável DISCORD_BOT_TOKEN não foi encontrada.")

AGE_EMOJI_NAMES = ["menos13", "mais13", "mais18", "mais21"]
VERIFY_STAFF_ROLE_NAME = "staff"
VERIFY_BANNER_URL = "https://i.imgur.com/PwrzJ7z.png"
TICKET_COOLDOWN_SECONDS = 30
MAX_VERIFY_TICKETS_PER_USER = 1

DEFAULT_WELCOME_MESSAGE = (
    "...\n\n"
    "você chegou... mas por quanto tempo?\n\n"
    "bem-vindo ao {server}, {user}\n"
    "make yourself at home... or don't."
)

STATUS_LIST = [
    "i never left.",
    "it wasn’t little.",
    "some things never end…",
    "i still think about us.",
    "you were everything."
]

DEFAULT_VERIFY_MESSAGE = (
    "## **__Verificação de rosto__**\n\n"
    "**Este processo existe para manter o servidor seguro e evitar perfis fake, catfish e postagens enganosas.**\n\n"
    "> **Seu chat será privado.**\n"
    "> **Somente você, a staff e o bot terão acesso.**\n"
    "> **Nada será vazado.**\n"
    "> **Se não for aprovado, você não receberá acesso.**\n\n"
    "### **Como funciona**\n"
    "**1.** Clique em **Iniciar verificação**\n"
    "**2.** Escolha um membro da staff disponível\n"
    "**3.** Um chat privado será aberto\n"
    "**4.** Envie uma foto do seu rosto no ticket\n"
    "**5.** Aguarde aprovação manual\n\n"
    "### **Staff disponível**\n"
    "{staff_list}\n\n"
    "**Ao iniciar, você concorda em enviar apenas fotos suas.**\n"
    "**Fotos falsas, roubadas ou de terceiros resultam em recusa imediata.**"
)

DEFAULT_REJECT_MESSAGE = (
    "{user}, sua verificação **não foi aprovada**.\n\n"
    "Se quiser tentar novamente, fale com a staff e envie uma nova verificação real.\n"
    "**Perfis fake, fotos de terceiros ou conteúdo enganoso não são aceitos.**"
)

# =========================================================
# DATABASE
# =========================================================

def ensure_db_dir():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

def get_conn():
    ensure_db_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id INTEGER PRIMARY KEY,
                welcome_channel_id INTEGER,
                member_role_id INTEGER,
                age_channel_id INTEGER,
                age_message_id INTEGER,
                welcome_message TEXT,
                welcome_gif TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS age_roles (
                guild_id INTEGER NOT NULL,
                age_key TEXT NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, age_key)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS verify_cooldowns (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                last_opened_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
        """)

        existing_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(guild_config)").fetchall()
        }

        migrations = {
            "verify_category_id": "ALTER TABLE guild_config ADD COLUMN verify_category_id INTEGER",
            "verify_role_id": "ALTER TABLE guild_config ADD COLUMN verify_role_id INTEGER",
            "verify_message": "ALTER TABLE guild_config ADD COLUMN verify_message TEXT",
            "verify_log_channel_id": "ALTER TABLE guild_config ADD COLUMN verify_log_channel_id INTEGER",
            "reject_message": "ALTER TABLE guild_config ADD COLUMN reject_message TEXT",
            "ticket_panel_channel_id": "ALTER TABLE guild_config ADD COLUMN ticket_panel_channel_id INTEGER",
        }

        for col_name, sql in migrations.items():
            if col_name not in existing_cols:
                conn.execute(sql)

        conn.commit()

def ensure_guild_row(guild_id: int):
    ensure_db_dir()
    with get_conn() as conn:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(guild_config)").fetchall()
        }

        advanced_columns = {
            "verify_category_id", "verify_role_id", "verify_message",
            "verify_log_channel_id", "reject_message", "ticket_panel_channel_id"
        }

        if advanced_columns.issubset(columns):
            conn.execute("""
                INSERT OR IGNORE INTO guild_config (
                    guild_id,
                    welcome_channel_id,
                    member_role_id,
                    age_channel_id,
                    age_message_id,
                    welcome_message,
                    welcome_gif,
                    verify_category_id,
                    verify_role_id,
                    verify_message,
                    verify_log_channel_id,
                    reject_message,
                    ticket_panel_channel_id
                )
                VALUES (?, NULL, NULL, NULL, NULL, ?, NULL, NULL, NULL, ?, NULL, ?, NULL)
            """, (guild_id, DEFAULT_WELCOME_MESSAGE, DEFAULT_VERIFY_MESSAGE, DEFAULT_REJECT_MESSAGE))
        else:
            conn.execute("""
                INSERT OR IGNORE INTO guild_config (
                    guild_id,
                    welcome_channel_id,
                    member_role_id,
                    age_channel_id,
                    age_message_id,
                    welcome_message,
                    welcome_gif
                )
                VALUES (?, NULL, NULL, NULL, NULL, ?, NULL)
            """, (guild_id, DEFAULT_WELCOME_MESSAGE))

        conn.commit()

def get_guild_config(guild_id: int) -> dict:
    ensure_guild_row(guild_id)
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)).fetchone()
        return dict(row) if row else {}

def set_guild_config(guild_id: int, **kwargs):
    ensure_guild_row(guild_id)

    allowed = {
        "welcome_channel_id",
        "member_role_id",
        "age_channel_id",
        "age_message_id",
        "welcome_message",
        "welcome_gif",
        "verify_category_id",
        "verify_role_id",
        "verify_message",
        "verify_log_channel_id",
        "reject_message",
        "ticket_panel_channel_id",
    }

    fields = []
    values = []

    for key, value in kwargs.items():
        if key in allowed:
            fields.append(f"{key} = ?")
            values.append(value)

    if not fields:
        return

    values.append(guild_id)

    with get_conn() as conn:
        conn.execute(f"UPDATE guild_config SET {', '.join(fields)} WHERE guild_id = ?", values)
        conn.commit()

def set_age_role(guild_id: int, age_key: str, role_id: int):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO age_roles (guild_id, age_key, role_id)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, age_key)
            DO UPDATE SET role_id = excluded.role_id
        """, (guild_id, age_key, role_id))
        conn.commit()

def get_age_roles(guild_id: int) -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT age_key, role_id FROM age_roles WHERE guild_id = ?", (guild_id,)).fetchall()
        return {row["age_key"]: row["role_id"] for row in rows}

def set_verify_cooldown(guild_id: int, user_id: int):
    now_ts = int(datetime.now(timezone.utc).timestamp())
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO verify_cooldowns (guild_id, user_id, last_opened_at)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET last_opened_at = excluded.last_opened_at
        """, (guild_id, user_id, now_ts))
        conn.commit()

def get_verify_cooldown_remaining(guild_id: int, user_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT last_opened_at FROM verify_cooldowns
            WHERE guild_id = ? AND user_id = ?
        """, (guild_id, user_id)).fetchone()

    if not row:
        return 0

    elapsed = int(datetime.now(timezone.utc).timestamp()) - int(row["last_opened_at"])
    remaining = TICKET_COOLDOWN_SECONDS - elapsed
    return max(0, remaining)

# =========================================================
# HELPERS
# =========================================================

def admin_only(interaction: discord.Interaction) -> bool:
    return bool(interaction.user.guild_permissions.administrator)

def get_emoji_by_name(guild: discord.Guild, name: str):
    return discord.utils.get(guild.emojis, name=name)

async def get_text_channel(guild: discord.Guild, channel_id: Optional[int]):
    if not channel_id:
        return None

    channel = guild.get_channel(channel_id)
    if channel is not None and isinstance(channel, discord.TextChannel):
        return channel

    try:
        fetched = await bot.fetch_channel(channel_id)
        if isinstance(fetched, discord.TextChannel):
            return fetched
    except Exception:
        return None

    return None

async def get_category_channel(guild: discord.Guild, category_id: Optional[int]):
    if not category_id:
        return None

    category = guild.get_channel(category_id)
    if category is not None and isinstance(category, discord.CategoryChannel):
        return category

    try:
        fetched = await bot.fetch_channel(category_id)
        if isinstance(fetched, discord.CategoryChannel):
            return fetched
    except Exception:
        return None

    return None

def format_welcome_text(template: str, member: discord.Member) -> str:
    return (
        template
        .replace("{user}", member.mention)
        .replace("{username}", member.name)
        .replace("{server}", member.guild.name)
        .replace("{members}", str(member.guild.member_count))
    )

def clean_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None

    url = url.strip()
    if (url.startswith('"') and url.endswith('"')) or (url.startswith("'") and url.endswith("'")):
        url = url[1:-1].strip()

    url = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff]", "", url)

    if url.endswith(".gifv"):
        url = url[:-5] + ".gif"

    return url or None

def is_valid_image_url(url: Optional[str]) -> bool:
    if not url:
        return False

    url = clean_url(url)
    if not url:
        return False

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    return parsed.scheme in ("http", "https") and bool(parsed.netloc)

def has_allowed_image_extension(url: Optional[str]) -> bool:
    if not url:
        return False
    return url.lower().endswith((".gif", ".png", ".jpg", ".jpeg", ".webp"))

def build_age_embed(guild: discord.Guild) -> discord.Embed:
    e_menos13 = get_emoji_by_name(guild, "menos13")
    e_mais13 = get_emoji_by_name(guild, "mais13")
    e_mais18 = get_emoji_by_name(guild, "mais18")
    e_mais21 = get_emoji_by_name(guild, "mais21")

    def show(emoji, fallback):
        return str(emoji) if emoji else fallback

    embed = discord.Embed(
        title="how old are you?",
        description=(
            "eu ainda não sei sua idade...\n"
            "talvez você possa me dizer.\n\n"
            "escolha uma opção abaixo.\n\n"
            f"{show(e_menos13, '•')} **-13**\n"
            f"{show(e_mais13, '•')} **+13**\n"
            f"{show(e_mais18, '•')} **+18**\n"
            f"{show(e_mais21, '•')} **+21**"
        ),
        color=discord.Color.from_rgb(10, 10, 10)
    )

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.set_footer(text="one choice only.")
    return embed

def find_staff_role(guild: discord.Guild) -> Optional[discord.Role]:
    return discord.utils.find(lambda r: r.name.lower() == VERIFY_STAFF_ROLE_NAME.lower(), guild.roles)

def get_staff_members(guild: discord.Guild) -> list[discord.Member]:
    staff_role = find_staff_role(guild)
    if not staff_role:
        return []
    return [member for member in guild.members if not member.bot and staff_role in member.roles]

def format_verify_message(template: str, guild: discord.Guild) -> str:
    staff_members = get_staff_members(guild)
    staff_list = " | ".join(member.mention for member in staff_members) if staff_members else "`nenhum staff configurado`"
    return template.replace("{server}", guild.name).replace("{staff_list}", staff_list)

def sanitize_channel_name(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[^a-z0-9-_]", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    return name or "usuario"

def get_ticket_user_id(channel: discord.TextChannel) -> Optional[int]:
    if not channel.topic:
        return None
    match = re.fullmatch(r"verify_user:(\d+)", channel.topic.strip())
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None

def get_open_verify_channels_for_user(guild: discord.Guild, user_id: int) -> list[discord.TextChannel]:
    return [
        channel for channel in guild.text_channels
        if channel.topic == f"verify_user:{user_id}"
    ]

async def send_verify_log(guild: discord.Guild, title: str, description: str):
    cfg = get_guild_config(guild.id)
    log_channel = await get_text_channel(guild, cfg.get("verify_log_channel_id"))
    if not log_channel:
        return

    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.from_rgb(20, 20, 20),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=guild.name)

    try:
        await log_channel.send(embed=embed)
    except Exception:
        pass

# =========================================================
# BOT
# =========================================================

class Consumed(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True

        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        init_db()
        self.add_view(AgeView())
        self.add_view(StartVerifyPersistentView())
        self.add_view(TicketActionView())

bot = Consumed()

# =========================================================
# STATUS
# =========================================================

status_cycle = itertools.cycle(STATUS_LIST)

@tasks.loop(seconds=12)
async def change_status():
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(type=discord.ActivityType.playing, name=next(status_cycle))
    )

# =========================================================
# AGE VIEW
# =========================================================

class AgeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def handle_role(self, interaction: discord.Interaction, age_key: str):
        guild = interaction.guild
        member = interaction.user

        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("Isso só funciona dentro de servidor.", ephemeral=True)
            return

        age_roles = get_age_roles(guild.id)
        target_role_id = age_roles.get(age_key)

        if not target_role_id:
            await interaction.response.send_message("O sistema de idade ainda não foi configurado nesse servidor.", ephemeral=True)
            return

        target_role = guild.get_role(target_role_id)
        if not target_role:
            await interaction.response.send_message("O cargo configurado não foi encontrado.", ephemeral=True)
            return

        try:
            for role_id in age_roles.values():
                role = guild.get_role(role_id)
                if role and role in member.roles:
                    await member.remove_roles(role)

            await member.add_roles(target_role)

            label_map = {
                "menos13": "-13",
                "mais13": "+13",
                "mais18": "+18",
                "mais21": "+21",
            }

            await interaction.response.send_message(
                f"idade marcada como **{label_map.get(age_key, age_key)}**. cargo recebido: {target_role.mention}",
                ephemeral=True
            )

        except discord.Forbidden:
            await interaction.response.send_message("Não consegui mexer nos cargos. Deixa meu cargo acima dos cargos de idade.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Deu erro: `{e}`", ephemeral=True)

    @discord.ui.button(label="-13", style=discord.ButtonStyle.secondary, custom_id="age_menos13", row=0)
    async def menos13(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_role(interaction, "menos13")

    @discord.ui.button(label="+13", style=discord.ButtonStyle.primary, custom_id="age_mais13", row=0)
    async def mais13(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_role(interaction, "mais13")

    @discord.ui.button(label="+18", style=discord.ButtonStyle.danger, custom_id="age_mais18", row=0)
    async def mais18(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_role(interaction, "mais18")

    @discord.ui.button(label="+21", style=discord.ButtonStyle.success, custom_id="age_mais21", row=0)
    async def mais21(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_role(interaction, "mais21")

def build_age_view_for_guild(guild: discord.Guild) -> AgeView:
    view = AgeView()
    emoji_map = {
        "age_menos13": "menos13",
        "age_mais13": "mais13",
        "age_mais18": "mais18",
        "age_mais21": "mais21",
    }
    for item in view.children:
        if isinstance(item, discord.ui.Button):
            emoji_name = emoji_map.get(item.custom_id)
            if emoji_name:
                emoji = get_emoji_by_name(guild, emoji_name)
                if emoji:
                    item.emoji = emoji
    return view

# =========================================================
# VERIFY SYSTEM
# =========================================================

class TicketActionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    def _can_staff_act(self, guild: discord.Guild, user: discord.Member) -> bool:
        staff_role = find_staff_role(guild)
        return bool(user.guild_permissions.administrator or (staff_role and staff_role in user.roles))

    @discord.ui.button(label="Aprovar", style=discord.ButtonStyle.success, custom_id="verify_approve_ticket")
    async def approve_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        channel = interaction.channel
        actor = interaction.user

        if guild is None or not isinstance(channel, discord.TextChannel) or not isinstance(actor, discord.Member):
            await interaction.response.send_message("isso só funciona dentro do servidor.", ephemeral=True)
            return

        if not self._can_staff_act(guild, actor):
            await interaction.response.send_message("só a staff ou admin pode aprovar.", ephemeral=True)
            return

        target_user_id = get_ticket_user_id(channel)
        if not target_user_id:
            await interaction.response.send_message("não consegui identificar o usuário desse ticket.", ephemeral=True)
            return

        target_member = guild.get_member(target_user_id)
        if not target_member:
            await interaction.response.send_message("o usuário não está mais no servidor.", ephemeral=True)
            return

        cfg = get_guild_config(guild.id)
        verify_role_id = cfg.get("verify_role_id")

        if not verify_role_id:
            await interaction.response.send_message("cargo de verificação não configurado. usa `/setup_verificacao` primeiro.", ephemeral=True)
            return

        verify_role = guild.get_role(verify_role_id)
        if not verify_role:
            await interaction.response.send_message("o cargo configurado de verificação não foi encontrado.", ephemeral=True)
            return

        try:
            if verify_role not in target_member.roles:
                await target_member.add_roles(verify_role)

            await interaction.response.send_message(
                f"{target_member.mention} foi aprovado e recebeu o cargo {verify_role.mention}.",
                ephemeral=False
            )

            await send_verify_log(
                guild,
                "Verificação aprovada",
                f"**Usuário:** {target_member.mention}\n**Staff:** {actor.mention}\n**Canal:** {channel.mention}"
            )
        except discord.Forbidden:
            await interaction.response.send_message("não consegui dar o cargo. deixa meu cargo acima do cargo de verificação.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"deu erro ao aprovar: `{e}`", ephemeral=True)

    @discord.ui.button(label="Recusar", style=discord.ButtonStyle.danger, custom_id="verify_reject_ticket")
    async def reject_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        channel = interaction.channel
        actor = interaction.user

        if guild is None or not isinstance(channel, discord.TextChannel) or not isinstance(actor, discord.Member):
            await interaction.response.send_message("isso só funciona dentro do servidor.", ephemeral=True)
            return

        if not self._can_staff_act(guild, actor):
            await interaction.response.send_message("só a staff ou admin pode recusar.", ephemeral=True)
            return

        target_user_id = get_ticket_user_id(channel)
        target_member = guild.get_member(target_user_id) if target_user_id else None
        cfg = get_guild_config(guild.id)
        reject_message = cfg.get("reject_message") or DEFAULT_REJECT_MESSAGE
        formatted = reject_message.replace("{user}", target_member.mention if target_member else "usuário")

        await interaction.response.send_message(formatted, ephemeral=False)

        await send_verify_log(
            guild,
            "Verificação recusada",
            f"**Usuário:** {(target_member.mention if target_member else 'não encontrado')}\n**Staff:** {actor.mention}\n**Canal:** {channel.mention}"
        )

    @discord.ui.button(label="Fechar ticket", style=discord.ButtonStyle.secondary, custom_id="verify_close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        channel = interaction.channel
        user = interaction.user

        if guild is None or not isinstance(channel, discord.TextChannel) or not isinstance(user, discord.Member):
            await interaction.response.send_message("isso só funciona dentro do servidor.", ephemeral=True)
            return

        allowed = False
        staff_role = find_staff_role(guild)

        if user.guild_permissions.administrator:
            allowed = True
        elif staff_role and staff_role in user.roles:
            allowed = True
        elif channel.topic == f"verify_user:{user.id}":
            allowed = True

        if not allowed:
            await interaction.response.send_message("só staff, admin, ou quem abriu pode fechar esse ticket.", ephemeral=True)
            return

        await interaction.response.send_message("fechando em 2 segundos...")

        await send_verify_log(
            guild,
            "Ticket fechado",
            f"**Fechado por:** {user.mention}\n**Canal:** {channel.name}"
        )

        await asyncio.sleep(2)
        await channel.delete()

class StaffSelect(discord.ui.Select):
    def __init__(self, guild: discord.Guild, requester: discord.Member):
        self.guild = guild
        self.requester = requester

        staff_members = get_staff_members(guild)
        options = [
            discord.SelectOption(
                label=member.display_name[:100],
                value=str(member.id),
                description=f"ID: {member.id}"
            )
            for member in staff_members[:25]
        ]

        super().__init__(
            placeholder="Selecione um verificador...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        user = interaction.user

        if guild is None or not isinstance(user, discord.Member):
            await interaction.response.send_message("isso só funciona dentro do servidor.", ephemeral=True)
            return

        if user.id != self.requester.id:
            await interaction.response.send_message("esse menu não é seu.", ephemeral=True)
            return

        user_channels = get_open_verify_channels_for_user(guild, user.id)
        if len(user_channels) >= MAX_VERIFY_TICKETS_PER_USER:
            await interaction.response.send_message(
                f"você já tem um ticket aberto: {user_channels[0].mention}",
                ephemeral=True
            )
            return

        remaining = get_verify_cooldown_remaining(guild.id, user.id)
        if remaining > 0:
            await interaction.response.send_message(
                f"calma. espera **{remaining}s** antes de abrir outro ticket.",
                ephemeral=True
            )
            return

        staff_id = int(self.values[0])
        staff_member = guild.get_member(staff_id)
        if not staff_member:
            await interaction.response.send_message("não encontrei esse staff. tenta de novo.", ephemeral=True)
            return

        cfg = get_guild_config(guild.id)
        category = await get_category_channel(guild, cfg.get("verify_category_id"))
        channel_name = f"verify-{sanitize_channel_name(user.name)}-{str(user.id)[-4:]}"

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
            staff_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True, attach_files=True, embed_links=True),
        }

        staff_role = find_staff_role(guild)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)

        try:
            created_channel = await guild.create_text_channel(
                name=channel_name,
                overwrites=overwrites,
                topic=f"verify_user:{user.id}",
                category=category
            )
        except discord.Forbidden:
            await interaction.response.send_message("não consegui criar o canal. vê se eu tenho permissão de gerenciar canais.", ephemeral=True)
            return
        except Exception as e:
            await interaction.response.send_message(f"deu erro ao criar o canal: `{e}`", ephemeral=True)
            return

        set_verify_cooldown(guild.id, user.id)

        embed = discord.Embed(
            title="verificação iniciada",
            description=(
                f"{user.mention}, envie sua verificação aqui.\n\n"
                f"**verificador escolhido:** {staff_member.mention}\n\n"
                "**Este canal é privado e seguro.**\n"
                "Somente você e a staff conseguem ver isso."
            ),
            color=discord.Color.from_rgb(0, 0, 0)
        )
        embed.set_footer(text=guild.name)

        await created_channel.send(
            content=f"{user.mention} {staff_member.mention}",
            embed=embed,
            view=TicketActionView()
        )

        await send_verify_log(
            guild,
            "Ticket de verificação criado",
            f"**Usuário:** {user.mention}\n**Staff escolhida:** {staff_member.mention}\n**Canal:** {created_channel.mention}"
        )

        await interaction.response.send_message(f"ticket criado em {created_channel.mention}", ephemeral=True)

class StaffPickerView(discord.ui.View):
    def __init__(self, guild: discord.Guild, requester: discord.Member):
        super().__init__(timeout=180)
        self.add_item(StaffSelect(guild, requester))

class StartVerifyPersistentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Iniciar verificação", style=discord.ButtonStyle.secondary, custom_id="verify_start_button")
    async def start_verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user

        if guild is None or not isinstance(user, discord.Member):
            await interaction.response.send_message("isso só funciona dentro do servidor.", ephemeral=True)
            return

        user_channels = get_open_verify_channels_for_user(guild, user.id)
        if len(user_channels) >= MAX_VERIFY_TICKETS_PER_USER:
            await interaction.response.send_message(f"você já tem um ticket aberto: {user_channels[0].mention}", ephemeral=True)
            return

        staff_members = get_staff_members(guild)
        if not staff_members:
            await interaction.response.send_message(f"não achei ninguém com o cargo `{VERIFY_STAFF_ROLE_NAME}`.", ephemeral=True)
            return

        view = StaffPickerView(guild, user)
        await interaction.response.send_message("escolha abaixo com quem você quer verificar:", view=view, ephemeral=True)

# =========================================================
# EVENTS
# =========================================================

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Slash commands sincronizados: {len(synced)}")
    except Exception as e:
        print(f"Erro ao sincronizar slash commands: {e}")

    if not change_status.is_running():
        change_status.start()

    print(f"Bot online como {bot.user}")

@bot.event
async def on_member_join(member: discord.Member):
    cfg = get_guild_config(member.guild.id)

    member_role_id = cfg.get("member_role_id")
    welcome_channel_id = cfg.get("welcome_channel_id")
    welcome_message = cfg.get("welcome_message") or DEFAULT_WELCOME_MESSAGE
    welcome_gif = clean_url(cfg.get("welcome_gif"))

    if member_role_id:
        role = member.guild.get_role(member_role_id)
        if role:
            try:
                await member.add_roles(role)
            except Exception as e:
                print(f"Erro ao dar cargo automático em {member.guild.name}: {e}")

    channel = await get_text_channel(member.guild, welcome_channel_id)
    if not channel:
        return

    text = format_welcome_text(welcome_message, member)

    embed = discord.Embed(description=text, color=discord.Color.from_rgb(0, 0, 0))
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=member.guild.name)

    gif_url = clean_url(welcome_gif)
    if gif_url and is_valid_image_url(gif_url) and has_allowed_image_extension(gif_url):
        embed.set_image(url=gif_url)

    try:
        await asyncio.sleep(1.2)
        await channel.send(embed=embed)
    except discord.HTTPException as e:
        print(f"Erro ao enviar embed de boas-vindas em {member.guild.name}: {e}")
        try:
            if gif_url and is_valid_image_url(gif_url):
                await channel.send(content=f"{text}\n{gif_url}")
            else:
                await channel.send(content=text)
        except Exception as e2:
            print(f"Erro no fallback de boas-vindas em {member.guild.name}: {e2}")
    except Exception as e:
        print(f"Erro ao enviar boas-vindas em {member.guild.name}: {e}")

# =========================================================
# COMMANDS
# =========================================================

@bot.tree.command(name="ping", description="Ver a latência do bot")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency * 1000)}ms")

@bot.tree.command(name="setup_boasvindas", description="Define o canal e o cargo automático")
@app_commands.check(admin_only)
@app_commands.describe(canal="Canal onde a mensagem de boas-vindas será enviada", cargo_membro="Cargo automático ao entrar")
async def setup_boasvindas(interaction: discord.Interaction, canal: discord.TextChannel, cargo_membro: discord.Role):
    if interaction.guild is None:
        await interaction.response.send_message("Usa isso dentro de um servidor.", ephemeral=True)
        return

    set_guild_config(interaction.guild.id, welcome_channel_id=canal.id, member_role_id=cargo_membro.id)
    await interaction.response.send_message(
        f"configuração salva.\n\ncanal: {canal.mention}\ncargo automático: {cargo_membro.mention}",
        ephemeral=True
    )

@bot.tree.command(name="mensagem_boasvindas", description="Define o texto e gif da mensagem de boas-vindas")
@app_commands.check(admin_only)
@app_commands.describe(mensagem="Usa {user}, {username}, {server}, {members}", gif="Link direto do gif/imagem (opcional)")
async def mensagem_boasvindas(interaction: discord.Interaction, mensagem: str, gif: Optional[str] = None):
    if interaction.guild is None:
        await interaction.response.send_message("Usa isso dentro de um servidor.", ephemeral=True)
        return

    gif = clean_url(gif)
    if gif:
        if not is_valid_image_url(gif):
            await interaction.response.send_message("esse link parece inválido. manda um link direto começando com https://", ephemeral=True)
            return
        if not has_allowed_image_extension(gif):
            await interaction.response.send_message("manda um link direto que termine em .gif, .png, .jpg, .jpeg ou .webp", ephemeral=True)
            return

    set_guild_config(interaction.guild.id, welcome_message=mensagem, welcome_gif=gif)

    preview = (
        mensagem
        .replace("{user}", interaction.user.mention)
        .replace("{username}", interaction.user.name)
        .replace("{server}", interaction.guild.name)
        .replace("{members}", str(interaction.guild.member_count))
    )

    extra = f"\n\ngif salvo: `{gif}`" if gif else "\n\nsem gif."
    await interaction.response.send_message(f"mensagem de boas-vindas salva.\n\nprévia:\n{preview}{extra}", ephemeral=True)

@bot.tree.command(name="limpar_gif_boasvindas", description="Remove o gif salvo da mensagem de boas-vindas")
@app_commands.check(admin_only)
async def limpar_gif_boasvindas(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Usa isso dentro de um servidor.", ephemeral=True)
        return
    set_guild_config(interaction.guild.id, welcome_gif=None)
    await interaction.response.send_message("gif das boas-vindas removido.", ephemeral=True)

@bot.tree.command(name="preview_boasvindas", description="Mostra uma prévia da mensagem de boas-vindas")
@app_commands.check(admin_only)
async def preview_boasvindas(interaction: discord.Interaction):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Usa isso dentro de um servidor.", ephemeral=True)
        return

    cfg = get_guild_config(interaction.guild.id)
    welcome_message = cfg.get("welcome_message") or DEFAULT_WELCOME_MESSAGE
    welcome_gif = clean_url(cfg.get("welcome_gif"))
    text = format_welcome_text(welcome_message, interaction.user)

    embed = discord.Embed(description=text, color=discord.Color.from_rgb(0, 0, 0))
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.set_footer(text=interaction.guild.name)

    if welcome_gif and is_valid_image_url(welcome_gif) and has_allowed_image_extension(welcome_gif):
        embed.set_image(url=welcome_gif)

    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="setup_idade", description="Configura o canal e os cargos do sistema de idade")
@app_commands.check(admin_only)
@app_commands.describe(
    canal="Canal onde a mensagem da idade será enviada",
    cargo_menos13="Cargo para -13",
    cargo_mais13="Cargo para +13",
    cargo_mais18="Cargo para +18",
    cargo_mais21="Cargo para +21"
)
async def setup_idade(interaction: discord.Interaction, canal: discord.TextChannel, cargo_menos13: discord.Role, cargo_mais13: discord.Role, cargo_mais18: discord.Role, cargo_mais21: discord.Role):
    if interaction.guild is None:
        await interaction.response.send_message("Usa isso dentro de um servidor.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    set_guild_config(guild_id, age_channel_id=canal.id)
    set_age_role(guild_id, "menos13", cargo_menos13.id)
    set_age_role(guild_id, "mais13", cargo_mais13.id)
    set_age_role(guild_id, "mais18", cargo_mais18.id)
    set_age_role(guild_id, "mais21", cargo_mais21.id)

    await interaction.response.send_message(
        (
            "sistema de idade configurado.\n\n"
            f"canal: {canal.mention}\n"
            f"-13: {cargo_menos13.mention}\n"
            f"+13: {cargo_mais13.mention}\n"
            f"+18: {cargo_mais18.mention}\n"
            f"+21: {cargo_mais21.mention}\n\n"
            "emojis esperados no servidor:\n"
            "`menos13` `mais13` `mais18` `mais21`"
        ),
        ephemeral=True
    )

@bot.tree.command(name="postar_idade", description="Posta a mensagem da idade com botões")
@app_commands.check(admin_only)
async def postar_idade(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Usa isso dentro de um servidor.", ephemeral=True)
        return

    cfg = get_guild_config(interaction.guild.id)
    channel_id = cfg.get("age_channel_id")
    if not channel_id:
        await interaction.response.send_message("configura primeiro com `/setup_idade`.", ephemeral=True)
        return

    channel = await get_text_channel(interaction.guild, channel_id)
    if not channel:
        await interaction.response.send_message("não consegui acessar o canal configurado.", ephemeral=True)
        return

    embed = build_age_embed(interaction.guild)
    view = build_age_view_for_guild(interaction.guild)
    msg = await channel.send(embed=embed, view=view)
    set_guild_config(interaction.guild.id, age_message_id=msg.id)

    await interaction.response.send_message(f"mensagem de idade enviada em {channel.mention}.", ephemeral=True)

@bot.tree.command(name="setup_verificacao", description="Configura categoria, cargo e canal de logs da verificação")
@app_commands.check(admin_only)
@app_commands.describe(
    categoria="Categoria onde os tickets de verificação vão abrir",
    cargo_aprovado="Cargo que a pessoa recebe ao ser aprovada",
    canal_logs="Canal onde o bot envia logs da verificação"
)
async def setup_verificacao(interaction: discord.Interaction, categoria: discord.CategoryChannel, cargo_aprovado: discord.Role, canal_logs: discord.TextChannel):
    if interaction.guild is None:
        await interaction.response.send_message("Usa isso dentro de um servidor.", ephemeral=True)
        return

    set_guild_config(
        interaction.guild.id,
        verify_category_id=categoria.id,
        verify_role_id=cargo_aprovado.id,
        verify_log_channel_id=canal_logs.id
    )

    await interaction.response.send_message(
        (
            "configuração de verificação salva.\n\n"
            f"categoria: {categoria.name}\n"
            f"cargo ao aprovar: {cargo_aprovado.mention}\n"
            f"canal de logs: {canal_logs.mention}"
        ),
        ephemeral=True
    )

@bot.tree.command(name="mensagem_verificacao", description="Define o texto do painel de verificação")
@app_commands.check(admin_only)
@app_commands.describe(mensagem="Você pode usar {server} e {staff_list}")
async def mensagem_verificacao(interaction: discord.Interaction, mensagem: str):
    if interaction.guild is None:
        await interaction.response.send_message("Usa isso dentro de um servidor.", ephemeral=True)
        return

    set_guild_config(interaction.guild.id, verify_message=mensagem)
    preview = format_verify_message(mensagem, interaction.guild)
    await interaction.response.send_message(f"mensagem de verificação salva.\n\nprévia:\n{preview}", ephemeral=True)

@bot.tree.command(name="mensagem_recusa", description="Define a mensagem enviada quando alguém for recusado")
@app_commands.check(admin_only)
@app_commands.describe(mensagem="Use {user} para mencionar a pessoa recusada")
async def mensagem_recusa(interaction: discord.Interaction, mensagem: str):
    if interaction.guild is None:
        await interaction.response.send_message("Usa isso dentro de um servidor.", ephemeral=True)
        return

    set_guild_config(interaction.guild.id, reject_message=mensagem)
    preview = mensagem.replace("{user}", interaction.user.mention)
    await interaction.response.send_message(f"mensagem de recusa salva.\n\nprévia:\n{preview}", ephemeral=True)

@bot.tree.command(name="postar_verificacao", description="Posta o painel de verificação")
@app_commands.check(admin_only)
async def postar_verificacao(interaction: discord.Interaction):
    if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Usa isso dentro de um servidor.", ephemeral=True)
        return

    cfg = get_guild_config(interaction.guild.id)
    verify_message = cfg.get("verify_message") or DEFAULT_VERIFY_MESSAGE

    staff_members = get_staff_members(interaction.guild)
    if not staff_members:
        await interaction.response.send_message(f"não achei ninguém com o cargo `{VERIFY_STAFF_ROLE_NAME}`.", ephemeral=True)
        return

    embed = discord.Embed(
        description=format_verify_message(verify_message, interaction.guild),
        color=discord.Color.from_rgb(0, 0, 0)
    )
    embed.set_image(url=VERIFY_BANNER_URL)

    view = StartVerifyPersistentView()
    await interaction.channel.send(embed=embed, view=view)
    set_guild_config(interaction.guild.id, ticket_panel_channel_id=interaction.channel.id)
    await interaction.response.send_message("painel de verificação enviado.", ephemeral=True)

@bot.tree.command(name="config", description="Mostra a configuração atual do servidor")
@app_commands.check(admin_only)
async def config(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Usa isso dentro de um servidor.", ephemeral=True)
        return

    guild = interaction.guild
    cfg = get_guild_config(guild.id)
    age_roles = get_age_roles(guild.id)

    def fmt_channel(channel_id):
        if not channel_id:
            return "`não definido`"
        channel = guild.get_channel(channel_id)
        return channel.mention if isinstance(channel, discord.TextChannel) else f"`{channel_id}`"

    def fmt_role(role_id):
        if not role_id:
            return "`não definido`"
        role = guild.get_role(role_id)
        return role.mention if role else f"`{role_id}`"

    def fmt_category(category_id):
        if not category_id:
            return "`não definida`"
        category = guild.get_channel(category_id)
        return category.name if isinstance(category, discord.CategoryChannel) else f"`{category_id}`"

    emoji_checks = [f"{name}: {'✅' if get_emoji_by_name(guild, name) else '❌'}" for name in AGE_EMOJI_NAMES]
    welcome_gif = clean_url(cfg.get("welcome_gif")) or "não definido"
    staff_role = find_staff_role(guild)

    text = (
        f"**canal boas-vindas:** {fmt_channel(cfg.get('welcome_channel_id'))}\n"
        f"**cargo automático:** {fmt_role(cfg.get('member_role_id'))}\n"
        f"**canal idade:** {fmt_channel(cfg.get('age_channel_id'))}\n"
        f"**mensagem idade id:** `{cfg.get('age_message_id') or 'não definida'}`\n"
        f"**gif boas-vindas:** `{welcome_gif}`\n"
        f"**cargo staff verificação:** {staff_role.mention if staff_role else '`não encontrado`'}\n"
        f"**categoria verificação:** {fmt_category(cfg.get('verify_category_id'))}\n"
        f"**cargo ao aprovar:** {fmt_role(cfg.get('verify_role_id'))}\n"
        f"**canal logs verificação:** {fmt_channel(cfg.get('verify_log_channel_id'))}\n"
        f"**canal painel verificação:** {fmt_channel(cfg.get('ticket_panel_channel_id'))}\n\n"
        f"**-13:** {fmt_role(age_roles.get('menos13'))}\n"
        f"**+13:** {fmt_role(age_roles.get('mais13'))}\n"
        f"**+18:** {fmt_role(age_roles.get('mais18'))}\n"
        f"**+21:** {fmt_role(age_roles.get('mais21'))}\n\n"
        f"**emojis:**\n" + "\n".join(emoji_checks)
    )

    await interaction.response.send_message(text, ephemeral=True)

@bot.tree.command(name="reset_idade", description="Reseta o id salvo da mensagem de idade")
@app_commands.check(admin_only)
async def reset_idade(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Usa isso dentro de um servidor.", ephemeral=True)
        return

    set_guild_config(interaction.guild.id, age_message_id=None)
    await interaction.response.send_message("id da mensagem de idade resetado.", ephemeral=True)

@bot.tree.command(name="reset_verificacao", description="Reseta a configuração da verificação")
@app_commands.check(admin_only)
async def reset_verificacao(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Usa isso dentro de um servidor.", ephemeral=True)
        return

    set_guild_config(
        interaction.guild.id,
        verify_category_id=None,
        verify_role_id=None,
        verify_log_channel_id=None,
        ticket_panel_channel_id=None,
        verify_message=DEFAULT_VERIFY_MESSAGE,
        reject_message=DEFAULT_REJECT_MESSAGE,
    )

    await interaction.response.send_message("configuração de verificação resetada.", ephemeral=True)

# =========================================================
# ERROS
# =========================================================

@setup_boasvindas.error
@mensagem_boasvindas.error
@limpar_gif_boasvindas.error
@preview_boasvindas.error
@setup_idade.error
@postar_idade.error
@setup_verificacao.error
@mensagem_verificacao.error
@mensagem_recusa.error
@postar_verificacao.error
@config.error
@reset_idade.error
@reset_verificacao.error
async def admin_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.errors.CheckFailure):
        if interaction.response.is_done():
            await interaction.followup.send("tu precisa ser administrador pra usar isso.", ephemeral=True)
        else:
            await interaction.response.send_message("tu precisa ser administrador pra usar isso.", ephemeral=True)
        return

    if interaction.response.is_done():
        await interaction.followup.send(f"deu erro: `{error}`", ephemeral=True)
    else:
        await interaction.response.send_message(f"deu erro: `{error}`", ephemeral=True)

# =========================================================
# RUN
# =========================================================

bot.run(TOKEN)
