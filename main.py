# main.py
import os
import json
import re
import asyncio
import logging
from datetime import datetime, timedelta, timezone


import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv


# -------------------------
# Load environment variables
# -------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
KIRA_FARMUPDATES_CHANNEL_ID = int(os.getenv("KIRA_FARMUPDATES_CHANNEL_ID"))
BOTFARMUPDATES_CHANNEL_ID = int(os.getenv("BOTFARMUPDATES_CHANNEL_ID"))
FARMS_STATUS_CHANNEL_ID = int(os.getenv("FARMS_STATUS_CHANNEL_ID"))
ROLE_TO_PING = os.getenv("PING_ROLE", "internal")


# -------------------------
# Logging
# -------------------------
handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")


# -------------------------
# Bot setup
# -------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
GUILD_OBJ = discord.Object(id=GUILD_ID) if GUILD_ID != 0 else None


# -------------------------
# Regex for Kira messages
# -------------------------
KIRA_REGEX = re.compile(
    r"`\[(?P<time>\d{2}:\d{2}:\d{2})\]`\s*`\[.*?\]`\s*\*\*\[(?P<user>.*?)\]\*\*\s*(?P<msg>.*)"
)


# -------------------------
# Status constants
# -------------------------
STATUS_UNKNOWN = "Unknown"
STATUS_FARMING = "Currently being farmed"
STATUS_READY = "Ready to be farmed"


# -------------------------
# JSON file and persistence
# -------------------------
FARMS_JSON_FILE = "farms.json"


