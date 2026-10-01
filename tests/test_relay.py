"""The relay hands a model call to the view that polls for it, and the answer back."""

import anyio
import pytest

from maidr_mcp.relay import UNKNOWN_VIEW, UPDATE, Relay

pytestmark = pytest.mark.anyio


async def answer(relay, view_id, result):
    """A view: poll once, answer every call."""
    for call in await relay.poll(view_id):
        assert relay.reply(
            view_id, call.call_id, result | {"tool": call.tool, "arguments": call.arguments}
        )


async def test_a_call_reaches_the_view_and_its_answer_comes_back():
    relay = Relay()
    view_id = relay.open(svg="<svg/>")
    async with anyio.create_task_group() as tg:
        tg.start_soon(answer, relay, view_id, {"ok": True})
        result = await relay.call(view_id, "maidr_navigate", {"layerId": "L", "row": 0, "col": 1})
    assert result == {
        "ok": True,
        "tool": "maidr_navigate",
        "arguments": {"layerId": "L", "row": 0, "col": 1},
    }


async def test_a_call_waiting_before_the_poll_is_picked_up():
    relay = Relay()
    view_id = relay.open()
    async with anyio.create_task_group() as tg:
        tg.start_soon(relay.call, view_id, "maidr_list_charts", {})
        await anyio.sleep(0.05)
        calls = await relay.poll(view_id)
        assert [c.tool for c in calls] == ["maidr_list_charts"]
        relay.reply(view_id, calls[0].call_id, {"ok": True})


async def test_an_unknown_view_is_an_error_the_model_can_act_on():
    result = await Relay().call("nope", "maidr_list_charts", {})
    assert result["ok"] is False and "Show it again" in result["error"]


async def test_an_unanswered_call_times_out_and_is_withdrawn():
    relay = Relay(reply_seconds=0.1, poll_seconds=0.1)
    view_id = relay.open()
    result = await relay.call(view_id, "maidr_navigate", {"layerId": "L"})
    assert result["ok"] is False and "did not answer" in result["error"]
    assert await relay.poll(view_id) == []  # a view that comes back late does not run it


async def test_an_answer_must_come_from_the_view_that_was_asked():
    relay = Relay(reply_seconds=0.2)
    view_id, other = relay.open(), relay.open()
    async with anyio.create_task_group() as tg:

        async def impostor():
            calls = await relay.poll(view_id)
            assert relay.reply(other, calls[0].call_id, {"ok": True}) is False

        tg.start_soon(impostor)
        result = await relay.call(view_id, "maidr_list_charts", {})
    assert result["ok"] is False


async def test_a_poll_with_nothing_to_do_returns_empty():
    relay = Relay(poll_seconds=0.05)
    assert await relay.poll(relay.open()) == []


async def test_a_short_poll_answers_at_once():
    relay = Relay()  # a long poll would wait 20 seconds
    view_id = relay.open()
    with anyio.fail_after(1):
        assert await relay.poll(view_id, wait=0) == []
    async with anyio.create_task_group() as tg:
        tg.start_soon(relay.call, view_id, "maidr_list_charts", {})
        await anyio.sleep(0.01)
        with anyio.fail_after(1):
            calls = await relay.poll(view_id, wait=0)
        assert [c.tool for c in calls] == ["maidr_list_charts"]
        relay.reply(view_id, calls[0].call_id, {"ok": True})


async def test_a_poll_waits_no_longer_than_the_relay_allows():
    relay = Relay(poll_seconds=0.05)
    view_id = relay.open()
    with anyio.fail_after(1):
        assert await relay.poll(view_id, wait=20) == []
        assert await relay.poll(view_id, wait=-1) == []  # taken as 0


async def test_a_poll_the_host_cut_short_leaves_later_calls_to_the_view():
    # The host gave up on the view's long poll, but the server's is still waiting. The view
    # polls again, short; a call that comes after must go to the view, not the abandoned poll.
    relay = Relay()
    view_id = relay.open()
    abandoned, result = [], {}

    async def abandoned_poll():
        abandoned.extend(await relay.poll(view_id, wait=0.2))

    async def model():
        result.update(await relay.call(view_id, "maidr_list_charts", {}))

    async with anyio.create_task_group() as tg:
        tg.start_soon(abandoned_poll)
        await anyio.sleep(0.01)
        assert await relay.poll(view_id, wait=0) == []
        tg.start_soon(model)
        await anyio.sleep(0.3)  # the abandoned poll runs out, and must take nothing
        assert abandoned == []
        await answer(relay, view_id, {"ok": True})  # the view's next poll
    assert result["ok"] is True


async def test_a_view_outliving_a_restart_is_taken_back():
    relay = Relay()
    async with anyio.create_task_group() as tg:
        tg.start_soon(answer, relay, "from-before-the-restart", {"ok": True})
        await anyio.sleep(0.05)
        result = await relay.call("from-before-the-restart", "maidr_list_charts", {})
    assert result["ok"] is True


