import asyncio
import copy
import datetime as dt
import json
import os
import tempfile
import traceback
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

import github_sync

TOKEN = os.getenv("DISCORD_TOKEN", "")
POTW_CHANNEL_ID = int(os.getenv("POTW_CHANNEL_ID", "1447357568958599329"))
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "1447383150811676683"))
TIMEZONE = ZoneInfo("America/New_York")
SOLVER_ROLE_NAME = "POTW Solver"
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
BASE_DIR = Path(__file__).resolve().parent

PROBLEMS_PATH = DATA_DIR / "problems.json"
LEADERBOARD_PATH = DATA_DIR / "leaderboard.json"
HISTORY_PATH = DATA_DIR / "points_history.json"
ARCHIVES_DIR = DATA_DIR / "archives"
ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
BANK_PATH = BASE_DIR / "problem_bank.json"

POST_LOCK = asyncio.Lock()
STATE_LOCK = asyncio.Lock()
dirty = True


def read_json(path, default):
    if not path.exists():
        return copy.deepcopy(default)
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path, value):
    """Atomic local write, so a restart does not see half a JSON file."""
    fd, name = tempfile.mkstemp(prefix=".potw_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def seed(name):
    return read_json(BASE_DIR / name, {})


def initialize():
    """Never overwrite existing Railway /data on boot."""
    if not PROBLEMS_PATH.exists():
        save_json(PROBLEMS_PATH, seed("seed_problems.json"))
    if not LEADERBOARD_PATH.exists():
        save_json(LEADERBOARD_PATH, seed("seed_leaderboard.json"))
    if not HISTORY_PATH.exists():
        save_json(HISTORY_PATH, {"events": []})


def get_problems():
    return read_json(PROBLEMS_PATH, {})


def get_leaderboard():
    lb = read_json(LEADERBOARD_PATH, {"users": {}})
    lb.setdefault("users", {})
    lb.setdefault("weekly_users", {})
    lb.setdefault("names", {})
    lb.setdefault("active_week", 0)
    return lb


def get_history():
    return read_json(HISTORY_PATH, {"events": []})


def iso_week_tag(now):
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


def normalize_answer(answer):
    if isinstance(answer, list):
        return [parse_number(str(x)) for x in answer]
    return [parse_number(str(answer))]


def parse_number(value):
    try:
        s = value.strip()
        if "/" in s:
            return Fraction(s)
        return Fraction(Decimal(s))
    except (ValueError, InvalidOperation, ZeroDivisionError, OverflowError):
        raise ValueError("Invalid number") from None


def choose_next_problem(problems):
    bank = read_json(BANK_PATH, {"problems": []}).get("problems", [])
    index = int(problems.get("next_bank_index", 0))
    if index >= len(bank):
        return None
    entry = bank[index]
    for required in ("statement", "answer", "source"):
        if required not in entry:
            raise ValueError(f"Problem bank entry {index} lacks {required}")
    problems["next_bank_index"] = index + 1
    return {"week": int(problems["current_problem"]["week"]) + 1,
            "statement": entry["statement"], "answer": entry["answer"],
            "source": entry["source"], "solved_users": [], "attempts": {}}


def build_archive(lb, now, tag):
    return {
        "tag": tag,
        "week": int(lb.get("active_week", 0)),
        "closed_at_utc": now.astimezone(dt.timezone.utc).isoformat(),
        "weekly_users": copy.deepcopy(lb.get("weekly_users", {})),
        "all_time_users": copy.deepcopy(lb.get("users", {})),
        "names": copy.deepcopy(lb.get("names", {})),
    }


def archive_path(archive):
    return ARCHIVES_DIR / f"{archive['tag']}_week_{archive['week']:03d}.json"


def save_archive_once(archive):
    path = archive_path(archive)
    if not path.exists():
        save_json(path, archive)


def mark_dirty():
    global dirty
    dirty = True


initialize()
intents = discord.Intents.default()
intents.members = False
bot = commands.Bot(command_prefix="!", intents=intents)


async def channel_for(channel_id):
    return bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)


async def solver_role_cleanup(guild, ids):
    if guild is None:
        return
    role = discord.utils.get(guild.roles, name=SOLVER_ROLE_NAME)
    if role is None:
        return
    for uid in ids:
        try:
            member = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
            await member.remove_roles(role, reason="POTW weekly rollover")
        except discord.NotFound:
            continue
        except Exception:
            traceback.print_exc()


