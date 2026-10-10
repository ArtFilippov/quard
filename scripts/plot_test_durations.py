"""Plot pytest-durations test-call timings without counting setup or teardown."""

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import LogLocator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="pytest-durations JSON report")
    parser.add_argument("output", type=Path, help="output PNG path")
    args = parser.parse_args()

    report = json.loads(args.report.read_text(encoding="utf-8"))
    calls = sorted(report["categories"]["test call"], key=lambda item: item["total"], reverse=True)
    if not calls:
        parser.error("the report contains no test calls")
    slowest = calls[:30]
    total = sum(item["total"] for item in calls)
    slow_total = sum(item["total"] for item in slowest)

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11})
    fig, (bars, distribution) = plt.subplots(
        1, 2, figsize=(25, 13), gridspec_kw={"width_ratios": [1.5, 1]}
    )
    fig.patch.set_facecolor("#f8fafc")
    for axis in (bars, distribution):
        axis.set_facecolor("white")
        axis.spines[["top", "right"]].set_visible(False)
        axis.grid(axis="x" if axis is bars else "y", color="#e2e8f0", zorder=0)

    labels = []
    for item in slowest:
        filename, *parts = item["name"].split("::")
        labels.append(f"{parts[-1]}  ·  {Path(filename).name}")
    times = [item["total"] for item in slowest]
    positions = range(len(slowest))
    bars.barh(positions, times, color="#2563eb", height=0.75, zorder=2)
    bars.set_yticks(positions, labels, fontsize=8.7)
    bars.invert_yaxis()
    bars.tick_params(axis="y", length=0, pad=10)
    bars.set_xlim(0, max(times) * 1.16)
    for position, seconds in enumerate(times):
        bars.text(seconds + max(times) * 0.012, position, f"{seconds:.1f}", va="center", fontsize=9)
    bars.set_xlabel("Время вызова теста, с")
    bars.set_title("30 самых долгих тестов", fontsize=16, loc="left", pad=18)

    ranks = range(1, len(calls) + 1)
    distribution.plot(ranks, [item["total"] for item in calls], color="#94a3b8", linewidth=1.8)
    distribution.scatter(
        range(1, len(slowest) + 1), times, color="#2563eb", s=20, zorder=3
    )
    distribution.axvline(len(slowest), linestyle="--", color="#2563eb", alpha=0.6)
    distribution.set_yscale("log")
    distribution.yaxis.set_major_locator(LogLocator(base=10))
    distribution.set_xlim(1, len(calls))
    distribution.set_xlabel("Тесты по убыванию длительности")
    distribution.set_ylabel("Время вызова, с · логарифмическая шкала")
    distribution.set_title(f"Все {len(calls)} замеренных тестов", fontsize=16, loc="left", pad=18)
    distribution.text(
        0.96, 0.96, f"Топ-30: {slow_total / total:.0%} времени вызовов",
        transform=distribution.transAxes, ha="right", va="top", fontsize=12,
        bbox={"boxstyle": "round,pad=0.5", "facecolor": "#eff6ff", "edgecolor": "none"},
    )

    fig.suptitle("Сколько времени занимают тесты Quard", x=0.38, y=0.977, fontsize=22)
    fig.text(
        0.38, 0.945,
        "Профиль pytest · 10 октября 2026 · только test call; setup/teardown не включены",
        ha="center", fontsize=12, color="#475569",
    )
    fig.text(
        0.38, 0.032,
        "Источник: pytest-durations JSON из HANDOFF.md. Один запуск; 649,65 с — "
        "время всего прогона, не сумма вызовов.",
        ha="center", fontsize=10, color="#475569",
    )
    fig.subplots_adjust(left=0.37, right=0.97, top=0.90, bottom=0.095, wspace=0.20)
    fig.savefig(args.output, dpi=170, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"Saved {args.output}: {len(calls)} test calls, {total:.2f} s total call time")


if __name__ == "__main__":
    main()
