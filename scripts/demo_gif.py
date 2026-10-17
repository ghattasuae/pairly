#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Turn the demo recordings of one spec into a GIF for the README. Development only.

    uv run --with av python -m scripts.demo_gif --videos DIR --spec 14-hire-demo \\
        --out docs/media/hire-demo.gif

``DIR`` holds the .webm files and ``report.json`` of a run of
``apps/web/e2e/playwright.record.config.ts`` (see docs/demo.md). Each browser context
of a test is one video. The report dates every test, so the videos of the sign-in
set-up tests are left out and the rest are joined in the order they were made.

Frames are sampled at ``--fps``. A frame that does not change the picture lengthens the
previous one, up to ``--max-still`` seconds, and so does a frame caught in the middle
of a change (a page loading), so waiting for the worker does not become
minutes of a still page; the last frame of every video stays ``--hold`` seconds. One
palette for the whole GIF and no dithering keep unchanged pixels unchanged, which keeps
the file small. When the GIF is over ``--max-mb``, it is written again narrower.

PyAV (BSD-3-Clause, with LGPL FFmpeg) decodes the videos; it is not a project
dependency, ``uv run --with av`` brings it for this run only. Pillow writes the GIF.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops

COLORS = 128  # one palette for the whole GIF; UI screens need few colours


@dataclass
class Segment:
    """One video: the test it belongs to and when it was written."""

    path: Path
    test: str
    written: float


@dataclass
class Frames:
    images: list[Image.Image] = field(default_factory=list)
    durations: list[int] = field(default_factory=list)


def _tests(report: dict[str, Any], spec: str) -> list[tuple[str, float, float]]:
    """(title, start, end) in epoch seconds of every test of ``spec``."""
    found: list[tuple[str, float, float]] = []

    def walk(suite: dict[str, Any]) -> None:
        for item in suite.get("specs", []):
            if spec not in str(item.get("file", "")):
                continue
            for test in item.get("tests", []):
                for result in test.get("results", []):
                    start = dt.datetime.fromisoformat(result["startTime"].replace("Z", "+00:00"))
                    begin = start.timestamp()
                    found.append((item["title"], begin, begin + result["duration"] / 1000))
        for child in suite.get("suites", []):
            walk(child)

    for suite in report.get("suites", []):
        walk(suite)
    return found


def segments(videos: Path, spec: str) -> list[Segment]:
    """The videos of ``spec``'s tests other than the set-up, oldest first."""
    report = json.loads((videos / "report.json").read_text())
    tests = _tests(report, spec)
    if not tests:
        raise SystemExit(f"no test of {spec} in {videos / 'report.json'}")
    chosen: list[Segment] = []
    for path in sorted(videos.glob("*.webm")):
        # Birth time: when the context opened its page, inside its test (the file is
        # finished, and its mtime set, a while after the context closes).
        stat = path.stat()
        written = getattr(stat, "st_birthtime", stat.st_mtime)
        started = [t for t in tests if t[1] <= written + 0.05]
        owner = max(started, key=lambda t: t[1]) if started else None
        if owner is None or owner[0].lower().startswith("set up"):
            continue
        chosen.append(Segment(path, owner[0], written))
    return sorted(chosen, key=lambda s: s.written)


def _decoded(path: Path, fps: float) -> Iterator[Image.Image]:
    import av  # type: ignore[import-not-found]  # brought by uv run --with av

    step = 1 / fps
    next_at = 0.0
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        for frame in container.decode(stream):
            at = float(frame.time or 0.0)
            if at + 1e-6 < next_at:
                continue
            next_at = at + step
            yield frame.to_image().convert("RGB")


