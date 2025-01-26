import copy
import json
import logging
import os
import socket
import ssl
import sys
import threading
import time
import uuid
from pprint import pprint

import customtkinter as ctk
from PIL import Image

from main import ConnectionToServer, recvall, HEADER_SIZE, SERVER_PORT, SERVER_IP, DataPacketState, ResponseState, \
    RequestState, GameWaitingState

ctk.set_default_color_theme(r"resources/ui_theme.json")

logging.basicConfig(format='%(levelname)s - %(message)s', level=logging.DEBUG)

BACK_ICON = "\U0001F878"
HAMBURGER_ICON = "☰"


class Client:
    def __init__(self):
        self.connection: ConnectionToServer = ...
        self.uuid: str = ...
        self.connection_token: str = str(uuid.uuid4())
        self.controller = Controller(self)
        self.controller.gui.after(100, self.connect_to_server)
        # self.controller.gui.after(
        #     100, lambda: threading.Thread(target=self.controller.ui_connection_established).start())
        self.controller.gui.mainloop()
        threading.Thread(target=self.server_thread).start()

    def connect_to_server(self):
        ssl_context: ssl.SSLContext = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.VerifyMode.CERT_OPTIONAL
        server_socket: ssl.SSLSocket = ssl_context.wrap_socket(socket.socket(), server_hostname=SERVER_IP)
        try:
            server_socket.connect((socket.gethostname(), SERVER_PORT))
        except ConnectionRefusedError as e:
            logging.warning("Could not connect to server " + str(e))
            sys.exit()
        self.connection: ConnectionToServer = ConnectionToServer(server_socket, f"{SERVER_IP}:{SERVER_PORT}")
        logging.info("Established connection to server.")
        encoded_token = self.connection_token.encode()
        self.connection.socket.sendall(f"{len(encoded_token):<8}".encode() + encoded_token)

        threading.Thread(target=self.controller.ui_connection_established).start()

        # Start handling incoming packets.
        threading.Thread(target=self.server_thread).start()

    def server_thread(self):
        while True:
            try:
                header: bytes = recvall(self.connection.socket, HEADER_SIZE)
                if not len(header):
                    break
                packet: dict = json.loads(recvall(self.connection.socket, int(header.decode())))
                threading.Thread(target=lambda: self.handle_packet(packet)).start()
            except Exception as e:
                logging.error(f"Unexpected Error: {e}")
                break
        logging.warning("Lost connection to server.")
        self.connection.socket.close()

    def handle_packet(self, packet: dict) -> None:

        # Handle request.
        if packet["state"].startswith("RQ"):
            match packet["state"]:
                case _:
                    logging.warning("Received invalid request.")
                    result = ResponseState.INVALID_REQUEST, ""
            self.connection.respond(result[0], result[1])
            logging.info(f"Responded to server with {result[0]}.")

        # Handle response.
        elif packet["state"].startswith("RS"):
            match packet["state"]:
                case ResponseState.UUID.value:
                    self.uuid: str = packet["data"]
                    logging.info(f"Received UUID ({self.uuid}).")
                case ResponseState.ALREADY_IN_GAME.value:
                    logging.warning("Already in a game.")
                case ResponseState.CREATE_GAME_SUCCESS.value:
                    self.controller.response_create_game_success(packet["data"])
                case ResponseState.CREATE_GAME_FAILED.value:
                    self.controller.response_create_game_failed()
                case ResponseState.JOIN_GAME_SUCCESS.value:
                    self.controller.response_join_game_success(packet["data"])
                case ResponseState.JOIN_GAME_FAILED.value:
                    self.controller.response_join_game_failed()
                case ResponseState.START_GAME_SUCCESS.value:
                    logging.info("Game started successfully.")
                case ResponseState.START_GAME_FAILED.value:
                    logging.warning(f"Failed to start game: {packet["data"]}.")
                case ResponseState.INVALID_REQUEST.value:
                    logging.warning("Request sent was invalid.")
                case _:
                    logging.warning(f"Received invalid response ({packet}).")
            self.connection.has_responded = True

        # Handle data packet.
        elif packet["state"].startswith("DP"):
            match packet["state"]:
                case DataPacketState.GAME_DATA.value:
                    self.controller.process_game_data(packet["data"])

                case DataPacketState.UUID.value:
                    self.uuid: str = packet["data"]
                    logging.info(f"Received UUID ({self.uuid}).")

                case _:
                    logging.warning("Received invalid data packet.")

        # Handle invalid packet.
        else:
            logging.warning("Received invalid packet.")