def load_farms():
    """
    Returns dict with keys: last_message_id, status_message_id, farms (list).
    Converts numeric runtime/regrow_time stored in JSON back to timedelta.
    """
    if not os.path.exists(FARMS_JSON_FILE):
        return {"last_message_id": None, "status_message_id": None, "farms": []}
    try:
        with open(FARMS_JSON_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return {"last_message_id": None, "status_message_id": None, "farms": []}


    # Support older list-only format
    if isinstance(data, list):
        data = {"last_message_id": None, "status_message_id": None, "farms": data}


    # Convert stored secs back to timedelta
    for farm in data.get("farms", []):
        # next_ready was stored as ISO str or None
        if "next_ready" in farm and farm["next_ready"]:
            try:
                farm["next_ready"] = datetime.fromisoformat(farm["next_ready"])
            except Exception:
                farm["next_ready"] = None
        # runtime/regrow_time stored as seconds (or minutes/hours depending on older formats)
        if "runtime" in farm and isinstance(farm["runtime"], (int, float)):
            # runtime previously saved as seconds — convert to timedelta
            farm["runtime"] = timedelta(seconds=farm["runtime"])
        if "regrow_time" in farm and isinstance(farm["regrow_time"], (int, float)):
            farm["regrow_time"] = timedelta(seconds=farm["regrow_time"])
        if "status" not in farm:
            farm["status"] = STATUS_UNKNOWN


    return data


def save_farms(data):
    """
    Writes data (dict) to FARMS_JSON_FILE converting datetimes/timedeltas to serializable forms.
    """
    out = {
        "last_message_id": data.get("last_message_id"),
        "status_message_id": data.get("status_message_id"),
        "farms": []
    }
    for farm in data.get("farms", []):
        f = farm.copy()
        if "next_ready" in f and isinstance(f["next_ready"], datetime):
            f["next_ready"] = f["next_ready"].isoformat()
        if "runtime" in f and isinstance(f["runtime"], timedelta):
            f["runtime"] = int(f["runtime"].total_seconds())
        if "regrow_time" in f and isinstance(f["regrow_time"], timedelta):
            f["regrow_time"] = int(f["regrow_time"].total_seconds())
        out["farms"].append(f)
    with open(FARMS_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=4)


# -------------------------
# Load initial data
# -------------------------
data = load_farms()
farms = data.get("farms", [])


# If empty, populate sensible defaults (only for first-run convenience)
if not farms:
    farms = [
        {
            "name": "Farm_1",
            "coords": "(123, 64, -456)",
            "total_output": "50 ci wheat",
            "runtime": timedelta(minutes=30),
            "regrow_time": timedelta(hours=0.01),
            "next_ready": None,
            "status": STATUS_UNKNOWN
        },
        {
            "name": "Surmadri Wheat Farm",
            "coords": "(123, 64, -456)",
            "total_output": "5 cs wheat",
            "runtime": timedelta(minutes=15),
            "regrow_time": timedelta(hours=2),
            "next_ready": None,
            "status": STATUS_UNKNOWN
        }
    ]
    data["farms"] = farms
    save_farms(data)


# -------------------------
# Scheduler / notification helpers
# -------------------------
_scheduled_tasks = {}


async def notify_farm_ready_plain(farm):
    """
    Sends plain ping message to BOTFARMUPDATES_CHANNEL_ID when a farm becomes ready.
    """
    channel = bot.get_channel(BOTFARMUPDATES_CHANNEL_ID)
    if not channel:
        print("Bot updates channel not found:", BOTFARMUPDATES_CHANNEL_ID)
        return


    guild = channel.guild
    role = discord.utils.get(guild.roles, name=ROLE_TO_PING) if guild else None


    farm["status"] = STATUS_READY
    farm["next_ready"] = None
    save_farms(data)
    await update_farms_embed()
    await channel.send(f"{role.mention if role else '@'+ROLE_TO_PING} {farm['name']} is ready to be farmed again!")


async def schedule_notification_for_farm(farm):
    """
    Internal: sleep until next_ready then notify
    """
    if "next_ready" not in farm or farm["next_ready"] is None:
        return
    nr = farm["next_ready"]
    if isinstance(nr, str):
        try:
            nr = datetime.fromisoformat(nr)
        except Exception:
            farm["next_ready"] = None
            save_farms(data)
            return
    if nr.tzinfo is None:
        nr = nr.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delay = (nr - now).total_seconds()
    if delay <= 0:
        await notify_farm_ready_plain(farm)
        return
    try:
        await asyncio.sleep(delay)
        await notify_farm_ready_plain(farm)
    except asyncio.CancelledError:
        return


def schedule_task_for_farm(farm):
    """
    Create/replace scheduled notification task for a farm.
    """
    name = farm["name"]
    old = _scheduled_tasks.get(name)
    if old and not old.done():
        old.cancel()
    if farm.get("next_ready"):
        task = asyncio.create_task(schedule_notification_for_farm(farm))
        _scheduled_tasks[name] = task


# -------------------------
# Utility: robust farm finder
# -------------------------
def find_farms_by_name_partial(name_raw: str):
    """
    Return list of farms matching cleaned name (exact match first, else partial matches).
    """
    if not name_raw:
        return []
    clean = re.sub(r"\s+", " ", name_raw.strip().lower())
    exact = [f for f in farms if f["name"].strip().lower() == clean]
    if exact:
        return exact
    # fallback: contains
    partial = [f for f in farms if clean in f["name"].strip().lower()]
    return partial


def get_farm_exact(name_raw: str):
    matches = find_farms_by_name_partial(name_raw)
    return matches[0] if matches else None


# -------------------------
# Live embed management (single message)
# -------------------------
async def update_farms_embed():
    channel = bot.get_channel(FARMS_STATUS_CHANNEL_ID)
    if not channel:
        # channel not found: likely not in guild / missing perms
        print("Status channel not found:", FARMS_STATUS_CHANNEL_ID)
        return


    embed = discord.Embed(
        title="CivMC Agriculture Farms",
        description="Current status of all farms:",
        color=discord.Color.green()
    )


    for farm in farms:
        coords = farm.get("coords", "unknown")
        output = farm.get("total_output", "unknown")
        runtime = farm.get("runtime", timedelta())
        if isinstance(runtime, (int, float)):
            runtime = timedelta(seconds=runtime)
        runtime_minutes = int(runtime.total_seconds() / 60) if isinstance(runtime, timedelta) else "?"
        status = farm.get("status", STATUS_UNKNOWN)


        if farm.get("next_ready") and isinstance(farm["next_ready"], datetime):
            nr = farm["next_ready"]
            if nr.tzinfo is None:
                nr = nr.replace(tzinfo=timezone.utc)
            status_display = f"⏳ Will be ready <t:{int(nr.timestamp())}:R>"
        else:
            if status == STATUS_READY:
                status_display = "🌱 Ready"
            elif status == STATUS_FARMING:
                status_display = "⏳ Currently being farmed"
            else:
                status_display = "❌ Unknown"


        embed.add_field(
            name=farm["name"],
            value=(
                f"**Coords:** {coords}\n"
                f"**Total Output:** {output}\n"
                f"**Runtime:** {runtime_minutes} minutes\n"
                f"**Status:** {status_display}\n"
            ),
            inline=False
        )


    msg_id = data.get("status_message_id")
    try:
        if msg_id:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed)
        else:
            msg = await channel.send(embed=embed)
            data["status_message_id"] = msg.id
            save_farms(data)
    except discord.NotFound:
        # message deleted: recreate and store new id
        msg = await channel.send(embed=embed)
        data["status_message_id"] = msg.id
        save_farms(data)
        await channel.send("⚠️ The live farm status embed was deleted. A new one has been created.")


