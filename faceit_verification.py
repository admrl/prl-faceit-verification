import os
import discord
import requests
import asyncio
import aiohttp
import json
import secrets
from collections import defaultdict
from discord.ext import commands, tasks
from dotenv import load_dotenv
from datetime import datetime, timedelta, UTC
from discord import ui
import pickle
import pathlib

# Import Flask for the web server
from flask import Flask, request
from threading import Thread

# Load environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
FACEIT_API_KEY = os.getenv("FACEIT_API_KEY")
ORGANIZER_ID = os.getenv("ORGANIZER_ID")
ANNOUNCEMENT_CHANNEL_ID = int(os.getenv("ANNOUNCEMENT_CHANNEL_ID", 0))
ALLOWED_CHANNEL_ID = int(os.getenv("ALLOWED_CHANNEL_ID", 0)) # For the /stats command

# New environment variables for OAuth
FACEIT_CLIENT_ID = os.getenv("FACEIT_CLIENT_ID")
FACEIT_CLIENT_SECRET = os.getenv("FACEIT_CLIENT_SECRET")
# FACEIT_REDIRECT_URI should be the URL of your Render web service + the route, e.g., "https://your-service-name.onrender.com/oauth-callback"
FACEIT_REDIRECT_URI = os.getenv("FACEIT_REDIRECT_URI")

# Validate critical environment variables
if not all([TOKEN, FACEIT_API_KEY, ORGANIZER_ID, ANNOUNCEMENT_CHANNEL_ID, FACEIT_CLIENT_ID, FACEIT_CLIENT_SECRET, FACEIT_REDIRECT_URI]):
    print("❌ Missing required environment variables. Bot may not function properly.")
    # Uncomment the line below to exit if critical variables are missing
    # exit(1)

# Bot setup
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# Rate limiting configuration
user_cooldowns = defaultdict(float)
RATE_LIMIT_COOLDOWN = 5  # seconds between commands per user

# Verification codes storage
verification_states = {}

# Flask web server setup
app = Flask(__name__)

# Faceit level images
FACEIT_LEVELS = {
    1: "https://media.discordapp.net/attachments/845553368737906699/1410690729709142036/LVL1.png",
    2: "https://media.discordapp.net/attachments/845553368737906699/1410690729285648624/LVL_2.png",
    3: "https://media.discordapp.net/attachments/845553368737906699/1410690730376040529/LVL3.png",
    4: "https://media.discordapp.net/attachments/845553368737906696/1410690730808180736/LVL4.png",
    5: "https://media.discordapp.net/attachments/845553368737906699/1410690731181342933/LVL5.png",
    6: "https://media.discordapp.net/attachments/845553368737906699/1410690731483336876/LVL6.png",
    7: "https://media.discordapp.net/attachments/845553368737906699/1410690731756093591/LVL7.png",
    8: "https://media.discordapp.net/attachments/845553368737906699/1410690732087312436/LVL8.png",
    9: "https://media.discordapp.net/attachments/845553368737906699/1410690732452352152/LVL9.png",
    10: "https://media.discordapp.net/attachments/845553368737906699/1410690732808998972/LVL10.png"
}

# Region flags
REGION_FLAGS = {
    "SEA": "🇸🇬",
    "OCE": "🇦🇺",
    "EU": "🇪🇺",
    "NA": "🇺🇸",
    "SA": "🇧🇷"
}

# File to store announced tournaments (now a dictionary)
ANNOUNCED_TOURNAMENTS_FILE = "announced_tournaments.pkl"

def load_announced_tournaments():
    """Loads announced tournaments from a file."""
    if pathlib.Path(ANNOUNCED_TOURNAMENTS_FILE).exists():
        try:
            with open(ANNOUNCED_TOURNAMENTS_FILE, 'rb') as f:
                # The file might contain a set or a dictionary, so we handle both.
                data = pickle.load(f)
                return data if isinstance(data, dict) else {k: None for k in data}
        except Exception as e:
            print(f"Error loading announced tournaments: {e}")
            return {}
    return {}

def save_announced_tournaments():
    """Saves announced tournaments to a file."""
    with open(ANNOUNCED_TOURNAMENTS_FILE, 'wb') as f:
        pickle.dump(announced_tournaments, f)

# Store announced tournaments as a dictionary {championship_id: message_id}
announced_tournaments = load_announced_tournaments()
# Store recently discovered tournaments for force posting
recent_tournaments = []

