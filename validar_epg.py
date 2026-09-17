#!/usr/bin/env python3
"""Valida un archivo XMLTV antes de publicarlo o consumirlo."""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


DATE_RE = re.compile(r"^(\d{14})(?:\s+([+-])(\d{2})(\d{2}))?$")


class EPGValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidationSummary:
    channels: int
    programmes: int


def parse_xmltv_date(value: str) -> datetime:
    match = DATE_RE.fullmatch(value.strip())
    if not match:
        raise EPGValidationError(f"fecha XMLTV inválida: {value!r}")
    try:
        result = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
    except ValueError as exc:
        raise EPGValidationError(f"fecha XMLTV inválida: {value!r}") from exc

    if match.group(2):
        hours, minutes = int(match.group(3)), int(match.group(4))
        if hours > 23 or minutes > 59:
            raise EPGValidationError(f"zona horaria XMLTV inválida: {value!r}")
        offset = timedelta(hours=hours, minutes=minutes)
        if match.group(2) == "-":
            offset = -offset
        result = result.replace(tzinfo=timezone(offset))
    else:
        result = result.replace(tzinfo=timezone.utc)
    return result


def validate_epg(path: Path) -> ValidationSummary:
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError) as exc:
        raise EPGValidationError(f"XML no legible o mal formado: {exc}") from exc

    root = tree.getroot()
    if root.tag != "tv":
        raise EPGValidationError(f"la raíz debe ser <tv>, se encontró <{root.tag}>")

    channel_ids: set[str] = set()
    for channel in root.findall("channel"):
        channel_id = (channel.get("id") or "").strip()
        if not channel_id:
            raise EPGValidationError("hay un <channel> sin id")
        if channel_id in channel_ids:
            raise EPGValidationError(f"channel.id duplicado: {channel_id}")
        channel_ids.add(channel_id)

    programmes = root.findall("programme")
    if not channel_ids or not programmes:
        raise EPGValidationError(
            f"resultado vacío: {len(channel_ids)} canales, {len(programmes)} programas"
        )

    exact_programmes: set[bytes] = set()
    schedules: set[tuple[str, str, str]] = set()
    for programme in programmes:
        channel_id = (programme.get("channel") or "").strip()
        if channel_id not in channel_ids:
            raise EPGValidationError(
                f"programme.channel={channel_id!r} no tiene un channel.id correspondiente"
            )

        start_text, stop_text = programme.get("start", ""), programme.get("stop", "")
        start, stop = parse_xmltv_date(start_text), parse_xmltv_date(stop_text)
        if stop <= start:
            raise EPGValidationError(
                f"stop debe ser posterior a start: {start_text!r} -> {stop_text!r}"
            )

        signature = ET.tostring(programme, encoding="utf-8")
        if signature in exact_programmes:
            raise EPGValidationError(f"programa exactamente duplicado en {channel_id}")
        exact_programmes.add(signature)

        schedule = channel_id, start_text, stop_text
        if schedule in schedules:
            raise EPGValidationError(
                f"horario duplicado en {channel_id}: {start_text} -> {stop_text}"
            )
        schedules.add(schedule)

    return ValidationSummary(len(channel_ids), len(programmes))


def main() -> int:
    path = Path(__file__).resolve().with_name("epg.xml")
    try:
        summary = validate_epg(path)
    except EPGValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"EPG válida: raíz <tv>, {summary.channels} canales y "
        f"{summary.programmes} programas; fechas, referencias y duplicados comprobados."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
