"""What a host sees of the server: tools, the chart view, and calls relayed through it."""

from typing import Any

import anyio
import pytest
from mcp import Client

from maidr_mcp.relay import Relay
from maidr_mcp.server import (
    CDN,
    INSTRUCTIONS,
    MAIDR_JS_VERSION,
    SVG_META_KEY,
    VIEW_URI,
    _given,
    build_server,
    view_html,
)

pytestmark = pytest.mark.anyio

BAR = {
    "type": "bar",
    "title": "Tips",
    "categories": ["Sat", "Sun"],
    "series": [{"values": [87, 76]}],
}

MODEL_TOOLS = (
    "maidr_list_charts",
    "maidr_get_layer_data",
    "maidr_navigate",
    "maidr_list_commands",
    "maidr_run_command",
)

# The ids maidr's own maidr_run_command offers (the enum of its inputSchema, in its order;
# maidr's docs/WEBMCP.md lists them). e2e/run.sh checks RunnableCommand in server.py against
# the pinned maidr.js; keep this list equal to it.
MAIDR_RUNNABLE_COMMANDS = [
    "move_left",
    "move_right",
    "move_up",
    "move_down",
    "move_to_left_extreme",
    "move_to_right_extreme",
    "move_to_top_extreme",
    "move_to_bottom_extreme",
    "next_layer",
    "previous_layer",
    "return_to_subplot",
    "enter_grid_cell",
    "announce_point",
    "announce_position",
    "toggle_text",
    "toggle_sound",
    "toggle_braille",
    "toggle_high_contrast",
    "toggle_monitor",
    "autoplay_forward",
    "autoplay_backward",
    "autoplay_upward",
    "autoplay_downward",
    "stop_autoplay",
    "speed_up_autoplay",
    "speed_down_autoplay",
    "reset_autoplay_speed",
    "go_to_min_value",
    "go_to_max_value",
    "next_navigation_mode",
    "previous_navigation_mode",
    "tactile_zoom_in",
    "tactile_zoom_out",
    "tactile_reset_zoom",
]


async def test_tools_carry_the_mcp_apps_metadata_hosts_read():
    async with Client(build_server()) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert tools["show_chart"].meta["ui"] == {"resourceUri": VIEW_URI}
    for name in ("maidr_view_poll", "maidr_view_reply", "maidr_view_svg"):
        assert tools[name].meta["ui"]["visibility"] == ["app"], name
    for name in MODEL_TOOLS:
        # A UI on these would mount a new view per call instead of driving the open one.
        assert not (tools[name].meta or {}).get("ui"), name
        assert "viewId" in tools[name].input_schema["required"], name


def test_the_instructions_say_when_to_call_each_model_tool():
    for name in ("show_chart", *MODEL_TOOLS):
        assert name in INSTRUCTIONS, name
    assert "on-next-focus" in INSTRUCTIONS
    # Within a turn the model context is stale; maidr_list_charts reads the position live.
    assert "reader.position is live" in INSTRUCTIONS


def test_the_view_tells_the_reader_when_the_model_left_something_waiting():
    html = view_html()
    assert '<p id="status" role="status">' in html
    assert "waiting for you: Tab into the chart" in html


async def test_the_view_is_an_mcp_app_loading_only_from_jsdelivr():
    async with Client(build_server()) as client:
        content = (await client.read_resource(VIEW_URI)).contents[0]
    assert content.mime_type == "text/html;profile=mcp-app"
    assert content.meta["ui"]["csp"] == {"resourceDomains": [CDN]}
    assert f"{CDN}/npm/maidr@{MAIDR_JS_VERSION}/dist/maidr.js" in content.text
    assert f"{CDN}/npm/@modelcontextprotocol/ext-apps@" in content.text
    assert "{{" not in content.text


def test_the_maidr_js_version_can_be_pinned(monkeypatch):
    monkeypatch.setenv("MAIDR_MCP_MAIDR_JS_VERSION", "4.10.0")
    assert "maidr@4.10.0/dist/maidr.js" in view_html()


async def test_show_chart_gives_the_model_a_view_id_and_the_view_its_svg():
    async with Client(build_server()) as client:
        result = await client.call_tool("show_chart", {"chart": BAR})
    assert not result.is_error
    view_id = result.structured_content["viewId"]
    assert result.structured_content["layers"] == [{"type": "bar", "points": 2}]
    assert view_id in result.content[0].text
    assert result.meta[SVG_META_KEY].startswith("<svg")


