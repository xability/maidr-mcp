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
from matplotlib.figure import Figure  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field, model_validator  # noqa: E402

with warnings.catch_warnings():
    # "Setting matplotlib backend to maidr": nothing here calls show().
    warnings.simplefilter("ignore", UserWarning)
    import maidr  # noqa: E402

# Imported after maidr, which patches mplfinance.plot as it is imported.
import mplfinance as mpf  # noqa: E402

Number = Annotated[float, Field(allow_inf_nan=False)]
Label = Annotated[str, Field(max_length=100)]

MAX_CATEGORIES = 100
MAX_SERIES = 8
MAX_LINE_POINTS = 2000
MAX_SCATTER_POINTS = 2000
MAX_SAMPLE = 50_000
MAX_GROUPS = 20
MAX_HEAT = 50
MAX_CANDLES = 500
MAX_SLICES = 30


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


class StepChart(LineChart):
    """One line per series that holds each value, then jumps to the next."""

    type: Literal["step"]
    where: Literal["post", "pre", "mid"] = Field(
        "post",
        description="post: each value holds until the next x; pre: back to the previous x; "
        "mid: the jump falls halfway between.",
    )


class ScatterChart(_Chart):
    type: Literal["scatter"]
    x: list[Number] = Field(min_length=1, max_length=MAX_SCATTER_POINTS)
    y: list[Number] = Field(min_length=1, max_length=MAX_SCATTER_POINTS)
    trend: bool = Field(False, description="Add a straight trend line fitted by least squares.")

    @model_validator(mode="after")
    def _paired(self) -> ScatterChart:
        if len(self.x) != len(self.y):
            raise ValueError("x and y need the same length")
        if self.trend and len(set(self.x)) < 2:
            raise ValueError("a trend line needs at least two different x values")
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


class ViolinChart(_Chart):
    """One violin per group: the shape of its distribution, with its box plot inside."""

    type: Literal["violin"]
    groups: list[Group] = Field(
        min_length=1, max_length=MAX_GROUPS, description="One violin per group."
    )

    @model_validator(mode="after")
    def _spread(self) -> ViolinChart:
        if len({g.name for g in self.groups}) != len(self.groups):
            raise ValueError("group names must be distinct")
        # A group without spread has no density to draw, and maidr would then read fewer
        # violins than boxes.
        if any(len(set(g.values)) < 2 for g in self.groups):
            raise ValueError("each group needs at least two different values")
        return self


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


class PieChart(_Chart):
    """Slices of a whole, read clockwise from 12 o'clock in the order given."""

    type: Literal["pie"]
    categories: list[Label] = Field(
        min_length=1, max_length=MAX_SLICES, description="Slice labels."
    )
    values: list[Annotated[float, Field(ge=0, allow_inf_nan=False)]] = Field(
        min_length=1,
        max_length=MAX_SLICES,
        description="One value per slice; maidr works out each slice's share.",
    )
    # A pie has no axes: py-maidr reads these two as the names of a slice's label and value.
    x_label: Label | None = Field(None, description="What the slices are, such as Day.")
    y_label: Label | None = Field(None, description="What the values measure.")

    @model_validator(mode="after")
    def _one_value_per_slice(self) -> PieChart:
        if len(set(self.categories)) != len(self.categories):
            raise ValueError("categories must be distinct")
        if len(self.values) != len(self.categories):
            raise ValueError("values needs one value per category")
        if not any(self.values):
            raise ValueError("at least one value must be above zero")
        return self


class CandlestickChart(_Chart):
    """One candle per period: its open, high, low and close."""

    type: Literal["candlestick"]
    dates: list[Label] = Field(
        min_length=1,
        max_length=MAX_CANDLES,
        description="ISO dates or date-times, oldest first, such as 2024-01-31.",
    )
    open: list[Number] = Field(min_length=1, max_length=MAX_CANDLES)
    high: list[Number] = Field(min_length=1, max_length=MAX_CANDLES)
    low: list[Number] = Field(min_length=1, max_length=MAX_CANDLES)
    close: list[Number] = Field(min_length=1, max_length=MAX_CANDLES)

    @model_validator(mode="after")
    def _candles(self) -> CandlestickChart:
        prices = (self.open, self.high, self.low, self.close)
        if any(len(p) != len(self.dates) for p in prices):
            raise ValueError("open, high, low and close need one value per date")
        try:
            dates = _dates(self.dates)
        except (ValueError, TypeError) as e:
            raise ValueError("dates must be ISO dates such as 2024-01-31") from e
        if not dates.is_monotonic_increasing or not dates.is_unique:
            raise ValueError("dates must be distinct and oldest first")
        for o, h, lo, c in zip(*prices, strict=True):
            if not lo <= min(o, c) <= max(o, c) <= h:
                raise ValueError("each candle needs low <= open, close <= high")
        return self


AnyChart = (
    BarChart
    | LineChart
    | StepChart
    | ScatterChart
    | HistogramChart
    | BoxChart
    | ViolinChart
    | HeatmapChart
    | PieChart
    | CandlestickChart
)
Chart = Annotated[AnyChart, Field(discriminator="type")]


@dataclass(frozen=True)
class Rendered:
    svg: str
    """The chart's SVG, with its MAIDR JSON in the ``maidr`` attribute."""
    layers: list[dict[str, Any]]
    """Each layer's maidr type and point count, for the model's summary."""