async def post_potw_and_reset(force=False):
    """Post next POTW; safely snapshot prior weekly standings before clearing them."""
    async with POST_LOCK:
        now = dt.datetime.now(TIMEZONE)
        async with STATE_LOCK:
            problems = get_problems()
            lb = get_leaderboard()
            tag = iso_week_tag(now)
            if not force and problems.get("last_posted_tag") == tag:
                return "already_posted"
            upcoming = problems.get("current_problem")
            if not upcoming or not upcoming.get("statement"):
                return "no_problem"
            try:
                ch = await channel_for(POTW_CHANNEL_ID)
            except Exception:
                traceback.print_exc()
                return "no_channel"

            # Save the outgoing week before changing any scores. Keeping this
            # snapshot locally makes it available for GitHub retries.
            if int(lb.get("active_week", 0)) > 0 and int(lb["active_week"]) != int(upcoming["week"]):
                save_archive_once(build_archive(lb, now, tag))
            try:
                await ch.send(
                    f"@everyone\n**POTW - Week {upcoming['week']}**\n"
                    f"{upcoming['statement']}\n\n"
                    f"*{upcoming.get('source', 'Source: Unknown')}*\n\n"
                    "Submit with \`/answer\`."
                )
            except Exception:
                traceback.print_exc()
                return "send_failed"

            old_solver_ids = problems.get("active_problem", {}).get("solved_users", [])
            # For legacy installations, the stored current problem might be
            # the active one, so clear its roles as well when appropriate.
            if not old_solver_ids:
                old_solver_ids = upcoming.get("solved_users", [])
            problems["active_problem"] = copy.deepcopy(upcoming)
            problems["active_problem"]["solved_users"] = []
            problems["active_problem"]["attempts"] = {}
            lb["weekly_users"] = {}
            lb["active_week"] = int(upcoming["week"])
            problems["last_posted_week"] = int(upcoming["week"])
            problems["last_posted_tag"] = tag

            following = choose_next_problem(problems)
            if following is not None:
                problems["current_problem"] = following
            else:
                problems["current_problem"] = {}
            save_json(LEADERBOARD_PATH, lb)
            save_json(PROBLEMS_PATH, problems)
            mark_dirty()
        await solver_role_cleanup(ch.guild, old_solver_ids)
        return "posted"


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        registered = await bot.tree.sync()
        print(f"Synced {len(registered)} slash commands.")
    except Exception:
        traceback.print_exc()
    if not weekly_job.is_running():
        weekly_job.start()
    if not github_job.is_running():
        github_job.start()


@bot.tree.command(name="potw", description="Show the currently active Problem of the Week.")
async def potw_command(interaction: discord.Interaction):
    probs = get_problems()
    p = probs.get("active_problem")
    if not p:
        await interaction.response.send_message("No posted POTW has been recorded yet.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"**POTW - Week {p['week']}**\n{p['statement']}\n\n"
        f"*{p.get('source', 'Source: Unknown')}*"
    )


