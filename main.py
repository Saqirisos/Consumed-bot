import os
import sqlite3
import random
import itertools
import asyncio
from typing import Optional

import discord
from discord.ext import commands, tasks
from discord import app_commands

# =========================================================
# CONFIG GERAL
# =========================================================

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "bot.db")

if not TOKEN:
    raise ValueError("A variável DISCORD_BOT_TOKEN não foi encontrada.")

# Nomes dos emojis que precisam existir em cada servidor
# iguais aos que tu mostrou no print:
# menos13 / mais13 / mais18 / mais21
AGE_EMOJI_NAMES = ["menos13", "mais13", "mais18", "mais21"]

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

# =========================================================
# DATABASE
# =========================================================

def get_conn():
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
        conn.commit()

def ensure_guild_row(guild_id: int):
    with get_conn() as conn:
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
        row = conn.execute("""
            SELECT * FROM guild_config
            WHERE guild_id = ?
        """, (guild_id,)).fetchone()
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
        conn.execute(
            f"UPDATE guild_config SET {', '.join(fields)} WHERE guild_id = ?",
            values
        )
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
        rows = conn.execute("""
            SELECT age_key, role_id
            FROM age_roles
            WHERE guild_id = ?
        """, (guild_id,)).fetchall()
        return {row["age_key"]: row["role_id"] for row in rows}

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

def format_welcome_text(template: str, member: discord.Member) -> str:
    return (
        template
        .replace("{user}", member.mention)
        .replace("{username}", member.name)
        .replace("{server}", member.guild.name)
        .replace("{members}", str(member.guild.member_count))
    )

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

# =========================================================
# BOT
# =========================================================

class Consumed(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True

        super().__init__(
            command_prefix="!",
            intents=intents
        )

    async def setup_hook(self):
        init_db()

bot = Consumed()

# =========================================================
# STATUS
# =========================================================

status_cycle = itertools.cycle(STATUS_LIST)

@tasks.loop(seconds=12)
async def change_status():
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(
            type=discord.ActivityType.playing,
            name=next(status_cycle)
        )
    )

# =========================================================
# VIEW DOS BOTÕES
# =========================================================

