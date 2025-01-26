import json
import logging
import os
import ssl
import threading
import time
import typing
from enum import Enum

from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.getenv("DB_HOST")
DB_USERNAME = os.getenv("DB_USERNAME")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB = os.getenv("DB")

SERVER_PORT = 8108
SERVER_IP = "127.0.0.1"
HEADER_SIZE = 8
GAME_CODE_LENGTH = 4


class PacketState(Enum):
    pass


class RequestState(PacketState):
    NEW_GAME = "RQ1"
    GAME_JOIN = "RQ2"
    GAME_START = "RQ3"
    GAME_DATA = "RQ4"
    UUID = "RQ5"


class ResponseState(PacketState):
    CREATE_GAME_SUCCESS = "RS1"
    CREATE_GAME_FAILED = "RS2"
    JOIN_GAME_SUCCESS = "RS3"
    JOIN_GAME_FAILED = "RS4"
    START_GAME_SUCCESS = "RS5"
    START_GAME_FAILED = "RS6"
    ALREADY_IN_GAME = "RS7"
    NOT_IN_GAME = "RS8"
    INVALID_REQUEST = "RS9"
    UNKNOWN_ERROR = "RS10"
    GAME_DATA = "RS11"
    UUID = "RS12"
    GAME_NOT_STARTED = "RS13"
    NOT_TURN = "RS14"
    INVALID_CARD = "RS15"
    INVALID_PREDICTION = "RS16"
    SUCCESS = "RS17"


class DataPacketState(PacketState):
    GAME_DATA = "DP1"
    UUID = "DP2"
    TOKEN = "DP3"


class GameWaitingState(Enum):
    NONE = "G0"
    GAME_START = "G1"
    ROUND_START = "G2"
    PREDICTION = "G3"
    PLACE_CARD = "G4"
    MIN_PLAYERS = "G5"
    GAME_END = "G6"


class NetworkConnection:
    def __init__(self, peer_socket: ssl.SSLSocket, peer_address):
        self.socket: ssl.SSLSocket = peer_socket
        self.address = peer_address
        self.requests: list[tuple] = []
        self.has_responded: bool = True
        threading.Thread(target=self.loop_requests).start()

    def send_packet(self, packet_state: PacketState, data: typing.Any) -> None:
        logging.info(f"Sent packet {packet_state.value}.")
        self.socket.sendall(create_message({"state": packet_state.value, "data": data}))

    def request(self, request_state: RequestState, data: typing.Any) -> None:
        self.requests.append((request_state, data))

    def respond(self, response_state: ResponseState, data: typing.Any) -> None:
        self.send_packet(response_state, data)

    def loop_requests(self):
        while True:
            if self.has_responded and len(self.requests) > 0:
                self.has_responded = False
                request = self.requests.pop(0)
                self.send_packet(request[0], request[1])
            else:
                time.sleep(0.1)

    def __str__(self):
        return self.address


class ConnectionToClient(NetworkConnection):
    def __init__(
            self, client_socket: ssl.SSLSocket, client_address, hashed_token: str, client_uuid: str):
        super().__init__(client_socket, client_address)
        self.hashed_token: str = hashed_token
        self.uuid: str = client_uuid
        self.in_game: str = ""

    def __repr__(self):
        return self.uuid


class ConnectionToServer(NetworkConnection):
    def __init__(self, server_socket: ssl.SSLSocket, server_address):
        super().__init__(server_socket, server_address)


def recvall(s: ssl.SSLSocket, n: int) -> bytes:
    data = bytearray()
    while len(data) < n:
        packet = s.recv(n - len(data))
        if not packet:
            break
        data.extend(packet)
    return data


def create_message(message: dict) -> bytes:
    json_message = json.dumps(message).encode()
    return f"{len(json_message):<8}".encode() + json_message


SUITS: list[str] = ["H", "C", "D", "S"]
VALUES: list[str] = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14"]

FULL_DECK: list[dict] = [{"suit": suit, "value": value} for suit in SUITS for value in VALUES]
