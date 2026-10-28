"""Execute installed WebSocket SDK connection assembly against fake sockets."""
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from desktop import cloud_ai, cloud_speech


@pytest.fixture
def transport(monkeypatch):
    sdk = pytest.importorskip("websockets.sync.client")
    calls, canceled = [], threading.Event()
    run = cloud_ai._Run(time.monotonic() + 2, canceled, lambda: True)
    class Raw:
        family, type, proto = socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP

        def settimeout(self, value):
            calls.append(("timeout", value))

        def connect(self, address):
            calls.append(("tcp", address))

        def setsockopt(self, *args):
            calls.append(("socket_option", args))

        def shutdown(self, _how):
            calls.append(("shutdown",))

        def close(self):
            calls.append(("close",))
    class Secured(Raw):
        def do_handshake(self):
            calls.append(("tls",))
    raw, secured = Raw(), Secured()
    class Context:
        def wrap_socket(self, value, **options):
            assert value is raw
            assert options == {"server_hostname": "api.openai.com", "do_handshake_on_connect": False}
            calls.append(("wrap",))
            return secured
    class Connection:
        def __init__(self, received_socket, protocol, **options):
            assert received_socket is secured
            assert options == {"ping_interval": None, "ping_timeout": None, "close_timeout": .1, "max_queue": 4}
            self.protocol = protocol
            self.recv_events_thread = SimpleNamespace(join=lambda: None)
            calls.append(("sdk_connection",))

        def handshake(self, headers, user_agent, timeout):
            assert headers == {"Authorization": "Bearer SYNTHETIC_NEVER_A_KEY"}
            assert user_agent == "JarvisDesktop/4" and 0 < timeout <= 2
            calls.append(("authorization",))

        def start_keepalive(self):
            calls.append(("keepalive",))

        def close_socket(self):
            calls.append(("sdk_close",))
    monkeypatch.setattr(sdk, "ClientConnection", Connection)
    monkeypatch.setattr(cloud_speech.ssl, "create_default_context", Context)
    monkeypatch.setattr(cloud_speech.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))])
    monkeypatch.setattr(cloud_speech.socket, "socket", lambda *_args: raw)
    def connect(uri=None):
        return cloud_speech._connect(uri or "wss://api.openai.com/v1/realtime?model=" + cloud_speech.DEFAULT_MODEL,
            run=run, additional_headers={"Authorization": "Bearer SYNTHETIC_NEVER_A_KEY"},
            proxy=None, compression=None, open_timeout=2, close_timeout=.1, ping_interval=None,
            ping_timeout=None, max_size=cloud_speech.MAX_EVENT_BYTES, max_queue=4,
            logger=cloud_speech._LOGGER, user_agent_header="JarvisDesktop/4")
    yield SimpleNamespace(connect=connect, calls=calls, raw=raw, secured=secured, canceled=canceled, run=run)
    run.interrupt()


def test_real_sdk_accepts_verified_tls_wrapper_and_finite_connection_options(transport):
    connection = transport.connect()
    names = [entry[0] for entry in transport.calls]
    assert names.index("tcp") < names.index("wrap") < names.index("tls") < names.index("authorization")
    assert names.count("authorization") == 1
    assert connection.protocol.uri.host == "api.openai.com"
    assert connection.protocol.max_message_size == cloud_speech.MAX_EVENT_BYTES
    assert transport.raw in transport.run.sockets and transport.secured in transport.run.sockets


def test_actual_sdk_assembly_cannot_authorize_after_tls_cancellation(transport):
    def finish_tls_canceled():
        transport.calls.append(("tls",))
        transport.canceled.set()
    transport.secured.do_handshake = finish_tls_canceled
    with pytest.raises(cloud_ai.CloudCancelled):
        transport.connect()
    assert "authorization" not in [entry[0] for entry in transport.calls]
    assert "sdk_connection" not in [entry[0] for entry in transport.calls]


def test_canceled_tcp_completion_does_not_enter_tls_or_sdk_authentication(transport):
    def late_connect(_address):
        transport.canceled.set()
    transport.raw.connect = late_connect
    with pytest.raises(cloud_ai.CloudCancelled):
        transport.connect()
    assert not {"wrap", "tls", "sdk_connection", "authorization"} & {entry[0] for entry in transport.calls}


@pytest.mark.parametrize("uri", ["ws://api.openai.com/v1/realtime", "wss://foreign.invalid/v1/realtime",
                                  "wss://api.openai.com/v1/realtime?model=unapproved"])
def test_off_origin_or_non_allowlisted_model_never_reaches_tcp(transport, uri):
    with pytest.raises(cloud_ai.CloudUnavailable, match="origin_refused"):
        transport.connect(uri)
    assert transport.calls == []