class AgeView(discord.ui.View):
    def __init__(self, guild: Optional[discord.Guild] = None):
        super().__init__(timeout=None)

        # Se tiver guild, tenta botar os emojis custom nos botões
        if guild is not None:
            btn_menos13 = self.get_item("age_menos13")
            btn_mais13 = self.get_item("age_mais13")
            btn_mais18 = self.get_item("age_mais18")
            btn_mais21 = self.get_item("age_mais21")

            if btn_menos13:
                btn_menos13.emoji = get_emoji_by_name(guild, "menos13")
            if btn_mais13:
                btn_mais13.emoji = get_emoji_by_name(guild, "mais13")
            if btn_mais18:
                btn_mais18.emoji = get_emoji_by_name(guild, "mais18")
            if btn_mais21:
                btn_mais21.emoji = get_emoji_by_name(guild, "mais21")

    async def handle_role(self, interaction: discord.Interaction, age_key: str):
        guild = interaction.guild
        member = interaction.user

        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Isso só funciona dentro de servidor.",
                ephemeral=True
            )
            return

        age_roles = get_age_roles(guild.id)
        target_role_id = age_roles.get(age_key)

        if not target_role_id:
            await interaction.response.send_message(
                "O sistema de idade ainda não foi configurado nesse servidor.",
                ephemeral=True
            )
            return

        target_role = guild.get_role(target_role_id)
        if not target_role:
            await interaction.response.send_message(
                "O cargo configurado não foi encontrado.",
                ephemeral=True
            )
            return

        try:
            # remove cargos de idade anteriores
            for role_id in age_roles.values():
                role = guild.get_role(role_id)
                if role and role in member.roles:
                    await member.remove_roles(role)

            # adiciona o novo
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
            await interaction.response.send_message(
                "Não consegui mexer nos cargos. Deixa meu cargo acima dos cargos de idade.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"Deu erro: `{e}`",
                ephemeral=True
            )

    @discord.ui.button(
        label="-13",
        style=discord.ButtonStyle.secondary,
        custom_id="age_menos13",
        row=0
    )
    async def menos13(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_role(interaction, "menos13")

    @discord.ui.button(
        label="+13",
        style=discord.ButtonStyle.primary,
        custom_id="age_mais13",
        row=0
    )
    async def mais13(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_role(interaction, "mais13")

    @discord.ui.button(
        label="+18",
        style=discord.ButtonStyle.danger,
        custom_id="age_mais18",
        row=0
    )
    async def mais18(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_role(interaction, "mais18")

    @discord.ui.button(
        label="+21",
        style=discord.ButtonStyle.success,
        custom_id="age_mais21",
        row=0
    )
    async def mais21(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_role(interaction, "mais21")

# =========================================================
# EVENTOS
# =========================================================

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Slash commands sincronizados: {len(synced)}")
    except Exception as e:
        print(f"Erro ao sincronizar slash commands: {e}")

    # persistent view pros botões continuarem funcionando após restart
    bot.add_view(AgeView())

    if not change_status.is_running():
        change_status.start()

    print(f"Bot online como {bot.user}")

@bot.event
async def on_member_join(member: discord.Member):
    cfg = get_guild_config(member.guild.id)

    member_role_id = cfg.get("member_role_id")
    welcome_channel_id = cfg.get("welcome_channel_id")
    welcome_message = cfg.get("welcome_message") or DEFAULT_WELCOME_MESSAGE
    welcome_gif = cfg.get("welcome_gif")

    # cargo automático
    if member_role_id:
        role = member.guild.get_role(member_role_id)
        if role:
            try:
                await member.add_roles(role)
            except Exception as e:
                print(f"Erro ao dar cargo automático em {member.guild.name}: {e}")

    # canal de boas-vindas
    channel = await get_text_channel(member.guild, welcome_channel_id)
    if not channel:
        return

    text = format_welcome_text(welcome_message, member)

    embed = discord.Embed(
        description=text,
        color=discord.Color.from_rgb(0, 0, 0)
    )

    # foto da pessoa
    embed.set_thumbnail(url=member.display_avatar.url)

    # gif embaixo
    if welcome_gif:
        embed.set_image(url=welcome_gif)

    embed.set_footer(text=member.guild.name)

    try:
        await asyncio.sleep(1.2)
        await channel.send(embed=embed)
    except Exception as e:
        print(f"Erro ao enviar boas-vindas em {member.guild.name}: {e}")

# =========================================================
# COMANDOS
# =========================================================

@bot.tree.command(name="ping", description="Ver a latência do bot")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency * 1000)}ms")

@bot.tree.command(name="setup_boasvindas", description="Define o canal e o cargo automático")
@app_commands.check(admin_only)
@app_commands.describe(
    canal="Canal onde a mensagem de boas-vindas será enviada",
    cargo_membro="Cargo automático ao entrar"
)
async def setup_boasvindas(
    interaction: discord.Interaction,
    canal: discord.TextChannel,
    cargo_membro: discord.Role
):
    if interaction.guild is None:
        await interaction.response.send_message("Usa isso dentro de um servidor.", ephemeral=True)
        return

    set_guild_config(
        interaction.guild.id,
        welcome_channel_id=canal.id,
        member_role_id=cargo_membro.id
    )

    await interaction.response.send_message(
        f"configuração salva.\n\ncanal: {canal.mention}\ncargo automático: {cargo_membro.mention}",
        ephemeral=True
    )

@bot.tree.command(name="mensagem_boasvindas", description="Define o texto e gif da mensagem de boas-vindas")
@app_commands.check(admin_only)
@app_commands.describe(
    mensagem="Usa {user}, {username}, {server}, {members}",
    gif="Link do gif (opcional)"
)
async def mensagem_boasvindas(
    interaction: discord.Interaction,
    mensagem: str,
    gif: Optional[str] = None
):
    if interaction.guild is None:
        await interaction.response.send_message("Usa isso dentro de um servidor.", ephemeral=True)
        return

    set_guild_config(
        interaction.guild.id,
        welcome_message=mensagem,
        welcome_gif=gif
    )

    preview = (
        mensagem
        .replace("{user}", interaction.user.mention)
        .replace("{username}", interaction.user.name)
        .replace("{server}", interaction.guild.name)
        .replace("{members}", str(interaction.guild.member_count))
    )

    await interaction.response.send_message(
        f"mensagem de boas-vindas salva.\n\nprévia:\n{preview}",
        ephemeral=True
    )

@bot.tree.command(name="preview_boasvindas", description="Mostra uma prévia da mensagem de boas-vindas")
@app_commands.check(admin_only)
async def preview_boasvindas(interaction: discord.Interaction):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Usa isso dentro de um servidor.", ephemeral=True)
        return

    cfg = get_guild_config(interaction.guild.id)
    welcome_message = cfg.get("welcome_message") or DEFAULT_WELCOME_MESSAGE
    welcome_gif = cfg.get("welcome_gif")

    text = format_welcome_text(welcome_message, interaction.user)

    embed = discord.Embed(
        description=text,
        color=discord.Color.from_rgb(0, 0, 0)
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)

    if welcome_gif:
        embed.set_image(url=welcome_gif)

    embed.set_footer(text=interaction.guild.name)

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
async def setup_idade(
    interaction: discord.Interaction,
    canal: discord.TextChannel,
    cargo_menos13: discord.Role,
    cargo_mais13: discord.Role,
    cargo_mais18: discord.Role,
    cargo_mais21: discord.Role
):
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
        await interaction.response.send_message(
            "configura primeiro com `/setup_idade`.",
            ephemeral=True
        )
        return

    channel = await get_text_channel(interaction.guild, channel_id)
    if not channel:
        await interaction.response.send_message(
            "não consegui acessar o canal configurado.",
            ephemeral=True
        )
        return

    embed = build_age_embed(interaction.guild)
    view = AgeView(interaction.guild)

    msg = await channel.send(embed=embed, view=view)
    set_guild_config(interaction.guild.id, age_message_id=msg.id)

    await interaction.response.send_message(
        f"mensagem de idade enviada em {channel.mention}.",
        ephemeral=True
    )

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
        return channel.mention if channel else f"`{channel_id}`"

    def fmt_role(role_id):
        if not role_id:
            return "`não definido`"
        role = guild.get_role(role_id)
        return role.mention if role else f"`{role_id}`"

    emoji_checks = []
    for name in AGE_EMOJI_NAMES:
        emoji_checks.append(f"{name}: {'✅' if get_emoji_by_name(guild, name) else '❌'}")

    welcome_gif = cfg.get("welcome_gif") or "não definido"

    text = (
        f"**canal boas-vindas:** {fmt_channel(cfg.get('welcome_channel_id'))}\n"
        f"**cargo automático:** {fmt_role(cfg.get('member_role_id'))}\n"
        f"**canal idade:** {fmt_channel(cfg.get('age_channel_id'))}\n"
        f"**mensagem idade id:** `{cfg.get('age_message_id') or 'não definida'}`\n"
        f"**gif boas-vindas:** `{welcome_gif}`\n\n"
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

# =========================================================
# ERROS
# =========================================================

@setup_boasvindas.error
@mensagem_boasvindas.error
@preview_boasvindas.error
@setup_idade.error
@postar_idade.error
@config.error
@reset_idade.error
async def admin_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.errors.CheckFailure):
        if interaction.response.is_done():
            await interaction.followup.send(
                "tu precisa ser administrador pra usar isso.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "tu precisa ser administrador pra usar isso.",
                ephemeral=True
            )
        return

    if interaction.response.is_done():
        await interaction.followup.send(
            f"deu erro: `{error}`",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"deu erro: `{error}`",
            ephemeral=True
        )

# =========================================================
# RUN
# =========================================================

bot.run(TOKEN)
