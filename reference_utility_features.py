import ast
import base64
import codecs
import csv
import datetime
import io
import json
import math
import re
from collections.abc import Callable, Iterable
from urllib.parse import quote, quote_plus
from urllib.parse import urlsplit

import aiohttp
import discord
from discord.ext import commands


_BINARY_OPERATORS: dict[
    type[ast.operator],
    Callable[[int | float, int | float], int | float],
] = {
    ast.Add: lambda left, right: left + right,
    ast.Sub: lambda left, right: left - right,
    ast.Mult: lambda left, right: left * right,
    ast.Div: lambda left, right: left / right,
    ast.FloorDiv: lambda left, right: left // right,
    ast.Mod: lambda left, right: left % right,
    ast.Pow: lambda left, right: left**right,
}
_UNARY_OPERATORS: dict[type[ast.unaryop], Callable[[int | float], int | float]] = {
    ast.UAdd: lambda value: +value,
    ast.USub: lambda value: -value,
}
_LENGTH_UNITS = {
    "mm": 0.001,
    "cm": 0.01,
    "m": 1,
    "km": 1000,
    "in": 0.0254,
    "ft": 0.3048,
    "yd": 0.9144,
    "mi": 1609.344,
}
_WEIGHT_UNITS = {
    "mg": 0.001,
    "g": 1,
    "kg": 1000,
    "oz": 28.349523125,
    "lb": 453.59237,
}


