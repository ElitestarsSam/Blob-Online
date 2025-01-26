import copy
import hashlib
import json
import logging
import random
import socket
import ssl
import string
import threading
import typing
import uuid

import sqlite3

from main import ConnectionToClient, SERVER_PORT, DB_HOST, DB_USERNAME, DB_PASSWORD, DB, recvall, HEADER_SIZE, \
    ResponseState, RequestState, DataPacketState, GAME_CODE_LENGTH, FULL_DECK, GameWaitingState

logging.basicConfig(format="%(levelname)s - %(message)s", level=logging.DEBUG)


class Server:
    def __init__(self) -> None:
        self.database_connection = sqlite3.connect("BlobDB.db", check_same_thread=False)
        self.database = self.database_connection.cursor()
        logging.info(f"Connected to database.")

        # Initialize managers.
        self.user_manager: UserManager = UserManager(self)
        self.game_manager: GameManager = GameManager(self)
        self.controller: Controller = Controller(self)

        # Declare dictionaries of clients and games.
        self.clients: dict[str, ConnectionToClient] = {}
        self.disconnected_clients: dict[str, ConnectionToClient] = {}

        # Create SSL context and load certificate and key.
        self.ssl_context: ssl.SSLContext = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ssl_context.load_cert_chain(
            certfile=r"resources\ssl-tls\fullchain.pem",
            keyfile=r"resources\ssl-tls\privkey.pem")

        # Create socket
        self.socket: ssl.SSLSocket = self.ssl_context.wrap_socket(socket.socket(), server_side=True)

        # Bind socket to hostname and server port.
        try:
            self.socket.bind((socket.gethostname(), SERVER_PORT))
        except socket.error as e:
            logging.error(f"Socket error: {e}")
        self.socket.listen()
        logging.info(f"Server started: ('{socket.gethostname()}', {SERVER_PORT}).")

        # Accept clients and start new thread.
        self.accept_clients()

    def accept_clients(self) -> None:
        while True:
            try:
                # Accept incoming connections and start a new thread for each client.
                client_socket, client_address = self.socket.accept()
                threading.Thread(target=self.client_thread, args=(client_socket, client_address,)).start()
            except ssl.SSLError as e:
                logging.error(f"SSL error: {e}")
            except socket.error as e:
                logging.error(f"Socket error: {e}")
            except Exception as e:
                logging.error(f"Unexpected error: {e}")

    def client_thread(self, client_socket: ssl.SSLSocket, client_address) -> None:
        # Receive connection token from client and hash it.
        hashed_token: str = hashlib.sha256(
            recvall(client_socket, int(client_socket.recv(HEADER_SIZE).decode()))).hexdigest()

        # Check if the client has been previously connected and respond accordingly.
        if hashed_token in self.disconnected_clients:
            self.clients[hashed_token] = self.reconnect(
                client_socket=client_socket, hashed_token=hashed_token)
            logging.info(f"Reconnected to client: {self.clients[hashed_token].uuid}, Sending UUID...")
        else:
            self.clients[hashed_token] = ConnectionToClient(
                client_socket, client_address, hashed_token, str(uuid.uuid4()))
            logging.info(f"Connected to client: {self.clients[hashed_token].uuid}, Sending UUID...")
        client: ConnectionToClient = self.clients[hashed_token]
        client.send_packet(DataPacketState.UUID, client.uuid)
        self.database.execute(
            f"""INSERT INTO Users (uuid, username, connection_hash, guest, password_salt, password_hash) 
            VALUES ('{self.clients[hashed_token].uuid}', '', '{hashed_token}', 1, NULL, NULL);""")

        # Receive data from client and start a new thread to handle each packet.
        while True:
            try:
                # Receive header with size of the rest of the message.
                header: bytes = recvall(client.socket, HEADER_SIZE)

                # Break from the loop if the header is empty.
                if not len(header):
                    break
                packet: dict = json.loads(recvall(client.socket, int(header.decode())))
                threading.Thread(target=lambda: self.handle_packet(packet, hashed_token)).start()
            except socket.error as e:
                logging.error(f"Socket error: {e}")
                break
            except Exception as e:
                logging.error(f"Unexpected error: {e}")
                break
        logging.warning(f"Lost connection to client: {client.uuid}")
        client.socket.close()

        # Move the client instance to the disconnected clients dictionary.
        self.disconnected_clients[hashed_token] = self.clients.pop(hashed_token)

    def handle_packet(self, packet: dict, hashed_token: str) -> None:
        client_uuid: str = self.clients[hashed_token].uuid

        # Handle request.
        if packet["state"].startswith("RQ"):
            match packet["state"]:
                case RequestState.NEW_GAME.value:
                    logging.info(f"Request from client {client_uuid} to create a new game.")
                    result = self.controller.request_new_game(hashed_token)
                case RequestState.GAME_JOIN.value:
                    logging.info(f"Request from client {client_uuid} to join game {packet["data"]}.")
                    result = self.controller.request_game_join(hashed_token, packet["data"])
                case RequestState.GAME_START.value:
                    logging.info(f"Request from client {client_uuid} to start game.")
                    result = self.controller.request_game_start(hashed_token, packet["data"])
                case RequestState.UUID.value:
                    logging.info(f"Request from client {client_uuid} to obtain UUID.")
                    result = (ResponseState.UUID, str(client_uuid))
                case RequestState.GAME_DATA.value:
                    logging.info(f"Request from client {client_uuid} for game data.")
                    result = self.controller.request_game_data(hashed_token)
                case _:
                    logging.warning("Received invalid request.")
                    result = ResponseState.INVALID_REQUEST, ""
            self.clients[hashed_token].respond(result[0], result[1])
            logging.info(f"Responded to client {client_uuid} with {result[0]}.")

        # Handle response.
        elif packet["state"].startswith("RS"):
            match packet["state"]:
                case ResponseState.INVALID_REQUEST.value:
                    logging.warning("Request sent was invalid.")
                case _:
                    logging.warning("Received invalid response.")
            self.clients[hashed_token].has_responded = True

        # Handle invalid packet.
        else:
            logging.warning("Received invalid packet.")

    def reconnect(self, *, client_socket: ssl.SSLSocket, hashed_token: str) -> ConnectionToClient:
        client: ConnectionToClient = self.disconnected_clients.pop(hashed_token)
        client.socket = client_socket
        client.has_responded = True
        return client