def _dates(dates: list[str]) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(dates, format="ISO8601"))


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
        if isinstance(chart, StepChart):
            ax.step(x, s.values, where=chart.where, label=name)
        else:
            ax.plot(x, s.values, label=name)
    if len(chart.series) > 1:
        ax.legend()


def _draw_scatter(ax: plt.Axes, chart: ScatterChart) -> None:
    if chart.trend:
        # regplot draws the points and the fitted line, which py-maidr reads as a smooth layer.
        sns.regplot(x=np.asarray(chart.x), y=np.asarray(chart.y), ci=None, ax=ax)
    else:
        ax.scatter(chart.x, chart.y)


def _draw_histogram(ax: plt.Axes, chart: HistogramChart) -> None:
    ax.hist(chart.values, bins=chart.bins or "auto")


def _draw_box(ax: plt.Axes, chart: BoxChart) -> None:
    ax.boxplot([g.values for g in chart.groups], tick_labels=[g.name for g in chart.groups])


def _draw_violin(ax: plt.Axes, chart: ViolinChart) -> None:
    names = [g.name for g in chart.groups]
    # cut=0 ends each violin at its group's extremes, so maidr reads no values beyond the data.
    sns.violinplot(
        x=[g.name for g in chart.groups for _ in g.values],
        y=[v for g in chart.groups for v in g.values],
        order=names,
        cut=0,
        ax=ax,
    )


def _draw_heatmap(ax: plt.Axes, chart: HeatmapChart) -> None:
    frame = pd.DataFrame(chart.values, index=chart.y_labels, columns=chart.x_labels)
    if chart.z_label:
        sns.heatmap(frame, ax=ax, z_label=chart.z_label)  # z_label is read by py-maidr
    else:
        sns.heatmap(frame, ax=ax)


def _draw_pie(ax: plt.Axes, chart: PieChart) -> None:
    # Clockwise from 12 o'clock, so maidr reads the slices in the order the model gave them.
    ax.pie(
        chart.values,
        labels=chart.categories,
        autopct="%1.1f%%",
        startangle=90,
        counterclock=False,
    )


_DRAW = {
    "bar": _draw_bar,
    "line": _draw_line,
    "step": _draw_line,
    "scatter": _draw_scatter,
    "histogram": _draw_histogram,
    "box": _draw_box,
    "violin": _draw_violin,
    "heatmap": _draw_heatmap,
    "pie": _draw_pie,
}

FIGSIZE = (6.4, 4.4)
# Candles told apart by fill rather than by red and green: hollow up, filled down.
_CANDLE_STYLE = mpf.make_mpf_style(
    base_mpf_style="classic",
    rc={
        "font.size": 10,
        "font.weight": "normal",
        "axes.labelweight": "normal",
        "figure.titleweight": "normal",
    },
)


def _draw_candlestick(chart: CandlestickChart) -> tuple[Figure, plt.Axes]:
    """mplfinance draws a candlestick in a figure of its own, and lays it out itself."""
    prices = pd.DataFrame(
        {"Open": chart.open, "High": chart.high, "Low": chart.low, "Close": chart.close},
        index=_dates(chart.dates),
    )
    timed = (prices.index != prices.index.normalize()).any()
    fig, axes = mpf.plot(
        prices,
        type="candle",
        style=_CANDLE_STYLE,
        figsize=FIGSIZE,
        returnfig=True,
        scale_padding={"bottom": 1.6},  # room for the slanted dates and the x label
        # Read by py-maidr releases after 1.26, which announce each date as its tick shows it.
        datetime_format="%b %d, %Y %H:%M" if timed else "%b %d, %Y",
    )
    axes[0].set_xlabel("Date")
    return fig, axes[0]


def _draw(chart: AnyChart) -> tuple[Figure, plt.Axes]:
    if isinstance(chart, CandlestickChart):
        return _draw_candlestick(chart)
    fig, ax = plt.subplots(figsize=FIGSIZE)
    _DRAW[chart.type](ax, chart)
    return fig, ax


# pyplot and py-maidr's figure registry are process-wide, so draw one chart at a time.
_LOCK = threading.Lock()
_SVG = re.compile(r"<svg\b.*?</svg>", re.S)
_MAIDR_ATTR = re.compile(r'<svg\b[^>]*?\smaidr="([^"]*)"')


def render(chart: AnyChart) -> Rendered:
    """Draw ``chart`` and return its accessible SVG.

    Raises:
        RuntimeError: if py-maidr did not produce a chart it can read.
    """
    # rc_context: mplfinance applies its style to the global rcParams, which would restyle
    # every chart drawn after a candlestick.
    with _LOCK, plt.rc_context():
        try:
            fig, ax = _draw(chart)
            if chart.title:
                ax.set_title(chart.title)
            if chart.x_label:
                ax.set_xlabel(chart.x_label)
            if chart.y_label:
                ax.set_ylabel(chart.y_label)
            if not isinstance(chart, CandlestickChart):
                fig.tight_layout()
            page = maidr.render(fig, use_cdn=True).get_html_string()
        finally:
            # The lock makes every open figure this chart's, including one mplfinance opened
            # before failing.
            for number in plt.get_fignums():
                maidr.close(plt.figure(number))
            plt.close("all")
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
