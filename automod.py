import discord
from discord.ext import commands
import logging

from database import client as db
from utils.llama import analyze_message
from utils.spam_detector import check_spam, reset_user

log = logging.getLogger("automod.cog")

# Severity → action thresholds
SEVERITY_ACTIONS = {
    "low":    "warn",
    "medium": "warn",
    "high":   "delete_and_warn",
}

# Warning count thresholds
WARN_ACTION_KICK = 3
WARN_ACTION_BAN = 5


class AutoMod(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ──────────────────────────────────────────────
    # Core message handler
    # ──────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bots and DMs
        if message.author.bot or not message.guild:
            return

        guild_config = await db.get_guild_config(message.guild.id)

        if not guild_config.get("automod_enabled", True):
            return

        member = message.author
        guild = message.guild
        content = message.content

        # ── Step 1: Rule-based spam check (fast, no API call) ──
        spam_result = check_spam(member.id, content)
        if spam_result["flagged"]:
            await self._handle_violation(
                message=message,
                category="spam",
                severity="medium",
                reason=spam_result["reason"],
                guild_config=guild_config,
            )
            return

        # ── Step 2: LLM-based content check ──
        if guild_config.get("llm_check_enabled", True) and content.strip():
            llm_result = await analyze_message(content)
            if llm_result.get("flagged"):
                await self._handle_violation(
                    message=message,
                    category=llm_result.get("category", "unknown"),
                    severity=llm_result.get("severity", "medium"),
                    reason=llm_result.get("reason", "Flagged by AI"),
                    guild_config=guild_config,
                )

    # ──────────────────────────────────────────────
    # Violation handler
    # ──────────────────────────────────────────────

    async def _handle_violation(
        self,
        message: discord.Message,
        category: str,
        severity: str,
        reason: str,
        guild_config: dict,
    ):
        member = message.author
        guild = message.guild
        bot_member = guild.me

        log.info(f"Violation: {member} | {category} | {severity} | {reason}")

        action = SEVERITY_ACTIONS.get(severity, "warn")

        # Always delete high-severity or spam messages
        if action == "delete_and_warn" or category == "spam":
            try:
                await message.delete()
                log.info(f"Deleted message from {member}")
            except discord.Forbidden:
                log.warning("Missing permission to delete message")

        # Add warning to DB
        warning = await db.add_warning(
            guild_id=guild.id,
            user_id=member.id,
            mod_id=self.bot.user.id,
            reason=reason,
            category=category,
            severity=severity,
            message_content=message.content[:500],
        )

        warn_count = await db.get_warning_count(guild.id, member.id)

        # Log to mod log channel
        await self._send_mod_log(guild, guild_config, member, category, severity, reason, warn_count, message)

        # DM the user
        await self._notify_user(member, reason, category, warn_count)

        # Escalate based on warning count
        threshold_kick = guild_config.get("warn_threshold_kick", WARN_ACTION_KICK)
        threshold_ban = guild_config.get("warn_threshold_ban", WARN_ACTION_BAN)

        if warn_count >= threshold_ban:
            await self._ban_user(guild, guild_config, member, reason, warn_count)
        elif warn_count >= threshold_kick:
            await self._kick_user(guild, guild_config, member, reason, warn_count)

    # ──────────────────────────────────────────────
    # Enforcement actions
    # ──────────────────────────────────────────────

    async def _kick_user(self, guild, guild_config, member, reason, warn_count):
        try:
            await member.kick(reason=f"AutoMod: {warn_count} warnings — {reason}")
            reset_user(member.id)
            await db.add_mod_log(guild.id, "kick", member.id, self.bot.user.id, reason, {"warn_count": warn_count})
            log.info(f"Kicked {member} ({warn_count} warnings)")

            await self._send_action_log(guild, guild_config, member, "⚠️ Kicked", reason, warn_count)
        except discord.Forbidden:
            log.warning(f"Cannot kick {member} — missing permissions or higher role")

    async def _ban_user(self, guild, guild_config, member, reason, warn_count):
        try:
            await member.ban(reason=f"AutoMod: {warn_count} warnings — {reason}", delete_message_days=1)
            reset_user(member.id)
            await db.add_mod_log(guild.id, "ban", member.id, self.bot.user.id, reason, {"warn_count": warn_count})
            log.info(f"Banned {member} ({warn_count} warnings)")

            await self._send_action_log(guild, guild_config, member, "🔨 Banned", reason, warn_count)
        except discord.Forbidden:
            log.warning(f"Cannot ban {member} — missing permissions or higher role")

    # ──────────────────────────────────────────────
    # Notifications
    # ──────────────────────────────────────────────

    async def _notify_user(self, member: discord.Member, reason: str, category: str, warn_count: int):
        try:
            embed = discord.Embed(
                title="⚠️ AutoMod Warning",
                description=f"You have been warned in **{member.guild.name}**.",
                color=discord.Color.orange(),
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Category", value=category.replace("_", " ").title(), inline=True)
            embed.add_field(name="Total Warnings", value=str(warn_count), inline=True)
            embed.set_footer(text="Continued violations may result in a kick or ban.")
            await member.send(embed=embed)
        except discord.Forbidden:
            log.info(f"Could not DM {member} — DMs disabled")

    async def _send_mod_log(self, guild, guild_config, member, category, severity, reason, warn_count, message):
        channel_id = guild_config.get("mod_log_channel_id")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if not channel:
            return

        color_map = {"low": discord.Color.yellow(), "medium": discord.Color.orange(), "high": discord.Color.red()}
        color = color_map.get(severity, discord.Color.orange())

        embed = discord.Embed(title="🚨 AutoMod Action", color=color)
        embed.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=False)
        embed.add_field(name="Category", value=category.replace("_", " ").title(), inline=True)
        embed.add_field(name="Severity", value=severity.title(), inline=True)
        embed.add_field(name="Warnings", value=str(warn_count), inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(name="Message", value=f"```{message.content[:300]}```", inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.timestamp = message.created_at

        await channel.send(embed=embed)

    async def _send_action_log(self, guild, guild_config, member, action_label, reason, warn_count):
        channel_id = guild_config.get("mod_log_channel_id")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if not channel:
            return

        embed = discord.Embed(
            title=f"{action_label}",
            description=f"{member.mention} (`{member.id}`) was actioned by AutoMod.",
            color=discord.Color.dark_red(),
        )
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="Total Warnings", value=str(warn_count))
        embed.set_thumbnail(url=member.display_avatar.url)

        await channel.send(embed=embed)


async def setup(bot):
    await bot.add_cog(AutoMod(bot))