def _evaluate_math(node: ast.AST) -> int | float:
    if isinstance(node, ast.Expression):
        return _evaluate_math(node.body)
    if isinstance(node, ast.Constant):
        if type(node.value) is int:
            value: int | float = node.value
        elif type(node.value) is float:
            value = node.value
        else:
            raise ValueError("Only numeric constants and basic arithmetic are supported.")
        if not math.isfinite(value) or abs(value) > 1e100:
            raise ValueError("Number is outside the supported range.")
        return value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
        return _UNARY_OPERATORS[type(node.op)](_evaluate_math(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
        left = _evaluate_math(node.left)
        right = _evaluate_math(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e100):
            raise ValueError("Exponent is too large.")
        result = _BINARY_OPERATORS[type(node.op)](left, right)
        if type(result) not in (int, float) or not math.isfinite(result) or abs(result) > 1e100:
            raise ValueError("Result is outside the supported range.")
        return result
    raise ValueError("Only numeric constants and basic arithmetic are supported.")


def calculate(expression: str) -> int | float:
    if not expression.strip() or len(expression) > 120:
        raise ValueError("Enter an expression up to 120 characters long.")
    try:
        return _evaluate_math(ast.parse(expression, mode="eval"))
    except (SyntaxError, OverflowError, ZeroDivisionError) as error:
        raise ValueError("That expression could not be calculated.") from error


def _convert_text(mode: str, text: str, *, decode: bool = False) -> str:
    mode = mode.casefold()
    if mode == "rot13":
        return codecs.decode(text, "rot_13")
    if decode:
        if mode == "base32":
            padded = text.upper() + "=" * ((8 - len(text) % 8) % 8)
            return base64.b32decode(padded).decode("utf-8")
        if mode == "hex":
            return bytes.fromhex(text).decode("utf-8")
        if mode == "binary":
            chunks = text.split()
            return bytes(int(chunk, 2) for chunk in chunks).decode("utf-8")
    else:
        if mode == "base32":
            return base64.b32encode(text.encode("utf-8")).decode("ascii").rstrip("=")
        if mode == "hex":
            return text.encode("utf-8").hex()
        if mode == "binary":
            return " ".join(f"{byte:08b}" for byte in text.encode("utf-8"))
    raise ValueError("Choose base32, hex, rot13, or binary.")


def _is_https_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.netloc)


class ReferenceUtilityFeatures(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        await self.bot.db.execute(
            """CREATE TABLE IF NOT EXISTS reference_todos (
                todo_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                task TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        await self.bot.db.execute(
            """CREATE TABLE IF NOT EXISTS reference_profiles (
                user_id INTEGER PRIMARY KEY,
                bio TEXT NOT NULL DEFAULT '',
                background_url TEXT NOT NULL DEFAULT '',
                social_links TEXT NOT NULL DEFAULT '{}'
            )"""
        )
        await self.bot.db.commit()

    @commands.group(name="convert", invoke_without_command=True)
    async def convert(self, ctx: commands.Context) -> None:
        await ctx.send(
            "Use `convert length`, `convert weight`, `convert encode`, or `convert decode`."
        )

    @convert.command(name="length")
    async def convert_length(
        self,
        ctx: commands.Context,
        amount: float,
        from_unit: str,
        to_unit: str,
    ) -> None:
        source = _LENGTH_UNITS.get(from_unit.casefold())
        target = _LENGTH_UNITS.get(to_unit.casefold())
        if source is None or target is None:
            return await ctx.send("Length units: mm, cm, m, km, in, ft, yd, mi.")
        result = amount * source / target
        await ctx.send(f"`{amount:g} {from_unit}` = `{result:.8g} {to_unit}`")

    @convert.command(name="weight")
    async def convert_weight(
        self,
        ctx: commands.Context,
        amount: float,
        from_unit: str,
        to_unit: str,
    ) -> None:
        source = _WEIGHT_UNITS.get(from_unit.casefold())
        target = _WEIGHT_UNITS.get(to_unit.casefold())
        if source is None or target is None:
            return await ctx.send("Weight units: mg, g, kg, oz, lb.")
        result = amount * source / target
        await ctx.send(f"`{amount:g} {from_unit}` = `{result:.8g} {to_unit}`")

    @convert.command(name="encode")
    async def convert_encode(self, ctx: commands.Context, mode: str, *, text: str) -> None:
        try:
            result = _convert_text(mode, text)
        except ValueError as error:
            return await ctx.send(str(error))
        if len(result) > 1800:
            return await ctx.send("Encoded output is too long to send as a message.")
        await ctx.send(f"```{result}```")

    @convert.command(name="decode")
    async def convert_decode(self, ctx: commands.Context, mode: str, *, text: str) -> None:
        try:
            result = _convert_text(mode, text, decode=True)
        except (ValueError, UnicodeDecodeError) as error:
            return await ctx.send(f"Could not decode that input: {error}")
        if len(result) > 1800:
            return await ctx.send("Decoded output is too long to send as a message.")
        await ctx.send(f"```{result}```")

    @commands.command(name="calculate", aliases=["calc"])
    async def calculate_command(self, ctx: commands.Context, *, expression: str) -> None:
        try:
            result = calculate(expression)
        except (ValueError, ArithmeticError) as error:
            return await ctx.send(str(error))
        await ctx.send(f"`{expression}` = `{result:,}`")

    @commands.command(name="wiki", aliases=["wikipedia"])
    async def wiki(self, ctx: commands.Context, *, query: str) -> None:
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(query.replace(' ', '_'))}"
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers={"User-Agent": "FalconBot/1.0"}) as response:
                    if response.status == 404:
                        return await ctx.send("No Wikipedia article found for that search.")
                    response.raise_for_status()
                    article = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return await ctx.send("Wikipedia is unavailable right now. Try again later.")
        page = article.get("content_urls", {}).get("desktop", {}).get("page")
        description = article.get("extract") or "No summary is available for this article."
        embed = discord.Embed(
            title=article.get("title", query)[:256],
            description=description[:4000],
            url=page,
            color=0xFFDD00,
        )
        thumbnail = article.get("thumbnail", {}).get("source")
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)
        await ctx.send(embed=embed)

    @commands.command(name="github")
    async def github_search(self, ctx: commands.Context, *, query: str) -> None:
        await ctx.send(f"GitHub repository search: https://github.com/search?q={quote_plus(query)}&type=repositories")

    @commands.command(name="youtube")
    async def youtube_search(self, ctx: commands.Context, *, query: str) -> None:
        await ctx.send(f"YouTube search: https://www.youtube.com/results?search_query={quote_plus(query)}")

    @commands.command(name="globalboard")
    async def globalboard(self, ctx: commands.Context, category: str = "messages") -> None:
        category = category.casefold()
        if category == "messages":
            query = (
                "SELECT user_id, total_msgs FROM users WHERE guild_id=? "
                "ORDER BY total_msgs DESC, user_id LIMIT 10"
            )
            label = "messages"
        elif category == "invites":
            query = (
                "SELECT user_id, joins - left + bonus AS score FROM users WHERE guild_id=? "
                "ORDER BY score DESC, user_id LIMIT 10"
            )
            label = "invites"
        else:
            return await ctx.send("Choose a global leaderboard: `messages` or `invites`.")
        async with self.bot.db.execute(query, (ctx.guild.id if ctx.guild else 0,)) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return await ctx.send("No leaderboard data for this server yet.")
        lines = []
        for rank, (user_id, score) in enumerate(rows, 1):
            user = self.bot.get_user(user_id)
            name = user.mention if user else f"<@{user_id}>"
            lines.append(f"**{rank}.** {name} - **{score:,}** {label}")
        embed = discord.Embed(
            title=f"Server {label.title()} Leaderboard",
            description="\n".join(lines),
            color=0xFFDD00,
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="fakehack")
    async def fakehack(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        target = member or ctx.author
        embed = discord.Embed(
            title="Simulated Hack Complete",
            description=(
                f"Target: {target.mention}\n"
                "This is a fictional roleplay result. No account, device, or server was accessed."
            ),
            color=0xFFDD00,
        )
        embed.add_field(name="Files scanned", value="0", inline=True)
        embed.add_field(name="Credentials accessed", value="0", inline=True)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="animal")
    async def animal(self, ctx: commands.Context, animal_type: str = "cat") -> None:
        animal_type = animal_type.casefold()
        if animal_type == "cat":
            fact_url = "https://catfact.ninja/fact"
            image_url = "https://api.thecatapi.com/v1/images/search"
        elif animal_type == "dog":
            fact_url = "https://dogapi.dog/api/v2/facts?limit=1"
            image_url = "https://dog.ceo/api/breeds/image/random"
        elif animal_type == "fox":
            fact_url = None
            image_url = "https://randomfox.ca/floof/"
        else:
            return await ctx.send("Choose an animal: `cat`, `dog`, or `fox`.")

        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                fact = None
                if fact_url:
                    async with session.get(fact_url) as response:
                        response.raise_for_status()
                        data = await response.json()
                        if animal_type == "dog":
                            fact = data["data"][0]["attributes"]["body"]
                        else:
                            fact = data.get("fact")
                async with session.get(image_url) as response:
                    response.raise_for_status()
                    image_data = await response.json()
                    if animal_type == "dog":
                        image = image_data.get("message")
                    elif animal_type == "fox":
                        image = image_data.get("image")
                    else:
                        image = image_data[0].get("url")
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, IndexError, TypeError):
            return await ctx.send("Animal facts or images are unavailable right now.")
        embed = discord.Embed(title=f"A {animal_type.title()} fact", color=0xFFDD00)
        if fact:
            embed.description = fact[:4000]
        if isinstance(image, str) and _is_https_url(image):
            embed.set_image(url=image)
        await ctx.send(embed=embed)

    @commands.command(name="meme")
    async def meme(self, ctx: commands.Context) -> None:
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get("https://meme-api.com/gimme/wholesomememes") as response:
                    response.raise_for_status()
                    data = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return await ctx.send("The meme source is unavailable right now.")
        image = data.get("url", "")
        if data.get("nsfw") or not _is_https_url(image):
            return await ctx.send("The meme source did not return a safe image.")
        embed = discord.Embed(
            title=data.get("title", "Wholesome meme")[:256],
            url=data.get("postLink"),
            color=0xFFDD00,
        )
        embed.set_image(url=image)
        embed.set_footer(text=f"r/{data.get('subreddit', 'wholesomememes')}")
        await ctx.send(embed=embed)

    @commands.group(name="profileedit", invoke_without_command=True)
    async def profileedit(self, ctx: commands.Context) -> None:
        await ctx.send("Use `profileedit bio`, `profileedit background`, or `profileedit social`.")

    @profileedit.command(name="bio")
    async def profileedit_bio(self, ctx: commands.Context, *, bio: str) -> None:
        if len(bio) > 300:
            return await ctx.send("Profile bios must be 300 characters or fewer.")
        await self.bot.db.execute(
            "INSERT INTO reference_profiles (user_id, bio) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET bio=excluded.bio",
            (ctx.author.id, bio.strip()),
        )
        await self.bot.db.commit()
        await ctx.send("Profile bio updated.")

    @profileedit.command(name="background")
    async def profileedit_background(self, ctx: commands.Context, url: str) -> None:
        if not _is_https_url(url):
            return await ctx.send("Use a valid HTTPS image URL.")
        await self.bot.db.execute(
            "INSERT INTO reference_profiles (user_id, background_url) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET background_url=excluded.background_url",
            (ctx.author.id, url[:1000]),
        )
        await self.bot.db.commit()
        await ctx.send("Profile background updated.")

    @profileedit.command(name="social")
    async def profileedit_social(
        self,
        ctx: commands.Context,
        platform: str,
        url: str,
    ) -> None:
        platform = platform.casefold()
        if not re.fullmatch(r"[a-z0-9_-]{1,20}", platform):
            return await ctx.send("Platform names may contain letters, numbers, `_`, or `-`.")
        if not _is_https_url(url):
            return await ctx.send("Use a valid HTTPS profile URL.")
        async with self.bot.db.execute(
            "SELECT social_links FROM reference_profiles WHERE user_id=?", (ctx.author.id,)
        ) as cursor:
            row = await cursor.fetchone()
        try:
            links = json.loads(row[0]) if row else {}
        except (TypeError, ValueError):
            links = {}
        if platform not in links and len(links) >= 5:
            return await ctx.send("You can configure up to five social links.")
        links[platform] = url[:1000]
        await self.bot.db.execute(
            "INSERT INTO reference_profiles (user_id, social_links) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET social_links=excluded.social_links",
            (ctx.author.id, json.dumps(links)),
        )
        await self.bot.db.commit()
        await ctx.send(f"{platform.title()} link updated.")

    @profileedit.command(name="clear")
    async def profileedit_clear(self, ctx: commands.Context) -> None:
        await self.bot.db.execute(
            "DELETE FROM reference_profiles WHERE user_id=?", (ctx.author.id,)
        )
        await self.bot.db.commit()
        await ctx.send("Your custom profile details were cleared.")

    @commands.command(name="profilecard")
    async def profilecard(
        self,
        ctx: commands.Context,
        member: discord.Member | None = None,
    ) -> None:
        target = member or ctx.author
        async with self.bot.db.execute(
            "SELECT bio, background_url, social_links FROM reference_profiles WHERE user_id=?",
            (target.id,),
        ) as cursor:
            row = await cursor.fetchone()
        embed = discord.Embed(
            title=f"{target.display_name}'s Profile",
            description=row[0] if row and row[0] else "No bio set.",
            color=0xFFDD00,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        if row and _is_https_url(row[1]):
            embed.set_image(url=row[1])
        if row and row[2]:
            try:
                links = json.loads(row[2])
            except (TypeError, ValueError):
                links = {}
            if isinstance(links, dict):
                for platform, url in list(links.items())[:5]:
                    if _is_https_url(url):
                        embed.add_field(name=platform.title()[:256], value=f"[Open profile]({url})", inline=True)
        await ctx.send(embed=embed)

    @commands.group(name="todo", invoke_without_command=True)
    async def todo(self, ctx: commands.Context) -> None:
        await self.todo_list(ctx)

    @todo.command(name="add")
    async def todo_add(self, ctx: commands.Context, *, task: str) -> None:
        if not task.strip() or len(task) > 500:
            return await ctx.send("Todo text must be between 1 and 500 characters.")
        cursor = await self.bot.db.execute(
            "INSERT INTO reference_todos (user_id, guild_id, task, created_at) VALUES (?, ?, ?, ?)",
            (
                ctx.author.id,
                ctx.guild.id if ctx.guild else 0,
                task.strip(),
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
            ),
        )
        await self.bot.db.commit()
        await ctx.send(f"Todo `{cursor.lastrowid}` added.")

    @todo.command(name="list")
    async def todo_list(self, ctx: commands.Context) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        async with self.bot.db.execute(
            "SELECT todo_id, task FROM reference_todos WHERE user_id=? AND guild_id=? "
            "ORDER BY todo_id DESC LIMIT 20",
            (ctx.author.id, guild_id),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return await ctx.send("Your todo list is empty.")
        lines = [f"`{todo_id}` {task}" for todo_id, task in rows]
        chunks = []
        current_chunk = ""
        for line in lines:
            candidate = f"{current_chunk}\n{line}" if current_chunk else line
            if len(candidate) > 2000:
                chunks.append(current_chunk)
                current_chunk = line
            else:
                current_chunk = candidate
        if current_chunk:
            chunks.append(current_chunk)
        for chunk in chunks:
            await ctx.send(chunk)

    @todo.command(name="remove")
    async def todo_remove(self, ctx: commands.Context, todo_id: int) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        cursor = await self.bot.db.execute(
            "DELETE FROM reference_todos WHERE todo_id=? AND user_id=? AND guild_id=?",
            (todo_id, ctx.author.id, guild_id),
        )
        await self.bot.db.commit()
        await ctx.send("Todo removed." if cursor.rowcount else "Todo not found.")

    @commands.command(name="export")
    @commands.has_guild_permissions(administrator=True)
    async def export_data(self, ctx: commands.Context, data_type: str) -> None:
        guild = ctx.guild
        if guild is None:
            return await ctx.send("This command can only be used in a server.")
        headers: tuple[str, ...]
        rows: Iterable[tuple[object, ...]]
        if data_type == "members":
            headers = ("user_id", "username", "display_name", "joined_at", "bot")
            rows = (
                (member.id, str(member), member.display_name, member.joined_at, member.bot)
                for member in guild.members
            )
        elif data_type == "roles":
            headers = ("role_id", "name", "position", "permissions", "managed")
            rows = (
                (role.id, role.name, role.position, role.permissions.value, role.managed)
                for role in guild.roles
            )
        elif data_type == "bans":
            try:
                bans = [entry async for entry in guild.bans()]
            except discord.HTTPException:
                return await ctx.send("Could not retrieve the server ban list.")
            headers = ("user_id", "username", "reason")
            rows = ((entry.user.id, str(entry.user), entry.reason or "") for entry in bans)
        elif data_type == "messages":
            if not ctx.channel.permissions_for(guild.me).read_message_history:
                return await ctx.send("I need Read Message History permission to export messages.")
            headers = ("message_id", "created_at", "author_id", "author", "content")
            messages = [message async for message in ctx.channel.history(limit=1000, oldest_first=True)]
            rows = (
                (message.id, message.created_at, message.author.id, str(message.author), message.content)
                for message in messages
            )
        else:
            return await ctx.send("Choose `members`, `roles`, `bans`, or `messages`.")

        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(headers)
        writer.writerows(rows)
        payload = io.BytesIO(buffer.getvalue().encode("utf-8-sig"))
        await ctx.send(file=discord.File(payload, filename=f"{data_type}-{guild.id}.csv"))