# -------------------------
# Kira message processing
# -------------------------
async def process_kira_message(message: discord.Message):
    text = message.content
    match = KIRA_REGEX.fullmatch(text)
    if not match:
        return

    time_str = match.group("time")
    user = match.group("user")
    msg = match.group("msg")

    if "|" not in msg:
        return

    farm_name_part, status_part = map(str.strip, msg.split("|", 1))
    status_text = status_part.lower()

    farm = get_farm_exact(farm_name_part)
    if not farm:
        print("Unknown farm in Kira message:", farm_name_part)
        return

    bot_channel = bot.get_channel(BOTFARMUPDATES_CHANNEL_ID)
    created_at = message.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    else:
        created_at = created_at.astimezone(timezone.utc)

    # Normalize regrow_time/runtime to timedelta
    if not isinstance(farm.get("regrow_time"), timedelta):
        if isinstance(farm.get("regrow_time"), (int, float)):
            farm["regrow_time"] = timedelta(seconds=farm["regrow_time"])
        else:
            farm["regrow_time"] = timedelta(seconds=0)
    if not isinstance(farm.get("runtime"), timedelta):
        if isinstance(farm.get("runtime"), (int, float)):
            farm["runtime"] = timedelta(seconds=farm["runtime"])
        else:
            farm["runtime"] = timedelta(seconds=0)

    # -------------------------
    # Handle start
    # -------------------------
    if "being started" in status_text:
        farm["status"] = STATUS_FARMING
        farm["next_ready"] = None
        save_farms(data)
        await update_farms_embed()

        # cancel any existing scheduled task
        task = _scheduled_tasks.get(farm["name"])
        if task and not task.done():
            task.cancel()
            _scheduled_tasks.pop(farm["name"], None)

        # schedule switchover after 2*runtime
        async def failsafe_task():
            try:
                await asyncio.sleep((farm["runtime"] * 2).total_seconds())
                # only run if farm hasn't already finished
                if farm.get("next_ready") is None:
                    next_ready_dt = datetime.now(timezone.utc) + farm["regrow_time"]
                    farm["next_ready"] = next_ready_dt
                    farm["status"] = STATUS_FARMING
                    save_farms(data)
                    await update_farms_embed()

                    # 🚨 schedule the actual ready ping
                    schedule_task_for_farm(farm)

                    if bot_channel:
                        role = discord.utils.get(bot_channel.guild.roles, name=ROLE_TO_PING)
                        await bot_channel.send(
                            f"{role.mention if role else '@'+ROLE_TO_PING} {farm['name']} has auto-switched to regrowing (failsafe). Next ready <t:{int(next_ready_dt.timestamp())}:R>"
                        )
            except asyncio.CancelledError:
                return


        _scheduled_tasks[farm["name"]] = asyncio.create_task(failsafe_task())

        # Send embed notification for start
        if bot_channel:
            embed = discord.Embed(
                title=f"{farm['name']} — started",
                description=f"{user} has started farming **{farm['name']}**.",
                color=discord.Color.orange()
            )
            embed.add_field(name="Kira time (UTC)", value=time_str, inline=True)
            embed.add_field(name="Recorded at (UTC)", value=created_at.strftime("%Y-%m-%d %H:%M:%S"), inline=True)
            await bot_channel.send(embed=embed)

    # -------------------------
    # Handle finish
    # -------------------------
    elif "has finished" in status_text:
        next_ready_dt = created_at + farm["regrow_time"]
        farm["next_ready"] = next_ready_dt
        farm["status"] = STATUS_FARMING
        save_farms(data)
        await update_farms_embed()
        schedule_task_for_farm(farm)  # cancels failsafe and replaces with real timer

        if bot_channel:
            embed = discord.Embed(
                title=f"{farm['name']} — finished",
                description=f"{user} has finished farming **{farm['name']}**.",
                color=discord.Color.green()
            )
            embed.add_field(name="Kira time (UTC)", value=time_str, inline=True)
            embed.add_field(name="Recorded at (UTC)", value=created_at.strftime("%Y-%m-%d %H:%M:%S"), inline=True)
            embed.add_field(name="Next Ready (UTC)", value=f"<t:{int(next_ready_dt.timestamp())}:F>", inline=False)
            await bot_channel.send(embed=embed)

    # persist last processed message id
    data["last_message_id"] = message.id
    save_farms(data)



