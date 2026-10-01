"""Charts drawn from the data a model sends, made accessible by py-maidr.

The model describes a chart as plain data, one of the specs below, and the server
draws it with matplotlib, so the conversation carries numbers rather than an SVG.
py-maidr reads the drawn artists and returns the SVG with its MAIDR JSON in the
``maidr`` attribute, which maidr.js binds in the chart view.
"""

from __future__ import annotations

import html
import json
import os
import re
import threading
import warnings
from dataclasses import dataclass
from typing import Annotated, Any, Literal

# py-maidr also builds a maidr.js loader, which is discarded here; "latest" spares it
# looking the published version up over the network on the first render.
os.environ.setdefault("MAIDR_CDN_VERSION", "latest")

import matplotlib  # noqa: E402

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import seaborn as sns  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field, model_validator  # noqa: E402

with warnings.catch_warnings():
    # "Setting matplotlib backend to maidr": nothing here calls show().
    warnings.simplefilter("ignore", UserWarning)
    import maidr  # noqa: E402

Number = Annotated[float, Field(allow_inf_nan=False)]
Label = Annotated[str, Field(max_length=100)]

MAX_CATEGORIES = 100
MAX_SERIES = 8
MAX_LINE_POINTS = 2000
MAX_SCATTER_POINTS = 2000
MAX_SAMPLE = 50_000
MAX_GROUPS = 20
MAX_HEAT = 50


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Chart(_Spec):
    title: str | None = Field(None, max_length=200, description="The chart's title.")
    x_label: Label | None = Field(None, description="The x axis label.")
    y_label: Label | None = Field(None, description="The y axis label.")


class Series(_Spec):
    name: Label | None = Field(None, description="The series name, shown in the legend.")
    values: list[Number] = Field(min_length=1, max_length=MAX_LINE_POINTS)


class BarChart(_Chart):
    """Bars per category. Several series sit side by side, or stack when ``stacked``."""

    type: Literal["bar"]
    categories: list[Label] = Field(min_length=1, max_length=MAX_CATEGORIES)
    series: list[Series] = Field(
        min_length=1, max_length=MAX_SERIES, description="One value per category in each series."
    )
    stacked: bool = Field(
        False, description="Stack several series instead of placing them side by side."
    )

    @model_validator(mode="after")
    def _one_value_per_category(self) -> BarChart:
        if len(set(self.categories)) != len(self.categories):
            raise ValueError("categories must be distinct")
        for s in self.series:
            if len(s.values) != len(self.categories):
                raise ValueError("each series needs one value per category")
        return self


class LineChart(_Chart):
    """One line per series over a shared x."""

    type: Literal["line"]
    x: list[float | Label] = Field(
        min_length=1, max_length=MAX_LINE_POINTS, description="Numbers, or labels such as dates."
    )
    series: list[Series] = Field(
        min_length=1, max_length=MAX_SERIES, description="One value per x in each series."
    )

    @model_validator(mode="after")
    def _one_value_per_x(self) -> LineChart:
        for s in self.series:
            if len(s.values) != len(self.x):
                raise ValueError("each series needs one value per x")
        if any(isinstance(v, float) and not np.isfinite(v) for v in self.x):
            raise ValueError("x must be finite")
        if not all(isinstance(v, float) for v in self.x) and len({str(v) for v in self.x}) != len(
            self.x
        ):
            raise ValueError("x labels must be distinct")
        return self


class ScatterChart(_Chart):
    type: Literal["scatter"]
    x: list[Number] = Field(min_length=1, max_length=MAX_SCATTER_POINTS)
    y: list[Number] = Field(min_length=1, max_length=MAX_SCATTER_POINTS)

    @model_validator(mode="after")
    def _paired(self) -> ScatterChart:
        if len(self.x) != len(self.y):
            raise ValueError("x and y need the same length")
        return self


class HistogramChart(_Chart):
    type: Literal["histogram"]
    values: list[Number] = Field(min_length=1, max_length=MAX_SAMPLE, description="The raw sample.")
    bins: int | None = Field(
        None, ge=1, le=100, description="Number of bins; chosen automatically when omitted."
    )


class Group(_Spec):
    name: Label
    values: list[Number] = Field(min_length=1, max_length=MAX_SAMPLE)


class BoxChart(_Chart):
    type: Literal["box"]
    groups: list[Group] = Field(
        min_length=1, max_length=MAX_GROUPS, description="One box per group."
    )


class HeatmapChart(_Chart):
    type: Literal["heatmap"]
    x_labels: list[Label] = Field(min_length=1, max_length=MAX_HEAT, description="Column labels.")
    y_labels: list[Label] = Field(
        min_length=1, max_length=MAX_HEAT, description="Row labels, top first."
    )
    values: list[list[Number]] = Field(
        min_length=1, max_length=MAX_HEAT, description="One row per y label, one value per x label."
    )
    z_label: Label | None = Field(None, description="What the values measure.")

    @model_validator(mode="after")
    def _grid(self) -> HeatmapChart:
        if len(self.values) != len(self.y_labels) or any(
            len(r) != len(self.x_labels) for r in self.values
        ):
            raise ValueError("values needs one row per y label and one value per x label")
        return self


