import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import motor.motor_asyncio
from datetime import datetime, timedelta
import logging
from typing import Dict, List, Optional
from collections import defaultdict
from functools import wraps
import os
import string
import random
from utils import serverInitTemplate
from sqldb import dbSql, Users, Server, Subscriptions
from dotenv import load_dotenv

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('ModBot')

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')


class RateLimiter:
    def __init__(self):
        self.command_usage = defaultdict(lambda: defaultdict(list))
        logger.info("RateLimiter initialized")  # Add logging
        
    def clean_old_usage(self, user_id: int, command_name: str, interval_seconds: int):
        """Remove usage records older than the rate limit interval"""
        current_time = datetime.utcnow()
        self.command_usage[user_id][command_name] = [
            timestamp for timestamp in self.command_usage[user_id][command_name]
            if current_time - timestamp < timedelta(seconds=interval_seconds)
        ]


    def is_rate_limited(self, user_id: int, command_name: str, times: int, interval_seconds: int) -> tuple[bool, Optional[float]]:
        """Check if a user has exceeded their rate limit for a command"""
        self.clean_old_usage(user_id, command_name, interval_seconds)
        
        usage = self.command_usage[user_id][command_name]
        
        if len(usage) >= times:
            current_time = datetime.utcnow()
            oldest_usage = usage[0]
            retry_after = interval_seconds - (current_time - oldest_usage).total_seconds()
            return True, max(0, retry_after)
        
        return False, None

    def add_usage(self, user_id: int, command_name: str):
        """Record a command usage"""
        self.command_usage[user_id][command_name].append(datetime.utcnow())

# Global rate limiter instance
rate_limiter = RateLimiter()


class ReactionRolesSelectView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        # Set a unique custom_id for this view
        self.custom_id = f"reaction_roles_select_view_{guild_id}"

class ReactionRolesButtonView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        # Set a unique custom_id for this view
        self.custom_id = f"reaction_roles_button_view_{guild_id}"

class ReactionRolesSelect(discord.ui.Select):
    def __init__(self, roles: List[discord.Role], placeholder: str, guild_id: int):
        options = [
            discord.SelectOption(
                label=role.name,
                value=str(role.id),
                description=f"Select this to change {role.name} role",
                emoji='📌' 
            )
            for role in roles
        ]
        
        # Set a unique custom_id for this select menu
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=len(roles),
            options=options,
            custom_id=f"reaction_roles_select_{guild_id}"
        )

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user
        guild = interaction.guild
        
        if not member.guild.me.guild_permissions.manage_roles:
            embed = discord.Embed(
                title="Missing Permissions",
                description="I need the `Manage Roles` permission to assign roles.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        selected_roles = [guild.get_role(int(role_id)) for role_id in self.values]
        roles_to_add = [role for role in selected_roles if role not in member.roles]
        roles_to_remove = [role for role in selected_roles if role in member.roles]

        if roles_to_add:
            await member.add_roles(*roles_to_add)
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove)

        embed = discord.Embed(
            title="Roles Updated",
            description="Here are your updated roles:",
            color=discord.Color.dark_gold()
        )
        embed.add_field(
            name="Added Roles",
            value="\n".join(role.mention for role in roles_to_add) if roles_to_add else "None",
            inline=False
        )
        embed.add_field(
            name="Removed Roles",
            value="\n".join(role.mention for role in roles_to_remove) if roles_to_remove else "None",
            inline=False
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)
        self.placeholder = "React to update your roles"
        await interaction.message.edit(view=self.view)