class Controller:
    def __init__(self, client: Client):
        self.cached_data = {
            "game_data": {},
            "users": {
                # "a": {
                #     "username": "a",
                #     "games_played": 0,
                #     "games_won": 0,
                #     "games_lost": 0,
                #     "lifetime_score": 0
                # },
                # "b": {
                #     "username": "b",
                #     "games_played": 0,
                #     "games_won": 0,
                #     "games_lost": 0,
                #     "lifetime_score": 0
                # }
            }
        }
        self.client: Client = client
        self.gui = GUI(self)

    def close(self):
        self.gui.destroy()
        os._exit(1)

    def response_create_game_success(self, data):
        self.cached_data["game_data"] = data
        self.set_ui_page(self.gui.lobby_page)
        self.gui.lobby_page.host_code_label.configure(text="Code: " + self.cached_data["game_data"]["code"])
        self.gui.lobby_page.bottom_host_bar.grid()
        self.set_ui_hamburger_icon()

    def response_create_game_failed(self):
        self.gui.info_message_box.configure(text="Failed to create game.", text_color="#b41c2b")
        self.gui.info_message_box.grid()
        threading.Thread(target=self.ui_remove_info_box).start()

    def response_join_game_success(self, data):
        self.cached_data["game_data"] = data
        self.set_ui_page(self.gui.lobby_page)
        self.gui.lobby_page.player_code_label.configure(text="Code: " + self.cached_data["game_data"]["code"])
        self.gui.lobby_page.bottom_player_bar.grid()
        self.set_ui_hamburger_icon()

    def response_join_game_failed(self):
        self.gui.info_message_box.configure(text="Failed to join game.", text_color="#b41c2b")
        self.gui.info_message_box.grid()
        threading.Thread(target=self.ui_remove_info_box).start()

    def process_game_data(self, data):
        pprint(data)
        self.cached_data["game_data"] = data
        for player in data["players"].items():
            self.cached_data["users"][player[0]] = {
                "username": player[1]["username"]
            }
        if data["started"]:
            if self.gui.page != self.gui.game_page:
                self.set_ui_page(self.gui.game_page)
            self.ui_set_game_players()
            self.ui_display_game_players()
            self.ui_display_player_cards()
            self.ui_display_pile()
        match data["waiting_for"][1]:
            case GameWaitingState.GAME_START.value:
                self.gui.message_bar.configure(text=f"Waiting for {
                    self.cached_data["users"][data["waiting_for"][0]]["username"]} to start the game.")
                self.gui.message_bar.grid()
                self.ui_display_lobby_players()
            case GameWaitingState.ROUND_START.value:
                self.gui.message_bar.configure(text=f"Waiting for {
                    self.cached_data["users"][data["waiting_for"][0]]["username"]} to start the round.")
                self.gui.message_bar.grid()
            case GameWaitingState.PREDICTION.value:
                self.gui.message_bar.configure(text=f"Waiting for {
                    self.cached_data["users"][data["waiting_for"][0]]["username"]} to make their prediction.")
                self.gui.message_bar.grid()
            case GameWaitingState.PLACE_CARD.value:
                self.gui.message_bar.configure(text=f"Waiting for {
                    self.cached_data["users"][data["waiting_for"][0]]["username"]} to place their card.")
                self.gui.message_bar.grid()
            case GameWaitingState.MIN_PLAYERS.value:
                self.gui.message_bar.configure(text="")
                self.gui.message_bar.grid()
                self.ui_display_lobby_players()
            case GameWaitingState.GAME_END.value:
                self.gui.message_bar.configure(text=f"Waiting for {
                    self.cached_data["users"][data["waiting_for"][0]]["username"]} to end the game.")
                self.gui.message_bar.grid()
            case GameWaitingState.NONE.value:
                self.gui.message_bar.configure(text="")
                self.gui.message_bar.grid_remove()
        if len(data["players"]) > 2:
            self.gui.lobby_page.start_button.configure(state="normal")

    def ui_display_lobby_players(self):
        i = -1
        for i, player in enumerate(self.cached_data["game_data"]["initial_player_order"]):
            self.gui.lobby_page.users[i].configure(text=self.cached_data["users"][player]["username"])
            self.gui.lobby_page.users[i].grid()
        for x in range(i + 1, 7):
            self.gui.lobby_page.users[x].grid_remove()

    def ui_set_game_players(self):
        players = copy.deepcopy(self.cached_data["game_data"]["initial_player_order"])
        players.remove(self.client.uuid)
        match len(players):
            case 2:
                self.gui.game_page.active_users[players[0]] = 1
                self.gui.game_page.active_users[players[1]] = 4
            case 3:
                self.gui.game_page.active_users[players[0]] = 1
                self.gui.game_page.active_users[players[1]] = 6
                self.gui.game_page.active_users[players[2]] = 4
            case 4:
                self.gui.game_page.active_users[players[0]] = 0
                self.gui.game_page.active_users[players[1]] = 2
                self.gui.game_page.active_users[players[2]] = 3
                self.gui.game_page.active_users[players[3]] = 5
            case 5:
                self.gui.game_page.active_users[players[0]] = 0
                self.gui.game_page.active_users[players[1]] = 1
                self.gui.game_page.active_users[players[2]] = 6
                self.gui.game_page.active_users[players[3]] = 4
                self.gui.game_page.active_users[players[4]] = 5
            case 6:
                self.gui.game_page.active_users[players[0]] = 0
                self.gui.game_page.active_users[players[1]] = 1
                self.gui.game_page.active_users[players[2]] = 2
                self.gui.game_page.active_users[players[3]] = 3
                self.gui.game_page.active_users[players[4]] = 4
                self.gui.game_page.active_users[players[5]] = 5

    def ui_display_game_players(self):
        for player in self.gui.game_page.active_users.items():
            player_data = self.cached_data["game_data"]["players"][player[0]]["rounds"][
                f"{self.cached_data["game_data"]["round_number"]}"]
            self.gui.game_page.users[player[1]].username_label.configure(
                text=self.cached_data["users"][player[0]]["username"])
            self.gui.game_page.users[player[1]].cards_left_label.configure(
                text=f"Cards Left: {player_data["cards_left"]}")
            self.gui.game_page.users[player[1]].prediction_label.configure(
                text=f"Prediction: {player_data["prediction"]}")
            self.gui.game_page.users[player[1]].tricks_won_label.configure(
                text=f"Tricks Won: {player_data["tricks_won"]}")
            self.gui.game_page.users[player[1]].grid()

    def ui_display_pile(self):
        if self.cached_data["game_data"]["pile"]:
            card = self.cached_data["game_data"]["pile"][0]
            self.gui.game_page.top_card.configure(
                text=self.cached_data["users"][card["player"]]["username"],
                image=ctk.CTkImage(Image.open(
                    f"resources/images/cards/{card["value"]}{card["suit"]}.png"), size=(230, 350)))
        else:
            self.gui.game_page.top_card.configure(
                text="", image=ctk.CTkImage(Image.open(f"resources/images/cards/Placeholder.png"), size=(230, 350)))
        i = -1
        for i, card in enumerate(self.cached_data["game_data"]["pile"], 1):
            self.gui.game_page.previous_cards[i].configure(
                text=self.cached_data["users"][card["player"]]["username"],
                image=ctk.CTkImage(Image.open(
                    f"resources/images/cards/{card["value"]}{card["suit"]}.png"), size=(100, 150)))
            self.gui.game_page.previous_cards[i].grid()
        for x in range(i + 1, 6):
            self.gui.game_page.previous_cards[x].configure(
                text="", image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)))
            self.gui.game_page.previous_cards[x].grid()

    def ui_display_player_cards(self):
        def sort_key(c: dict):
            return c["suit"], int(c["value"])

        i = -1
        hand = sorted(
            self.cached_data["game_data"]["players"][self.client.uuid]["rounds"][
                f"{self.cached_data["game_data"]["round_number"]}"]["hand"], key=sort_key)
        for i, card in enumerate(hand):
            self.gui.game_page.player_cards[i].configure(
                image=ctk.CTkImage(Image.open(
                    f"resources/images/cards/{card["value"]}{card["suit"]}.png"), size=(100, 150)))
            self.gui.game_page.player_cards[i].grid()
        for x in range(i + 1, 17):
            self.gui.game_page.player_cards[x].configure(
                image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)))
            self.gui.game_page.player_cards[x].grid()

    def ui_remove_info_box(self):
        time.sleep(5)
        self.gui.info_message_box.grid_remove()

    def ui_connection_established(self):
        time.sleep(1.5)
        self.gui.loading_page.loading_label.configure(text="Connection established.", text_color="#009f42")
        self.gui.loading_page.loading_bar.configure(mode="determinate")
        self.gui.loading_page.loading_bar.set(0)
        time.sleep(1)
        self.gui.page.grid_remove()
        self.gui.page = self.gui.menu_page
        self.gui.page.grid(row=0, column=0, sticky="nesw", rowspan=10, columnspan=10)

    def ui_corner_icon(self):
        self.gui.logo_label.focus_set()
        match self.gui.corner_icon_tracker:
            case "back":
                self.gui.page.grid_remove()
                self.gui.page = self.gui.page_tracker.pop()
                self.gui.page.grid(row=0, column=0, sticky="nesw", rowspan=10, columnspan=10)
                self.gui.corner_icon_tracker = ""
                self.gui.corner_icon.grid_remove()
            case "hamburger":
                pass
            case _:
                pass

    def ui_theme_toggle(self):
        if self.gui.menu_page.theme_switch.get():
            ctk.set_appearance_mode("light")
        else:
            ctk.set_appearance_mode("dark")

    def ui_play_button(self):
        self.set_ui_page(self.gui.play_button_page)
        self.set_ui_back_icon()

    def ui_social_button(self):
        self.set_ui_page(self.gui.social_page)
        self.set_ui_back_icon()

    def set_ui_page(self, page):
        self.gui.page_tracker.append(self.gui.page)
        self.gui.page.grid_remove()
        self.gui.page = page
        self.gui.page.grid(row=0, column=0, sticky="nesw", rowspan=10, columnspan=10)

    def set_ui_back_icon(self):
        self.gui.corner_icon.configure(text=BACK_ICON)
        self.gui.corner_icon_tracker = "back"
        self.gui.corner_icon.grid()

    def set_ui_hamburger_icon(self):
        self.gui.corner_icon.configure(text=HAMBURGER_ICON)
        self.gui.corner_icon_tracker = "hamburger"
        self.gui.corner_icon.grid()

    def ui_create_game_button(self):
        self.client.connection.request(RequestState.NEW_GAME, "")

    def ui_join_game_button(self):
        code = self.gui.play_button_page.code_var.get()
        self.client.connection.request(RequestState.GAME_JOIN, code)

    def ui_code_entry_validation(self, *args):
        code = self.gui.play_button_page.code_var.get()
        if code != "Enter Code":
            code = "".join(filter(str.isalpha, code.upper()))[:6]
        self.gui.play_button_page.code_var.set(code)

    def ui_code_entry_set_placeholder(self, event=None):
        if not self.gui.play_button_page.code_var.get():
            self.gui.play_button_page.code_var.set("Enter Code")
            self.gui.play_button_page.code_entry.configure(text_color="#919191")

    def ui_code_entry_clear_placeholder(self, event=None):
        if self.gui.play_button_page.code_var.get() == "Enter Code":
            self.gui.play_button_page.code_var.set("")
            self.gui.play_button_page.code_entry.configure(text_color="#ffffff")

    def ui_trumps_entry_validation(self, *args):
        text = self.gui.lobby_page.custom_trumps_var.get()
        text = "".join(filter({"H", "C", "D", "S", "-"}.__contains__, text.upper()))[:17]
        self.gui.lobby_page.custom_trumps_var.set(text)

    def ui_toggle_trumps_entry(self):
        if self.gui.lobby_page.custom_trumps_switch.get():
            self.gui.lobby_page.custom_trumps_entry.configure(state="normal", text_color="#ffffff")
        else:
            self.gui.lobby_page.custom_trumps_entry.configure(state="disabled", text_color="#919191")

    def ui_start_game_button(self):
        if self.gui.lobby_page.custom_trumps_switch.get():
            trump_order = self.gui.lobby_page.custom_trumps_var.get()
        else:
            trump_order = "HCDS-"
        if self.gui.lobby_page.custom_starting_cards_switch.get():
            starting_cards = len(self.gui.lobby_page.custom_starting_cards_var.get())
        else:
            starting_cards = 0
        self.client.connection.request(RequestState.GAME_START, {
            "trump_order": trump_order,
            "starting_cards": starting_cards
        })

    def ui_starting_cards_validation(self, *args):
        text = self.gui.lobby_page.custom_starting_cards_var.get()
        text = "".join(filter(str.isdigit, text))
        try:
            if text == "":
                pass
            elif int(text) not in range(1, 52 % len(self.cached_data["game_data"]["players"]) + 1):
                text = text[:-1]
        except Exception:
            if text == "":
                pass
            elif int(text) not in range(1, 13):
                text = text[:-1]
        self.gui.lobby_page.custom_starting_cards_var.set(text)

    def ui_toggle_starting_cards(self):
        if self.gui.lobby_page.custom_starting_cards_switch.get():
            self.gui.lobby_page.custom_starting_cards_entry.configure(state="normal", text_color="#ffffff")
        else:
            self.gui.lobby_page.custom_starting_cards_entry.configure(state="disabled", text_color="#919191")

    def get_leaderboard_data(self):
        return self.cached_data["leaderboard"]

    def get_user_data(self, user_uuid):
        pass

    def get_user_leaderboard_data(self, user_uuid):
        data = self.cached_data["users"][user_uuid]
        return [
            data["username"], data["games_played"], data["games_won"], data["games_lost"], data["lifetime_score"]]


