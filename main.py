import asyncio
import copy
import json
import os
import socket
import sys
import time
import traceback
import threading

from PyQt5.QtWidgets import QApplication, QMainWindow, QTextBrowser, QVBoxLayout, QWidget, QPushButton, QTabWidget, QTableWidget, QTableWidgetItem, QHBoxLayout, QHeaderView, QDialog, QLabel, QComboBox, QCheckBox, QSpinBox, QFormLayout, QGroupBox, QDialogButtonBox, QProgressDialog
from PyQt5.QtCore import QThread, pyqtSignal, Qt
from PyQt5.QtGui import QIcon, QBrush, QColor
from PyQt5.QtWidgets import QStyle

import requests
import urllib3
from colr import color as colr
from rich.console import Console as RichConsole

from src.colors import Colors
from src.config import Config
from src.constants import *
from src.content import Content
from src.errors import Error
from src.Loadouts import Loadouts
from src.logs import Logging
from src.names import Names
from src.player_stats import PlayerStats
from src.presences import Presences
from src.rank import Rank
from src.requestsV import Requests
from src.rpc import Rpc
from src.server import Server
from src.states.coregame import Coregame
from src.states.menu import Menu
from src.states.pregame import Pregame
from src.stats import Stats
from src.table import Table
from src.websocket import Ws
from src.os_info import get_os
from src.questions import TABLE_OPTS, FLAGS_OPTS

from src.account_manager.account_manager import AccountManager
from src.account_manager.account_config import AccountConfig
from src.account_manager.account_auth import AccountAuth

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

WINDOW_TITLE = '쭈니곤듀의 발로란트 도우미 ><'

os.system(f"title {WINDOW_TITLE}")

server = ""

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)

def program_exit(status: int):  # so we don't need to import the entire sys module
    log(f"exited program with error code {status}")
    raise sys.exit(status)