async def test_a_model_call_is_relayed_to_the_view_and_answered():
    async with Client(build_server()) as client:
        shown = await client.call_tool("show_chart", {"chart": BAR})
        view_id = shown.structured_content["viewId"]

        async def view():
            polled = await client.call_tool("maidr_view_poll", {"viewId": view_id})
            (call,) = polled.structured_content["calls"]
            assert call["tool"] == "maidr_navigate"
            assert call["arguments"] == {"layerId": "L", "row": 0, "col": 1}  # no unasked keys
            await client.call_tool(
                "maidr_view_reply",
                {
                    "viewId": view_id,
                    "callId": call["callId"],
                    "result": {"ok": True, "applied": "now"},
                },
            )

        async with anyio.create_task_group() as tg:
            tg.start_soon(view)
            moved = await client.call_tool(
                "maidr_navigate", {"viewId": view_id, "layerId": "L", "row": 0, "col": 1}
            )
        assert moved.structured_content == {"ok": True, "applied": "now"}

        svg = await client.call_tool("maidr_view_svg", {"viewId": view_id})
        assert svg.structured_content["svg"].startswith("<svg")


async def test_a_layer_without_a_point_count_is_named_without_one():
    heatmap = {"type": "heatmap", "x_labels": ["a"], "y_labels": ["r"], "values": [[1]]}
    async with Client(build_server()) as client:
        result = await client.call_tool("show_chart", {"chart": heatmap})
    assert "maidr reads it as heat." in result.content[0].text
    assert "None" not in result.content[0].text


async def test_the_command_tools_keep_maidrs_names_arguments_and_hints():
    async with Client(build_server()) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    listing, running = tools["maidr_list_commands"], tools["maidr_run_command"]
    assert set(listing.input_schema["properties"]) == {"viewId", "chartId"}
    assert listing.input_schema["required"] == ["viewId"]
    assert listing.annotations.read_only_hint is True
    assert set(running.input_schema["properties"]) == {"viewId", "command", "chartId"}
    assert sorted(running.input_schema["required"]) == ["command", "viewId"]
    # Toggles flip a mode: running one is not read-only, nor destructive.
    assert running.annotations.read_only_hint is False
    assert running.annotations.destructive_hint is False
    assert "on-next-focus" in running.description


async def test_run_command_offers_the_commands_maidr_runs():
    async with Client(build_server()) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    command = tools["maidr_run_command"].input_schema["properties"]["command"]
    assert command["enum"] == MAIDR_RUNNABLE_COMMANDS
    assert "open_settings" not in command["enum"]  # opens a dialog: the reader's own


async def _relay_once(client: Client, view_id: str, tool: str, arguments: dict[str, Any]):
    """Plays the view for one model call: returns the call it was handed, and answers it."""
    handed = {}

    async def view():
        polled = await client.call_tool("maidr_view_poll", {"viewId": view_id})
        (call,) = polled.structured_content["calls"]
        handed.update(call)
        await client.call_tool(
            "maidr_view_reply",
            {"viewId": view_id, "callId": call["callId"], "result": {"ok": True, "echo": True}},
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(view)
        result = await client.call_tool(tool, {"viewId": view_id, **arguments})
    assert result.structured_content == {"ok": True, "echo": True}
    return handed


async def test_the_command_tools_are_relayed_to_maidr_unchanged():
    async with Client(build_server()) as client:
        shown = await client.call_tool("show_chart", {"chart": BAR})
        view_id = shown.structured_content["viewId"]

        listed = await _relay_once(client, view_id, "maidr_list_commands", {})
        assert (listed["tool"], listed["arguments"]) == ("maidr_list_commands", {})
        listed = await _relay_once(client, view_id, "maidr_list_commands", {"chartId": "c"})
        assert listed["arguments"] == {"chartId": "c"}

        ran = await _relay_once(client, view_id, "maidr_run_command", {"command": "toggle_braille"})
        assert ran["tool"] == "maidr_run_command"
        assert ran["arguments"] == {"command": "toggle_braille"}  # no chartId it was not given
        ran = await _relay_once(
            client, view_id, "maidr_run_command", {"command": "autoplay_forward", "chartId": "c"}
        )
        assert ran["arguments"] == {"chartId": "c", "command": "autoplay_forward"}


async def test_a_command_maidr_does_not_run_never_reaches_the_view():
    relay = Relay(poll_seconds=0.1)
    async with Client(build_server(relay)) as client:
        shown = await client.call_tool("show_chart", {"chart": BAR})
        view_id = shown.structured_content["viewId"]
        for command in ("open_settings", "delete_everything"):
            result = await client.call_tool(
                "maidr_run_command", {"viewId": view_id, "command": command}
            )
            assert result.is_error, command
        polled = await client.call_tool("maidr_view_poll", {"viewId": view_id})
    assert polled.structured_content["calls"] == []


def test_only_the_arguments_given_are_passed_on():
    assert _given({"layerId": "L", "chartId": None, "row": 0}) == {"layerId": "L", "row": 0}