class FaceitAPI:
    def __init__(self, api_key):
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "FACEIT-Championship-Bot/2.0 (Discord Bot; +http://discord.gg/RZYBSQdraT)"
        }
        self.base_url = "https://open.faceit.com/data/v4"
        self.session = None  # Session will be managed internally
        self.last_api_call = 0
        self.rate_limit_delay = 1.5

    async def _rate_limited_request(self, method, url, params=None, data=None):
        """Internal helper to manage aiohttp requests with rate limiting."""
        if self.session is None or self.session.closed:
            print("aiohttp session is closed or missing. Creating a new one.")
            self.session = aiohttp.ClientSession()

        current_time = asyncio.get_event_loop().time()
        time_since_last_call = current_time - self.last_api_call

        if time_since_last_call < self.rate_limit_delay:
            await asyncio.sleep(self.rate_limit_delay - time_since_last_call)

        self.last_api_call = asyncio.get_event_loop().time()

        try:
            async with self.session.request(method, url, params=params, json=data, headers=self.headers) as response:
                if response.status == 429:
                    print("❌ FACEIT API Rate Limit exceeded! Backing off...")
                    return None
                    
                response.raise_for_status() # Raise an exception for HTTP errors (4xx or 5xx)
                
                return await response.json()
        except aiohttp.ClientError as e:
            print(f"Network error during API call: {e}")
            return None
        except json.JSONDecodeError as e:
            print(f"JSON decode error: {e}")
            return None
        except Exception as e:
            print(f"An unexpected error occurred during API call: {e}")
            return None

    async def get_player_data(self, username=None, player_id=None):
        url = f"{self.base_url}/players"
        if player_id:
            return await self._rate_limited_request('GET', f"{url}/{player_id}")
        elif username:
            return await self._rate_limited_request('GET', url, params={"nickname": username})

    async def get_player_stats(self, player_id):
        url = f"{self.base_url}/players/{player_id}/stats/cs2"
        return await self._rate_limited_request('GET', url)

    async def get_match_history(self, player_id, limit=5):
        url = f"{self.base_url}/players/{player_id}/history"
        return await self._rate_limited_request('GET', url, params={"game": "cs2", "limit": limit})
    
    async def get_championships(self, organizer_id):
        url = f"{self.base_url}/organizers/{organizer_id}/championships"
        return await self._rate_limited_request('GET', url, params={"offset": 0, "limit": 50})
        
    async def get_championship_by_id(self, championship_id):
        url = f"{self.base_url}/championships/{championship_id}"
        return await self._rate_limited_request('GET', url)
        
    async def get_championship_standings(self, championship_id):
        url = f"{self.base_url}/championships/{championship_id}/results"
        return await self._rate_limited_request('GET', url)

# Initialize the API client
faceit_api = FaceitAPI(FACEIT_API_KEY)

def calculate_win_rate(wins, total_matches):
    if not total_matches or not wins or int(total_matches) == 0:
        return "0"
    return str(round((int(wins) / int(total_matches)) * 100, 1))

def format_championship_url(championship_id, championship_name):
    clean_name = championship_name.replace(' ', '%20')
    return f"https://www.faceit.com/en/championship/{championship_id}/{clean_name}"

def is_admin(user):
    return user.guild_permissions.administrator

def convert_timestamp_to_datetime(timestamp):
    if not timestamp or timestamp == 0:
        return None
    return datetime.fromtimestamp(timestamp / 1000, UTC)