@bot.tree.command(name="answer", description="Submit a numeric answer, such as 3, -1.5 or 2/7.")
async def answer_command(interaction: discord.Interaction, guess: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    uid = str(interaction.user.id)
    try:
        value = parse_number(guess)
    except ValueError:
        await interaction.followup.send("Enter a number such as 3, -1.5 or 2/7.", ephemeral=True)
        return

    async with STATE_LOCK:
        probs = get_problems()
        lb = get_leaderboard()
        p = probs.get("active_problem")
        if not p:
            message = "No POTW is currently active."
            awarded = None
        elif uid in p.get("solved_users", []):
            message = "You already solved this week's problem."
            awarded = None
        elif value not in normalize_answer(p["answer"]):
            attempts = p.setdefault("attempts", {})
            attempts[uid] = attempts.get(uid, 0) + 1
            save_json(PROBLEMS_PATH, probs)
            message = "Incorrect."
            awarded = None
        else:
            wrong = int(p.setdefault("attempts", {}).get(uid, 0))
            # Existing scoring rule: 1.0, 0.8, 0.6, 0.4, 0.2, 0.0
            awarded = round(max(1.0 - 0.2 * wrong, 0.0), 1)
            lb["users"][uid] = round(float(lb["users"].get(uid, 0)) + awarded, 1)
            weekly = lb["weekly_users"]
            weekly[uid] = round(float(weekly.get(uid, 0)) + awarded, 1)
            lb["names"][uid] = interaction.user.display_name
            p.setdefault("solved_users", []).append(uid)
            history = get_history()
            history.setdefault("events", []).append({
                "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "week": int(p["week"]), "user_id": uid,
                "display_name": interaction.user.display_name,
                "wrong_attempts": wrong, "points": awarded
            })
            # Local events are durable even when GitHub is unavailable.
            save_json(HISTORY_PATH, history)
            save_json(LEADERBOARD_PATH, lb)
            save_json(PROBLEMS_PATH, probs)
            mark_dirty()
            message = f"Correct! +{awarded:.1f} points."

    await interaction.followup.send(message, ephemeral=True)
    if awarded is None:
        return
    try:
        ann = await channel_for(ANNOUNCE_CHANNEL_ID)
        await ann.send(f"**{discord.utils.escape_markdown(interaction.user.display_name)}** solved this week's POTW!")
    except Exception:
        traceback.print_exc()
    if interaction.guild:
        role = discord.utils.get(interaction.guild.roles, name=SOLVER_ROLE_NAME)
        if role:
            try:
                member = interaction.user if isinstance(interaction.user, discord.Member) else await interaction.guild.fetch_member(interaction.user.id)
                await member.add_roles(role, reason="Solved POTW")
            except Exception:
                traceback.print_exc()


@bot.tree.command(name="leaderboard", description="View weekly or all-time scores.")
async def leaderboard_command(interaction: discord.Interaction, page: int = 1, all_time: bool = False):
    lb = get_leaderboard()
    scores = lb["users"] if all_time else lb["weekly_users"]
    if page < 1:
        await interaction.response.send_message("Page must be 1 or greater.", ephemeral=True)
        return
    ordered = sorted(scores.items(), key=lambda item: (-float(item[1]), item[0]))
    segment = ordered[(page - 1) * 10:page * 10]
    title = "All-time leaderboard" if all_time else f"Week {lb['active_week']} leaderboard"
    lines = [f"**{title} (page {page})**"]
    for i, (uid, score) in enumerate(segment, (page - 1) * 10 + 1):
        name = lb.get("names", {}).get(uid, f"User {uid}")
        lines.append(f"{i}. {discord.utils.escape_markdown(name)}: {float(score):.1f}")
    if not segment:
        lines.append("No scores on this page.")
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="points", description="Show your current weekly and all-time POTW points.")
async def points_command(interaction: discord.Interaction):
    lb = get_leaderboard()
    uid = str(interaction.user.id)
    await interaction.response.send_message(
        f"Week {lb['active_week']}: **{float(lb['weekly_users'].get(uid, 0)):.1f}** points\n"
        f"All-time: **{float(lb['users'].get(uid, 0)):.1f}** points",
        ephemeral=True
    )


@bot.tree.command(name="force_post", description="Post the upcoming POTW now (administrator only).")
async def force_post_command(interaction: discord.Interaction):
    if not interaction.guild or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    status = await post_potw_and_reset(force=True)
    await interaction.followup.send(f"POTW post status: {status}", ephemeral=True)


@bot.tree.command(name="sync_leaderboard", description="Trigger a GitHub leaderboard sync (administrator only).")
async def sync_command(interaction: discord.Interaction):
    if not interaction.guild or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Admin only.", ephemeral=True)
        return
    mark_dirty()
    await interaction.response.send_message("Leaderboard queued for GitHub sync.", ephemeral=True)


@tasks.loop(minutes=1)
async def weekly_job():
    now = dt.datetime.now(TIMEZONE)
    if now.weekday() == 6 and now.hour >= 20:
        status = await post_potw_and_reset()
        if status not in ("already_posted", "posted"):
            print("Weekly post status:", status)


@weekly_job.before_loop
async def wait_for_ready():
    await bot.wait_until_ready()


async def _sync_once():
    global dirty
    if not github_sync.configured():
        return
    # Hold the state lock while copying the snapshots only.
    async with STATE_LOCK:
        lb = copy.deepcopy(get_leaderboard())
        history = copy.deepcopy(get_history())
        archives = [(p, read_json(p, {})) for p in sorted(ARCHIVES_DIR.glob("*.json"))]
        dirty = False
    try:
        # Publish archives first; only publish reset/current scores after
        # archived weeks are safely stored in GitHub.
        for _, archive in archives:
            if archive:
                await asyncio.to_thread(github_sync.publish_archive, archive)
        await asyncio.to_thread(github_sync.publish_snapshot, lb, history)
    except Exception:
        dirty = True
        traceback.print_exc()


@tasks.loop(minutes=1)
async def github_job():
    if dirty:
        await _sync_once()


@github_job.before_loop
async def before_sync():
    await bot.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Set DISCORD_TOKEN in Railway Variables.")
    bot.run(TOKEN)