# -------------------------
# Slash commands: /farms (single farm) with dynamic autocomplete
# -------------------------
@bot.tree.command(name="farms", description="Show the status of a specific farm", guild=GUILD_OBJ)
@app_commands.describe(farm_name="Select a farm to view its status")
async def farms_command(interaction: discord.Interaction, farm_name: str):
    # Accept either exact name (from autocomplete) or typed name
    farm = get_farm_exact(farm_name)
    if not farm:
        await interaction.response.send_message(f"❌ Farm '{farm_name}' not found.", ephemeral=True)
        return


    embed = discord.Embed(title=f"{farm['name']} Status", color=discord.Color.green())
    coords = farm.get("coords", "unknown")
    output = farm.get("total_output", "unknown")
    runtime = farm.get("runtime", timedelta())
    if isinstance(runtime, (int, float)):
        runtime = timedelta(seconds=runtime)
    runtime_minutes = int(runtime.total_seconds() / 60) if isinstance(runtime, timedelta) else "?"
    if farm.get("next_ready") and isinstance(farm["next_ready"], datetime):
        nr = farm["next_ready"]
        if nr.tzinfo is None:
            nr = nr.replace(tzinfo=timezone.utc)
        status_display = f"⏳ Will be ready <t:{int(nr.timestamp())}:R>"
    else:
        status = farm.get("status", STATUS_UNKNOWN)
        if status == STATUS_READY:
            status_display = "🌱 Ready"
        elif status == STATUS_FARMING:
            status_display = "⏳ Currently being farmed"
        else:
            status_display = "❌ Unknown"


    embed.add_field(
        name=farm["name"],
        value=(
            f"**Coords:** {coords}\n"
            f"**Total Output:** {output}\n"
            f"**Runtime:** {runtime_minutes} minutes\n"
            f"**Status:** {status_display}\n"
        ),
        inline=False
    )
    await interaction.response.send_message(embed=embed)


@farms_command.autocomplete("farm_name")
async def farms_autocomplete(interaction: discord.Interaction, current: str):
    # Provide up to 25 matching choices (discord limit)
    current_low = current or ""
    choices = [app_commands.Choice(name=f["name"], value=f["name"]) for f in farms if current_low.lower() in f["name"].lower()][:25]
    return choices


# -------------------------
# Slash command: /removefarm (AUTOFILL NAME via dynamic autocomplete)
# -------------------------
@bot.tree.command(name="removefarm", description="Remove a farm", guild=GUILD_OBJ)
@app_commands.describe(farm_name="Select a farm to remove")
async def removefarm(interaction: discord.Interaction, farm_name: str):
    global farms
    farm_name_str = str(farm_name).strip().lower()
    farm = next((f for f in farms if f["name"].strip().lower() == farm_name_str), None)
    if not farm:
        await interaction.response.send_message(f"❌ Farm '{farm_name}' not found.", ephemeral=True)
        return


    # Cancel any scheduled task
    _task = _scheduled_tasks.get(farm["name"])
    if _task and not _task.done():
        _task.cancel()
        _scheduled_tasks.pop(farm["name"], None)


    # Remove farm from list
    farms = [f for f in farms if f["name"].strip().lower() != farm_name_str]
    data["farms"] = farms
    save_farms(data)


    # Update the live embed
    await update_farms_embed()


    await interaction.response.send_message(f"✅ Farm '{farm['name']}' removed.", ephemeral=True)




# -------------------------
# Autocomplete for /removefarm
# -------------------------
@removefarm.autocomplete("farm_name")
async def removefarm_autocomplete(interaction: discord.Interaction, current: str):
    current_low = current or ""
    # Return up to 25 matching farms dynamically
    return [
        app_commands.Choice(name=f["name"], value=f["name"])
        for f in farms
        if current_low.lower() in f["name"].lower()
    ][:25]


# -------------------------
# Slash command: /addfarm (REQUIRED fields)
# -------------------------
@bot.tree.command(name="addfarm", description="Add a new farm (required fields)", guild=GUILD_OBJ)
@app_commands.describe(
    name="Name of the farm (must be unique)",
    coords="Coordinates string, e.g. (x, y, z)",
    total_output="Total output description, e.g. '5 cs wheat'",
    runtime_minutes="Expected runtime in minutes (integer)",
    regrow_hours="Regrow time in hours (decimal allowed)"
)
async def addfarm(
    interaction: discord.Interaction,
    name: str,
    coords: str,
    total_output: str,
    runtime_minutes: int,
    regrow_hours: float
):
    # Only basic duplicate check (case-insensitive)
    if any(f["name"].strip().lower() == name.strip().lower() for f in farms):
        await interaction.response.send_message(f"❌ A farm named '{name}' already exists.", ephemeral=True)
        return


    new_farm = {
        "name": name.strip(),
        "coords": coords.strip(),
        "total_output": total_output.strip(),
        "runtime": timedelta(minutes=int(runtime_minutes)),
        "regrow_time": timedelta(hours=float(regrow_hours)),
        "next_ready": None,
        "status": STATUS_UNKNOWN
    }
    farms.append(new_farm)
    data["farms"] = farms
    save_farms(data)


    # Schedule if had next_ready (none on create) and update embed
    await update_farms_embed()
    # respond to user
    await interaction.response.send_message(f"✅ Farm **{name}** added.", ephemeral=False)