async def create_championship_embed(championship):
    """
    Creates a Discord embed for a given championship.
    """
    embed = discord.Embed(
        title=championship.get('name', 'N/A'),
        url=format_championship_url(championship.get('championship_id', ''), championship.get('name', 'N/A')),
        color=discord.Color.from_rgb(255, 85, 0) # Orange color for FACEIT
    )

    prize_pool = championship.get('prize_pool', {}).get('prize', 'N/A')
    prizepool_currency = championship.get('prize_pool', {}).get('currency', '')
    
    start_time_utc = convert_timestamp_to_datetime(championship.get('starts_at', 0))
    start_time_string = start_time_utc.strftime("%B %d, %H:%M UTC") if start_time_utc else 'N/A'

    end_time_utc = convert_timestamp_to_datetime(championship.get('ends_at', 0))
    end_time_string = end_time_utc.strftime("%B %d, %H:%M UTC") if end_time_utc else 'N/A'

    organizer_name = championship.get('organizer', {}).get('name', 'N/A')
    organizer_url = f"https://www.faceit.com/en/organizers/{organizer_name}"
    
    status = championship.get('status', 'N/A')
    region = championship.get('region', 'N/A').upper()
    
    # Tournament details
    embed.add_field(name="Details", value=f"**Status:** {status.capitalize()}\n"
                                           f"**Region:** {REGION_FLAGS.get(region, '')} {region}\n"
                                           f"**Size:** {championship.get('total_players', 'N/A')}/{championship.get('max_players', 'N/A')}\n"
                                           f"**Entry Fee:** {championship.get('registration_fee', 'N/A')}\n"
                                           f"**Anticheat:** {championship.get('anticheat_required', False)}\n"
                                           f"**Hosted by:** [{organizer_name}]({organizer_url})",
                    inline=True)

    # Time details
    embed.add_field(name="Date & Time", value=f"**Starts:** {start_time_string}\n"
                                               f"**Ends:** {end_time_string}\n",
                    inline=True)
    
    # Prize pool and banner
    if prize_pool and prize_pool != 'N/A':
        embed.add_field(name="Prize Pool", value=f"{prize_pool} {prizepool_currency}", inline=False)
        
    banner_url = championship.get('cover_image', '')
    if banner_url:
        embed.set_image(url=banner_url)

    embed.set_footer(text="Join and compete for the prize pool!")
    
    return embed

def create_finished_embed(championship, standings=None):
    """Creates a final embed for a finished or cancelled tournament."""
    embed = discord.Embed(
        title=f"🏆 {championship.get('name', 'N/A')} - Final Results",
        url=format_championship_url(championship.get('championship_id', ''), championship.get('name', 'N/A')),
        color=discord.Color.dark_gray()
    )

    organizer_name = championship.get('organizer', {}).get('name', 'N/A')
    organizer_url = f"https://www.faceit.com/en/organizers/{organizer_name}"

    embed.add_field(name="Status", value=f"**Status:** {championship.get('status', 'N/A').capitalize()}\n"
                                        f"**Hosted by:** [{organizer_name}]({organizer_url})",
                    inline=True)

    if standings:
        leaderboard_text = ""
        for i, team in enumerate(standings, 1):
            team_name = team.get("leaderboard_name", "N/A")
            points = team.get("points", "N/A")
            leaderboard_text += f"**{i}.** {team_name} - {points} points\n"
        if leaderboard_text:
            embed.add_field(name="Final Standings", value=leaderboard_text, inline=False)
    else:
        embed.add_field(name="Final Standings", value="No standings available.", inline=False)
    
    embed.set_footer(text="Thank you for participating!")

    return embed

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    print(f"Organizer ID: {ORGANIZER_ID}")
    print(f"Announcement Channel ID: {ANNOUNCEMENT_CHANNEL_ID}")
    print(f"User-Agent: {faceit_api.headers.get('User-Agent', 'Not set')}")
    print(f"Loaded {len(announced_tournaments)} previously announced tournaments")

    if ORGANIZER_ID and ANNOUNCEMENT_CHANNEL_ID:
        check_championships.start()
        print("✅ Championship announcement system started")
    else:
        print("⚠️ Championship announcement system disabled - missing ORGANIZER_ID or ANNOUNCEMENT_CHANNEL_ID")
    