def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0)
    try:
        # doesn't even have to be reachable
        s.connect(("10.254.254.254", 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = "127.0.0.1"
    finally:
        s.close()
    return IP


Logging = Logging()
log = Logging.log

# OS Logging
log(f"Operating system: {get_os()}\n")

# Moved to WorkerThread


class WorkerThread(QThread):
    update_table = pyqtSignal(dict)
    update_recently_met = pyqtSignal(list)
    loading_status = pyqtSignal(bool, str)

    def __init__(self):
        super().__init__()
        self.refresh_event = threading.Event()
        self.config_lock = threading.Lock()
        with self.config_lock:
            self.pending_config_update = None

    def queue_config_update(self, new_config):
        with self.config_lock:
            self.pending_config_update = copy.deepcopy(new_config)

    def apply_config_update(self, new_config):
        global cfg, table
        with self.config_lock:
            for key, value in new_config.items():
                if key in DEFAULT_CONFIG:
                    setattr(cfg, key, value)
            table = Table(cfg, log)

    def run(self):
        global server, Wss, Requests
        global table, cfg, content, rank, pstats, presences, menu, coregame, current_map, colors, loadoutsClass, rpc, Wss, valoApiSkins, seasonID, previousSeasonID, gamemodes, agent_dict, map_urls, pregame, namesClass, richConsole
        acc_manager = AccountManager(log, AccountConfig, AccountAuth, NUMBERTORANKS)

        ErrorSRC = Error(log, acc_manager)

        Requests.check_version(version, Requests.copy_run_update_script)
        Requests.check_status()
        Requests = Requests(version, log, ErrorSRC)

        cfg = Config(log)

        content = Content(Requests, log)

        rank = Rank(Requests, log, content, before_ascendant_seasons)
        pstats = PlayerStats(Requests, log, cfg)

        namesClass = Names(Requests, log)

        presences = Presences(Requests, log)

        menu = Menu(Requests, log, presences)
        pregame = Pregame(Requests, log)
        coregame = Coregame(Requests, log)

        Server_inst = Server(log, ErrorSRC)
        Server_inst.start_server()

        agent_dict = content.get_all_agents()

        map_info = content.get_all_maps()
        map_urls = content.get_map_urls(map_info)
        map_splashes = content.get_map_splashes(map_info)

        current_map = coregame.get_current_map(map_urls, map_splashes)

        colors = Colors(log, hide_names, agent_dict, AGENTCOLORLIST)

        loadoutsClass = Loadouts(Requests, log, colors, Server_inst, current_map)
        table = Table(cfg, log)

        stats = Stats()

        if cfg.get_feature_flag("discord_rpc"):
            rpc = Rpc(map_urls, gamemodes, colors, log)
        else:
            rpc = None

        Wss = Ws(Requests.lockfile, Requests, cfg, colors, hide_names, Server_inst, rpc)
        # loop = asyncio.new_event_loop()
        # asyncio.set_event_loop(loop)
        # loop.run_forever()

        log(f"VALORANT rank yoinker v{version}")

        valoApiSkins = requests.get("https://valorant-api.com/v1/weapons/skins")
        gameContent = content.get_content()
        seasonID = content.get_latest_season_id(gameContent)
        previousSeasonID = content.get_previous_season_id(gameContent)
        lastGameState = ""

        # Cache rank+stats per player for the current match so PREGAME data can be reused in INGAME
        match_player_cache = {
            "match_id": None,
            "players": {},  # puuid -> {"playerRank", "previousPlayerRank", "ppstats", "ts"}
        }
        MATCH_PLAYER_CACHE_TTL_SECONDS = 300  # safety TTL

        def reset_match_player_cache(match_id=None):
            match_player_cache["match_id"] = match_id
            match_player_cache["players"] = {}

        def ensure_match_player_cache(match_id):
            if not match_id:
                return

            # New match => reset cache
            if match_player_cache["match_id"] != match_id:
                reset_match_player_cache(match_id)
                return

            # TTL cleanup (safety)
            now = time.time()
            expired = []
            for puuid, cached in match_player_cache["players"].items():
                ts = cached.get("ts", now)
                if (now - ts) > MATCH_PLAYER_CACHE_TTL_SECONDS:
                    expired.append(puuid)

            for puuid in expired:
                del match_player_cache["players"][puuid]

        def get_or_fetch_rank_and_stats(player_subject, current_match_id):
            if current_match_id:
                ensure_match_player_cache(current_match_id)
                cached = match_player_cache["players"].get(player_subject)
                if cached is not None:
                    return (
                        cached["playerRank"],
                        cached["previousPlayerRank"],
                        cached["ppstats"],
                    )

            # Cache miss -> fetch
            playerRank = rank.get_rank(player_subject, seasonID)
            previousPlayerRank = rank.get_rank(player_subject, previousSeasonID)
            ppstats = pstats.get_stats(player_subject)

            if current_match_id and match_player_cache["match_id"] == current_match_id:
                match_player_cache["players"][player_subject] = {
                    "playerRank": dict(playerRank) if isinstance(playerRank, dict) else playerRank,
                    "previousPlayerRank": dict(previousPlayerRank) if isinstance(previousPlayerRank, dict) else previousPlayerRank,
                    "ppstats": dict(ppstats) if isinstance(ppstats, dict) else ppstats,
                    "ts": time.time(),
                }

            return playerRank, previousPlayerRank, ppstats

        print("\nvRY Mobile", color(f"- {get_ip()}:{cfg.port}", fore=(255, 127, 80)))

        print(
            color(
                "\nVisit https://vry.netlify.app/matchLoadouts to view full player inventories\n",
                fore=(255, 253, 205),
            )
        )

        richConsole = RichConsole()



        firstTime = True
        firstPrint = True
        while True:
                pending_config = None
                with self.config_lock:
                    if self.pending_config_update is not None:
                        pending_config = self.pending_config_update
                        self.pending_config_update = None
                if pending_config is not None:
                    self.apply_config_update(pending_config)
                recently_met_rows = []
                table.clear()
                table.set_default_field_names()
                table.reset_runtime_col_flags()

                # check if short ranks should be used
                if cfg.get_feature_flag("short_ranks"):
                    Ranks = SHORT_NUMBERTORANKS
                else:
                    Ranks = NUMBERTORANKS

                try:

                    # loop = asyncio.get_event_loop()
                    # loop.run_until_complete(Wss.conntect_to_websocket())
                    # if firstTime:
                    #     loop = asyncio.new_event_loop()
                    #     asyncio.set_event_loop(loop)
                    #     game_state = loop.run_until_complete(Wss.conntect_to_websocket(game_state))
                    if firstTime:
                        run = True
                        while run:
                            presence = presences.get_presence()
                            private_presence = presences.get_private_presence(presence)
                            # wait until your own valorant presence is initialized
                            if private_presence is not None:
                                if cfg.get_feature_flag("discord_rpc"):
                                    rpc.set_rpc(private_presence)
                                game_state = presences.get_game_state(presence)
                                if game_state is not None:
                                    run = False
                            time.sleep(2)
                        log(f"first game state: {game_state}")
                    else:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        previous_game_state = game_state
                        game_state = loop.run_until_complete(
                            Wss.recconect_to_websocket(game_state)
                        )
                        # We invalidate the cached responses when going from any state to menus
                        if previous_game_state != game_state and game_state == "MENUS":
                            rank.invalidate_cached_responses()
                            reset_match_player_cache()
                            if hasattr(pstats, "clear_runtime_cache"):
                                pstats.clear_runtime_cache()
                        log(f"new game state: {game_state}")
                        loop.close()
                    firstTime = False
                    # loop = asyncio.new_event_loop()
                    # asyncio.set_event_loop(loop)
                    # loop.run_until_complete()
                except TypeError:
                    game_state = "DISCONNECTED"
                    reset_match_player_cache()
                    if hasattr(pstats, "clear_runtime_cache"):
                        pstats.clear_runtime_cache()

                if game_state == "DISCONNECTED":
                    richConsole.print("[yellow]Disconnected from Valorant. Attempting to reconnect...[/yellow]")
                    # Loop waits for the Valorant client to respond
                    while True:
                        # Rereads the lockfile
                        Requests.lockfile = Requests.get_lockfile()

                        if Requests.lockfile is None:
                            time.sleep(5)
                            continue

                        presence_check = presences.get_presence()

                        if presence_check is not None:
                            break

                        time.sleep(5)

                    richConsole.print("[green]Reconnected successfully! Loading...[/green]")

                    Requests.get_headers(refresh=True)

                    Wss = Ws(Requests.lockfile, Requests, cfg, colors, hide_names, Server_inst, rpc)

                    firstTime = True
                    lastGameState = ""
                    reset_match_player_cache()
                    if hasattr(pstats, "clear_runtime_cache"):
                        pstats.clear_runtime_cache()
                    continue

                if True:
                    log(f"getting new {game_state} scoreboard")
                    lastGameState = game_state
                    game_state_dict = {
                        "INGAME": color("In-Game", fore=(241, 39, 39)),
                        "PREGAME": color("Agent Select", fore=(103, 237, 76)),
                        "MENUS": color("In-Menus", fore=(238, 241, 54)),
                    }

                    if (not firstPrint) and cfg.get_feature_flag("pre_cls"):
                        os.system("cls")

                    is_leaderboard_needed = False

                    # get new presence
                    presence = presences.get_presence()
                    priv_presence = presences.get_private_presence(presence)

                    # Temp fix: Riot is swapping between nested and flat API structures.
                    party_state = ""
                    if "partyPresenceData" in priv_presence: # Check for nested structure
                        party_state = priv_presence["partyPresenceData"]["partyState"]
                    elif "partyState" in priv_presence: # Check for flattened structure
                        party_state = priv_presence["partyState"]
                    else:
                        # No known structure found, log and fail
                        log("ERROR: Unknown presence API structure in 'main'.")
                        party_state = priv_presence["partyPresenceData"]["partyState"]

                    if (
                        priv_presence["provisioningFlow"] == "CustomGame"
                        or party_state == "CUSTOM_GAME_SETUP"
                    ):
                        gamemode = "Custom Game"
                    else:
                        gamemode = gamemodes.get(priv_presence["queueId"])

                    heartbeat_data = {
                        "time": int(time.time()),
                        "state": game_state,
                        "mode": gamemode,
                        "puuid": Requests.puuid,
                        "players": {},
                    }

                    if game_state == "INGAME":
                        coregame_stats = coregame.get_coregame_stats()
                        if coregame_stats == None:
                            continue
                        coregame_match_id = coregame.get_coregame_match_id()
                        ensure_match_player_cache(coregame_match_id)
                        Players = coregame_stats["Players"]
                        # data for chat to function
                        partyMembers = menu.get_party_members(Requests.puuid, presence)
                        partyMembersList = [a["Subject"] for a in partyMembers]

                        players_data = {}
                        players_data.update({"ignore": partyMembersList})
                        for player in Players:
                            if player["Subject"] == Requests.puuid:
                                if cfg.get_feature_flag("discord_rpc"):
                                    rpc.set_data({"agent": player["CharacterID"]})
                            players_data.update(
                                {
                                    player["Subject"]: {
                                        "team": player["TeamID"],
                                        "agent": player["CharacterID"],
                                        "streamer_mode": player["PlayerIdentity"]["Incognito"],
                                    }
                                }
                            )
                        Wss.set_player_data(players_data)

                        server = coregame_stats.get("GamePodID", "")
                        presences.wait_for_presence(namesClass.get_players_puuid(Players))
                        names = namesClass.get_names_from_puuids(Players)
                        loadouts_arr = loadoutsClass.get_match_loadouts(
                            coregame_match_id,
                            Players,
                            cfg.weapon,
                            valoApiSkins,
                            names,
                            state="game",
                        )
                        loadouts = loadouts_arr[0]
                        loadouts_data = loadouts_arr[1]
                        # with alive_bar(total=len(Players), title='Fetching Players', bar='classic2') as bar:
                        isRange = False
                        playersLoaded = 1

                        heartbeat_data["map"] = (map_urls[coregame_stats["MapID"].lower()],)
                        self.loading_status.emit(True, "Loading player stats...")
                        with richConsole.status("Loading Players...") as status:
                                partyOBJ = menu.get_party_json(
                                    namesClass.get_players_puuid(Players), presence
                                )
                                # log(f"retrieved names dict: {names}")
                                Players.sort(
                                    key=lambda Players: Players["PlayerIdentity"].get(
                                        "AccountLevel"
                                    ),
                                    reverse=True,
                                )
                                Players.sort(key=lambda Players: Players["TeamID"], reverse=True)
                                partyCount = 0
                                partyNum = 0
                                partyIcons = {}
                                lastTeamBoolean = False
                                lastTeam = "Red"

                                already_played_with = []
                                stats_data = stats.read_data()

                                for p in Players:
                                    if p["Subject"] == Requests.puuid:
                                        allyTeam = p["TeamID"]
                                for player in Players:
                                    status.update(
                                        f"Loading players... [{playersLoaded}/{len(Players)}]"
                                    )
                                    playersLoaded += 1

                                    if player["Subject"] in stats_data.keys():
                                        if (
                                            player["Subject"] != Requests.puuid
                                            and player["Subject"] not in partyMembersList
                                        ):
                                            curr_player_stat = stats_data[player["Subject"]][-1]
                                            i = 1
                                            while (
                                                curr_player_stat["match_id"] == coregame.match_id
                                                and len(stats_data[player["Subject"]]) > i
                                            ):
                                                i += 1
                                                # if curr_player_stat["match_id"] == coregame.match_id and len(stats_data[player["Subject"]]) > 1:
                                                curr_player_stat = stats_data[player["Subject"]][-i]
                                            if curr_player_stat["match_id"] != coregame.match_id:
                                                # checking for party members and self players
                                                times = 0
                                                m_set = ()
                                            for m in stats_data[player["Subject"]]:
                                                if (
                                                    m["match_id"] != coregame.match_id
                                                    and m["match_id"] not in m_set
                                                ):
                                                    times += 1
                                                    m_set += (m["match_id"],)
                                            if player["PlayerIdentity"]["Incognito"] == False:
                                                team_role = (
                                                    "Ally"
                                                    if player["TeamID"] == allyTeam
                                                    else "Enemy"
                                                )
                                                current_agent = agent_dict.get(
                                                    player["CharacterID"].lower(), "Unknown"
                                                )
                                                already_played_with.append(
                                                    {
                                                        "times": times,
                                                        "name": curr_player_stat["name"],
                                                        "previous_agent": curr_player_stat["agent"],
                                                            "current_agent": current_agent,
                                                            "team": team_role,
                                                            "time_diff": time.time()
                                                        - curr_player_stat["epoch"],
                                                    }
                                                )
                                            else:
                                                team_role = (
                                                    "Ally"
                                                    if player["TeamID"] == allyTeam
                                                    else "Enemy"
                                                )
                                                current_agent = agent_dict.get(
                                                    player["CharacterID"].lower(), "Unknown"
                                                )
                                                team_string = (
                                                    "your" if team_role == "Ally" else "enemy"
                                                )
                                                already_played_with.append(
                                                    {
                                                            "times": times,
                                                            "name": agent_dict.get(
                                                                player["CharacterID"].lower(),
                                                                "Unknown",
                                                            )
                                                            + " on "
                                                            + team_string
                                                            + " team",
                                                            "previous_agent": curr_player_stat["agent"],
                                                            "current_agent": current_agent,
                                                            "team": team_role,
                                                            "time_diff": time.time()
                                                            - curr_player_stat["epoch"],
                                                        }
                                                    )

                                    party_icon = ""
                                # set party premade icon
                                for party in partyOBJ:
                                    if player["Subject"] in partyOBJ[party]:
                                        if party not in partyIcons:
                                            partyIcons.update(
                                                {party: PARTYICONLIST[partyCount]}
                                            )
                                            # PARTY_ICON
                                            party_icon = PARTYICONLIST[partyCount]
                                            partyNum = partyCount + 1
                                            partyCount += 1
                                        else:
                                            # PARTY_ICON
                                            party_icon = partyIcons[party]
                                playerRank, previousPlayerRank, ppstats = get_or_fetch_rank_and_stats(
                                    player["Subject"], coregame_match_id
                                )

                                if player["Subject"] == Requests.puuid:
                                    if cfg.get_feature_flag("discord_rpc"):
                                        rpc.set_data(
                                            {
                                                "rank": playerRank["rank"],
                                                "rank_name": colors.escape_ansi(
                                                    NUMBERTORANKS[playerRank["rank"]]
                                                )
                                                + " | "
                                                + str(playerRank["rr"])
                                                + "rr",
                                            }
                                        )
                                # rankStatus = playerRank[1]
                                # useless code since rate limit is handled in the requestsV
                                # while not rankStatus:
                                #     print("You have been rate limited, 😞 waiting 10 seconds!")
                                #     time.sleep(10)
                                #     playerRank = rank.get_rank(player["Subject"], seasonID)
                                #     rankStatus = playerRank[1]

                                hs = ppstats["hs"]
                                kda = ppstats["kda"]

                                rr_numeric_value = ppstats["RankedRatingEarned"]
                                afk_penalty = ppstats["AFKPenalty"]
                                ranked_rating_earned = colors.get_rr_gradient(
                                    rr_numeric_value, afk_penalty
                                )

                                player_level = player["PlayerIdentity"].get("AccountLevel")

                                if player["PlayerIdentity"]["Incognito"]:
                                    Namecolor = colors.get_color_from_team(
                                        player["TeamID"],
                                        names[player["Subject"]],
                                        player["Subject"],
                                        Requests.puuid,
                                        agent=player["CharacterID"],
                                        party_members=partyMembersList,
                                    )
                                else:
                                    Namecolor = colors.get_color_from_team(
                                        player["TeamID"],
                                        names[player["Subject"]],
                                        player["Subject"],
                                        Requests.puuid,
                                        party_members=partyMembersList,
                                    )
                                if lastTeam != player["TeamID"]:
                                    if lastTeamBoolean:
                                        table.add_empty_row()
                                lastTeam = player["TeamID"]
                                lastTeamBoolean = True
                                if player["PlayerIdentity"]["HideAccountLevel"]:
                                    if (
                                        player["Subject"] == Requests.puuid
                                        or player["Subject"] in partyMembersList
                                        or hide_levels == False
                                    ):
                                        PLcolor = colors.level_to_color(player_level)
                                    else:
                                        PLcolor = ""
                                else:
                                    PLcolor = colors.level_to_color(player_level)
                                # AGENT
                                # agent = str(agent_dict.get(player["CharacterID"].lower()))
                                agent = colors.get_agent_from_uuid(
                                    player["CharacterID"].lower()
                                )
                                if agent == "" and len(Players) == 1:
                                    isRange = True

                                # NAME
                                name = Namecolor

                                # VIEWS
                                # views = get_views(names[player["Subject"]])

                                # skin
                                skin = loadouts.get(player["Subject"], "")

                                # RANK
                                rankName = Ranks[playerRank["rank"]]
                                if cfg.get_feature_flag("aggregate_rank_rr") and cfg.table.get(
                                    "rr"
                                ):
                                    rankName += f" ({playerRank['rr']})"

                                # RANK RATING
                                rr = playerRank["rr"]

                                # short peak rank string
                                has_letter = any(
                                    c.isalpha() for c in str(playerRank["peakrankep"])
                                )
                                peakRankAct = (
                                    f" ({playerRank['peakrankep']}a{playerRank['peakrankact']})"
                                    if has_letter
                                    else f" (e{playerRank['peakrankep']}a{playerRank['peakrankact']})"
                                )
                                if not cfg.get_feature_flag("peak_rank_act"):
                                    peakRankAct = ""

                                # PEAK RANK
                                peakRank = Ranks[playerRank["peakrank"]] + peakRankAct

                                # PREVIOUS RANK
                                previousRank = Ranks[previousPlayerRank["rank"]]

                                # LEADERBOARD
                                leaderboard = playerRank["leaderboard"]

                                hs = colors.get_hs_gradient(hs)
                                wr = (
                                    colors.get_wr_gradient(playerRank["wr"])
                                    + f" ({playerRank['numberofgames']})"
                                )

                                if int(leaderboard) > 0:
                                    is_leaderboard_needed = True

                                # LEVEL
                                level = PLcolor
                                table.add_row_table(
                                    [
                                        party_icon,
                                        agent,
                                        name,
                                        # views,
                                        skin,
                                        rankName,
                                        rr,
                                        peakRank,
                                        previousRank,
                                        leaderboard,
                                        hs,
                                        wr,
                                        kda,
                                        level,
                                        ranked_rating_earned,
                                    ]
                                )

                                heartbeat_data["players"][player["Subject"]] = {
                                    "puuid": player["Subject"],
                                    "name": names[player["Subject"]],
                                    "partyNumber": partyNum if party_icon != "" else 0,
                                    "agent": agent_dict.get(player["CharacterID"].lower(), "Unknown"),
                                    "rank": playerRank["rank"],
                                    "peakRank": playerRank["peakrank"],
                                    "peakRankAct": peakRankAct,
                                    "rr": rr,
                                    "kda": ppstats["kda"],
                                    "headshotPercentage": ppstats["hs"],
                                    "winPercentage": f"{playerRank['wr']} ({playerRank['numberofgames']})",
                                    "level": player_level,
                                    "agentImgLink": loadouts_data["Players"][
                                        player["Subject"]
                                    ].get("Agent", None),
                                    "team": loadouts_data["Players"][player["Subject"]].get(
                                        "Team", None
                                    ),
                                    "sprays": loadouts_data["Players"][player["Subject"]].get(
                                        "Sprays", None
                                    ),
                                    "title": loadouts_data["Players"][player["Subject"]].get(
                                        "Title", None
                                    ),
                                    "playerCard": loadouts_data["Players"][
                                        player["Subject"]
                                    ].get("PlayerCard", None),
                                    "weapons": loadouts_data["Players"][player["Subject"]].get(
                                        "Weapons", None
                                    ),
                                }

                                stats.save_data(
                                    {
                                        player["Subject"]: {
                                            "name": names[player["Subject"]],
                                            "agent": agent_dict.get(player["CharacterID"].lower(), "Unknown"),
                                            "map": current_map,
                                            "rank": playerRank["rank"],
                                            "rr": rr,
                                            "match_id": coregame.match_id,
                                            "epoch": time.time(),
                                        }
                                    }
                                )
                                # bar()
                        self.loading_status.emit(False, "")
                        if cfg.get_feature_flag("last_played"):
                            recently_met_rows = [
                                {
                                    "player": played["name"],
                                    "team": played["team"],
                                    "previous_agent": played["previous_agent"],
                                    "current_agent": played["current_agent"],
                                    "last_met": f"{stats.convert_time(played['time_diff'])} ago",
                                    "times": played["times"],
                                }
                                for played in already_played_with
                            ]
                        else:
                            recently_met_rows = []
                    elif game_state == "PREGAME":
                        already_played_with = []
                        pregame_stats = pregame.get_pregame_stats()
                        if pregame_stats == None:
                            continue
                        server = pregame_stats.get("GamePodID", "")
                        Players = pregame_stats["AllyTeam"]["Players"]
                        presences.wait_for_presence(namesClass.get_players_puuid(Players))
                        names = namesClass.get_names_from_puuids(Players)
                        pregame_match_id = pregame_stats.get("ID")
                        ensure_match_player_cache(pregame_match_id)
                        # temporary until other regions gets fixed?
                        # loadouts = loadoutsClass.get_match_loadouts(pregame.get_pregame_match_id(), pregame_stats, cfg.weapon, valoApiSkins, names,
                        #   state="pregame")
                        playersLoaded = 1
                        self.loading_status.emit(True, "Loading player stats...")
                        with richConsole.status("Loading Players...") as status:
                            # with alive_bar(total=len(Players), title='Fetching Players', bar='classic2') as bar:
                            presence = presences.get_presence()
                            partyOBJ = menu.get_party_json(
                                namesClass.get_players_puuid(Players), presence
                            )
                            partyMembers = menu.get_party_members(Requests.puuid, presence)
                            partyMembersList = [a["Subject"] for a in partyMembers]
                            # log(f"retrieved names dict: {names}")
                            Players.sort(
                                key=lambda Players: Players["PlayerIdentity"].get(
                                    "AccountLevel"
                                ),
                                reverse=True,
                            )
                            partyCount = 0
                            partyIcons = {}
                            for player in Players:
                                status.update(
                                    f"Loading players... [{playersLoaded}/{len(Players)}]"
                                )
                                playersLoaded += 1
                                party_icon = ""

                                # set party premade icon
                                for party in partyOBJ:
                                    if player["Subject"] in partyOBJ[party]:
                                        if party not in partyIcons:
                                            partyIcons.update(
                                                {party: PARTYICONLIST[partyCount]}
                                            )
                                            # PARTY_ICON
                                            party_icon = PARTYICONLIST[partyCount]
                                            partyNum = partyCount + 1
                                        else:
                                            # PARTY_ICON
                                            party_icon = partyIcons[party]
                                        partyCount += 1
                                playerRank, previousPlayerRank, ppstats = get_or_fetch_rank_and_stats(
                                    player["Subject"], pregame_match_id
                                )

                                if player["Subject"] == Requests.puuid:
                                    if cfg.get_feature_flag("discord_rpc"):
                                        rpc.set_data(
                                            {
                                                "rank": playerRank["rank"],
                                                "rank_name": colors.escape_ansi(
                                                    NUMBERTORANKS[playerRank["rank"]]
                                                )
                                                + " | "
                                                + str(playerRank["rr"])
                                                + "rr",
                                            }
                                        )
                                # rankStatus = playerRank[1]
                                # useless code since rate limit is handled in the requestsV
                                # while not rankStatus:
                                #     print("You have been rate limited, 😞 waiting 10 seconds!")
                                #     time.sleep(10)
                                #     playerRank = rank.get_rank(player["Subject"], seasonID)
                                #     rankStatus = playerRank[1]
                                # playerRank = playerRank[0]

                                hs = ppstats["hs"]
                                kda = ppstats["kda"]

                                rr_numeric_value = ppstats["RankedRatingEarned"]
                                afk_penalty = ppstats["AFKPenalty"]
                                ranked_rating_earned = colors.get_rr_gradient(
                                    rr_numeric_value, afk_penalty
                                )

                                player_level = player["PlayerIdentity"].get("AccountLevel")
                                if player["PlayerIdentity"]["Incognito"]:
                                    NameColor = colors.get_color_from_team(
                                        pregame_stats["Teams"][0]["TeamID"],
                                        names[player["Subject"]],
                                        player["Subject"],
                                        Requests.puuid,
                                        agent=player["CharacterID"],
                                        party_members=partyMembersList,
                                    )
                                else:
                                    NameColor = colors.get_color_from_team(
                                        pregame_stats["Teams"][0]["TeamID"],
                                        names[player["Subject"]],
                                        player["Subject"],
                                        Requests.puuid,
                                        party_members=partyMembersList,
                                    )

                                if player["PlayerIdentity"]["HideAccountLevel"]:
                                    if (
                                        player["Subject"] == Requests.puuid
                                        or player["Subject"] in partyMembersList
                                        or hide_levels == False
                                    ):
                                        PLcolor = colors.level_to_color(player_level)
                                    else:
                                        PLcolor = ""
                                else:
                                    PLcolor = colors.level_to_color(player_level)
                                if player["CharacterSelectionState"] == "locked":
                                    agent_color = color(
                                        agent_dict.get(player["CharacterID"].lower(), "Unknown"),
                                        fore=(255, 255, 255),
                                    )
                                elif player["CharacterSelectionState"] == "selected":
                                    agent_color = color(
                                        agent_dict.get(player["CharacterID"].lower(), "Unknown"),
                                        fore=(128, 128, 128),
                                    )
                                else:
                                    agent_color = color(
                                        agent_dict.get(player["CharacterID"].lower(), "Unknown"),
                                        fore=(54, 53, 51),
                                    )

                                # AGENT
                                agent = agent_color

                                # NAME
                                name = NameColor

                                # VIEWS
                                # views = get_views(names[player["Subject"]])

                                # temporary until other regions gets fixed?
                                # skin
                                # skin = loadouts[player["Subject"]]

                                # RANK
                                rankName = Ranks[playerRank["rank"]]
                                if cfg.get_feature_flag("aggregate_rank_rr") and cfg.table.get(
                                    "rr"
                                ):
                                    rankName += f" ({playerRank['rr']})"

                                # RANK RATING
                                rr = playerRank["rr"]

                                # short peak rank string
                                has_letter = any(
                                    c.isalpha() for c in str(playerRank["peakrankep"])
                                )
                                peakRankAct = (
                                    f" ({playerRank['peakrankep']}a{playerRank['peakrankact']})"
                                    if has_letter
                                    else f" (e{playerRank['peakrankep']}a{playerRank['peakrankact']})"
                                )
                                if not cfg.get_feature_flag("peak_rank_act"):
                                    peakRankAct = ""
                                # PEAK RANK
                                peakRank = Ranks[playerRank["peakrank"]] + peakRankAct

                                # PREVIOUS RANK
                                previousRank = Ranks[previousPlayerRank["rank"]]

                                # LEADERBOARD
                                leaderboard = playerRank["leaderboard"]

                                hs = colors.get_hs_gradient(hs)
                                wr = (
                                    colors.get_wr_gradient(playerRank["wr"])
                                    + f" ({playerRank['numberofgames']})"
                                )

                                if int(leaderboard) > 0:
                                    is_leaderboard_needed = True

                                # LEVEL
                                level = PLcolor

                                table.add_row_table(
                                    [
                                        party_icon,
                                        agent,
                                        name,
                                        # views,
                                        "",
                                        rankName,
                                        rr,
                                        peakRank,
                                        previousRank,
                                        leaderboard,
                                        hs,
                                        wr,
                                        kda,
                                        level,
                                        ranked_rating_earned,
                                    ]
                                )

                                heartbeat_data["players"][player["Subject"]] = {
                                    "name": names[player["Subject"]],
                                    "partyNumber": partyNum if party_icon != "" else 0,
                                    "agent": agent_dict.get(player["CharacterID"].lower(), "Unknown"),
                                    "rank": playerRank["rank"],
                                    "peakRank": playerRank["peakrank"],
                                    "peakRankAct": peakRankAct,
                                    "level": player_level,
                                    "rr": rr,
                                    "kda": ppstats["kda"],
                                    "headshotPercentage": ppstats["hs"],
                                    "winPercentage": f"{playerRank['wr']} ({playerRank['numberofgames']})",
                                }

                                # bar()
                        self.loading_status.emit(False, "")
                    if game_state == "MENUS":
                        reset_match_player_cache()
                        if hasattr(pstats, "clear_runtime_cache"):
                            pstats.clear_runtime_cache()

                        server = ""
                        already_played_with = []
                        Players = menu.get_party_members(Requests.puuid, presence)
                        names = namesClass.get_names_from_puuids(Players)
                        playersLoaded = 1
                        self.loading_status.emit(True, "Loading player stats...")
                        with richConsole.status("Loading Players...") as status:
                            # with alive_bar(total=len(Players), title='Fetching Players', bar='classic2') as bar:
                            # log(f"retrieved names dict: {names}")
                            Players.sort(
                                key=lambda Players: Players["PlayerIdentity"].get(
                                    "AccountLevel"
                                ),
                                reverse=True,
                            )
                            seen = []
                            for player in Players:

                                if player not in seen:
                                    status.update(
                                        f"Loading players... [{playersLoaded}/{len(Players)}]"
                                    )
                                    playersLoaded += 1
                                    party_icon = PARTYICONLIST[0]
                                    playerRank = rank.get_rank(player["Subject"], seasonID)
                                    previousPlayerRank = rank.get_rank(
                                        player["Subject"], previousSeasonID
                                    )
                                    if player["Subject"] == Requests.puuid:
                                        if cfg.get_feature_flag("discord_rpc"):
                                            rpc.set_data(
                                                {
                                                    "rank": playerRank["rank"],
                                                    "rank_name": colors.escape_ansi(
                                                        NUMBERTORANKS[playerRank["rank"]]
                                                    )
                                                    + " | "
                                                    + str(playerRank["rr"])
                                                    + "rr",
                                                }
                                            )

                                    # rankStatus = playerRank[1]
                                    # useless code since rate limit is handled in the requestsV
                                    # while not rankStatus:
                                    #     print("You have been rate limited, 😞 waiting 10 seconds!")
                                    #     time.sleep(10)
                                    #     playerRank = rank.get_rank(player["Subject"], seasonID)
                                    #     rankStatus = playerRank[1]
                                    # playerRank = playerRank["rank"]

                                    ppstats = pstats.get_stats(player["Subject"])
                                    hs = ppstats["hs"]
                                    kda = ppstats["kda"]

                                    rr_numeric_value = ppstats["RankedRatingEarned"]
                                    afk_penalty = ppstats["AFKPenalty"]
                                    ranked_rating_earned = colors.get_rr_gradient(
                                        rr_numeric_value, afk_penalty
                                    )

                                    player_level = player["PlayerIdentity"].get("AccountLevel")
                                    PLcolor = colors.level_to_color(player_level)

                                    # AGENT
                                    agent = ""

                                    # NAME
                                    name = color(names[player["Subject"]], fore=(76, 151, 237))

                                    # RANK
                                    rankName = Ranks[playerRank["rank"]]
                                    if cfg.get_feature_flag(
                                        "aggregate_rank_rr"
                                    ) and cfg.table.get("rr"):
                                        rankName += f" ({playerRank['rr']})"

                                    # RANK RATING
                                    rr = playerRank["rr"]

                                    # short peak rank string
                                    has_letter = any(
                                        c.isalpha() for c in str(playerRank["peakrankep"])
                                    )
                                    peakRankAct = (
                                        f" ({playerRank['peakrankep']}a{playerRank['peakrankact']})"
                                        if has_letter
                                        else f" (e{playerRank['peakrankep']}a{playerRank['peakrankact']})"
                                    )
                                    if not cfg.get_feature_flag("peak_rank_act"):
                                        peakRankAct = ""

                                    # PEAK RANK
                                    peakRank = (
                                        Ranks[playerRank["peakrank"]] + peakRankAct
                                    )

                                    # PREVIOUS RANK
                                    previousRank = Ranks[previousPlayerRank["rank"]]

                                    # LEADERBOARD
                                    leaderboard = playerRank["leaderboard"]

                                    hs = colors.get_hs_gradient(hs)
                                    wr = (
                                        colors.get_wr_gradient(playerRank["wr"])
                                        + f" ({playerRank['numberofgames']})"
                                    )

                                    if int(leaderboard) > 0:
                                        is_leaderboard_needed = True

                                    # LEVEL
                                    level = PLcolor

                                    table.add_row_table(
                                        [
                                            party_icon,
                                            agent,
                                            name,
                                            "",
                                            rankName,
                                            rr,
                                            peakRank,
                                            previousRank,
                                            leaderboard,
                                            hs,
                                            wr,
                                            kda,
                                            level,
                                            ranked_rating_earned,
                                        ]
                                    )

                                    heartbeat_data["players"][player["Subject"]] = {
                                        "name": names[player["Subject"]],
                                        "rank": playerRank["rank"],
                                        "peakRank": playerRank["peakrank"],
                                        "peakRankAct": peakRankAct,
                                        "level": player_level,
                                        "rr": rr,
                                        "kda": ppstats["kda"],
                                        "headshotPercentage": ppstats["hs"],
                                        "winPercentage": f"{playerRank['wr']} ({playerRank['numberofgames']})",
                                    }

                                    # bar()
                            seen.append(player["Subject"])
                        self.loading_status.emit(False, "")
                    if (title := game_state_dict.get(game_state)) is None:
                        # program_exit(1)
                        time.sleep(9)

                    title_parts = [f"VALORANT status: {title}"]

                    if cfg.get_feature_flag("server_id") and server != "":
                        parts = server.split('.')
                        if len(parts) > 2:
                            short_serverID = '.'.join(parts[2:])
                        else:
                            short_serverID = server
                        title_parts.append(f" {colr('- ' + short_serverID, fore=(200, 200, 200))}")

                    table.set_title(''.join(title_parts))

                    if title is not None:
                        if cfg.get_feature_flag("auto_hide_leaderboard") and (
                            not is_leaderboard_needed
                        ):
                            table.set_runtime_col_flag("Pos.", False)

                        if game_state == "MENUS":
                            table.set_runtime_col_flag("Party", False)
                            table.set_runtime_col_flag("Agent", False)
                            table.set_runtime_col_flag(cfg.weapon.capitalize(), False)

                        if game_state == "INGAME":
                            if isRange:
                                table.set_runtime_col_flag("Party", False)
                                table.set_runtime_col_flag("Agent", False)

                        # We don't to show the RR column if the "aggregate_rank_rr" feature flag is True.
                        table.set_runtime_col_flag(
                            "RR",
                            cfg.table.get("rr")
                            and not cfg.get_feature_flag("aggregate_rank_rr"),
                        )

                        table.set_caption(f"VALORANT rank yoinker v{version}")
                        Server_inst.send_payload("heartbeat", heartbeat_data)
                        table.display()
                        self.update_table.emit(table.raw_data)
                        self.update_recently_met.emit(recently_met_rows)
                        firstPrint = False

                        # print(f"VALORANT rank yoinker v{version}")
                        if cfg.get_feature_flag("last_played"):
                            if len(already_played_with) > 0:
                                print("\n")
                                for played in already_played_with:
                                    print(
                                        f"Already played with {played['name']} (last {played['previous_agent']}) {stats.convert_time(played['time_diff'])} ago. (Total played {played['times']} times)"
                                    )
                        already_played_with = []
                if cfg.cooldown == 0:
                    self.refresh_event.wait()
                else:
                    self.refresh_event.wait(cfg.cooldown)

                if self.refresh_event.is_set():
                    self.refresh_event.clear()
                    rank.invalidate_cached_responses()
                    reset_match_player_cache()
                    if hasattr(pstats, "clear_runtime_cache"):
                        pstats.clear_runtime_cache()


class SettingsDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.base_config = config
        self.setWindowTitle("Settings")
        self.setMinimumSize(500, 650)

        layout = QVBoxLayout()

        form_layout = QFormLayout()
        self.weapon_combo = QComboBox()
        self.weapon_combo.addItems(WEAPONS)
        self.weapon_combo.setCurrentText(
            config.get("weapon", DEFAULT_CONFIG["weapon"])
        )
        form_layout.addRow("Weapon", self.weapon_combo)

        self.chat_limit_spin = QSpinBox()
        self.chat_limit_spin.setRange(0, 100)
        self.chat_limit_spin.setValue(
            int(config.get("chat_limit", DEFAULT_CONFIG["chat_limit"]))
        )
        form_layout.addRow("Chat history length", self.chat_limit_spin)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(0, 65535)
        self.port_spin.setValue(int(config.get("port", DEFAULT_CONFIG["port"])))
        form_layout.addRow("Server port", self.port_spin)

        layout.addLayout(form_layout)

        table_group = QGroupBox("Table Columns")
        table_layout = QVBoxLayout()
        self.table_checks = {}
        table_config = config.get("table", DEFAULT_CONFIG["table"])
        for key, label in TABLE_OPTS.items():
            checkbox = QCheckBox(label)
            checkbox.setChecked(table_config.get(key, DEFAULT_CONFIG["table"][key]))
            table_layout.addWidget(checkbox)
            self.table_checks[key] = checkbox
        table_group.setLayout(table_layout)
        layout.addWidget(table_group)

        flags_group = QGroupBox("Feature Flags")
        flags_layout = QVBoxLayout()
        self.flags_checks = {}
        flags_config = config.get("flags", DEFAULT_CONFIG["flags"])
        for key, label in FLAGS_OPTS.items():
            checkbox = QCheckBox(label)
            checkbox.setChecked(flags_config.get(key, DEFAULT_CONFIG["flags"][key]))
            flags_layout.addWidget(checkbox)
            self.flags_checks[key] = checkbox
        flags_group.setLayout(flags_layout)
        layout.addWidget(flags_group)

        button_box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        self.setLayout(layout)

    def get_config(self):
        new_config = copy.deepcopy(self.base_config)
        new_config["weapon"] = self.weapon_combo.currentText()
        new_config["chat_limit"] = int(self.chat_limit_spin.value())
        new_config["port"] = int(self.port_spin.value())

        table_config = dict(new_config.get("table", {}))
        for key, checkbox in self.table_checks.items():
            table_config[key] = checkbox.isChecked()
        new_config["table"] = table_config

        flags_config = dict(new_config.get("flags", {}))
        for key, checkbox in self.flags_checks.items():
            flags_config[key] = checkbox.isChecked()
        new_config["flags"] = flags_config
        return new_config


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(1000, 700)
        icon_path = resource_path("image.png")
        self.setWindowIcon(QIcon(icon_path))

        main_layout = QVBoxLayout()

        top_layout = QHBoxLayout()
        top_layout.addStretch()

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.refresh_button.clicked.connect(self.manual_refresh)
        top_layout.addWidget(self.refresh_button)

        self.settings_button = QPushButton("Settings")
        self.settings_button.setIcon(
            self.style().standardIcon(QStyle.SP_FileDialogDetailedView)
        )
        self.settings_button.clicked.connect(self.open_settings)
        top_layout.addWidget(self.settings_button)

        main_layout.addLayout(top_layout)

        self.live_table = QTableWidget()
        self.live_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.live_table.verticalHeader().setVisible(False)
        self.live_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.live_table.setStyleSheet("background-color: #121212; color: #FFFFFF; font-size: 14px; font-weight: 700; font-family: 'Malgun Gothic'; gridline-color: #fff;") #border: 2px solid #fff;
        self.live_table.horizontalHeader().setStyleSheet("background-color: #121212; color: #FFFFFF")

        main_layout.addWidget(self.live_table)

        self.recent_label = QLabel("Recently met players")
        self.recent_label.setStyleSheet("color: #FFFFFF; font-size: 14px; font-weight: 700;")
        main_layout.addWidget(self.recent_label)

        self.recent_table = QTableWidget()
        self.recent_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.recent_table.verticalHeader().setVisible(False)
        self.recent_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.recent_table.setStyleSheet("background-color: #121212; color: #FFFFFF; font-size: 13px; font-weight: 600; font-family: 'Malgun Gothic'; gridline-color: #fff;")
        self.recent_table.horizontalHeader().setStyleSheet("background-color: #121212; color: #FFFFFF")
        main_layout.addWidget(self.recent_table)

        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

        self.worker = WorkerThread()
        self.worker.update_table.connect(self.update_live_table)
        self.worker.update_recently_met.connect(self.update_recently_met_table)
        self.worker.loading_status.connect(self.update_loading_status)
        self.loading_dialog = QProgressDialog("Loading player stats...", None, 0, 0, self)
        self.loading_dialog.setWindowTitle("Loading")
        self.loading_dialog.setCancelButton(None)
        self.loading_dialog.setWindowModality(Qt.ApplicationModal)
        self.loading_dialog.setMinimumDuration(0)
        self.loading_dialog.hide()

        self.worker.start()

    def update_live_table(self, data):
        headers = data.get("headers", [])
        rows = data.get("rows", [])

        self.live_table.clear()
        self.live_table.setColumnCount(len(headers))
        self.live_table.setHorizontalHeaderLabels(headers)
        self.live_table.setRowCount(len(rows))

        for row_idx, row_data in enumerate(rows):
            for col_idx, (text, color_rgb) in enumerate(row_data):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if color_rgb:
                    r, g, b = color_rgb
                    item.setForeground(QBrush(QColor(r, g, b)))
                self.live_table.setItem(row_idx, col_idx, item)

        # Set specific column widths
        header = self.live_table.horizontalHeader()
        for h in headers:
            header.setSectionResizeMode(headers.index(h), QHeaderView.ResizeToContents)
        if "Name" in headers:
            name_idx = headers.index("Name")
            header.setSectionResizeMode(name_idx, QHeaderView.Stretch)
        # if "Peak Rank" in headers:
        #     peak_idx = headers.index("Peak Rank")
        #     header.setSectionResizeMode(peak_idx, QHeaderView.ResizeToContents)
        #     # self.live_table.setColumnWidth(peak_idx, 150)
        # if "HS" in headers:
        #     hs_idx = headers.index("HS")
        #     header.setSectionResizeMode(hs_idx, QHeaderView.ResizeToContents)
        #     # self.live_table.setColumnWidth(hs_idx, 50)
        # if "Level" in headers:
        #     lvl_idx = headers.index("Level")
        #     header.setSectionResizeMode(lvl_idx, QHeaderView.ResizeToContents)
        #     # self.live_table.setColumnWidth(lvl_idx, 50)
        # if "Rank" in headers:
        #     rank_idx = headers.index("Rank")
        #     header.setSectionResizeMode(rank_idx, QHeaderView.ResizeToContents)
        #     # self.live_table.setColumnWidth(rank_idx, 50)

    def update_recently_met_table(self, players):
        headers = [
            "Player",
            "Ally/Enemy",
            "Previous Agent",
            "Current Agent",
            "Last Met",
            "Times Met",
        ]
        self.recent_table.clear()
        self.recent_table.setColumnCount(len(headers))
        self.recent_table.setHorizontalHeaderLabels(headers)
        self.recent_table.setRowCount(len(players))

        for row_idx, player in enumerate(players):
            values = [
                player.get("player", ""),
                player.get("team", ""),
                player.get("previous_agent", ""),
                player.get("current_agent", ""),
                player.get("last_met", ""),
                str(player.get("times", "")),
            ]
            for col_idx, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.recent_table.setItem(row_idx, col_idx, item)

        header = self.recent_table.horizontalHeader()
        for idx in range(len(headers)):
            header.setSectionResizeMode(idx, QHeaderView.ResizeToContents)
        if "Player" in headers:
            header.setSectionResizeMode(headers.index("Player"), QHeaderView.Stretch)

    def update_loading_status(self, is_loading, message):
        if is_loading:
            self.loading_dialog.setLabelText(message or "Loading player stats...")
            self.loading_dialog.show()
        else:
            self.loading_dialog.hide()

    def open_settings(self):
        config = self.load_config()
        dialog = SettingsDialog(config, self)
        if dialog.exec_() == QDialog.Accepted:
            new_config = dialog.get_config()
            self.save_config(new_config)
            self.worker.queue_config_update(new_config)
            self.worker.refresh_event.set()

    def load_config(self):
        config = {}
        if os.path.exists("config.json"):
            try:
                with open("config.json", "r") as file:
                    config = json.load(file)
            except json.JSONDecodeError:
                config = {}

        merged_config = copy.deepcopy(DEFAULT_CONFIG)
        merged_config.update(config)
        merged_config["table"] = DEFAULT_CONFIG["table"] | config.get("table", {})
        merged_config["flags"] = DEFAULT_CONFIG["flags"] | config.get("flags", {})
        return merged_config

    def save_config(self, config):
        with open("config.json", "w") as file:
            json.dump(config, file, indent=4)

    def manual_refresh(self):
        self.worker.refresh_event.set()

def main_gui():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    try:
        main_gui()
    except KeyboardInterrupt:
        # lame implementation of fast ctrl+c exit
        os._exit(0)
    except:
        log(traceback.format_exc())
        print(
            colr(
                "The program has encountered an error. If the problem persists, please reach support"
                f" with the logs found in {os.getcwd()}/logs",
                fore=(255, 0, 0),
            )
        )
        os._exit(1)
