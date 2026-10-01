"""Each chart spec draws a chart py-maidr reads as the layer type maidr.js expects."""

import html
import json
import re

import matplotlib.pyplot as plt
import pytest
from pydantic import TypeAdapter, ValidationError

from maidr_mcp.charts import Chart, render

CHART = TypeAdapter(Chart)


def maidr_layers(rendered):
    """The layers of the MAIDR JSON py-maidr put in the SVG."""
    schema = json.loads(html.unescape(re.search(r'\smaidr="([^"]*)"', rendered.svg).group(1)))
    return [layer for row in schema["subplots"] for cell in row for layer in cell["layers"]]


CANDLES = {
    "type": "candlestick",
    "dates": ["2024-01-02", "2024-01-03", "2024-01-04"],
    "open": [10, 11, 12],
    "high": [12, 13, 14],
    "low": [9, 10, 11],
    "close": [11, 12, 11],
}

CASES = {
    "bar": (
        {"type": "bar", "categories": ["Sat", "Sun", "Thur"], "series": [{"values": [87, 76, 62]}]},
        ["bar"],
        [3],
    ),
    "bar side by side": (
        {
            "type": "bar",
            "categories": ["Adelie", "Gentoo"],
            "series": [
                {"name": "Below", "values": [70, 58]},
                {"name": "Above", "values": [82, 66]},
            ],
        },
        ["dodged_bar"],
        [4],
    ),
    "bar stacked": (
        {
            "type": "bar",
            "stacked": True,
            "categories": ["Adelie", "Gentoo"],
            "series": [
                {"name": "Below", "values": [70, 58]},
                {"name": "Above", "values": [82, 66]},
            ],
        },
        ["stacked_bar"],
        [4],
    ),
    "line over numbers": (
        {
            "type": "line",
            "x": [1, 2, 3],
            "series": [{"name": "a", "values": [1, 4, 2]}, {"name": "b", "values": [2, 1, 3]}],
        },
        ["line"],
        [6],
    ),
    "line over labels": (
        {"type": "line", "x": ["Jan", "Feb", "Mar"], "series": [{"values": [3, 5, 4]}]},
        ["line"],
        [3],
    ),
    "step": (
        {
            "type": "step",
            "where": "pre",
            "x": [1, 2, 3],
            "series": [{"name": "a", "values": [1, 4, 2]}, {"name": "b", "values": [2, 1, 3]}],
        },
        ["step"],
        [6],
    ),
    "scatter": ({"type": "scatter", "x": [1, 2, 3, 4], "y": [3, 1, 2, 5]}, ["point"], [4]),
    "scatter with a trend line": (
        {"type": "scatter", "trend": True, "x": [1, 2, 3, 4], "y": [3, 1, 2, 5]},
        ["point", "smooth"],
        [4, 30],
    ),
    "histogram": ({"type": "histogram", "values": [1, 2, 2, 3, 3, 3, 4], "bins": 4}, ["hist"], [4]),
    "box": (
        {
            "type": "box",
            "groups": [
                {"name": "g1", "values": [1, 2, 3, 4, 10]},
                {"name": "g2", "values": [2, 3, 4, 5, 6]},
            ],
        },
        ["box"],
        [2],
    ),
    "violin": (
        {
            "type": "violin",
            "groups": [
                {"name": "g1", "values": [1, 2, 3, 4, 10]},
                {"name": "g2", "values": [2, 3, 4, 5, 6]},
            ],
        },
        ["violin_box", "violin_kde"],
        [2, 60],
    ),
    "heatmap": (
        {
            "type": "heatmap",
            "x_labels": ["c1", "c2"],
            "y_labels": ["r1", "r2"],
            "values": [[1, 2], [3, 4]],
            "z_label": "Score",
        },
        ["heat"],
        [None],
    ),
    "pie": (
        {"type": "pie", "categories": ["Sat", "Sun", "Thur"], "values": [87, 76, 62]},
        ["pie"],
        [3],
    ),
    "candlestick": (CANDLES, ["candlestick"], [3]),
}


@pytest.mark.parametrize("spec, types, points", CASES.values(), ids=CASES.keys())
def test_each_spec_draws_the_layer_maidr_reads(spec, types, points):
    rendered = render(
        CHART.validate_python({"title": "A chart", "x_label": "X", "y_label": "Y", **spec})
    )
    assert rendered.svg.startswith("<svg")
    assert ' maidr="' in rendered.svg
    assert [layer["type"] for layer in rendered.layers] == types
    assert [layer["points"] for layer in rendered.layers] == points