@tasks.loop(minutes=5)
async def check_championships():
    """
    Checks for new, ongoing, or finished championships and updates Discord.
    """
    await bot.wait_until_ready()
    
    try:
        championships_data = await faceit_api.get_championships(ORGANIZER_ID)
        if championships_data is None or 'items' not in championships_data:
            print("❌ Failed to fetch championships data.")
            return

        for championship in championships_data['items']:
            championship_id = championship.get('championship_id')
            status = championship.get('status', 'not_started')

            if championship_id in announced_tournaments:
                message_id = announced_tournaments[championship_id]
                announcement_channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)

                if announcement_channel:
                    try:
                        message = await announcement_channel.fetch_message(message_id)
                        
                        if status == "finished" or status == "cancelled":
                            standings = await faceit_api.get_championship_standings(championship_id)
                            final_embed = create_finished_embed(championship, standings=standings)
                            await message.edit(embed=final_embed)
                            print(f"Updated championship {championship_id} to a finished state.")
                            announced_tournaments.pop(championship_id) # Remove it from the tracked list
                            save_announced_tournaments()
                        
                    except discord.NotFound:
                        print(f"Message for championship {championship_id} not found. Will not re-announce.")
                        announced_tournaments.pop(championship_id)
                        save_announced_tournaments()
                    except discord.Forbidden:
                        print("❌ Missing permissions to edit messages in the announcement channel.")
                    except Exception as e:
                        print(f"An unexpected error occurred while updating message: {e}")
            else:
                if status == "upcoming" or status == "in_progress":
                    announcement_channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
                    if announcement_channel:
                        embed = await create_championship_embed(championship)
                        try:
                            message = await announcement_channel.send(embed=embed)
                            announced_tournaments[championship_id] = message.id
                            save_announced_tournaments()
                            print(f"✅ Announced new championship: {championship.get('name', 'N/A')}")
                        except discord.Forbidden:
                            print("❌ Missing permissions to send messages in the announcement channel.")
                        except Exception as e:
                            print(f"An unexpected error occurred while announcing: {e}")
                
    except Exception as e:
        print(f"An error occurred in check_championships: {e}")

@check_championships.before_loop
async def before_check_championships():
    await bot.wait_until_ready()

# --- SLASH COMMANDS ---

@bot.slash_command(name="stats", description="Get FACEIT stats for a player")
async def stats(ctx, username: discord.Option(str, "FACEIT username")):
    current_time = datetime.now().timestamp()
    if current_time - user_cooldowns[ctx.author.id] < RATE_LIMIT_COOLDOWN:
        await ctx.respond(f"⏰ Cooldown: Please wait {RATE_LIMIT_COOLDOWN - (current_time - user_cooldowns[ctx.author.id]):.1f}s.", ephemeral=True)
        return
        
    await ctx.defer()
    
    player_data = await faceit_api.get_player_data(username=username)

    if not player_data:
        return await ctx.followup.send("❌ Player not found. Check the username and try again.", ephemeral=True)

    player_id = player_data.get("player_id")
    player_stats = await faceit_api.get_player_stats(player_id)
    player_history = await faceit_api.get_match_history(player_id)
    
    # Check for empty player_stats. This happens for non-CS2 players.
    if player_stats is None or 'lifetime' not in player_stats:
        return await ctx.followup.send(f"❌ {username} does not have any CS2 stats or is not a CS2 player.", ephemeral=True)

    lifetime_stats = player_stats.get("lifetime", {})

    # Extract all stats
    wins = lifetime_stats.get("Wins", "0")
    matches = lifetime_stats.get("Matches", "0")
    kd_ratio = lifetime_stats.get("Average K/D Ratio", "0.0")
    headshot_percentage = lifetime_stats.get("Average Headshots %", "0%")
    win_rate = calculate_win_rate(wins, matches)
    
    # Extract recent results
    recent_results = player_history.get("items", [])
    recent_result_icons = []
    for match in recent_results:
        # Check if the player won the match
        player_team_id = match.get("teams", {}).get("faction1", {}).get("players", [])[0].get("player_id")
        winning_team_id = match.get("results", {}).get("winner_id")
        
        if winning_team_id and player_team_id:
            if winning_team_id == match.get("teams", {}).get("faction1", {}).get("faction_id"):
                 recent_result_icons.append("🟢")
            else:
                 recent_result_icons.append("🔴")

    # Build the embed
    embed = discord.Embed(
        title=f"FACEIT Stats for {username}",
        url=player_data.get("faceit_url").replace('{lang}', 'en'),
        color=discord.Color.from_rgb(255, 85, 0)
    )

    embed.set_thumbnail(url=FACEIT_LEVELS.get(player_data.get("games", {}).get("cs2", {}).get("skill_level", 0)))
    embed.set_author(name=f"Level {player_data.get('games', {}).get('cs2', {}).get('skill_level', 0)}",
                     icon_url=FACEIT_LEVELS.get(player_data.get("games", {}).get("cs2", {}).get("skill_level", 0)))
    
    embed.add_field(name="Lifetime Stats", value=f"**Wins:** {wins}\n"
                                                 f"**Matches:** {matches}\n"
                                                 f"**Win Rate:** {win_rate}%\n"
                                                 f"**K/D Ratio:** {kd_ratio}\n"
                                                 f"**HS%:** {headshot_percentage}",
                    inline=False)
    
    embed.add_field(name="Recent Matches", value=" ".join(recent_result_icons) or "No recent matches found.", inline=False)
    
    await ctx.followup.send(embed=embed)
    user_cooldowns[ctx.author.id] = current_time