async def test_idle_views_are_forgotten():
    relay = Relay(idle_seconds=0)
    old = relay.open(svg="<svg/>")
    await anyio.sleep(0.01)
    relay.open()
    assert relay.svg(old) is None


async def test_a_cancelled_call_leaves_nothing_behind():
    relay = Relay(max_queue=1)
    view_id = relay.open()
    with anyio.move_on_after(0.05):  # the model's request goes away mid-wait
        await relay.call(view_id, "maidr_list_charts", {})
    assert relay._waiting == {}
    assert relay._views[view_id].queue == []  # the slot is free for the next call


async def test_limits():
    relay = Relay(max_views=1, max_queue=1, reply_seconds=0.1)
    view_id = relay.open()
    with pytest.raises(RuntimeError):
        relay.open()
    async with anyio.create_task_group() as tg:
        tg.start_soon(relay.call, view_id, "maidr_list_charts", {})
        await anyio.sleep(0.01)
        result = await relay.call(view_id, "maidr_list_charts", {})
    assert result == {"ok": False, "error": "too many calls are waiting for this chart"}


async def test_an_update_replaces_the_chart_and_reaches_the_view():
    relay = Relay()
    view_id = relay.open(svg="<svg>old</svg>")
    async with anyio.create_task_group() as tg:
        tg.start_soon(answer, relay, view_id, {"ok": True, "readerInChart": False})
        result = await relay.update(view_id, "<svg>new</svg>")
    assert result == {"ok": True, "readerInChart": False, "tool": UPDATE, "arguments": {}}
    assert relay.svg(view_id) == "<svg>new</svg>"
    assert relay.revision(view_id) == 1


async def test_an_update_to_an_unknown_view_is_an_error_naming_show_chart():
    relay = Relay()
    result = await relay.update("nope", "<svg/>")
    assert result == {"ok": False, "error": UNKNOWN_VIEW}
    assert "show_chart" in UNKNOWN_VIEW
    assert not relay.holds("nope")


async def test_an_update_nobody_answers_is_kept_for_the_view_to_catch_up():
    relay = Relay(reply_seconds=0.05)
    view_id = relay.open(svg="<svg>old</svg>")
    result = await relay.update(view_id, "<svg>new</svg>")
    assert result["ok"] is False and "did not answer" in result["error"]
    assert relay.svg(view_id) == "<svg>new</svg>"
    # A view still showing revision 0 is answered at once, so that it fetches the new chart.
    with anyio.fail_after(1):
        assert await relay.poll(view_id, revision=0) == []
    # One showing it waits for calls as usual.
    with anyio.move_on_after(0.05) as waiting:
        await relay.poll(view_id, revision=1)
    assert waiting.cancelled_caught


async def test_a_view_taken_back_after_a_restart_keeps_the_revision_it_shows():
    relay = Relay(poll_seconds=0.01)
    assert await relay.poll("from-before-the-restart", revision=2) == []
    assert relay.revision("from-before-the-restart") == 2
    assert relay.svg("from-before-the-restart") is None


async def test_an_update_is_refused_without_replacing_when_the_queue_is_full():
    relay = Relay(max_queue=1, reply_seconds=0.1)
    view_id = relay.open(svg="<svg>old</svg>")
    async with anyio.create_task_group() as tg:
        tg.start_soon(relay.call, view_id, "maidr_list_charts", {})
        await anyio.sleep(0.01)
        result = await relay.update(view_id, "<svg>new</svg>")
    assert result == {"ok": False, "error": "too many calls are waiting for this chart"}
    assert relay.svg(view_id) == "<svg>old</svg>"
    assert relay.revision(view_id) == 0


async def test_a_view_behind_is_answered_at_once_whatever_its_wait_and_its_poll_is_the_latest():
    # update_chart's call went unanswered, so the server is on revision 1. A copy of the view
    # still on revision 0, mounted again from show_chart's result, long-polls: it is answered at
    # once, and its poll is the latest, so a call after it is not taken by the poll before.
    relay = Relay(reply_seconds=0.5)
    view_id = relay.open(svg="<svg>old</svg>")
    await relay.update(view_id, "<svg>new</svg>")
    earlier, result = [], {}

    async def earlier_poll():
        earlier.extend(await relay.poll(view_id, revision=1, wait=0.2))

    async def model():
        result.update(await relay.call(view_id, "maidr_list_charts", {}))

    async with anyio.create_task_group() as tg:
        tg.start_soon(earlier_poll)
        await anyio.sleep(0.01)
        with anyio.fail_after(1):
            assert await relay.poll(view_id, revision=0, wait=20) == []
        tg.start_soon(model)
        await anyio.sleep(0.25)  # the earlier poll runs out, and must take nothing
        assert earlier == []
        await answer(relay, view_id, {"ok": True})  # the view's next poll
    assert result["ok"] is True
