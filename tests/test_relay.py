"""The relay hands a model call to the view that polls for it, and the answer back."""

import anyio
import pytest

from maidr_mcp.relay import Relay

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