# --- NEW OAUTH VERIFICATION COMMANDS AND WEB SERVER ---

@app.route("/oauth-callback")
def oauth_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    discord_id = None
    
    # 1. Verify the state to prevent CSRF
    for key, value in verification_states.items():
        if value["state"] == state:
            discord_id = key
            break
    
    if not discord_id or not code:
        return "Verification failed. Please try again.", 400
    
    # Clean up the state token
    verification_states.pop(discord_id, None)

    # 2. Exchange the authorization code for an access token
    token_url = "https://accounts.faceit.com/oauth/token"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": FACEIT_CLIENT_ID,
        "client_secret": FACEIT_CLIENT_SECRET,
        "redirect_uri": FACEIT_REDIRECT_URI
    }
    
    try:
        response = requests.post(token_url, data=data)
        response.raise_for_status()
        tokens = response.json()
        access_token = tokens["access_token"]

        # 3. Use the access token to get the user's FACEIT profile
        headers = {"Authorization": f"Bearer {access_token}"}
        profile_url = "https://open.faceit.com/data/v4/players/me"
        profile_response = requests.get(profile_url, headers=headers)
        profile_response.raise_for_status()
        player_data = profile_response.json()
        
        # 4. Perform Discord verification
        faceit_nickname = player_data.get("nickname")
        skill_level = player_data.get("games", {}).get("cs2", {}).get("skill_level", 0)

        # Send a message back to the Discord bot via a queue or by directly getting the user object
        asyncio.run_coroutine_threadsafe(
            update_discord_user(discord_id, faceit_nickname, skill_level),
            bot.loop
        )

        return f"✅ Verification successful! Your Discord nickname will be updated to {faceit_nickname} shortly."
    
    except requests.exceptions.RequestException as e:
        print(f"Error during OAuth token exchange or profile fetch: {e}")
        return "❌ An error occurred during verification. Please try again later.", 500
    except Exception as e:
        print(f"An unexpected error occurred during verification: {e}")
        return "❌ An unexpected error occurred. Please try again later.", 500

async def update_discord_user(discord_id, faceit_nickname, skill_level):
    try:
        user = await bot.fetch_user(discord_id)
        if user:
            # Check if user is in the guild and update
            for guild in bot.guilds:
                member = guild.get_member(user.id)
                if member:
                    try:
                        await member.edit(nick=faceit_nickname)
                        # Add logic to assign role based on skill_level here
                        print(f"Updated {member.name}'s nickname to {faceit_nickname}")
                    except discord.Forbidden:
                        print(f"❌ Cannot update nickname for {member.name}. Check bot permissions.")
                    break
            
            # Send a DM to the user to confirm success
            await user.send(f"✅ Your FACEIT account has been successfully linked! Your nickname has been updated to `{faceit_nickname}`.")

    except Exception as e:
        print(f"Error updating Discord user {discord_id}: {e}")

@bot.slash_command(name="login", description="Start the FACEIT login process to link your account.")
async def login(ctx):
    # A unique state token to prevent CSRF attacks
    state = secrets.token_urlsafe(16)
    
    # Store the state token with the user's Discord ID
    verification_states[ctx.author.id] = {"state": state, "timestamp": datetime.now()}
    
    # Construct the authorization URL
    auth_url = (
        "https://accounts.faceit.com/oauth/authorize"
        f"?response_type=code"
        f"&client_id={FACEIT_CLIENT_ID}"
        f"&scope=openid profile" # 'profile' scope is necessary to get user info
        f"&redirect_uri={FACEIT_REDIRECT_URI}"
        f"&state={state}"
    )

    await ctx.respond(f"Please click the link below to log in and link your FACEIT account: \n\n{auth_url}", ephemeral=True)


def run_web_server():
    # Use 0.0.0.0 to listen on all public IPs, and get the port from the environment
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# Main function to run both the bot and the web server
async def main():
    # Start the Flask web server in a separate thread
    web_server_thread = Thread(target=run_web_server)
    web_server_thread.start()

    # Start the Discord bot
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())