class GameManager:
    def __init__(self, server: Server) -> None:
        self.server: Server = server

        self.lobbies: dict[str, Game] = {}
        self.started: dict[str, Game] = {}

    def generate_game_code(self) -> str:
        while True:
            code = ''.join(random.choices(string.ascii_uppercase, k=GAME_CODE_LENGTH))
            if code not in self.lobbies and code not in self.started:
                logging.info(f"Generated game code: {code}.")
                return code


class UserManager:
    def __init__(self, server: Server) -> None:
        self.server: Server = server
        self.guests: dict[str, dict] = {}


class Controller:
    def __init__(self, server: Server) -> None:
        self.server: Server = server
        self.database = self.server.database
        self.database_connection = self.server.database_connection
        self.user_manager = self.server.user_manager
        self.game_manager = self.server.game_manager

    def get_user_game_code(self, user_uuid: str) -> str | bool:
        self.database.execute(f"SELECT game_code FROM Users WHERE uuid = '{user_uuid}'")
        result = self.database.fetchone()
        if not result:
            logging.info(f"Requested {user_uuid} game code but user does not exist.")
            return False
        if not result[0]:
            logging.info(f"Requested {user_uuid} game code but user is not in a game.")
            return False
        logging.info(f"Requested {user_uuid} game code.")
        return result[0]

    def get_connection_hash(self, user_uuid: str) -> str | bool:
        self.database.execute(f"SELECT connection_hash FROM Users WHERE uuid = '{user_uuid}'")
        result = self.database.fetchone()
        if not result:
            logging.info(f"Requested {user_uuid} connection hash but user does not exist.")
            return False
        if not result[0]:
            logging.info(f"Requested {user_uuid} connection hash but user is not connected.")
            return False
        logging.info(f"Requested {user_uuid} connection hash.")
        return result[0]

    def get_username(self, user_uuid: str) -> str:
        self.database.execute(f"SELECT username, guest FROM Users WHERE uuid = '{user_uuid}'")
        result = self.database.fetchone()
        if not result:
            logging.info(f"Requested {user_uuid} username but user does not exist.")
            return ""
        logging.info(f"Requested {user_uuid} username.")
        if result[1]:
            return f"Guest({result[0]})"
        else:
            return result[0]

    def set_username(self, user_uuid: str, username: str) -> bool:
        if user_uuid in self.user_manager.guests:
            if any(x["username"] == username for x in self.user_manager.guests.values()):
                logging.info(f"Guest: {user_uuid} requested to set username to {username} but username is taken.")
                return False
            else:
                self.user_manager.guests[user_uuid]["username"] = username
                logging.info(f"Guest: {user_uuid} set username to {username}.")
                return True
        else:
            self.database.execute("SELECT id FROM Users WHERE username = %s", (username,))
            if self.database.fetchone():
                logging.info(f"User: {user_uuid} requested to set username to {username} but username is taken.")
                return False
            else:
                self.database.execute("UPDATE Users SET username = %s WHERE uuid = %s", (username, str(user_uuid)))
                self.database_connection.commit()
                logging.info(f"User: {user_uuid} set username to {username}.")
                return True

    def send_game_update(self, game_code: str) -> None:
        if game_code in self.game_manager.lobbies:
            for player in self.game_manager.lobbies[game_code].players:
                self.server.clients[self.get_connection_hash(player)].send_packet(
                    DataPacketState.GAME_DATA, self.get_game_data_for_player(player))
        elif game_code in self.game_manager.started:
            for player in self.game_manager.started[game_code].players:
                self.server.clients[self.get_connection_hash(player)].send_packet(
                    DataPacketState.GAME_DATA, self.get_game_data_for_player(player))
        logging.info(f"Sent game update for game {game_code}.")

    def request_new_game(self, hashed_token: str) -> tuple[ResponseState, typing.Any]:
        client_uuid = self.server.clients[hashed_token].uuid
        # Check if client is in a game.
        if self.server.clients[hashed_token].in_game:
            logging.info(f"Client: {client_uuid} requested to create a new game but is already in a game.")
            return ResponseState.ALREADY_IN_GAME, ""
        # Check if client is a guest.

        code = self.game_manager.generate_game_code()
        self.game_manager.lobbies[code] = Game(self.game_manager, code)
        self.game_manager.lobbies[code].add_player(client_uuid)
        self.server.clients[hashed_token].in_game = code
        self.database.execute(f"UPDATE Users SET game_code = '{code}' WHERE uuid = '{client_uuid}'")
        logging.info(f"Client: {client_uuid} created new game {code}.")
        return ResponseState.CREATE_GAME_SUCCESS, self.get_game_data_for_player(client_uuid)

    def request_game_join(self, hashed_token: str, code: str) -> tuple[ResponseState, typing.Any]:
        client_uuid = self.server.clients[hashed_token].uuid
        # Check if client is in a game.
        if self.server.clients[hashed_token].in_game:
            logging.info(f"Client: {client_uuid} requested to join game {code} but is already in a game.")
            return ResponseState.ALREADY_IN_GAME, ""
        # Check if game has already started.
        if code in self.game_manager.started:
            logging.info(f"Client: {client_uuid} requested to join game {code} but game has already started.")
            return ResponseState.JOIN_GAME_FAILED, f"Game {code} has already started."
        # Check if game exists.
        if code not in self.game_manager.lobbies:
            logging.info(f"Client: {client_uuid} requested to join game {code} but game does not exist")
            return ResponseState.JOIN_GAME_FAILED, f"Game {code} does not exist."
        # Check if game is full.
        if len(self.game_manager.lobbies[code].players) >= self.game_manager.lobbies[code].max_players:
            logging.info(f"Client: {client_uuid} requested to join game {code} but game is full.")
            return ResponseState.JOIN_GAME_FAILED, f"Game {code} is full."

        self.server.clients[hashed_token].in_game = code
        self.database.execute(f"UPDATE Users SET game_code = '{code}' WHERE uuid = '{client_uuid}'")
        self.game_manager.lobbies[code].add_player(client_uuid)
        logging.info(f"Client: {client_uuid} joined game {code}.")
        return ResponseState.JOIN_GAME_SUCCESS, self.get_game_data_for_player(client_uuid)

    def request_game_start(self, hashed_token: str, data: dict) -> tuple[ResponseState, str]:
        try:
            starting_cards = data["starting_cards"]
            trump_order = data["trump_order"]
        except Exception as e:
            logging.error(f"Error getting start game data from request: {e}")
            return ResponseState.START_GAME_FAILED, "Invalid data."

        client_uuid = self.server.clients[hashed_token].uuid
        # Check if client is in a game.
        if not self.server.clients[hashed_token].in_game:
            logging.info(f"Client: {client_uuid} requested to start game but is not in a game.")
            return ResponseState.NOT_IN_GAME, ""
        game_code = self.server.clients[hashed_token].in_game
        # Check if game has already started.
        if game_code in self.game_manager.started:
            logging.info(f"Client: {client_uuid} requested to start game but game has already started.")
            return ResponseState.START_GAME_FAILED, "Game has already started."
        # Check if client is the host.
        if self.game_manager.lobbies[game_code].host != client_uuid:
            logging.info(f"Client: {client_uuid} requested to start game but is not the host.")
            return ResponseState.START_GAME_FAILED, "You must be the host to start the game."
        # Check if there are enough players.
        if len(self.game_manager.lobbies[game_code].players) < 2:
            logging.info(f"Client: {client_uuid} requested to start game but not enough players.")
            return ResponseState.START_GAME_FAILED, "Not enough players to start the game."
        # Check if data is valid.
        if not 0 <= starting_cards <= (52 // len(self.game_manager.lobbies[game_code].players)):
            logging.info(f"Client: {client_uuid} requested to start game but invalid starting cards.")
            return ResponseState.START_GAME_FAILED, "Invalid starting cards."
        if trump_order != "".join(filter({"H", "C", "D", "S", "-"}.__contains__, trump_order.upper()))[:17]:
            logging.info(f"Client: {client_uuid} requested to start game but invalid trump order.")
            return ResponseState.START_GAME_FAILED, "Invalid trump order."

        self.game_manager.started[game_code] = self.game_manager.lobbies.pop(game_code)
        threading.Thread(
            target=lambda: self.game_manager.started[game_code].start_game(starting_cards, trump_order)).start()
        logging.info(f"Client: {client_uuid} started game {game_code}.")
        return ResponseState.START_GAME_SUCCESS, ""

    def request_game_data(self, hashed_token: str) -> tuple[ResponseState, typing.Any]:
        client_uuid = self.server.clients[hashed_token].uuid

        if self.server.clients[hashed_token].in_game:
            game_data = self.get_game_data_for_player(client_uuid)
            logging.info(f"Client: {client_uuid} requested game data.")
            return ResponseState.GAME_DATA, game_data
        else:
            logging.info(f"Client: {client_uuid} requested game data but is not in a game.")
            return ResponseState.NOT_IN_GAME, ""

    def request_card_to_place(self, hashed_token: str, card: dict) -> tuple[ResponseState, str]:
        client_uuid = self.server.clients[hashed_token].uuid
        # Check if client is in a game.
        if not self.server.clients[hashed_token].in_game:
            logging.info(f"Client: {client_uuid} requested card {card} to place but is not in a game.")
            return ResponseState.NOT_IN_GAME, ""
        game_code = self.server.clients[hashed_token].in_game
        # Check if game has started.
        if game_code not in self.game_manager.started:
            logging.info(f"Client: {client_uuid} requested card {card} to place but game has not started.")
            return ResponseState.GAME_NOT_STARTED, ""
        game = self.game_manager.started[game_code]
        # Check if it is the client's turn to place a card.
        if game.waiting_for[0] != client_uuid and \
                game.waiting_for[1] == GameWaitingState.PLACE_CARD:
            logging.info(f"Client: {client_uuid} requested card {card} to place but is not their turn.")
            return ResponseState.NOT_TURN, ""
        # Check if the card is valid.
        if not game.is_card_valid(client_uuid, card):
            logging.info(f"Client: {client_uuid} requested card {card} to place but card is not valid.")
            return ResponseState.INVALID_CARD, ""
        logging.info(f"Client: {client_uuid} requested card {card} to place was valid.")
        # Place the card.
        game.pile.append(card)
        game.private_data[client_uuid][game.round_number]["hand"].remove(game.pile[-1])
        game.players[client_uuid]["rounds"][game.round_number]["cards_left"] -= 1
        game.waiting_for = (client_uuid, GameWaitingState.NONE)
        logging.info(f"Game: {game_code} waiting for {game.waiting_for}.")
        return ResponseState.SUCCESS, ""

    def request_prediction(self, hashed_token: str, prediction: int) -> tuple[ResponseState, str]:
        client_uuid = self.server.clients[hashed_token].uuid
        # Check if client is in a game.
        if not self.server.clients[hashed_token].in_game:
            logging.info(f"Client: {client_uuid} requested prediction {prediction} but is not in a game.")
            return ResponseState.NOT_IN_GAME, ""
        game_code = self.server.clients[hashed_token].in_game
        # Check if game has started.
        if game_code not in self.game_manager.started:
            logging.info(f"Client: {client_uuid} requested prediction {prediction} but game has not started.")
            return ResponseState.GAME_NOT_STARTED, ""
        game = self.game_manager.started[game_code]
        # Check if it is the client's turn to predict.
        if game.waiting_for[0] != client_uuid and \
                game.waiting_for[1] == GameWaitingState.PREDICTION:
            logging.info(f"Client: {client_uuid} requested prediction {prediction} but is not their turn.")
            return ResponseState.NOT_TURN, ""
        # Check if the prediction is valid.
        if not game.is_prediction_valid(client_uuid, prediction):
            logging.info(f"Client: {client_uuid} requested prediction {prediction} but prediction is not valid.")
            return ResponseState.INVALID_PREDICTION, ""
        logging.info(f"Client: {client_uuid} requested prediction {prediction} was valid.")
        # Set the prediction.
        game.players[client_uuid]["rounds"][game.round_number]["prediction"] = prediction
        game.waiting_for = (client_uuid, GameWaitingState.NONE)
        logging.info(f"Game: {game_code} waiting for {game.waiting_for}.")
        return ResponseState.SUCCESS, ""

    def get_game_data_for_player(self, player_uuid: str) -> dict:
        game_code = self.get_user_game_code(player_uuid)
        if game_code in self.game_manager.started:
            game = self.game_manager.started[game_code]
        elif game_code in self.game_manager.lobbies:
            game = self.game_manager.lobbies[game_code]
        else:
            return {}
        try:
            game_data: dict[str, typing.Any] = {
                "host": game.host,
                "code": game_code,
                "max_players": game.max_players,
                "trump_order": game.trump_order,
                "initial_player_order": game.initial_player_order,
                "current_player_order": game.current_player_order,
                "started": game.started,
                "number_of_rounds": game.number_of_rounds,
                "round_number": game.round_number,
                "tricks_available": game.tricks_available,
                "current_trump": game.current_trump,
                "waiting_for": (game.waiting_for[0], game.waiting_for[1].value),
                "pile": game.pile,
                "players": copy.deepcopy(game.players)
            }
            game_data["players"][player_uuid]["rounds"] = game_data["players"][player_uuid]["rounds"] | \
                                                          game.private_data[player_uuid]
            return game_data
        except Exception as e:
            logging.error(f"Error getting game data for player {player_uuid}: {e}")


class Game:
    def __init__(self, manager: GameManager, code: str) -> None:
        self.manager: GameManager = manager
        self.server: Server = self.manager.server

        # --- Settings --- #
        self.host: str = None
        self.code: str = code
        self.max_players: int = 7
        self.starting_cards: int = 0
        self.trump_order: str = "HCDS-"
        self.initial_player_order: list[str] = []
        self.current_player_order: list[str] = []

        # --- Game Status --- #
        self.started: bool = False
        self.number_of_rounds: int = None
        self.round_number: int = 0
        self.tricks_available: int = None
        self.current_trick: int = 0
        self.current_trump: str = None
        self.waiting_for: tuple[str, GameWaitingState] = None
        self.pile: list[dict] = []

        self.players: dict[str, dict] = {}
        self.private_data: dict[str, dict] = {}

    def send_update(self) -> None:
        self.server.controller.send_game_update(self.code)

    def add_player(self, player_uuid: str) -> None:
        username = self.server.controller.get_username(player_uuid)

        self.players[player_uuid] = {
            "rounds": {
                # 0: {"prediction": 0, "cards_left": 0, "tricks_won": 0, "score": 0}
            },
            "username": username,
            "total_score": 0,
        }
        self.private_data[player_uuid] = {
            # 0: {"initial_hand": [], "hand": []}
        }
        self.initial_player_order.append(player_uuid)

        if len(self.players) == 1:
            self.host = player_uuid
            self.waiting_for = (player_uuid, GameWaitingState.MIN_PLAYERS)
        else:
            self.send_update()

        if not len(self.players) < 2:
            self.waiting_for = (self.host, GameWaitingState.GAME_START)

    def remove_player(self, player_uuid: str) -> None:
        self.players.pop(player_uuid)
        self.initial_player_order.remove(player_uuid)
        self.current_player_order.remove(player_uuid)

        if len(self.players) == 0:
            self.close_game()
            return

        if self.host == player_uuid:
            self.host = self.initial_player_order[0]

        self.send_update()

    def start_game(self, starting_cards: int, trump_order: str) -> None:
        if self.started:
            return
        if len(self.players) < 2:
            return
        self.starting_cards = starting_cards
        self.trump_order = trump_order
        self.started = True
        self.waiting_for = (self.host, GameWaitingState.NONE)
        if self.starting_cards == 0:
            self.number_of_rounds = 52 // len(self.players)
        else:
            self.number_of_rounds = self.starting_cards
        self.start_round()

    def start_round(self) -> None:
        self.tricks_available = self.number_of_rounds - self.round_number
        self.current_trump = self.get_current_trump()
        self.current_player_order = self.get_round_player_order()
        for player in self.current_player_order:
            self.players[player]["rounds"][self.round_number] = {
                "prediction": 0,
                "cards_left": self.tricks_available,
                "tricks_won": 0,
                "score": 0
            }
        self.deal_cards()
        self.get_predictions()
        winner = self.current_player_order[0]
        while self.current_trick < self.tricks_available:
            self.current_trick += 1
            self.current_player_order = self.get_player_order(winner)
            winner = self.start_trick()
            self.private_data[winner][self.round_number]["tricks_won"] += 1
            self.send_update()
        # Calculate scores.
        for player in self.players:
            if self.players[player]["rounds"][self.round_number]["tricks_won"] == \
                    self.players[player]["rounds"][self.round_number]["prediction"]:
                self.players[player]["rounds"][self.round_number]["score"] = \
                    self.players[player]["rounds"][self.round_number]["tricks_won"] + 10
                self.players[player]["total_score"] += self.players[player]["rounds"][self.round_number]["score"]
        self.round_number += 1
        # End round.
        if self.round_number == self.number_of_rounds:
            self.waiting_for = (self.host, GameWaitingState.GAME_END)
        else:
            self.waiting_for = (self.host, GameWaitingState.ROUND_START)
        self.send_update()

    def start_trick(self) -> str:
        self.pile = []
        self.get_cards_to_place()
        return self.get_winning_card()["player"]

    def get_round_player_order(self):
        return self.get_player_order(self.initial_player_order[self.round_number % len(self.players)])

    def get_player_order(self, first_player: str):
        order = copy.deepcopy(self.initial_player_order)
        while True:
            if order[0] == first_player:
                break
            order.append(order.pop(0))
        return order

    def deal_cards(self):
        deck = random.sample(FULL_DECK, self.tricks_available * len(self.players))
        for player in self.current_player_order:
            self.private_data[player][self.round_number] = {
                "initial_hand": [],
                "hand": []
            }
        for _ in range(self.tricks_available):
            for player in self.current_player_order:
                card = deck.pop()
                card["player"] = player
                self.private_data[player][self.round_number]["hand"].append(card)
                self.private_data[player][self.round_number]["initial_hand"].append(card)

    def get_predictions(self) -> None:
        for player in self.current_player_order:
            self.waiting_for = (player, GameWaitingState.PREDICTION)
            self.send_update()
            while self.waiting_for[0] == player:
                pass
        self.waiting_for = (self.host, GameWaitingState.NONE)

    def get_cards_to_place(self) -> None:
        for player in self.current_player_order:
            self.waiting_for = (player, GameWaitingState.PLACE_CARD)
            self.send_update()
            while self.waiting_for[0] == player:
                pass
        self.waiting_for = (self.host, GameWaitingState.NONE)

    def get_current_trump(self):
        return self.trump_order[self.round_number % len(self.trump_order)]

    def is_card_valid(self, player_uuid: str, card: dict) -> bool:
        # Check if card is in player's hand.
        if card not in self.private_data[player_uuid][self.round_number]["hand"]:
            return False
        # Check if the card is the first placed.
        if not self.pile:
            return True
        # Check player has a card of the same suit.
        if any(x["suit"] == self.pile[0]["suit"] for x in self.private_data[player_uuid][self.round_number]["hand"]):
            return card["suit"] == self.pile[0]["suit"]
        return True

    def is_prediction_valid(self, player_uuid: str, prediction: int) -> bool:
        if not 0 <= prediction <= self.tricks_available:
            return False
        # Check if player is last.
        if player_uuid == self.current_player_order[-1]:
            if prediction == self.tricks_available - sum(
                    x[self.round_number]["prediction"] for x in self.players.values()):
                return False
        return True

    def get_winning_card(self) -> dict:
        def sort_key(card: dict):
            return (
                (card["suit"] == self.current_trump),
                (card["suit"] == self.pile[0][1]),
                card["value"]
            )

        winning_card = sorted(self.pile, key=sort_key)[-1]
        logging.info(f"{winning_card} won from {self.pile} in game {self.code}.")
        return winning_card

    def close_game(self):
        raise NotImplementedError


if __name__ == "__main__":
    Server()