class GUI(ctk.CTk):
    def __init__(self, controller: Controller):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.geometry("1280x720")
        self.title("Blob")
        self.iconbitmap("resources/images/icons/logo.ico")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.page_tracker = []
        self.corner_icon_tracker = ""

        self.loading_page = LoadingPage(controller, self)
        self.menu_page = MenuPage(controller, self)
        self.play_button_page = PlayButtonPage(controller, self)
        self.social_page = SocialPage(controller, self)
        self.lobby_page = LobbyPage(controller, self)
        self.game_page = GamePage(controller, self)

        self.page = self.loading_page
        self.page.grid(row=0, column=0, sticky="nesw", rowspan=10, columnspan=10)

        self.corner_icon = ctk.CTkButton(
            self, text="", fg_color="#282828", hover_color="#3f3f3f", border_width=0, width=80, height=80,
            font=ctk.CTkFont("Segoe UI Symbol", 50), command=controller.ui_corner_icon)
        self.corner_icon.grid(row=0, column=0, sticky="nw", pady=10, padx=10)
        self.corner_icon.grid_remove()

        self.logo_label = ctk.CTkLabel(
            self, image=ctk.CTkImage(Image.open("resources/images/icons/logo_100px.png"), size=(100, 100)), text="")
        self.logo_label.grid(row=0, column=9, sticky="ne")
        self.logo_label.bind("<Button-1>", lambda e: self.logo_label.focus_set())

        self.info_message_box = ctk.CTkButton(
            self, font=ctk.CTkFont("Segoe UI", 30), fg_color="#3f3f3f", hover_color="#3f3f3f", border_color="#121212",
            command=lambda: self.info_message_box.grid_remove(), corner_radius=0)
        self.info_message_box.grid(row=0, column=0, rowspan=10, columnspan=10, ipadx=20, ipady=20)
        self.info_message_box.grid_remove()

        self.message_bar = ctk.CTkLabel(
            self, text="", text_color="#388cfa", font=ctk.CTkFont("Segoe UI Bold", 20), width=500)
        self.message_bar.grid(row=0, column=1, rowspan=1, columnspan=8, sticky="nw", pady=30, padx=20)
        self.message_bar.grid_remove()

        self.protocol("WM_DELETE_WINDOW", controller.close)