Chart = Annotated[
    BarChart | LineChart | ScatterChart | HistogramChart | BoxChart | HeatmapChart,
    Field(discriminator="type"),
]


@dataclass(frozen=True)
class Rendered:
    svg: str
    """The chart's SVG, with its MAIDR JSON in the ``maidr`` attribute."""
    layers: list[dict[str, Any]]
    """Each layer's maidr type and point count, for the model's summary."""


def _series_names(series: list[Series]) -> list[str]:
    return [s.name or f"Series {i + 1}" for i, s in enumerate(series)]


def _draw_bar(ax: plt.Axes, chart: BarChart) -> None:
    names = _series_names(chart.series)
    if len(chart.series) == 1:
        ax.bar(chart.categories, chart.series[0].values)
    elif chart.stacked:
        bottom = np.zeros(len(chart.categories))
        for name, s in zip(names, chart.series, strict=True):
            ax.bar(chart.categories, s.values, bottom=bottom, label=name)
            bottom += np.asarray(s.values)
        ax.legend()
    else:
        x = np.arange(len(chart.categories))
        width = 0.8 / len(chart.series)
        for i, (name, s) in enumerate(zip(names, chart.series, strict=True)):
            ax.bar(x - 0.4 + width * (i + 0.5), s.values, width, label=name)
        ax.set_xticks(x)
        ax.set_xticklabels(chart.categories)
        ax.legend()


def _draw_line(ax: plt.Axes, chart: LineChart) -> None:
    x = chart.x if all(isinstance(v, float) for v in chart.x) else [str(v) for v in chart.x]
    for name, s in zip(_series_names(chart.series), chart.series, strict=True):
        ax.plot(x, s.values, label=name)
    if len(chart.series) > 1:
        ax.legend()


def _draw_scatter(ax: plt.Axes, chart: ScatterChart) -> None:
    ax.scatter(chart.x, chart.y)


def _draw_histogram(ax: plt.Axes, chart: HistogramChart) -> None:
    ax.hist(chart.values, bins=chart.bins or "auto")


def _draw_box(ax: plt.Axes, chart: BoxChart) -> None:
    ax.boxplot([g.values for g in chart.groups], tick_labels=[g.name for g in chart.groups])


def _draw_heatmap(ax: plt.Axes, chart: HeatmapChart) -> None:
    frame = pd.DataFrame(chart.values, index=chart.y_labels, columns=chart.x_labels)
    if chart.z_label:
        sns.heatmap(frame, ax=ax, z_label=chart.z_label)  # z_label is read by py-maidr
    else:
        sns.heatmap(frame, ax=ax)


_DRAW = {
    "bar": _draw_bar,
    "line": _draw_line,
    "scatter": _draw_scatter,
    "histogram": _draw_histogram,
    "box": _draw_box,
    "heatmap": _draw_heatmap,
}

# pyplot and py-maidr's figure registry are process-wide, so draw one chart at a time.
_LOCK = threading.Lock()
_SVG = re.compile(r"<svg\b.*?</svg>", re.S)
_MAIDR_ATTR = re.compile(r'<svg\b[^>]*?\smaidr="([^"]*)"')


def render(
    chart: BarChart | LineChart | ScatterChart | HistogramChart | BoxChart | HeatmapChart,
) -> Rendered:
    """Draw ``chart`` and return its accessible SVG.

    Raises:
        RuntimeError: if py-maidr did not produce a chart it can read.
    """
    with _LOCK:
        fig, ax = plt.subplots(figsize=(6.4, 4.4))
        try:
            _DRAW[chart.type](ax, chart)
            if chart.title:
                ax.set_title(chart.title)
            if chart.x_label:
                ax.set_xlabel(chart.x_label)
            if chart.y_label:
                ax.set_ylabel(chart.y_label)
            fig.tight_layout()
            page = maidr.render(fig, use_cdn=True).get_html_string()
        finally:
            maidr.close(fig)
            plt.close(fig)
    svg = _SVG.search(page)
    attr = _MAIDR_ATTR.search(svg.group(0)) if svg else None
    if attr is None:
        raise RuntimeError("py-maidr returned no accessible chart")
    schema = json.loads(html.unescape(attr.group(1)))
    layers = [
        {"type": layer.get("type"), "points": _count(layer.get("data"))}
        for row in schema.get("subplots", [])
        for cell in row
        for layer in cell.get("layers", [])
    ]
    return Rendered(svg=svg.group(0), layers=layers)


def _count(data: Any) -> int | None:
    if isinstance(data, list):
        return (
            sum(_count(d) or 0 for d in data) if data and isinstance(data[0], list) else len(data)
        )
    return None