def test_a_pie_is_read_in_the_order_given():
    pie = {"type": "pie", "categories": ["Sat", "Sun", "Thur", "Fri"], "values": [87, 76, 62, 19]}
    (layer,) = maidr_layers(render(CHART.validate_python(pie)))
    assert [(p["x"], p["y"]) for p in layer["data"]] == [
        ("Sat", 87),
        ("Sun", 76),
        ("Thur", 62),
        ("Fri", 19),
    ]


def test_candles_carry_the_prices_given():
    (layer,) = maidr_layers(render(CHART.validate_python(CANDLES)))
    assert [(c["open"], c["high"], c["low"], c["close"]) for c in layer["data"]] == [
        (10, 12, 9, 11),
        (11, 13, 10, 12),
        (12, 14, 11, 11),
    ]
    assert layer["data"][0]["value"].replace(" 00:00:00", "") in ("2024-01-02", "Jan 02, 2024")


def test_a_violin_reads_no_values_beyond_its_group():
    groups = [{"name": "g1", "values": [1, 2, 3, 4, 10]}, {"name": "g2", "values": [2, 3, 6]}]
    _, kde = maidr_layers(render(CHART.validate_python({"type": "violin", "groups": groups})))
    for group, points in zip(groups, kde["data"], strict=True):
        assert min(group["values"]) <= min(p["y"] for p in points)
        assert max(p["y"] for p in points) <= max(group["values"])


def test_a_candlestick_leaves_the_next_chart_alone():
    # mplfinance applies its style to matplotlib's global settings.
    before = dict(plt.rcParams)
    render(CHART.validate_python(CANDLES))
    assert dict(plt.rcParams) == before
    assert plt.get_fignums() == []


def test_text_from_the_model_stays_text():
    # The view puts the SVG in the page with innerHTML.
    title = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    rendered = render(
        CHART.validate_python(
            {"type": "bar", "title": title, "categories": [title], "series": [{"values": [1]}]}
        )
    )
    assert "<img" not in rendered.svg
    assert "<script" not in rendered.svg


@pytest.mark.parametrize(
    "spec",
    [
        {"type": "bar", "categories": ["a", "a"], "series": [{"values": [1, 2]}]},
        {"type": "bar", "categories": ["a", "b"], "series": [{"values": [1]}]},
        {"type": "bar", "categories": ["a"], "series": [{"values": [float("nan")]}]},
        {
            "type": "bar",
            "categories": [str(i) for i in range(101)],
            "series": [{"values": [1] * 101}],
        },
        {"type": "bar", "categories": ["a"], "series": [{"values": [1]}], "colour": "red"},
        {"type": "line", "x": ["a", "a"], "series": [{"values": [1, 2]}]},
        {"type": "line", "x": [1, float("inf")], "series": [{"values": [1, 2]}]},
        {"type": "scatter", "x": [1, 2], "y": [1]},
        {"type": "heatmap", "x_labels": ["a"], "y_labels": ["r1", "r2"], "values": [[1]]},
        {"type": "line", "x": ["a" * 101], "series": [{"values": [1]}]},
        {"type": "step", "x": [1], "series": [{"values": [1]}], "where": "left"},
        {"type": "scatter", "x": [1, 1], "y": [1, 2], "trend": True},
        {"type": "violin", "groups": [{"name": "a", "values": [1, 2]}] * 2},
        {"type": "violin", "groups": [{"name": "a", "values": [3, 3, 3]}]},
        {"type": "pie", "categories": ["a", "b"], "values": [1, -1]},
        {"type": "pie", "categories": ["a", "b"], "values": [0, 0]},
        {"type": "pie", "categories": ["a", "b"], "values": [1]},
        {**CANDLES, "dates": ["2024-01-02", "2024-01-04", "2024-01-03"]},
        {**CANDLES, "dates": ["2024-01-02", "tomorrow", "2024-01-04"]},
        {**CANDLES, "low": [9, 10, 12]},
        {**CANDLES, "close": [11, 12]},
        {"type": "radar", "values": [1, 2]},
    ],
    ids=[
        "duplicate categories",
        "short series",
        "not a number",
        "too many categories",
        "unknown key",
        "duplicate x labels",
        "infinite x",
        "unpaired scatter",
        "ragged heatmap",
        "long x label",
        "unknown step",
        "trend over one x",
        "duplicate violins",
        "violin without spread",
        "negative slice",
        "empty pie",
        "unpaired pie",
        "dates out of order",
        "not a date",
        "low above open",
        "missing close",
        "unknown type",
    ],
)
def test_bad_specs_are_refused(spec):
    with pytest.raises(ValidationError):
        CHART.validate_python(spec)