class Page(ctk.CTkFrame):
    def __init__(self, master, fg_color="transparent", **kwargs):
        super().__init__(master, fg_color=fg_color, **kwargs)
        self.bind("<Button-1>", lambda e: master.logo_label.focus_set())


class LoadingPage(Page):
    def __init__(self, controller: Controller, master, **kwargs):
        super().__init__(master, **kwargs)
        self.loading_frame = ctk.CTkFrame(self)
        self.loading_label = ctk.CTkLabel(
            self.loading_frame, text="Connecting to server...", font=ctk.CTkFont("Segoe UI", 30))
        self.loading_label.pack()
        self.loading_bar = ctk.CTkProgressBar(
            self.loading_frame, width=400, mode="indeterminate", indeterminate_speed=1.2, determinate_speed=1.5)
        self.loading_bar.start()
        self.loading_bar.pack(pady=20)
        self.loading_frame.pack(expand=True, fill="none")


class MenuPage(Page):
    def __init__(self, controller: Controller, master, **kwargs):
        super().__init__(master, **kwargs)
        self.menu_frame = ctk.CTkFrame(self)
        self.play_button = ctk.CTkButton(
            self.menu_frame, text="Play", width=500, height=100, command=controller.ui_play_button)
        self.play_button.pack(pady=(0, 50))
        self.social_button = ctk.CTkButton(
            self.menu_frame, text="Social", width=400, height=80, command=controller.ui_social_button,
            fg_color="#1d3b4d", hover_color="#214960")
        self.social_button.pack()
        self.menu_frame.pack(expand=True, fill="none")

        self.theme_frame = ctk.CTkFrame(self)
        self.dark_theme_label = ctk.CTkLabel(self.theme_frame, text="Dark", font=ctk.CTkFont("Segoe UI", 20))
        self.dark_theme_label.grid(column=0, row=0, padx=(0, 5))
        self.theme_switch = ctk.CTkSwitch(
            self.theme_frame, text="Light", font=ctk.CTkFont("Segoe UI", 20), command=controller.ui_theme_toggle)
        self.theme_switch.grid(column=1, row=0)
        self.theme_frame.pack(expand=False, fill="none", anchor="sw", padx=(10, 0), pady=(0, 10))


