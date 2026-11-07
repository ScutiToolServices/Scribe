import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from src.app import build_app
from src.config import Settings
from tests.conftest import FakeEngine, audio
from tests.seed import seed_token

TOKEN = {"X-Internal-Token": "tok"}
ANN = {"requester": {"username": "ann", "email": "a@x"}}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def port(tmp_path):
    port = free_port()
    settings = seed_token(Settings(host="127.0.0.1", port=port, data_dir=tmp_path, admin_password="pw", max_seconds=10), "tok")
    server = uvicorn.Server(uvicorn.Config(build_app(settings, FakeEngine()), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "the test server did not start"
    yield port
    server.should_exit = True
    thread.join(5)


async def with_session(port: int, work):
    async with create_mcp_http_client(headers=TOKEN, timeout=httpx.Timeout(30)) as http:
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await work(session)


async def test_upload_then_transcribe_over_mcp(port):
    async with httpx.AsyncClient() as http:
        up = await http.post(
            f"http://127.0.0.1:{port}/api/files", content=audio(2.0), headers={**TOKEN, "X-Requester-Username": "ann"}
        )
    file_id = up.json()["file_id"]

    async def work(session):
        names = {t.name for t in (await session.list_tools()).tools}
        result = await session.call_tool("tool_scribe_transcribe", {"file_id": file_id}, meta=ANN)
        return names, result

    names, result = await with_session(port, work)
    assert names == {"tool_scribe_transcribe"} and not result.isError
    body = json.loads(result.content[0].text)
    assert body["text"] == "hello world" and body["cached"] is False and "Transcribed" in body["message"]


async def test_transcribe_without_requester_is_a_tool_error(port):
    async def work(session):
        return await session.call_tool("tool_scribe_transcribe", {"file_id": "a" * 32})

    assert (await with_session(port, work)).isError


async def test_mcp_rejects_a_missing_token(port):
    async with httpx.AsyncClient() as http:
        assert (await http.post(f"http://127.0.0.1:{port}/mcp", json={})).status_code == 401
