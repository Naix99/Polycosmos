from __future__ import annotations
import atexit
import os
import sys
import asyncio
import random
import shutil
from typing import Tuple, List, Iterable, Dict
import threading
import importlib.util

import websockets
import copy
import Utils
import json
import logging
import pathlib

from NetUtils import NetworkItem, ClientStatus
from CommonClient import gui_enabled, logger, get_base_parser, ClientCommandProcessor, \
    CommonContext, server_loop
import settings
import os
import ssl


# --------------------- Styx Scribe useful globals -----------------
global subsume

styx_scribe_recieve_prefix = "Polycosmos to Client:"
styx_scribe_send_prefix = "Client to Polycosmos:"     

# --------------------- Styx Scribe useful globals -----------------


# Here we implement methods for the client

class HadesClientCommandProcessor(ClientCommandProcessor):
    def _cmd_resync(self):
        """Manually trigger a resync."""
        self.output(f"Syncing items.")
        self.ctx.syncing = True


class HadesContext(CommonContext):
    # ----------------- Client start up and ending section starts  --------------------------------
    command_processor = HadesClientCommandProcessor
    game = "Hades"
    items_handling = 0b111  # full remote
    cache_items_received_names = []
    hades_slot_data = None
    players_id_to_name = None
    creating_location_to_item_mapping = False
    missing_locations_cache = []
    checked_locations_cache = []
    location_name_to_id = None
    locations_received_names = []
    location_to_item_map_created = False
    deathlink_pending = False
    deathlink_enabled = False
    is_connected = False
    is_receiving_items_from_connect_package = False
    acculumated_score = 0

    dictionary_filler_items = {
        "Darkness": 0,
        "Keys": 0,
        "Gemstones": 0,
        "Diamonds": 0,
        "TitanBlood": 0,
        "Nectar": 0,
        "Ambrosia": 0,
    }

    def __init__(self, server_address, password):
        super(HadesContext, self).__init__(server_address, password)
        self.send_index: int = 0
        self.syncing = False
        self.awaiting_bridge = False
        # Load here any data you might need the client to know about

        # Add hook to comunicate with StyxScribe
        subsume.AddHook(self.send_location_check_to_server, styx_scribe_recieve_prefix + "Locations updated:",
                        "HadesClient")
        subsume.AddHook(self.on_game_completion, styx_scribe_recieve_prefix + "Hades defeated", "HadesClient")
        subsume.AddHook(self.check_connection_and_send_items_and_request_starting_info,
                        styx_scribe_recieve_prefix + "Data requested", "HadesClient")
        # Add hook to delete filler items once obtained so they are not triggered more than once
        subsume.AddHook(self.on_filler_item_recieved_signal, styx_scribe_recieve_prefix + "Got filler item:",
                        "HadesClient")
        # hook to send deathlink to other player when Zag dies
        subsume.AddHook(self.send_death, styx_scribe_recieve_prefix + "Zag died", "HadesClient")
        # hook for the score based system
        subsume.AddHook(self.update_internal_score, styx_scribe_recieve_prefix + "ScoreUpdate:", "HadesClient")

    async def server_auth(self, password_requested: bool = False):
        # This is called to autentificate with the server.
        if password_requested and not self.password:
            await super(HadesContext, self).server_auth(password_requested)
        await self.get_username()
        await self.send_connect()

    async def connection_closed(self):
        # This is called when the connection is closed (duh!)
        # This will send the message always, but only process by Styx scribe if actually in game
        subsume.Send(styx_scribe_send_prefix + "Connection Error")
        self.is_connected = False
        self.is_receiving_items_from_connect_package = False
        await super(HadesContext, self).connection_closed()

    # Do not touch this
    @property
    def endpoints(self):
        if self.server:
            return [self.server]
        else:
            return []

    async def shutdown(self):
        # What is called when the app gets shutdown
        subsume.close()
        await super(HadesContext, self).shutdown()

    # ----------------- Client start up and ending section end  --------------------------------

    # ----------------- Package Management section starts --------------------------------

    def on_package(self, cmd: str, args: dict):
        # This is what is done when a package arrives.
        if cmd in {"Connected"}:
            # What should be done in a connection package
            self.cache_items_received_names.clear()
            self.missing_locations_cache = args['missing_locations']
            self.checked_locations_cache = args['checked_locations']
            self.hades_slot_data = args['slot_data']
            self.location_name_to_id = {name: idnumber for idnumber, name, in self.location_names.items()}
            if 'death_link' in self.hades_slot_data and self.hades_slot_data['death_link']:
                asyncio.create_task(self.update_death_link(True))
                self.deathlink_enabled = True
            self.is_connected = True
            self.is_receiving_items_from_connect_package = True #ADD SCORE RELATED THING IN THIS REQUEST
            asyncio.create_task(self.send_msgs([{"cmd": "Get", "keys": ["hades:" + str(self.slot) + ":filler:Darkness",
                                                                        "hades:" + str(self.slot) + ":filler:Keys",
                                                                        "hades:" + str(self.slot) + ":filler:Gemstones",
                                                                        "hades:" + str(self.slot) + ":filler:Diamonds",
                                                                        "hades:" + str(
                                                                            self.slot) + ":filler:TitanBlood",
                                                                        "hades:" + str(self.slot) + ":filler:Nectar",
                                                                        "hades:" + str(self.slot) + ":filler:Ambrosia"]}]))

        if cmd in {"RoomInfo"}:
            # What should be done when room info is sent.
            self.seed_name = args['seed_name']

        if cmd in {"ReceivedItems"}:
            # What should be done when an Item is recieved.
            # NOTE THIS GETS ALL ITEMS THAT HAVE BEEN RECIEVED! WE USE THIS FOR RESYNCS!
            for item in args['items']:
                self.cache_items_received_names += [self.item_names[item.item]]
            msg = f"Received {', '.join([self.item_names[item.item] for item in args['items']])}"
            # We ignore sending the package to hades if just connected, since the game it not ready for it (and will request it itself later)
            if (self.is_receiving_items_from_connect_package):
                return;
            self.send_items()

        if cmd in {"LocationInfo"}:
            if self.creating_location_to_item_mapping:
                self.creating_location_to_item_mapping = False
                self.create_location_to_item_dictionary(args['locations'])
                return
            super().on_package(cmd, args)

        if cmd in {"Retrieved"}:
            self.update_filler_items_information(args)
            self.update_score_information(args)
            
        if cmd in {"Bounced"}:
            if "tags" in args:
                if "DeathLink" in args["tags"]:
                    self.on_deathlink(args["data"])

    def update_filler_items_information(self, args : dict):
        if "hades:" + str(self.slot) + ":filler:Darkness" in args["keys"]:
            if args["keys"]["hades:" + str(self.slot) + ":filler:Darkness"] is not None:
                self.dictionary_filler_items["Darkness"] = args["keys"][
                    "hades:" + str(self.slot) + ":filler:Darkness"]
        if "hades:" + str(self.slot) + ":filler:Keys" in args["keys"]:
            if args["keys"]["hades:" + str(self.slot) + ":filler:Keys"] is not None:
                self.dictionary_filler_items["Keys"] = args["keys"]["hades:" + str(self.slot) + ":filler:Keys"]
        if "hades:" + str(self.slot) + ":filler:Gemstones" in args["keys"]:
            if args["keys"]["hades:" + str(self.slot) + ":filler:Gemstones"] is not None:
                self.dictionary_filler_items["Gemstones"] = args["keys"][
                    "hades:" + str(self.slot) + ":filler:Gemstones"]
        if "hades:" + str(self.slot) + ":filler:Diamonds" in args["keys"]:
            if args["keys"]["hades:" + str(self.slot) + ":filler:Diamonds"] is not None:
                self.dictionary_filler_items["Diamonds"] = args["keys"][
                    "hades:" + str(self.slot) + ":filler:Diamonds"]
        if "hades:" + str(self.slot) + ":filler:TitanBlood" in args["keys"]:
            if args["keys"]["hades:" + str(self.slot) + ":filler:TitanBlood"] is not None:
                 self.dictionary_filler_items["TitanBlood"] = args["keys"][
                     "hades:" + str(self.slot) + ":filler:TitanBlood"]
        if "hades:" + str(self.slot) + ":filler:Nectar" in args["keys"]:
            if args["keys"]["hades:" + str(self.slot) + ":filler:Nectar"] is not None:
                self.dictionary_filler_items["Nectar"] = args["keys"]["hades:" + str(self.slot) + ":filler:Nectar"]
        if "hades:" + str(self.slot) + ":filler:Ambrosia" in args["keys"]:
            if args["keys"]["hades:" + str(self.slot) + ":filler:Ambrosia"] is not None:
                self.dictionary_filler_items["Ambrosia"] = args["keys"][
                     "hades:" + str(self.slot) + ":filler:Ambrosia"]
    
    def update_score_information(self, args : dict):
        if "hades:" + str(self.slot) + ":score" in args["keys"]:
            score = 0
            if args["keys"]["hades:" + str(self.slot) + ":score"] is not None:
                score = int(args["keys"]["hades:" + str(self.slot) + ":score"])
            subsume.Modules.StyxScribeShared.Root.Score = score
        
        if "hades:" + str(self.slot) + ":last_score_check" in args["keys"]:
            last_score=1
            if args["keys"]["hades:" + str(self.slot) + ":last_score_check"] is not None:
                last_score = int(args["keys"]["hades:" + str(self.slot) + ":last_score_check"])
            subsume.Modules.StyxScribeShared.Root.LastScoreCheck = last_score
        
        if "hades:" + str(self.slot) + ":last_room_completed" in args["keys"]:
            last_room=0
            if args["keys"]["hades:" + str(self.slot) + ":last_room_completed"] is not None:
                last_room = int(args["keys"]["hades:" + str(self.slot) + ":last_room_completed"])
            subsume.Modules.StyxScribeShared.Root.LastRoomComplete = last_room
        
    def request_stored_score_info(self):
        #request score
        payload =[{"cmd": "Get", "keys": ["hades:" + str(self.slot) + ":score","hades:" + str(self.slot) + ":last_score_check",
                                                                      "hades:" + str(self.slot) + ":last_room_completed"]}]
        asyncio.create_task(self.send_msgs(payload))        

    def send_items(self):
        # we filter the filler items according to how many we have recieved and send that payload
        payload_message = self.parse_array_to_string(self.filter_filler_items_from_cache())
        subsume.Send(styx_scribe_send_prefix + "Items Updated:" + payload_message)

    def parse_array_to_string(self, array_of_items):
        message = ""
        for itemname in array_of_items:
            message += itemname
            message += ","
        return message

    async def send_location_check_to_server(self, message):
        sendingLocationsId = []
        sendingLocationsName = message
        payload_message = []
        sendingLocationsId += [self.location_name_to_id[sendingLocationsName]]
        payload_message += [{"cmd": 'LocationChecks', "locations": sendingLocationsId}]
        if (self.hades_slot_data['location_system']==2):  
            last_score = int(message.split("Score")[1])
            payload_message += [{"cmd": "Set", "key": "hades:" + str(self.slot) + ":last_score_check", 
                               "want_reply": False, "default": 0, "operations": [{"operation": "replace", "value": last_score}]}]
        asyncio.create_task(self.send_msgs(payload_message))
        
    async def update_internal_score(self, message):        
        separatedMessage = message.split("-")
        score = int(separatedMessage[0])
        last_room = int(separatedMessage[1])
        payload = [{"cmd": "Set", "key": "hades:" + str(self.slot) + ":score",
                               "want_reply": False, "default": 0, "operations": [{"operation": "add", "value": score}]}]
        payload += [{"cmd": "Set", "key": "hades:" + str(self.slot) + ":last_room_completed",
                               "want_reply": False, "default": 0, "operations": [{"operation": "add", "value": last_room}]}]
        asyncio.create_task(self.send_msgs(payload))       


    async def check_connection_and_send_items_and_request_starting_info(self, message):
        if (self.check_for_connection()):
            self.is_receiving_items_from_connect_package = False
            await self.send_items_and_request_starting_info(message)

    async def send_items_and_request_starting_info(self, message):
        # Construct location to item mapping
        self.locations_received_names = []
        for location in self.checked_locations_cache:
            self.locations_received_names += [self.location_names[location]]
        subsume.Modules.StyxScribeShared.Root["LocationsUnlocked"] = self.locations_received_names

        self.store_settings_data()
        self.request_stored_score_info()
        self.request_location_to_item_dictionary()
        # send items that were already cached in connect
        self.send_items()

    def store_settings_data(self):
        heat_dictionary = {
            'HardLaborPactLevel': self.hades_slot_data['hard_labor_pact_amount'],
            'LastingConsequencesPactLevel': self.hades_slot_data['lasting_consequences_pact_amount'],
            'ConvenienceFeePactLevel': self.hades_slot_data['convenience_fee_pact_amount'],
            'JurySummonsPactLevel': self.hades_slot_data['jury_summons_pact_amount'],
            'ExtremeMeasuresPactLevel': self.hades_slot_data['extreme_measures_pact_amount'],
            'CalisthenicsProgramPactLevel': self.hades_slot_data['calisthenics_program_pact_amount'],
            'BenefitsPackagePactLevel': self.hades_slot_data['benefits_package_pact_amount'],
            'MiddleManagementPactLevel': self.hades_slot_data['middle_management_pact_amount'],
            'UnderworldCustomsPactLevel': self.hades_slot_data['underworld_customs_pact_amount'],
            'ForcedOvertimePactLevel': self.hades_slot_data['forced_overtime_pact_amount'],
            'HeightenedSecurityPactLevel': self.hades_slot_data['heightened_security_pact_amount'],
            'RoutineInspectionPactLevel': self.hades_slot_data['routine_inspection_pact_amount'],
            'DamageControlPactLevel': self.hades_slot_data['damage_control_pact_amount'],
            'ApprovalProcessPactLevel': self.hades_slot_data['approval_process_pact_amount'],
            'TightDeadlinePactLevel': self.hades_slot_data['tight_deadline_pact_amount'],
            'PersonalLiabilityPactLevel': self.hades_slot_data['personal_liability_pact_amount'],
        }
        subsume.Modules.StyxScribeShared.Root["HeatSettings"] = heat_dictionary
        filler_dictionary = {
            'DarknessPackValue': self.hades_slot_data['darkness_pack_value'],
            'KeysPackValue': self.hades_slot_data['keys_pack_value'],
            'GemstonesPackValue': self.hades_slot_data['gemstones_pack_value'],
            'DiamondsPackValue': self.hades_slot_data['diamonds_pack_value'],
            'TitanBloodPackValue': self.hades_slot_data['titan_blood_pack_value'],
            'NectarPackValue': self.hades_slot_data['nectar_pack_value'],
            'AmbrosiaPackValue': self.hades_slot_data['ambrosia_pack_value'],
        }
        subsume.Modules.StyxScribeShared.Root["FillerValues"] = filler_dictionary
        game_settings = {
            'LocationMode': self.hades_slot_data['location_system'],
            'ReverseOrderEM': self.hades_slot_data['reverse_order_em'],
        }
        subsume.Modules.StyxScribeShared.Root["GameSettings"] = game_settings

        # construct here any other dictionary with settings the main game should know about

    def request_location_to_item_dictionary(self):
        self.creating_location_to_item_mapping = True
        request = self.missing_locations_cache + self.checked_locations_cache
        asyncio.create_task(self.send_msgs([{"cmd": "LocationScouts", "locations": request, "create_as_hint": 0}]))

    def create_location_to_item_dictionary(self, itemsdict):
        itemmap = {}
        subsume.Modules.StyxScribeShared.Root["LocationToItemMap"] = {}
        for networkitem in itemsdict:
            subsume.Modules.StyxScribeShared.Root["LocationToItemMap"][self.location_names[networkitem.location]] = self.player_names[networkitem.player] + "-" + \
                                                                 self.item_names[networkitem.item]
        self.creating_location_to_item_dictionary = False
        subsume.Send(styx_scribe_send_prefix + "Data package finished")

    # ----------------- Package Management section ends --------------------------------

    # ------------------ Section for dealing with filler items not getting obtianed more than once ------------

    async def on_filler_item_recieved_signal(self, message):
        self.dictionary_filler_items[message] = self.dictionary_filler_items[message] + 1
        await self.send_msgs([{"cmd": "Set", "key": "hades:" + str(self.slot) + ":filler:" + message,
                               "want_reply": False, "default": 0, "operations": [{"operation": "add", "value": 1}]}])

    def filter_filler_items_from_cache(self):
        filtered_cache = copy.deepcopy(self.cache_items_received_names)
        for key in self.dictionary_filler_items:
            for i in range(0, self.dictionary_filler_items[key]):
                if key in filtered_cache:
                    filtered_cache.remove(key)
        return filtered_cache

    # ----------------- Filler items section ended --------------------------------

    # -------------deathlink section started --------------------------------
    def on_deathlink(self, data: dict):
        # What should be done when a deathlink message is recieved
        if (self.deathlink_pending):
            return
        self.deathlink_pending = True
        subsume.Send(styx_scribe_send_prefix + "Deathlink recieved")
        super().on_deathlink(data)
        asyncio.create_task(self.wait_and_lower_deathlink_flag())

    def send_death(self, death_text: str = ""):
        # What should be done to send a death link
        # Avoid sending death if we died from a deathlink
        if (self.deathlink_enabled == False or self.deathlink_pending == True):
            return
        asyncio.create_task(super().send_death(death_text))

    async def wait_and_lower_deathlink_flag(self):
        await asyncio.sleep(3)
        self.deathlink_pending = False

    # -------------deathlink section ended

    # -------------game completion section starts
    # this is to detect game completion. Note that on futher updates this will need --------------------------------
    # to be changed to adapt to new game completion conditions
    def on_game_completion(self, message):
        asyncio.create_task(self.send_msgs([{"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}]))
        self.finished_game = True

    # -------------game completion section ended --------------------------------

    # ------------ game connection QoL handling
    def check_for_connection(self):
        if (self.is_connected == False):
            subsume.Send(styx_scribe_send_prefix + "Connection Error")
            return False
        return True

    # ------------ gui section ------------------------------------------------

    def run_gui(self):
        import kvui
        from kvui import GameManager

        class HadesManager(GameManager):
            # logging_pairs for any separate logging tabs
            base_title = "Archipelago Hades Client"

        self.ui = HadesManager(self)
        self.ui_task = asyncio.create_task(self.ui.async_run(), name="UI")

def print_error_and_close(msg):
    logger.error("Error: " + msg)
    Utils.messagebox("Error", msg, error=True)
    sys.exit(1)


#  ------------ Methods to start the client + Hades + StyxScribe ------------

def launch_hades():
    subsume.Launch(True, None)


def launch():
    async def main(args):
        ctx = HadesContext(args.connect, args.password)
        ctx.server_task = asyncio.create_task(server_loop(ctx), name="server loop")
        if gui_enabled:
            ctx.run_gui()
        ctx.run_cli()

        await ctx.exit_event.wait()
        ctx.server_address = None

        await ctx.shutdown()


    import colorama
    # --------------------- Styx Scribe initialization -----------------
    styx_scribe_path = settings.get_settings()["hades_options"]["styx_scribe_path"]
    hadespath = os.path.dirname(styx_scribe_path)

    spec = importlib.util.spec_from_file_location("StyxScribe", str(styx_scribe_path))
    styx_scribe = importlib.util.module_from_spec(spec)
    sys.modules["StyxScribe"] = styx_scribe
    spec.loader.exec_module(styx_scribe)

    global subsume
    subsume = styx_scribe.StyxScribe("Hades")
    # hack to make it work without chdir
    subsume.proxy_purepaths = {
        None: hadespath / subsume.executable_cwd_purepath / styx_scribe.LUA_PROXY_STDIN,
        False: hadespath / subsume.executable_cwd_purepath / styx_scribe.LUA_PROXY_FALSE,
        True: hadespath / subsume.executable_cwd_purepath / styx_scribe.LUA_PROXY_TRUE
    }
    subsume.args[0] = os.path.normpath(os.path.join(hadespath, subsume.args[0]))
    subsume.executable_purepath = pathlib.PurePath(hadespath, subsume.executable_purepath)
    for i in range(len(subsume.plugins_paths)):
        subsume.plugins_paths[i] = pathlib.PurePath(hadespath, subsume.plugins_paths[i])

    subsume.LoadPlugins()
    # --------------------- Styx Scribe initialization -----------------

    thr = threading.Thread(target=launch_hades, args=(), kwargs={})
    thr.start()
    parser = get_base_parser()
    args = parser.parse_args()
    colorama.init()
    asyncio.run(main(args))
    colorama.deinit()