# -------------------------
# Slash command: /editfarm (OPTIONAL fields)
# -------------------------
@bot.tree.command(name="editfarm", description="Edit an existing farm (all fields optional)", guild=GUILD_OBJ)
@app_commands.describe(
    farm_name="Select the farm to edit",
    coords="New coords (optional)",
    total_output="New total output (optional)",
    runtime_minutes="New runtime in minutes (optional)",
    regrow_hours="New regrow time in hours (optional)",
)
async def editfarm(
    interaction: discord.Interaction,
    farm_name: str,
    coords: str = None,
    total_output: str = None,
    runtime_minutes: int = None,
    regrow_hours: float = None,
):
    farm = get_farm_exact(farm_name)
    if not farm:
        await interaction.response.send_message(f"❌ Farm '{farm_name}' not found.", ephemeral=True)
        return

    updated = []
    if coords is not None:
        farm["coords"] = coords.strip()
        updated.append("coords")
    if total_output is not None:
        farm["total_output"] = total_output.strip()
        updated.append("total_output")
    if runtime_minutes is not None:
        farm["runtime"] = timedelta(minutes=int(runtime_minutes))
        updated.append("runtime")
    if regrow_hours is not None:
        farm["regrow_time"] = timedelta(hours=float(regrow_hours))
        updated.append("regrow_time")

    data["farms"] = farms
    save_farms(data)


    # If next_ready / scheduling affected by edits, reschedule
    schedule_task_for_farm(farm)
    await update_farms_embed()
    await interaction.response.send_message(f"✅ Updated {', '.join(updated) if updated else 'nothing'} for **{farm['name']}**.", ephemeral=True)


@editfarm.autocomplete("farm_name")
async def editfarm_autocomplete(interaction: discord.Interaction, current: str):
    current_low = current or ""
    return [app_commands.Choice(name=f["name"], value=f["name"]) for f in farms if current_low.lower() in f["name"].lower()][:25]


# -------------------------
# Events: on_ready, on_message
# -------------------------
@bot.event
async def on_ready():
    # sync commands
    try:
        if GUILD_ID != 0:
            synced = await bot.tree.sync(guild=GUILD_OBJ)
            print(f"Synced {len(synced)} slash commands to guild {GUILD_ID}")
        else:
            synced = await bot.tree.sync()
            print(f"Globally synced {len(synced)} slash commands")
    except Exception as e:
        print("Error syncing commands:", e)


    # Catch up missed Kira messages (only after last_message_id)
    now = datetime.now(timezone.utc)
    kira_channel = bot.get_channel(KIRA_FARMUPDATES_CHANNEL_ID)
    if kira_channel:
        after_msg = discord.Object(id=data["last_message_id"]) if data.get("last_message_id") else None
        async for message in kira_channel.history(limit=200, after=after_msg, oldest_first=True):
            if message.author.name.lower() == "intentgames":
                await process_kira_message(message)


    # schedule existing farms
    for farm in farms:
        nr = farm.get("next_ready")
        if isinstance(nr, str):
            try:
                farm["next_ready"] = datetime.fromisoformat(nr)
            except Exception:
                farm["next_ready"] = None
        if isinstance(farm.get("next_ready"), datetime):
            if farm["next_ready"].tzinfo is None:
                farm["next_ready"] = farm["next_ready"].replace(tzinfo=timezone.utc)
            if farm["next_ready"] <= now:
                farm["status"] = STATUS_READY
                farm["next_ready"] = None
                save_farms(data)
            else:
                schedule_task_for_farm(farm)


    await update_farms_embed()
    print("Bot is online!")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    # Process Kira messages only in configured Kira channel
    if message.author.name.lower() == "intentgames" and message.channel.id == KIRA_FARMUPDATES_CHANNEL_ID:
        await process_kira_message(message)
    await bot.process_commands(message)


# -------------------------
# Run
# -------------------------
bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)