class ReactionRolesButton(discord.ui.Button):
    def __init__(self, role: discord.Role, guild_id: int):
        # Initialize with role-specific label and custom id
        super().__init__(
            label=role.name,
            custom_id=f"reaction_role_button_{guild_id}_{role.id}",
            style=discord.ButtonStyle.green
        )
        self.role = role

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user
        
        # Check bot permissions
        if not member.guild.me.guild_permissions.manage_roles:
            embed = discord.Embed(
                title="Missing Permissions",
                description="I need the `Manage Roles` permission to assign roles.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Toggle role
        if self.role in member.roles:
            await member.remove_roles(self.role)
            action = "Removed"
        else:
            await member.add_roles(self.role)
            action = "Added"

        # Send confirmation
        embed = discord.Embed(
            title="Role Updated",
            description=f"✅ {action} role: {self.role.mention}",
            color=discord.Color.dark_gold()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
def rate_limit(times: int, seconds: int, premium_multiplier: float = 2.0):
    """Rate limit decorator for app commands"""
    
    def decorator(func):
        
        @wraps(func)
        async def wrapper(self, interaction: discord.Interaction, *args, **kwargs):
            
            user_id = interaction.user.id
            command_name = func.__name__

            # Check if server is premium
            server_premium = False
            try:
                server = Server.query.filter_by(discord_id=interaction.guild.id).first()
                if server and server.isPremium:
                    server_premium = True
            except Exception as e:
                logger.error(f"Error checking premium status: {str(e)}")
                dbSql.session.rollback()
            finally:
                dbSql.session.close()

            # Adjust rate limit for premium servers
            effective_times = int(times * premium_multiplier) if server_premium else times
            
            # Check rate limit
            is_limited, retry_after = rate_limiter.is_rate_limited(
                user_id, 
                command_name, 
                effective_times,
                seconds
            )

            if is_limited:
                # Format retry time more readably
                if retry_after >= 60:
                    time_str = f"{retry_after // 60:.0f} minutes and {retry_after % 60:.0f} seconds"
                else:
                    time_str = f"{retry_after:.1f} seconds"
                
                embed = discord.Embed(
                    title="Rate Limited",
                    description=f"Please wait {time_str} before using this command again. This is ratelimited to prevent abuse.",
                    color=discord.Color.red()
                )
                if not server_premium:
                    premium_times = int(times * premium_multiplier)
                    embed.add_field(
                        name="Premium Benefits",
                        value=f"Premium servers can use this command {premium_times} times every {seconds} seconds! Consider upgrading for better limits.",
                        inline=False
                    )
                embed.set_footer(text="Developed by Pro-tonn")
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            # Record usage and execute command
            rate_limiter.add_usage(user_id, command_name)
            return await func(self, interaction, *args, **kwargs)

        return wrapper
    return decorator

class ModBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        intents.message_content = True
        intents.auto_moderation = True
        super().__init__(command_prefix='!', intents=intents)
        
    async def setup_hook(self):
        """Setup hook for the bot"""
        await self.load_extension('main')
        await self.tree.sync()

        # Register persistent views for each guild
        async for server in motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI).Protonn.ServerProperties.find({}):
            if 'configs' in server and 'reaction_roles' in server['configs']:
                reaction_roles = server['configs']['reaction_roles']
                if reaction_roles.get('active'):
                    guild = await self.fetch_guild(server['server_id'])
                    if guild:
                        selectView = ReactionRolesSelectView(guild.id)
                        buttonView = ReactionRolesButtonView(guild.id)
                        roles = [guild.get_role(role.id) for role in guild.roles if not role.is_bot_managed()]
                        roles = [r for r in roles if r is not None]
                        if roles:
                            roles = [guild.get_role(role_id) for role_id in reaction_roles['content']['roles']]
                            selectView.add_item(ReactionRolesSelect(roles, "React to get your roles", guild.id))
                            self.add_view(selectView)
                            for role in roles:
                                buttonView.add_item(ReactionRolesButton(role, guild.id))
                            self.add_view(buttonView)

        logger.info("Bot setup completed")

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """Handle slash command errors comprehensively"""
        try:
            # Handle different types of command errors
            if isinstance(error, app_commands.CommandOnCooldown):
                embed = discord.Embed(
                    title="Cooldown Active",
                    description=f"Please wait {error.retry_after:.2f} seconds before using this command again.",
                    color=discord.Color.red()
                )
            
            elif isinstance(error, app_commands.MissingPermissions):
                missing_perms = [perm.replace('_', ' ').title() for perm in error.missing_permissions]
                missing_perms = "\n• ".join(missing_perms)
                embed = discord.Embed(
                    title="Missing Permissions",
                    description=f"You need the following permissions to use this command:\n• {missing_perms}",
                    color=discord.Color.red()
                )
                
            elif isinstance(error, app_commands.BotMissingPermissions):
                missing_perms = [perm.replace('_', ' ').title() for perm in error.missing_permissions]
                missing_perms = "\n• ".join(missing_perms)
                embed = discord.Embed(
                    title="Bot Missing Permissions",
                    description=f"I need the following permissions to execute this command:\n• {missing_perms}",
                    color=discord.Color.red()
                )
                
            elif isinstance(error, app_commands.CheckFailure):
                embed = discord.Embed(
                    title="Permission Denied",
                    description="You don't have permission to use this command.",
                    color=discord.Color.red()
                )
                
            elif isinstance(error, app_commands.TransformerError):
                embed = discord.Embed(
                    title="Invalid Input",
                    description="The provided input was invalid. Please check the command usage and try again.",
                    color=discord.Color.red()
                )
                
            elif isinstance(error, app_commands.CommandNotFound):
                embed = discord.Embed(
                    title="Command Not Found",
                    description="This command doesn't exist. Use `/help` to see available commands.",
                    color=discord.Color.red()
                )
                
            elif isinstance(error, app_commands.NoPrivateMessage):
                embed = discord.Embed(
                    title="Server Only",
                    description="This command can only be used in servers, not in private messages.",
                    color=discord.Color.red()
                )
                
            else:
                # Handle unexpected errors
                embed = discord.Embed(
                    title="Error",
                    description="An unexpected error occurred. Please try again later or report this to the bot developers.",
                    color=discord.Color.red()
                )
                # Log the full error for debugging
                logger.error(f"Unhandled command error: {str(error)}")
            
            # Add standard footer to all error embeds
            embed.set_footer(text="Developed by Pro-tonn", icon_url=self.user.display_avatar)
            
            # Check if the interaction has been responded to
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
                
        except Exception as e:
            # Catch-all for errors in the error handler itself
            logger.error(f"Error in error handler: {str(e)}")
            try:
                await interaction.followup.send(
                    "An error occurred while processing your command.",
                    ephemeral=True
                )
            except:
                pass

    async def on_ready(self):
        """Called when the bot is ready"""
        logger.info(f'Logged in as {self.user} (ID: {self.user.id})')
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, 
            name="for suspicious activity"
        ))


class ModerationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
        self.db = self.mongo_client.Protonn.ServerProperties

        # Start background tasks
        self.update_server_properties.start()
        self.update_server_premiums.start()
        self.automated_sends.start()
        self.cleanup_old_data.start()

    async def generate_unique_code(self):
        """Generate a unique 5-character alphanumeric code"""
        characters = string.ascii_letters + string.digits
        while True:
            code = ''.join(random.choices(characters, k=5))
            # Check if code already exists in database
            existing = await self.mongo_client.Protonn.ClaimServer.find_one({"claim_code": code})
            if not existing:
                return code
    
    @commands.Cog.listener()
    async def on_ready(self):
        """Initialize servers when bot is ready"""
        await self.initialize_server()

    def cog_unload(self):
        """Cleanup when cog is unloaded"""
        self.update_server_properties.cancel()
        self.update_server_premiums.cancel()
        self.automated_sends.cancel()
        self.cleanup_old_data.cancel()

    async def clean_data(self):
        """Clean up old data"""

        pass

    async def initialize_server(self):
        """Initialize server properties"""
        guilds = self.bot.guilds  # Now properly named as plural

        for guild in guilds:  # Clearer iteration variable name
            try:
                # list of dictionaries of all channels with keys as channel id and values as channel name of all text channels
                channels = [{'id': channel.id, 'name': channel.name} for channel in guild.text_channels]
                roles = [{'id': role.id, 'name': role.name} for role in guild.roles]

                # check for existing server properties
                server_properties = await self.db.find_one({"server_id": guild.id})
                if server_properties:
                    continue
   
                await self.db.insert_one(serverInitTemplate(guild, channels, roles))

            except Exception as e:
                logger.error(f"Error in initialize server handler: {str(e)})")

    # ===== Background Tasks =====
    @tasks.loop(hours=1)
    async def update_server_premiums(self):
        """Update server premium status"""
        try:
            servers = Server.query.filter_by(isPremium=True).all()
            for server in servers:
                # Check if server has a subscription
                subscription = Subscriptions.query.filter_by(server_id=server.id).first()
                if subscription and subscription.expiry_date < datetime.utcnow():
                    # Update server premium status
                    server.isPremium = False
                    dbSql.session.commit()
        except Exception as e:
            dbSql.session.rollback()
            logger.error(f"Error in update server premiums task: {str(e)}")
        finally:
            dbSql.session.close()

    @tasks.loop(seconds=15)
    async def automated_sends(self):
        """Perform miscellaneous tasks"""
        for guild in self.bot.guilds:
            try:
                server_properties = await self.db.find_one({"server_id": guild.id})
                if not server_properties:
                    continue

                server_properties = server_properties['configs']
                
                reaction_roles = server_properties.get("reaction_roles", None)
                embedded_message = server_properties.get("embedded_message", None)

                if reaction_roles and reaction_roles['active'] and not reaction_roles['sent']:
                    channel = guild.get_channel(int(reaction_roles['channel']))
                    if channel:
                        roles = [guild.get_role(role_id) for role_id in reaction_roles['content']['roles']]
                        roles = [r for r in roles if r is not None]
                        
                        if roles:
                            if reaction_roles['content']['type'] == 'select':
                                view = ReactionRolesSelectView(guild.id)
                                view.add_item(ReactionRolesSelect(roles, "React to update your roles", guild.id))
                                self.bot.add_view(view)  # Register the view before sending
                            else:
                                view = ReactionRolesButtonView(guild.id)
                                for role in roles:
                                    view.add_item(ReactionRolesButton(role, guild.id))
                                
                            embed = discord.Embed(
                                title=reaction_roles['content']['title'],
                                description=reaction_roles['content']['description'],
                                color=discord.Color.dark_gold()
                            )
                            if reaction_roles['content']['thumbnail']:
                                embed.set_thumbnail(url=reaction_roles['content']['thumbnail'].format(server=guild.icon))
                            else:
                                embed.set_thumbnail(url=guild.icon)
                            embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
                            
                            # Send message first
                            message = await channel.send(embed=embed, view=view)
                            
                            
                            # Update database after successful send
                            await self.db.update_one(
                                {"server_id": guild.id},
                                {"$set": {
                                    "configs.reaction_roles.sent": True,
                                }}
                            )
                
                if embedded_message and embedded_message['active'] and not embedded_message['sent']:
                    channel = guild.get_channel(int(embedded_message['channel']))
                    if channel:
                        embed = discord.Embed(
                            title=embedded_message['message']['title'].format(server=guild.name),
                            description=embedded_message['message']['content'].format(server=guild.name, everyone=guild.default_role.mention),
                            color=discord.Color.dark_gold()
                        )
                        print(embedded_message['message']['thumbnail'])
                        if embedded_message['message']['thumbnail']:
                            embed.set_thumbnail(url=embedded_message['message']['thumbnail'].format(server=guild.icon))
                        
                        embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
                        
                        # Send message first
                        message = await channel.send(embed=embed)
                        
                        # Update database after successful send
                        await self.db.update_one(
                            {"server_id": guild.id},
                            {"$set": {
                                "configs.embedded_message.sent": True,
                            }}
                        )
                            
            except Exception as e:
                logger.error(f"Error in automated sends task: {str(e)}")
              
    @tasks.loop(seconds=30)
    async def update_server_properties(self):
        """Update server properties in the database"""
        for guild in self.bot.guilds:
            try:
                # Update server channels and roles
                channels = [{'id': channel.id, 'name': channel.name} for channel in guild.text_channels]
                # Get every non bot role in the server
                roles = [{'id': role.id, 'name': role.name} for role in guild.roles if not role.is_bot_managed()]

                # Update server properties
                await self.db.update_one(
                    {"server_id": guild.id},
                    {"$set": {"channels": channels, "roles": roles}}
                )

            except Exception as e:
                logger.error(f"Error in update server properties task: {str(e)}")

    @tasks.loop(minutes=30)
    async def cleanup_old_data(self):
        """Clean up old warnings and join logs"""
        try:
            collections = [self.mongo_client.Protonn.DownloadActivities, self.mongo_client.Protonn.MusicActivites]
            current_month_year = datetime.now().strftime("%Y-%m")

            # Define the filter to find entries not matching the current month and year
            filter_criteria = {"month": {"$ne": current_month_year}}

            # Perform the deletion
            for collection in collections:
                result = await collection.delete_many(filter_criteria)

                logger.info(f"Cleaned up {result.deleted_count} old records from {collection.name}")

            
        except Exception as e:
            logger.error(f"Error in cleanup task: {str(e)}")

    # ===== Event Listeners =====
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Handle voice channel cleanup when empty or when owner leaves"""
        try:
            # Check if user left a channel
            if before.channel and (not after.channel or before.channel != after.channel):
                # Check if bot has required permissions first
                if not before.channel.guild.me.guild_permissions.manage_channels:
                    logger.warning(f"Bot lacks manage_channels permission in guild {before.channel.guild.id}")
                    return

                # Check if channel is a private room
                channel_data = await self.mongo_client.Protonn.PrivateVoiceChannels.find_one({
                    "channel_id": str(before.channel.id)
                })
                
                if channel_data:
                    # Check if the leaving member is the owner
                    if str(member.id) == channel_data["owner_id"]:
                        try:
                            # Try to notify the owner
                            embed = discord.Embed(
                                title="Private VC Disbanded",
                                description="Your private room has been disband after you left. Thanks for using Pro-tonn!\n\n-# This message will be deleted in 5 minutes",
                                color=discord.Color.dark_gold()
                            )
                            embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
                            await member.send(embed=embed, delete_after=300)
                 
                            await self.mongo_client.Protonn.PrivateVoiceChannels.delete_one({
                                "channel_id": str(before.channel.id)
                            })
                            await before.channel.delete()
                                
                        except discord.Forbidden:
                            logger.warning(f"Missing permissions to delete channel {before.channel.id}")
                        except Exception as e:
                            logger.error(f"Error in deleting channel: {str(e)}")
                            
                    # If it's not the owner leaving, check if channel is empty
                    elif len(before.channel.members) == 0:
                        try:
                            # Delete empty private channel
                            await before.channel.delete()
                            await self.mongo_client.Protonn.PrivateVoiceChannels.delete_one({
                                "channel_id": str(before.channel.id)
                            })
                        except discord.Forbidden:
                            logger.warning(f"Missing permissions to delete channel {before.channel.id}")
                        except Exception as e:
                            logger.error(f"Error deleting channel: {str(e)}")

        except Exception as e:
            logger.error(f"Error in voice state update handler: {str(e)}")
            
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Handle member joins"""
        try:
            server_id = member.guild.id
          
            # Check if welcome system is active
            welcome_system = await self.db.find_one({"server_id": server_id})
            welcome_system = welcome_system['configs']['welcome_system'] if welcome_system else None
          
            if welcome_system["active"]:
                embed = discord.Embed(
                    title=welcome_system["message"]["title"].format(user=member.name, user_mention=member.mention, server=member.guild.name),
                    description=welcome_system["message"]["content"].format(user=member.name, user_mention=member.mention, server=member.guild.name, everyone=member.guild.default_role.mention),
                    color=discord.Color.dark_gold()
                )
                embed.set_thumbnail(url=welcome_system["message"]["thumbnail"].format(user=member.display_avatar.url, server=member.guild.icon))
                embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)

                # Send welcome message
                channel = member.guild.get_channel(welcome_system["channel"]) if welcome_system["channel"] else member.guild.system_channel
                if channel:
                    await channel.send(embed=embed)
                    

            auto_roles = await self.db.find_one({"server_id": server_id})
            auto_roles = auto_roles['configs']['auto_roles']
            if auto_roles["active"]:
                for role_id in auto_roles["roles"]:
                    role = member.guild.get_role(role_id)
                    if role:
                        await member.add_roles(role)


        except Exception as e:
            logger.error(f"Error in member join handler: {str(e)}")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Handle member removals"""
        try:
            server_id = member.guild.id
            exit_system = await self.db.find_one({"server_id": server_id})
            exit_system = exit_system['configs']['exit_system'] if exit_system else None
            if exit_system["active"]:
                embed = discord.Embed(
                    title=exit_system["message"]["title"].format(user=member.name, user_mention=member.mention, server=member.guild.name),
                    description=exit_system["message"]["content"].format(user=member.name, user_mention=member.mention, server=member.guild.name),
                    color=discord.Color.dark_gold()
                )
                embed.set_thumbnail(url=exit_system["message"]["thumbnail"].format(user=member.display_avatar.url, server=member.guild.icon))
                embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)

                # Send exit message
                channel = member.guild.get_channel(exit_system["channel"]) if exit_system["channel"] else member.guild.system_channel
                if channel:
                    await channel.send(embed=embed)

        except Exception as e:
            logger.error(f"Error in member remove handler: {str(e)}")

    #when member is kicked
    @commands.Cog.listener()
    async def on_member_kick(self, guild: discord.Guild, user: discord.User):
        """Handle member kicks"""
        try:
            server_id = member.guild.id
            exit_system = await self.db.find_one({"server_id": server_id})
            exit_system = exit_system['configs']['exit_system'] if exit_system else None
            if exit_system["active"]:
                embed = discord.Embed(
                    title=exit_system["message"]["title"].format(user=member.name, user_mention=member.mention, server=member.guild.name),
                    description=exit_system["message"]["content"].format(user=member.name, user_mention=member.mention, server=member.guild.name),
                    color=discord.Color.dark_gold()
                )
                embed.set_thumbnail(url=exit_system["message"]["thumbnail"].format(user=member.display_avatar.url, server=member.guild.icon))
                embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)

                # Send exit message
                channel = member.guild.get_channel(exit_system["channel"]) if exit_system["channel"] else member.guild.system_channel
                if channel:
                    await channel.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error in member kick handler: {str(e)}")

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        """Handle member bans"""
        try:
            server_id = member.guild.id
            ban_system = await self.db.find_one({"server_id": server_id})
            ban_system = ban_system['configs']['ban_system'] if ban_system else None
            if ban_system["active"]:
                embed = discord.Embed(
                    title=ban_system["message"]["title"].format(user=member.name, user_mention=member.mention, server=member.guild.name),
                    description=ban_system["message"]["content"].format(user=member.name, user_mention=member.mention, server=member.guild.name),
                    color=discord.Color.dark_gold()
                )
                embed.add_field(name="Reason", value=ban_system["message"]["reason"], inline=False)
                embed.set_thumbnail(url=ban_system["message"]["thumbnail"].format(user=member.display_avatar.url, server=member.guild.icon))
                embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)

                # Send ban message
                channel = member.guild.get_channel(ban_system["channel"]) if ban_system["channel"] else member.guild.system_channel
                if channel:
                    await channel.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error in member ban handler: {str(e)}")

    # When bot joins server
    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        """Handle bot joining a server"""
        try:

            # list of dictionaries of all channels with keys as channel id and values as channel name
            channels = [{'id': channel.id, 'name': channel.name} for channel in guild.channels]

            roles = [{'id': role.id, 'name': role.name} for role in guild.roles]

            # check for existing server properties
            server_properties = await self.db.find_one({"server_id": guild.id})
            if server_properties:
                return

            # Create raid protection entry
            await self.db.insert_one(serverInitTemplate(guild, channels, roles))
        except Exception as e:
            logger.error(f"Error in guild join handler: {str(e)}")

    @commands.Cog.listener()
    async def on_automod_action(self, action: discord.AutoModAction):
        """Respond to AutoMod actions triggered by Discord's AutoMod."""
        try:
            # Extract action details
            guild = action.guild
            rule = action.rule
            target_user = action.user
            channel = action.channel
            action_type = action.action
            content = action.content
            



            embed = discord.Embed(
                title="AutoMod Action Triggered",
                description=f"A message by {target_user.mention} violated an AutoMod rule.",
                color=discord.Color.orange()
            )
            embed.add_field(name="Rule", value=rule.name, inline=True)
            embed.add_field(name="Action", value=str(action_type), inline=True)
            embed.add_field(name="Channel", value=channel.mention if channel else "DM/Unknown", inline=False)
            embed.add_field(name="Content", value=content or "No content available", inline=False)
            embed.set_footer(text=f"User ID: {target_user.id} | Rule ID: {rule.id}")



            if log_channel:
                await log_channel.send(embed=embed)

            # Optional: Additional actions like notifying the user
            if target_user:
                try:
                    await target_user.send(
                        f"Your message violated the server's AutoMod rules and was flagged. If you believe this is a mistake, contact the moderators."
                    )
                except discord.Forbidden:
                    pass  # Cannot send DM to the user

        except Exception as e:
            logger.error(f"Error handling AutoMod action: {e}")

    # ===== Task Startup Conditions =====
    @update_server_properties.before_loop
    @update_server_premiums.before_loop
    @automated_sends.before_loop
    @cleanup_old_data.before_loop
    async def before_tasks(self):
        """Wait for bot to be ready before starting tasks"""
        await self.bot.wait_until_ready()
        await self.clean_data()

    # ===== Slash Commands =====
    @app_commands.command(name="help", description="Get help on how to use the bot")
    async def help(self, interaction: discord.Interaction):
        """Get help on how to use the bot"""
        
        embed = discord.Embed(
            title="Welcome to Pro-tonn",
            description="Pro-tonn is a moderation bot designed for server administrators to maintain order and engage members effectively. You can check our website for a detailed list of [commands](https://www.pro-tonn.dev/commands?tab=protonn)",
            color=discord.Color.dark_gold()
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar)
        embed.add_field(
            name="Announce (Admin command)",
            value="`/announce #channel message`\nCreate a quick server announcement",
            inline=True
        )
        embed.add_field(
            name="Claim (Admin command)",
            value="`/claim`\nClaim your server and get a unique code",
            inline=True
        )
        embed.add_field(
            name="Reset (Admin command)",
            value="`/reset`\nReset your server's unique code",
            inline=True
        )
        embed.add_field(
            name="Warn",
            value="`/warn @user reason`\nWarn a user",
            inline=True
        )
        embed.add_field(
            name="Purge (Admin command)",
            value="`/purge 5`\nDelete a specified amount of messages in the current channel",
            inline=True
        )
        embed.add_field(
            name="Create Private VC",
            value="`/create_room 5`\nCreate a private voice channel. This is turned off on the server by default",
            inline=True
        )
        embed.add_field(
            name='Join Private VC',
            value="`/join_room #channel`\nJoin a private voice channel",
            inline=True
        )
        embed.add_field(
            name="Add a user to your private VC",
            value="`/add_user @user`\nAdd a user to your private voice channel",
            inline=True
        )
        embed.add_field(
            name="Remove a user from your private VC",
            value="`/remove_user @user`\nRemove a user from your private voice channel",
            inline=True
        )
        embed.add_field(
            name="",
            value=f"**Server Configurations**\nYou can edit your server configurations on our [Dashboard](https://www.pro-tonn.dev/configuration/servers/{interaction.guild.id})",
            inline=False
        )

        embed.add_field(
            name="",
            value="**Need help?**\nJoin the [Support Server](https://discord.gg/r2Zb8M25c4) for assistance.",
            inline=False
        )
        embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
        await interaction.response.send_message(embed=embed)
      
    @app_commands.command(name="warn", description="Warn a user")
    @app_commands.default_permissions(manage_messages=True)
    async def warn(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str
    ):
        """Warn a user and record it in the database"""
 
        reason = reason if reason else "No reason provided"
        
        warningEmbed = discord.Embed(
            title="⚠️ Warning Issued",
            description=f"You have received a warning in ***{interaction.guild.name}***. More warnings may result in a ban. See below for details.",
            color=discord.Color.dark_gold()
        )
        warningEmbed.set_thumbnail(url=interaction.guild.icon)
        warningEmbed.add_field(name="", value=f"Reason: {reason}")
        warningEmbed.set_footer(text=f"Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)

        embed = discord.Embed(
            title="",
            description=f"✅ Warning has been sent to {user.mention}",
            color=discord.Color.green()
        )
        await user.send(embed=warningEmbed)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="userinfo", description="Get information about a user")
    @app_commands.default_permissions(manage_messages=True)
    async def userinfo(
        self,
        interaction: discord.Interaction,
        user: discord.Member
    ):
        """Get detailed information about a user"""
        try:
            
            # Create embed
            embed = discord.Embed(
                title=f"User Information - {user.name}",
                color=discord.Color.blue()
            )
            
            embed.add_field(
                name="User ID",
                value=f"`{user.id}`",
                inline=True
            )
            
            embed.add_field(
                name="Account Created",
                value=f"<t:{int(user.created_at.timestamp())}:R>",
                inline=True
            )
            
            embed.add_field(
                name="Joined Server",
                value=f"<t:{int(user.joined_at.timestamp())}:R>",
                inline=True
            )
            
            
            roles = [role.mention for role in user.roles[1:]]  # Exclude @everyone
            embed.add_field(
                name=f"Roles ({len(roles)})",
                value=" ".join(roles) if roles else "None",
                inline=False
            )
             
            embed.set_thumbnail(url=user.display_avatar.url)
            embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error fetching user info: {str(e)}")
            await interaction.response.send_message("An error occurred while fetching user information.", ephemeral=True)

    @app_commands.command(name="purge", description="Deletes a specified amount of messages in current channel")
    @app_commands.default_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 100]):
        """Purge messages in the current channel"""
        try:
            embed = discord.Embed(
                title="Messages Purged",
                description=f"{limit} messages have been purged in this channel.",
                color=discord.Color.dark_gold()
            )
            embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            await interaction.channel.purge(limit=limit)
        except Exception as e:
            logger.error(f"Error purging messages: {str(e)}")
            await interaction.response.send_message("An error occurred while purging messages.", ephemeral=True)

    @app_commands.command(name="announce", description="Create a quick server announcement")
    @app_commands.default_permissions(manage_messages=True)
    async def announce(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: str,
        mention: Optional[discord.Role]=None
    ):
        """Create a server announcement with proper permission checking"""
        try:
            # Check if bot has required permissions in the target channel
            bot_permissions = channel.permissions_for(interaction.guild.me)
            missing_permissions = []
            
            if not bot_permissions.send_messages:
                missing_permissions.append("Send Messages")
            if not bot_permissions.view_channel:
                missing_permissions.append("View Channel")
            if not bot_permissions.embed_links:
                missing_permissions.append("Embed Links")
            
            # If missing any required permissions, send error message
            if missing_permissions:
                missing_perms = "\n-> ".join(missing_permissions)
                embed = discord.Embed(
                    title="Missing Channel Permissions",
                    description=f"I need the following permissions in {channel.mention} to send announcements:\n-> {missing_perms}",
                    color=discord.Color.red()
                )
                embed.add_field(name="", value="Alternatively, you can give me the `Administrator` permission to help prevent this issue for other channels. Please check my role permissions and try again.", inline=False)
                embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            # If mention role is specified, check if bot can mention it
            if mention and not bot_permissions.mention_everyone and mention.is_default():
                embed = discord.Embed(
                    title="Missing Permissions",
                    description=f"I don't have permission to mention @everyone/@here in {channel.mention}",
                    color=discord.Color.red()
                )
                embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            # Create and send the announcement
            embed = discord.Embed(
                title="📢 Announcement",
                description=message,
                timestamp=datetime.now(),
                color=discord.Color.dark_gold()
            )
            embed.set_thumbnail(url=interaction.guild.icon)
            embed.add_field(name="", value=f"Sent by {interaction.user.mention}", inline=False)
            embed.set_footer(text=f"Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)

            # Send the announcement
            if mention:
                await channel.send(content=mention.mention, embed=embed)
            else:
                await channel.send(embed=embed)
            
            # Send success message
            success_embed = discord.Embed(
                title="",
                description=f"✅ Announcement sent in {channel.mention}",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=success_embed, ephemeral=True)

        except discord.Forbidden as e:
            embed = discord.Embed(
                title="Permission Error",
                description=f"I don't have permission to send messages in {channel.mention}. Please check my role permissions.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error in announce command: {str(e)}")
            embed = discord.Embed(
                title="Error",
                description="An unexpected error occurred while sending the announcement.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
    @app_commands.command(name="get_avatar", description="Get the avatar of a user")
    async def get_avatar(self, interaction: discord.Interaction, user: discord.User):
        """Get the avatar of a user"""
        try:
            embed = discord.Embed(
                title=f"{user.name}'s Avatar",
                color=discord.Color.dark_gold()
            )
            embed.set_image(url=user.display_avatar.url)
            embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
            await interaction.response.send_message(embed=embed)
        except Exception as e:
            logger.error(f"Error fetching user avatar: {str(e)}")
            await interaction.response.send_message("An error occurred while fetching the user's avatar.", ephemeral=True)
    
    @app_commands.command(name="claim", description="Claim your server and get a unique code (Admin only)")
    @rate_limit(times=5, seconds=60)
    @app_commands.checks.has_permissions(administrator=True)
    async def claim(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            server_id = str(interaction.guild.id)
            existing_server = await self.mongo_client.Protonn.ClaimServer.find_one({"server_id": server_id})
            if existing_server:
                embed = discord.Embed(
                    title="Server Already Registered",
                    description=f"This server is already registered with code: ||`{existing_server['claim_code']}`||",
                    color=discord.Color.yellow()
                )
                embed.set_footer(text="Developed by Pro-tonn", icon_url=f"{self.bot.user.display_avatar}")
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            
            claim_code = await self.generate_unique_code()
                       
            # Prepare server data with botname
            server_data = {
                "server_name": interaction.guild.name,
                "server_id": server_id,
                "isPremium": False,
                "claim_code": claim_code,
                "claimed_by": {
                    "id": str(interaction.user.id),
                    "name": interaction.user.name,
                    "timestamp": datetime.utcnow()  
                }
            }

            # Insert server data
            await self.mongo_client.Protonn.ClaimServer.insert_one(server_data)

            embed = discord.Embed(
                title="Server Claimed Successfully",
                description=f"Your server's unique code is: ||`{claim_code}`||\nKeep this code safe!",
                color=discord.Color.green()
            )
            embed.add_field(
                name="Server Details",
                value=f"Name: {interaction.guild.name}\nID: {server_id}",
                inline=False
            )
            embed.set_footer(text="Developed by Pro-tonn", icon_url=f"{self.bot.user.display_avatar}")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error claiming server: {str(e)}")
            await interaction.followup.send("An error occurred while claiming the server.", ephemeral=True)

    @app_commands.command(name="reset", description="Reset your server's unique code (Admin only)")
    @rate_limit(times=5, seconds=60)
    @app_commands.checks.has_permissions(administrator=True)
    async def reset(self, interaction:discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            server_id = str(interaction.guild.id)
            existing_server = await self.mongo_client.Protonn.ClaimServer.find_one({"server_id": server_id})
            if not existing_server:
                embed = discord.Embed(
                    title="Server Not Registered",
                    description="This server is not registered. Use `/claim` first.",
                    color=discord.Color.red()
                )
                embed.set_footer(text="Developed by Pro-tonn", icon_url=f"{self.bot.user.display_avatar}")
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
       
            new_code = await self.generate_unique_code()

            # Update server data with botname preserved
            update_data = {
                "$set": {
                    "claim_code": new_code,
                    "reset_history": {
                        "previous_code": existing_server.get('claim_code'),
                        "reset_by": {
                            "id": str(interaction.user.id),
                            "name": interaction.user.name,
                            "timestamp": datetime.utcnow()
                        }
                    }
                }
            }

            await self.mongo_client.Protonn.ClaimServer.update_one({"server_id": server_id}, update_data)

            embed = discord.Embed(
                title="Server Reset Successfully",
                description=f"Your server's unique code has been reset to: ||`{new_code}`||\n Keep this code safe!",
                color=discord.Color.green()
            )
            embed.add_field(
                name="Previous Code",
                value=f"`{existing_server['claim_code']}`",
                inline=False
            )
            embed.add_field(
                name="Server Details",
                value=f"Name: {interaction.guild.name}\nID: {server_id}",
                inline=False
            )
            embed.set_footer(text="Developed by Pro-tonn", icon_url=f"{self.bot.user.display_avatar}")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error resetting server: {str(e)}")
            await interaction.followup.send("An error occurred while resetting the server.", ephemeral=True)

    @app_commands.command(name="create_room", description="Create a private voice channel")
    @rate_limit(times=1, seconds=(60*15), premium_multiplier=5) # 1 time every 15 minutes
    async def create_room(
        self,
        interaction: discord.Interaction,
        size: app_commands.Range[int, 2, 15]
    ):
        """Create a private voice channel with specified size limit"""

        can_create = await self.db.find_one({"server_id": interaction.guild.id})
        can_create = can_create['configs']['private_vc'] if can_create else None
        if not can_create['active']:
            embed = discord.Embed(
                title="Private Rooms Disabled",
                description="Private rooms have been disabled in this server. Please contact an admin for more information. If you believe this is an error, please contact support.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Check bot permissions first
        if not interaction.guild.me.guild_permissions.manage_channels:
            embed = discord.Embed(
                title="❌ Missing Permissions",
                description="I need the `Manage Channels` permission to create private rooms.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Find or create the private rooms category
        category = discord.utils.get(interaction.guild.categories, name="Private VCs")
        if not category:
            # Set up category with proper permissions
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(connect=False),
                interaction.guild.me: discord.PermissionOverwrite(manage_channels=True, connect=True),  # Add bot permissions
                interaction.user: discord.PermissionOverwrite(connect=True, manage_channels=True)
            }
            category = await interaction.guild.create_category("Private VCs", overwrites=overwrites)
        else:
            # Update existing category permissions if needed
            await category.set_permissions(interaction.guild.me, manage_channels=True, connect=True)

        # Create private voice channel with inherited permissions
        channel_name = f"🔒 {interaction.user.name}'s Room"
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(connect=False),
            interaction.guild.me: discord.PermissionOverwrite(manage_channels=True, connect=True),  # Add bot permissions
            interaction.user: discord.PermissionOverwrite(connect=True, manage_channels=True)
        }

        # Check if user already has a private room
        existing_channel = await self.mongo_client.Protonn.PrivateVoiceChannels.find_one({
            "owner_id": str(interaction.user.id),
            "guild_id": str(interaction.guild.id)
        })
        if existing_channel:
            embed = discord.Embed(
                title="Private VC Already Exists",
                description=f"You already have a private room: <#{existing_channel['channel_id']}>. You can only have one private room at a time. The existing room will be deleted when empty.",
                color=discord.Color.red()
            )

            embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        vc = await interaction.guild.create_voice_channel(
            name=channel_name,
            category=category,
            user_limit=size,
            overwrites=overwrites
        )

        # Store channel info in database
        await self.mongo_client.Protonn.PrivateVoiceChannels.insert_one({
            "channel_id": str(vc.id),
            "owner_id": str(interaction.user.id),
            "guild_id": str(interaction.guild.id),
            "created_at": datetime.utcnow()
        })

        embed = discord.Embed(
            title="Private VC Created",
            description=f"Your private voice channel has been created: {vc.mention}\nUser limit: {size}",
            color=discord.Color.dark_gold()
        )
        embed.set_thumbnail(url=interaction.guild.icon)
        embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="join_room", description="Request to join a private voice channel")
    @rate_limit(times=1, seconds=(60*15), premium_multiplier=5) # 1 time every 15 minutes
    async def join_room(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel
    ):
        """Send a join request to the owner of a private voice channel"""
        try:
            # Verify channel is a private room
            channel_data = await self.mongo_client.Protonn.PrivateVoiceChannels.find_one({
                "channel_id": str(channel.id)
            })
            
            if not channel_data:
                embed = discord.Embed(
                    title="Not a Private VC",
                    description="The selected channel is not a private room.",
                    color=discord.Color.red()
                )
                embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            # Get owner
            owner = interaction.guild.get_member(int(channel_data["owner_id"]))
            if not owner:
                embed = discord.Embed(
                    title="Owner Not Found",
                    description="The owner of this room is not available.",
                    color=discord.Color.red()
                )
                embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            # Create request embed
            request_embed = discord.Embed(
                title="Room Join Request",
                description=f"{interaction.user.mention} wants to join your private room {channel.mention}\n\n-# This message will be deleted in 5 minutes",
                color=discord.Color.dark_gold()
            )
            
            # Add accept/deny buttons
            class JoinRequestView(discord.ui.View):
                def __init__(self, bot, requester, channel):
                    super().__init__(timeout=300)  # 5 minute timeout
                    self.bot = bot
                    self.requester = requester
                    self.channel = channel

                @discord.ui.button(label="Accept", style=discord.ButtonStyle.green)
                async def accept(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                    if button_interaction.user.id != int(channel_data["owner_id"]):
                        await button_interaction.response.send_message(
                            "Only the room owner can handle this request.",
                            ephemeral=True
                        )
                        return

                    # Update channel permissions
                    await channel.set_permissions(self.requester, connect=True)
                    
                    # Notify requester
                    try:
                        accept_embed = discord.Embed(
                            title="Join Request Accepted",
                            description=f"You can now join {channel.mention}",
                            color=discord.Color.green()
                        )
                        await self.requester.send(embed=accept_embed)
                    except:
                        pass

                    # Disable buttons and update message
                    for child in self.children:
                        child.disabled = True
                    await button_interaction.message.edit(view=self)
                    
                    await button_interaction.response.send_message(
                        f"Granted access to {self.requester.mention}",
                        ephemeral=True
                    )

                @discord.ui.button(label="Deny", style=discord.ButtonStyle.red)
                async def deny(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                    if button_interaction.user.id != int(channel_data["owner_id"]):
                        await button_interaction.response.send_message(
                            "Only the room owner can handle this request.",
                            ephemeral=True
                        )
                        return

                    # Notify requester
                    try:
                        deny_embed = discord.Embed(
                            title="Join Request Denied",
                            description=f"Your request to join {channel.mention} was denied\n\n-# This message will be deleted in 5 minutes",
                            color=discord.Color.red()
                        )
                        await self.requester.send(embed=deny_embed, delete_after=300)
                    except:
                        pass

                    # Disable buttons and update message
                    for child in self.children:
                        child.disabled = True
                    await button_interaction.message.edit(view=self)
                    
                    await button_interaction.response.send_message(
                        f"Denied access to {self.requester.mention}",
                        ephemeral=True
                    )

            # Send request to owner
            view = JoinRequestView(self.bot, interaction.user, channel)
            await owner.send(embed=request_embed, view=view, delete_after=310)

            # Confirm to requester
            embed = discord.Embed(
                title="",
                description=f"✅ Join request sent to {owner.mention}. They will respond shortly.",
                color=discord.Color.green()
            )
            await interaction.response.send_message(
                embed=embed,
                ephemeral=True
            )

        except Exception as e:
            logger.error(f"Error handling join request: {str(e)}")
            await interaction.response.send_message(
                "An error occurred while processing your join request.",
                ephemeral=True
            )

    @app_commands.command(name="add_user", description="Give a user access to your private voice channel")
    async def add_user(
        self,
        interaction: discord.Interaction,
        user: discord.Member
    ):
        """Grant a specific user access to your private voice channel"""
        try:
            # Check if command user owns any private room
            channel_data = await self.mongo_client.Protonn.PrivateVoiceChannels.find_one({
                "owner_id": str(interaction.user.id),
                "guild_id": str(interaction.guild.id)
            })
            
            if not channel_data:
                embed = discord.Embed(
                    title="No Private VC",
                    description="You don't have a private room. Only private room owners can add users.",
                    color=discord.Color.red()
                )
                embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            # Get the channel
            channel = interaction.guild.get_channel(int(channel_data["channel_id"]))
            if not channel:
                embed = discord.Embed(
                    title="Channel Not Found",
                    description="Your private room could not be found. It may have been deleted.",
                    color=discord.Color.red()
                )
                embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            # Check if target user is already the owner
            if user.id == interaction.user.id:
                embed = discord.Embed(
                    title="Invalid User",
                    description="You already have access to your own private room.",
                    color=discord.Color.red()
                )
                embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            # Check if the channel has a user limit and if it's reached
            if channel.user_limit > 0:
                current_members = len(channel.members)  # This gets the actual number of users in the VC
                
                if current_members >= channel.user_limit:
                    embed = discord.Embed(
                        title="Room Full",
                        description=f"Your room has reached its user limit of {channel.user_limit} active users. Remove some users before adding new ones.",
                        color=discord.Color.red()
                    )
                    embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                    return

            # Grant access to the user
            await channel.set_permissions(user, connect=True)
            
            # Send confirmation messages
            embed = discord.Embed(
                title="Access Granted",
                description=f"{user.mention} has been given access to {channel.mention}",
                color=discord.Color.green()
            )
            embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
            await interaction.response.send_message(embed=embed, ephemeral=True)

        except discord.Forbidden as e:
            embed = discord.Embed(
                title="Permission Error",
                description="I don't have permission to modify channel access.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error in add_user command: {str(e)}")
            embed = discord.Embed(
                title="Error",
                description="An unexpected error occurred while adding the user.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="remove_user", description="Remove a user from your private voice channel")
    @rate_limit(times=1, seconds=30, premium_multiplier=5) # 1 time every 30 seconds
    async def remove_user(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: Optional[str] = None
    ):
        """Remove a user from your private voice channel"""
        try:
            # Check if command user owns any private room
            channel_data = await self.mongo_client.Protonn.PrivateVoiceChannels.find_one({
                "owner_id": str(interaction.user.id),
                "guild_id": str(interaction.guild.id)
            })
            
            if not channel_data:
                embed = discord.Embed(
                    title="No Private VC",
                    description="You don't have a private room. Only private room owners can remove users.",
                    color=discord.Color.red()
                )
                embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            # Get the channel
            channel = interaction.guild.get_channel(int(channel_data["channel_id"]))
            if not channel:
                embed = discord.Embed(
                    title="Channel Not Found",
                    description="Your private room could not be found. It may have been deleted.",
                    color=discord.Color.red()
                )
                embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            # Check if target user is the owner
            if user.id == interaction.user.id:
                embed = discord.Embed(
                    title="Invalid User",
                    description="You cannot remove yourself from your own private room.",
                    color=discord.Color.red()
                )
                embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            # Remove user's access
            await channel.set_permissions(user, connect=False)
            
            # If user is in the channel, disconnect them
            if user.voice and user.voice.channel and user.voice.channel.id == channel.id:
                await user.move_to(None)

            # Send confirmation message
            reason_text = f"\nReason: {reason}" if reason else ""
            embed = discord.Embed(
                title="User Removed",
                description=f"{user.mention} has been removed from {channel.mention}\nReason: {reason_text}",
                color=discord.Color.green()
            )
            embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
            await interaction.response.send_message(embed=embed, ephemeral=True)

            # Notify removed user
            try:
                user_embed = discord.Embed(
                    title="Removed from Private VC",
                    description=f"You have been removed from {interaction.user.name}'s private room\nReason: {reason_text}\n\n-# This message will be deleted in 5 minutes",
                    color=discord.Color.red()
                )
                user_embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
                await user.send(embed=user_embed, delete_after=300)
            except:
                pass  # If DM fails, continue silently

        except discord.Forbidden as e:
            embed = discord.Embed(
                title="Permission Error",
                description="I don't have permission to modify channel access or move members.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error in remove_user command: {str(e)}")
            embed = discord.Embed(
                title="Error",
                description="An unexpected error occurred while removing the user.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="quote", description="Send a message as an embed")
    async def quote(
        self,
        interaction: discord.Interaction,
        message_id: str
    ):
        """Send a message as an embed"""
        try:
            quote =  await self.db.find_one({"server_id": interaction.guild.id})
            quote = quote['configs']['quote'] if quote else None

            if quote["active"] == False:
                embed = discord.Embed(
                    title="Quote System Disabled",
                    description="The quote system is disabled in this server. Please contact an admin for more information. If you believe this is an error, please contact support.",
                    color=discord.Color.red()
                )
                embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            # Check if user role is allowed to use the command
            if quote["can_quote"]:
                can_quote = [interaction.guild.get_role(role) for role in quote["can_quote"]]
                user_roles = interaction.user.roles
                if not any(role in can_quote for role in user_roles) or not interaction.user.guild_permissions.administrator:
                    embed = discord.Embed(
                        title="Permission Denied",
                        description="You don't have permission to use this command.",
                        color=discord.Color.red()
                    )
                    embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                    return

            # search for message in guild
            message_id = int(message_id)
            quote_channel = int(quote["channel"]) if quote["channel"] else None
            if not quote_channel:
                embed = discord.Embed(
                    title="Quote Channel Not Set",
                    description="The quote channel is not set in this server. Please contact an admin for more information. If you believe this is an error, please contact support.",
                    color=discord.Color.red()
                )
                embed.set_footer(text="Developed by Pro-tonn", icon_url=self.bot.user.display_avatar)
                await interaction.response.send_message(embed=embed)
                return

            channel = interaction.guild.get_channel(quote_channel)
            message = await interaction.channel.fetch_message(message_id)
            if not message:
                await interaction.response.send_message("Message not found. Maybe the message was deleted?\n-# Make sure you are using this command in the channel the message was sent", ephemeral=True)
                return

            embed = discord.Embed(
                title=f"Quoted Message",
                description=f'"{message.content}"\n\n -{message.author.mention}',
                color=discord.Color.dark_gold(),
                timestamp=datetime.now()
            )
            embed.set_thumbnail(url=message.author.display_avatar.url)
            embed.add_field(name="", value=f"[View Original]({message.jump_url})", inline=False)
            embed.set_footer(text=f"Quoted by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            channel.send(embed=embed)

            
        except Exception as e:
            logger.error(f"Error quoting message: {str(e)}")
            await interaction.response.send_message("An error occurred while quoting the message.", ephemeral=True)

async def setup(bot):
    """Setup function for the cog"""
    await bot.add_cog(ModerationCog(bot))

# Create bot instance and run
bot = ModBot()

# Run the bot
if __name__ == "__main__":
    try:
        asyncio.run(bot.start(TOKEN))
    except KeyboardInterrupt:
        logger.info("Bot shutdown initiated")
        asyncio.run(bot.close())
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        asyncio.run(bot.close())