def _sized(image: Image.Image, width: int) -> Image.Image:
    if image.width == width:
        return image
    height = round(image.height * width / image.width)
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _palette(parts: list[Segment], width: int, fps: float) -> Image.Image:
    """One palette from a sample of frames (about one every 3 seconds)."""
    sample: list[Image.Image] = []
    every = max(1, round(3 * fps))
    for part in parts:
        for i, image in enumerate(_decoded(part.path, fps)):
            if i % every == 0:
                sample.append(
                    _sized(image, width).resize(
                        (width // 4, image.height * width // image.width // 4)
                    )
                )
    columns = 6
    w, h = sample[0].size
    rows = (len(sample) + columns - 1) // columns
    sheet = Image.new("RGB", (w * columns, h * rows), "white")
    for i, image in enumerate(sample):
        sheet.paste(image, ((i % columns) * w, (i // columns) * h))
    return sheet.quantize(colors=COLORS, method=Image.Quantize.MEDIANCUT)


def _settled(
    current: Image.Image, shown: Image.Image | None, threshold: int, tile: int
) -> Image.Image | None:
    """``shown`` with only the tiles of ``current`` that really changed, or None when
    none did. Video compression leaves faint noise everywhere; copying only the tiles
    that changed by more than ``threshold`` keeps every other pixel identical, so the
    GIF stores a small changed area per frame instead of the whole picture."""
    if shown is None:
        return current.copy()
    changed = ImageChops.difference(current.convert("L"), shown.convert("L")).point(
        lambda v: 255 if v > threshold else 0
    )
    if changed.getbbox() is None:
        return None
    small = changed.resize(
        ((current.width + tile - 1) // tile, (current.height + tile - 1) // tile),
        Image.Resampling.BOX,
    )
    marked = [small.getpixel((x, y)) for y in range(small.height) for x in range(small.width)]
    if sum(1 for v in marked if v) > len(marked) * 0.3:
        return current.copy()  # most of the page changed: take the whole frame
    out = shown.copy()
    any_tile = False
    for y in range(small.height):
        for x in range(small.width):
            if small.getpixel((x, y)):
                box = (
                    x * tile,
                    y * tile,
                    min((x + 1) * tile, current.width),
                    min((y + 1) * tile, current.height),
                )
                out.paste(current.crop(box), box[:2])
                any_tile = True
    return out if any_tile else None


def _steady(images: Iterator[Image.Image], threshold: int) -> Iterator[Image.Image | None]:
    """Each frame that the next frame barely changes (a page at rest), else None. Frames
    caught mid-change (a page loading, a scroll) would cost a whole picture each and
    are not readable anyway; the last frame always counts as at rest."""
    previous: Image.Image | None = None
    for image in images:
        if previous is not None:
            moved = ImageChops.difference(image.convert("L"), previous.convert("L"))
            busy = sum(moved.histogram()[threshold + 1 :])
            yield previous if busy <= image.width * image.height * 0.002 else None
        previous = image
    if previous is not None:
        yield previous


def collect(
    parts: list[Segment],
    width: int,
    fps: float,
    max_still: float,
    hold: float,
    threshold: int = 10,
    tile: int = 16,
) -> Frames:
    palette = _palette(parts, width, fps)
    frame_ms = round(1000 / fps)
    out = Frames()
    for part in parts:
        shown: Image.Image | None = None
        # Compare at the recorded size, resize only what is shown: resizing first
        # turns the video noise into changes everywhere.
        for image in _steady(_decoded(part.path, fps), threshold):
            if image is None:  # a frame in the middle of a change: its time goes on
                if out.durations:
                    out.durations[-1] = min(out.durations[-1] + frame_ms, round(max_still * 1000))
                continue
            settled = _settled(image, shown, threshold, tile)
            if settled is None:
                out.durations[-1] = min(out.durations[-1] + frame_ms, round(max_still * 1000))
                continue
            shown = settled
            out.images.append(
                _sized(settled, width).quantize(palette=palette, dither=Image.Dither.NONE)
            )
            out.durations.append(frame_ms)
        if out.durations:
            out.durations[-1] = max(out.durations[-1], round(hold * 1000))
    return out


def write(frames: Frames, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    first, rest = frames.images[0], frames.images[1:]
    first.save(
        out,
        save_all=True,
        append_images=rest,
        duration=frames.durations,
        loop=0,
        optimize=False,
        disposal=1,
    )
    return out.stat().st_size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.demo_gif")
    parser.add_argument("--videos", type=Path, required=True)
    parser.add_argument("--spec", required=True, help="spec file name part, e.g. 14-hire-demo")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--fps", type=float, default=10)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--max-mb", type=float, default=8)
    parser.add_argument("--max-still", type=float, default=1.5)
    parser.add_argument("--hold", type=float, default=2.5)
    args = parser.parse_args(argv)

    parts = segments(args.videos, args.spec)
    if not parts:
        print(f"no videos of {args.spec} in {args.videos}", file=sys.stderr)
        return 1
    for part in parts:
        print(f"  {part.path.name}: {part.test}")
    width = args.width
    while True:
        frames = collect(parts, width, args.fps, args.max_still, args.hold)
        size = write(frames, args.out)
        seconds = sum(frames.durations) / 1000
        print(
            f"{args.out}: {width} px wide, {len(frames.images)} frames, "
            f"{seconds:.0f} s, {size / 1e6:.2f} MB"
        )
        if size <= args.max_mb * 1e6:
            return 0
        if width <= 640:
            print(f"still over {args.max_mb} MB at {width} px", file=sys.stderr)
            return 1
        width = max(640, int(width * 0.9) // 16 * 16)


if __name__ == "__main__":
    raise SystemExit(main())