class PlayButtonPage(Page):
    def __init__(self, controller: Controller, master, **kwargs):
        super().__init__(master, **kwargs)
        self.play_frame = ctk.CTkFrame(self)
        self.create_button = ctk.CTkButton(
            self.play_frame, text="Create Game", width=500, height=100, command=controller.ui_create_game_button)
        self.create_button.pack(pady=80)
        self.join_button = ctk.CTkButton(
            self.play_frame, text="Join Game", width=500, height=100, command=controller.ui_join_game_button)
        self.join_button.pack(pady=0)
        self.code_var = ctk.StringVar(value="Enter Code")
        self.code_var.trace_add("write", controller.ui_code_entry_validation)
        self.code_entry = ctk.CTkEntry(
            self.play_frame, width=500, height=100, font=ctk.CTkFont("Segoe UI", 40),
            justify="center", textvariable=self.code_var, text_color="#919191")
        self.code_entry.pack(pady=20)
        self.code_entry.bind("<FocusIn>", controller.ui_code_entry_clear_placeholder)
        self.code_entry.bind("<FocusOut>", controller.ui_code_entry_set_placeholder)
        self.play_frame.pack(expand=True, fill="none")
        self.join_button.bind("<Button-1>", lambda e: master.logo_label.focus_set())
        self.create_button.bind("<Button-1>", lambda e: master.logo_label.focus_set())


