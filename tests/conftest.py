"""Every test runs against a fake network: `ping` answers and each STM controller sends a valid firmware-status reply,
so no test reaches a real controller. A test changes the answers through the `network` fixture."""

import socket
import struct
import subprocess
from types import SimpleNamespace

import pytest

import boardhog as bh

VALID_REPLY = struct.pack("<BBBBIII", 0, 0x64, 0, 0, 0x42424D50, 0, 0x722ACCE7)


class Network:
    """Fake ping and UDP. `down` addresses miss every echo, `replies` maps an address to its reply (None times out) and
    `sources` to where the reply comes from; `runs` and `sent` record each ping command and UDP request. A ping waits
    on `gate`, an Event, when one is set, and raises `ping_error` when one is set."""

    def __init__(self):
        self.down = set()
        self.replies = {}
        self.sources = {}
        self.runs = []
        self.sent = []
        self.gate = None
        self.ping_error = None
        self.on_send = None

    def run(self, argv, **kwargs):
        self.runs.append(list(argv))
        if self.gate is not None:
            self.gate.wait(5)
        if self.ping_error is not None:
            raise self.ping_error
        return subprocess.CompletedProcess(argv, 1 if argv[-1] in self.down else 0, b"", b"")

    def socket(self, family, kind):
        assert (family, kind) == (socket.AF_INET, socket.SOCK_DGRAM)
        return FakeSocket(self)


class FakeSocket:
    def __init__(self, network):
        self.network = network
        self.timeout = None
        self.target = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settimeout(self, value):
        self.timeout = value

    def sendto(self, data, address):
        if self.network.on_send is not None:
            self.network.on_send()
        self.network.sent.append((bytes(data), address, self.timeout))
        self.target = address

    def recvfrom(self, size):
        reply = self.network.replies.get(self.target[0], VALID_REPLY)
        if reply is None:
            raise TimeoutError("timed out")
        return reply, self.network.sources.get(self.target[0], self.target)


@pytest.fixture(autouse=True)
def network(monkeypatch):
    net = Network()
    monkeypatch.setattr(bh, "subprocess", SimpleNamespace(run=net.run, SubprocessError=subprocess.SubprocessError))
    monkeypatch.setattr(
        bh, "socket", SimpleNamespace(socket=net.socket, AF_INET=socket.AF_INET, SOCK_DGRAM=socket.SOCK_DGRAM)
    )
    return net
