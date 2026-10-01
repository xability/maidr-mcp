"""Each chart spec draws a chart py-maidr reads as the layer type maidr.js expects."""

import pytest
from pydantic import TypeAdapter, ValidationError

from maidr_mcp.charts import Chart, render

CHART = TypeAdapter(Chart)

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
    "scatter": ({"type": "scatter", "x": [1, 2, 3, 4], "y": [3, 1, 2, 5]}, ["point"], [4]),
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
        {"type": "pie", "values": [1, 2]},
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
        "unknown type",
    ],
)
def test_bad_specs_are_refused(spec):
    with pytest.raises(ValidationError):
        CHART.validate_python(spec)
