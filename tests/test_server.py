"""What a host sees of the server: tools, the chart view, and calls relayed through it."""

import re
from typing import Any

import anyio
import pytest
from mcp import Client

from maidr_mcp.relay import POLL_SECONDS, Relay
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
    "update_chart",
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


def test_the_view_waits_on_a_long_poll_as_long_as_the_server_holds_it():
    # The view reads a long poll that comes back early and empty as cut by the host. Held for
    # less than LONG_WAIT, every long poll would look cut and the view would fall back to
    # short polls on every host.
    long_wait = re.search(r"const LONG_WAIT = (\d+);", view_html())
    assert long_wait is not None
    assert float(long_wait.group(1)) == POLL_SECONDS


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


async def test_the_view_says_how_long_its_poll_may_wait():
    async with Client(build_server()) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        wait = tools["maidr_view_poll"].input_schema["properties"]["wait"]
        assert (wait["minimum"], wait["maximum"], wait["default"]) == (0, 20, 20)
        with anyio.fail_after(2):  # a short poll, for a host that cuts long ones
            polled = await client.call_tool("maidr_view_poll", {"viewId": "v", "wait": 0})
        assert polled.structured_content == {"calls": [], "revision": 0}
        for bad in (-1, 21, "soon"):
            refused = await client.call_tool("maidr_view_poll", {"viewId": "v", "wait": bad})
            assert refused.is_error, bad


async def test_a_poll_without_a_wait_holds_as_long_as_the_relay_allows():
    async with Client(build_server(Relay(poll_seconds=0.2))) as client:
        started = anyio.current_time()
        polled = await client.call_tool("maidr_view_poll", {"viewId": "v"})
        assert anyio.current_time() - started >= 0.2
    assert polled.structured_content == {"calls": [], "revision": 0}


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


LINE = {
    "type": "line",
    "title": "Tips by hour",
    "x": ["Lunch", "Dinner", "Late"],
    "series": [{"values": [12, 30, 7]}],
}


async def test_update_chart_replaces_the_chart_in_the_open_view():
    async with Client(build_server()) as client:
        shown = await client.call_tool("show_chart", {"chart": BAR})
        view_id = shown.structured_content["viewId"]

        async def view():
            polled = await client.call_tool("maidr_view_poll", {"viewId": view_id, "revision": 0})
            (call,) = polled.structured_content["calls"]
            assert call["tool"] == "maidr_view_update"
            assert polled.structured_content["revision"] == 1
            assert "Dinner" in polled.structured_content["svg"]  # the poll brings the chart
            fetched = await client.call_tool("maidr_view_svg", {"viewId": view_id})
            assert fetched.structured_content["revision"] == 1
            assert "Dinner" in fetched.structured_content["svg"]
            assert "Sat" not in fetched.structured_content["svg"]
            await client.call_tool(
                "maidr_view_reply",
                {
                    "viewId": view_id,
                    "callId": call["callId"],
                    "result": {"ok": True, "readerInChart": True},
                },
            )

        async with anyio.create_task_group() as tg:
            tg.start_soon(view)
            updated = await client.call_tool("update_chart", {"viewId": view_id, "chart": LINE})
    assert not updated.is_error
    assert updated.structured_content == {
        "viewId": view_id,
        "layers": [{"type": "line", "points": 3}],
        "readerInChart": True,
    }
    text = updated.content[0].text
    assert view_id in text and "line (3 points)" in text and "still in it" in text
    assert updated.meta is None or SVG_META_KEY not in updated.meta  # the model gets no SVG


async def test_update_chart_takes_every_chart_type_show_chart_does():
    pie = {"type": "pie", "title": "Tips by day", "categories": ["Sat", "Sun"], "values": [87, 76]}
    async with Client(build_server()) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        shown = await client.call_tool("show_chart", {"chart": BAR})
        view_id = shown.structured_content["viewId"]

        async def view():
            polled = await client.call_tool("maidr_view_poll", {"viewId": view_id, "revision": 0})
            (call,) = polled.structured_content["calls"]
            await client.call_tool(
                "maidr_view_reply",
                {
                    "viewId": view_id,
                    "callId": call["callId"],
                    "result": {"ok": True, "readerInChart": False},
                },
            )

        async with anyio.create_task_group() as tg:
            tg.start_soon(view)
            updated = await client.call_tool("update_chart", {"viewId": view_id, "chart": pie})
    show, update = tools["show_chart"].input_schema, tools["update_chart"].input_schema
    assert update["properties"]["chart"] == show["properties"]["chart"]
    assert update.get("$defs") == show.get("$defs")
    assert not updated.is_error
    assert updated.structured_content["layers"] == [{"type": "pie", "points": 2}]
    assert 'pie chart "Tips by day"' in updated.content[0].text


async def test_update_chart_on_an_unknown_view_tells_the_model_to_show_it():
    async with Client(build_server()) as client:
        result = await client.call_tool("update_chart", {"viewId": "nope", "chart": LINE})
    assert result.is_error
    assert "unknown viewId" in result.content[0].text
    assert "show_chart" in result.content[0].text


async def test_an_update_the_view_does_not_answer_is_kept_for_it():
    async with Client(build_server(Relay(reply_seconds=0.1))) as client:
        shown = await client.call_tool("show_chart", {"chart": BAR})
        view_id = shown.structured_content["viewId"]
        result = await client.call_tool("update_chart", {"viewId": view_id, "chart": LINE})
        assert result.is_error
        assert "did not answer" in result.content[0].text
        assert "keeps the new chart" in result.content[0].text
        fetched = await client.call_tool("maidr_view_svg", {"viewId": view_id})
    assert fetched.structured_content["revision"] == 1
    assert "Dinner" in fetched.structured_content["svg"]


async def test_a_view_behind_is_answered_at_once_with_the_chart_it_missed():
    async with Client(build_server(Relay(reply_seconds=0.1))) as client:
        shown = await client.call_tool("show_chart", {"chart": BAR})
        view_id = shown.structured_content["viewId"]
        await client.call_tool("update_chart", {"viewId": view_id, "chart": LINE})  # unanswered
        with anyio.fail_after(2):  # a long poll, from a view still showing show_chart's chart
            behind = await client.call_tool(
                "maidr_view_poll", {"viewId": view_id, "revision": 0, "wait": 20}
            )
            current = await client.call_tool(
                "maidr_view_poll", {"viewId": view_id, "revision": 1, "wait": 0}
            )
    assert behind.structured_content["calls"] == []
    assert behind.structured_content["revision"] == 1
    assert "Dinner" in behind.structured_content["svg"]
    assert current.structured_content == {"calls": [], "revision": 1}  # no SVG once caught up


async def test_an_update_the_view_could_not_make_is_an_error():
    async with Client(build_server()) as client:
        shown = await client.call_tool("show_chart", {"chart": BAR})
        view_id = shown.structured_content["viewId"]

        async def view():
            polled = await client.call_tool("maidr_view_poll", {"viewId": view_id})
            (call,) = polled.structured_content["calls"]
            await client.call_tool(
                "maidr_view_reply",
                {
                    "viewId": view_id,
                    "callId": call["callId"],
                    "result": {"ok": False, "error": "maidr did not take up the new chart"},
                },
            )

        async with anyio.create_task_group() as tg:
            tg.start_soon(view)
            result = await client.call_tool("update_chart", {"viewId": view_id, "chart": LINE})
    assert result.is_error
    assert result.content[0].text.startswith("maidr did not take up the new chart. The server")


def test_only_the_arguments_given_are_passed_on():
    assert _given({"layerId": "L", "chartId": None, "row": 0}) == {"layerId": "L", "row": 0}