class SocialPage(Page):
    def __init__(self, controller: Controller, master, **kwargs):
        super().__init__(master, **kwargs)


class LobbyPage(Page):
    def __init__(self, controller: Controller, master, **kwargs):
        super().__init__(master, **kwargs)
        self.grid_columnconfigure((0, 1, 2, 3, 4, 5, 6, 7), weight=10)
        self.grid_rowconfigure((0, 1, 2, 3, 4, 5, 6, 7), weight=10)

        self.user1 = ctk.CTkLabel(self, text="User 1", font=ctk.CTkFont("Segoe UI", 30))
        self.user1.grid(column=0, row=1, columnspan=2, padx=20, pady=20, sticky="nesw")
        self.user2 = ctk.CTkLabel(self, text="User 2", font=ctk.CTkFont("Segoe UI", 30))
        self.user2.grid(column=2, row=1, columnspan=2, padx=20, pady=20, sticky="nesw")
        self.user2.grid_remove()
        self.user3 = ctk.CTkLabel(self, text="User 3", font=ctk.CTkFont("Segoe UI", 30))
        self.user3.grid(column=4, row=1, columnspan=2, padx=20, pady=20, sticky="nesw")
        self.user3.grid_remove()
        self.user4 = ctk.CTkLabel(self, text="User 4", font=ctk.CTkFont("Segoe UI", 30))
        self.user4.grid(column=6, row=1, columnspan=2, padx=20, pady=20, sticky="nesw")
        self.user4.grid_remove()
        self.user5 = ctk.CTkLabel(self, text="User 5", font=ctk.CTkFont("Segoe UI", 30))
        self.user5.grid(column=1, row=2, columnspan=2, padx=20, pady=20, sticky="nesw")
        self.user5.grid_remove()
        self.user6 = ctk.CTkLabel(self, text="User 6", font=ctk.CTkFont("Segoe UI", 30))
        self.user6.grid(column=3, row=2, columnspan=2, padx=20, pady=20, sticky="nesw")
        self.user6.grid_remove()
        self.user7 = ctk.CTkLabel(self, text="User 7", font=ctk.CTkFont("Segoe UI", 30))
        self.user7.grid(column=5, row=2, columnspan=2, padx=20, pady=20, sticky="nesw")
        self.user7.grid_remove()
        self.users = [self.user1, self.user2, self.user3, self.user4, self.user5, self.user6, self.user7]

        self.bottom_player_bar = ctk.CTkFrame(self, fg_color="#245874", corner_radius=0)
        self.player_code_label = ctk.CTkLabel(self.bottom_player_bar, text="Code")
        self.player_code_label.pack(expand=True, fill="both")
        self.bottom_player_bar.grid(column=0, row=7, columnspan=8, sticky="nesw")
        self.bottom_player_bar.grid_remove()

        self.bottom_host_bar = ctk.CTkFrame(self, fg_color="#245874", corner_radius=0)
        self.bottom_host_bar.grid_columnconfigure(0, weight=10)

        self.host_code_label = ctk.CTkLabel(self.bottom_host_bar, text="Code")
        self.host_code_label.grid(column=0, row=0, sticky="nesw", pady=(20, 0))

        self.custom_starting_cards_frame = ctk.CTkFrame(self.bottom_host_bar, fg_color="#245874")
        self.custom_starting_cards_switch = ctk.CTkSwitch(
            self.custom_starting_cards_frame, text="Custom Staring Cards", font=ctk.CTkFont("Segoe UI", 30),
            command=controller.ui_toggle_starting_cards)
        self.custom_starting_cards_switch.grid(column=0, row=0, padx=(0, 10), sticky="nesw")
        self.custom_starting_cards_var = ctk.StringVar(value="1")
        self.custom_starting_cards_var.trace_add("write", controller.ui_starting_cards_validation)
        self.custom_starting_cards_entry = ctk.CTkEntry(
            self.custom_starting_cards_frame, textvariable=self.custom_starting_cards_var,
            font=ctk.CTkFont("Segoe UI", 30), width=400, state="disabled", text_color="#919191")
        self.custom_starting_cards_entry.grid(column=1, row=0, sticky="nesw")
        self.custom_starting_cards_frame.grid(column=0, row=1, sticky="nesw", padx=(200, 0), pady=(50, 0))

        self.custom_trumps_frame = ctk.CTkFrame(self.bottom_host_bar, fg_color="#245874")
        self.custom_trumps_switch = ctk.CTkSwitch(
            self.custom_trumps_frame, text="Custom Trump Order", font=ctk.CTkFont("Segoe UI", 30),
            command=controller.ui_toggle_trumps_entry)
        self.custom_trumps_switch.grid(column=0, row=0, padx=(0, 10), sticky="nesw")
        self.custom_trumps_var = ctk.StringVar(value="HCDS-")
        self.custom_trumps_var.trace_add("write", controller.ui_trumps_entry_validation)
        self.custom_trumps_entry = ctk.CTkEntry(
            self.custom_trumps_frame, textvariable=self.custom_trumps_var, font=ctk.CTkFont("Segoe UI", 30), width=400,
            state="disabled", text_color="#919191")
        self.custom_trumps_entry.grid(column=1, row=0, sticky="nesw")
        self.custom_trumps_frame.grid(column=0, row=2, sticky="nesw", padx=(200, 0), pady=(20, 0))

        self.start_button = ctk.CTkButton(
            self.bottom_host_bar, text="Start Game", state="disabled", command=controller.ui_start_game_button)
        self.start_button.grid(column=0, row=3, sticky="ns", pady=(50, 0))

        self.bottom_host_bar.grid(column=0, row=5, columnspan=8, rowspan=3, sticky="nesw")
        self.bottom_host_bar.grid_remove()


class GamePage(Page):
    def __init__(self, controller: Controller, master, **kwargs):
        super().__init__(master, **kwargs)
        self.grid_rowconfigure((0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11), weight=10, uniform="a")
        self.grid_columnconfigure((0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11), weight=10, uniform="b")

        self.user1 = GameUser(self)
        self.user1.grid(column=1, columnspan=2, row=4, rowspan=2, sticky="nesw", pady=10, padx=10)
        self.user1.grid_remove()
        self.user2 = GameUser(self)
        self.user2.grid(column=1, columnspan=2, row=2, rowspan=2, sticky="nesw", pady=10, padx=10)
        self.user2.grid_remove()
        self.user3 = GameUser(self)
        self.user3.grid(column=4, columnspan=2, row=0, rowspan=2, sticky="nesw", pady=10, padx=10)
        self.user3.grid_remove()
        self.user4 = GameUser(self)
        self.user4.grid(column=6, columnspan=2, row=0, rowspan=2, sticky="nesw", pady=10, padx=10)
        self.user4.grid_remove()
        self.user5 = GameUser(self)
        self.user5.grid(column=9, columnspan=2, row=2, rowspan=2, sticky="nesw", pady=10, padx=10)
        self.user5.grid_remove()
        self.user6 = GameUser(self)
        self.user6.grid(column=9, columnspan=2, row=4, rowspan=2, sticky="nesw", pady=10, padx=10)
        self.user6.grid_remove()
        self.user7 = GameUser(self)
        self.user7.grid(column=5, columnspan=2, row=0, rowspan=2, sticky="nesw", pady=10, padx=10)
        self.user7.grid_remove()
        self.users = [self.user1, self.user2, self.user3, self.user4, self.user5, self.user6, self.user7]
        self.active_users = {}

        self.top_card = ctk.CTkLabel(
            self, text="User 6",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(230, 350)),
            compound="top", font=ctk.CTkFont("Segoe UI", 15))
        self.top_card.grid(column=6, columnspan=3, row=2, rowspan=5, sticky="nesw", pady=10, padx=10)

        self.previous_card1 = ctk.CTkLabel(
            self, text="User 1",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            compound="top", font=ctk.CTkFont("Segoe UI", 15))
        self.previous_card1.grid(column=3, row=2, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.previous_card2 = ctk.CTkLabel(
            self, text="User 2",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            compound="top", font=ctk.CTkFont("Segoe UI", 15))
        self.previous_card2.grid(column=4, row=2, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.previous_card3 = ctk.CTkLabel(
            self, text="User 3",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            compound="top", font=ctk.CTkFont("Segoe UI", 15))
        self.previous_card3.grid(column=5, row=2, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.previous_card4 = ctk.CTkLabel(
            self, text="User 4",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            compound="top", font=ctk.CTkFont("Segoe UI", 15))
        self.previous_card4.grid(column=3, row=4, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.previous_card5 = ctk.CTkLabel(
            self, text="User 5",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            compound="top", font=ctk.CTkFont("Segoe UI", 15))
        self.previous_card5.grid(column=4, row=4, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.previous_cards = [
            self.top_card, self.previous_card1, self.previous_card2,
            self.previous_card3, self.previous_card4, self.previous_card5]

        self.player = ctk.CTkFrame(self, fg_color="#244d1c", corner_radius=0)
        self.player.grid(column=0, columnspan=12, row=7, rowspan=5, sticky="nesw", pady=(10, 0))
        self.player.grid_rowconfigure((0, 1, 2, 3, 4), weight=10, uniform="a")
        self.player.grid_columnconfigure((0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11), weight=10, uniform="b")

        self.player_frame_header = ctk.CTkLabel(self.player, text="Your Hand", fg_color="transparent")
        self.player_frame_header.grid(column=0, columnspan=12, row=0, sticky="nesw")

        self.player_card1 = ctk.CTkButton(
            self.player, text="",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            corner_radius=0, border_width=0, state="disabled")
        self.player_card1.grid(column=3, row=1, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.player_card2 = ctk.CTkButton(
            self.player, text="",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            corner_radius=0, border_width=0, state="disabled")
        self.player_card2.grid(column=4, row=1, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.player_card3 = ctk.CTkButton(
            self.player, text="",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            corner_radius=0, border_width=0, state="disabled")
        self.player_card3.grid(column=5, row=1, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.player_card4 = ctk.CTkButton(
            self.player, text="",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            corner_radius=0, border_width=0, state="disabled")
        self.player_card4.grid(column=6, row=1, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.player_card5 = ctk.CTkButton(
            self.player, text="",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            corner_radius=0, border_width=0, state="disabled")
        self.player_card5.grid(column=7, row=1, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.player_card6 = ctk.CTkButton(
            self.player, text="",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            corner_radius=0, border_width=0, state="disabled")
        self.player_card6.grid(column=8, row=1, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.player_card7 = ctk.CTkButton(
            self.player, text="",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            corner_radius=0, border_width=0, state="disabled")
        self.player_card7.grid(column=9, row=1, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.player_card8 = ctk.CTkButton(
            self.player, text="",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            corner_radius=0, border_width=0, state="disabled")
        self.player_card8.grid(column=10, row=1, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.player_card9 = ctk.CTkButton(
            self.player, text="",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            corner_radius=0, border_width=0, state="disabled")
        self.player_card9.grid(column=11, row=1, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.player_card10 = ctk.CTkButton(
            self.player, text="",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            corner_radius=0, border_width=0, state="disabled")
        self.player_card10.grid(column=3, row=3, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.player_card11 = ctk.CTkButton(
            self.player, text="",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            corner_radius=0, border_width=0, state="disabled")
        self.player_card11.grid(column=4, row=3, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.player_card12 = ctk.CTkButton(
            self.player, text="",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            corner_radius=0, border_width=0, state="disabled")
        self.player_card12.grid(column=5, row=3, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.player_card13 = ctk.CTkButton(
            self.player, text="",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            corner_radius=0, border_width=0, state="disabled")
        self.player_card13.grid(column=6, row=3, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.player_card14 = ctk.CTkButton(
            self.player, text="",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            corner_radius=0, border_width=0, state="disabled")
        self.player_card14.grid(column=7, row=3, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.player_card15 = ctk.CTkButton(
            self.player, text="",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            corner_radius=0, border_width=0, state="disabled")
        self.player_card15.grid(column=8, row=3, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.player_card16 = ctk.CTkButton(
            self.player, text="",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            corner_radius=0, border_width=0, state="disabled")
        self.player_card16.grid(column=9, row=3, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.player_card17 = ctk.CTkButton(
            self.player, text="",
            image=ctk.CTkImage(Image.open("resources/images/cards/Placeholder.png"), size=(100, 150)),
            corner_radius=0, border_width=0, state="disabled")
        self.player_card17.grid(column=10, row=3, rowspan=2, sticky="nesw", pady=5, padx=5)
        self.player_cards = [
            self.player_card1, self.player_card2, self.player_card3, self.player_card4, self.player_card5,
            self.player_card6, self.player_card7, self.player_card8, self.player_card9, self.player_card10,
            self.player_card11, self.player_card12, self.player_card13, self.player_card14, self.player_card15,
            self.player_card16, self.player_card17
        ]


class GameUser(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color="#245874", **kwargs)
        self.username_label = ctk.CTkLabel(self, text="Username", font=ctk.CTkFont("Segoe UI Bold", 20))
        self.username_label.pack(expand=True, fill="both")
        self.cards_left_label = ctk.CTkLabel(self, text="Cards left", font=ctk.CTkFont("Segoe UI", 20))
        self.cards_left_label.pack(expand=True, fill="both")
        self.prediction_label = ctk.CTkLabel(self, text="Prediction", font=ctk.CTkFont("Segoe UI", 20))
        self.prediction_label.pack(expand=True, fill="both")
        self.tricks_won_label = ctk.CTkLabel(self, text="Tricks won", font=ctk.CTkFont("Segoe UI", 20))
        self.tricks_won_label.pack(expand=True, fill="both")


if __name__ == "__main__":
    Client()
