#!/usr/bin/env python3
"""Genera un XMLTV solo con los canales del M3U oficial."""

from __future__ import annotations

import copy
import gzip
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import zlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from validar_epg import ValidationSummary, validate_epg

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "fuentes_epg.json"
ALIASES_PATH = ROOT / "config" / "aliases_epg.json"
OUTPUT_PATH = ROOT / "epg.xml"
USER_AGENT = "miEPG/1.0 (+GitHub Actions)"
M3U_USER_AGENT = "Mozilla/5.0 (compatible; miEPG/1.0)"


@dataclass(frozen=True)
class PlaylistChannel:
    name: str
    clean_name: str
    tvg_id: str
    tvg_name: str
    logo: str
    group: str


@dataclass
class SourceData:
    name: str
    channels: dict[str, ET.Element]
    programs: dict[str, list[ET.Element]]
    id_normalized: dict[str, list[str]]
    alias_normalized: dict[str, list[str]]


@dataclass(frozen=True)
class Match:
    source: str
    source_id: str
    method: str


def load_config() -> tuple[dict, dict[str, tuple[str, str]]]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    raw_aliases = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
    aliases = {
        normalize(key): (value["source"], value["channel_id"])
        for key, value in raw_aliases.items()
    }
    return config, aliases


def normalize(value: str) -> str:
    value = value.replace("+", " plus ")
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value).split())


def clean_channel_name(value: str) -> str:
    value = re.sub(r"\s+\*+\s*$", "", value.strip())
    value = re.sub(r"\s+(?:720p|1080p|2160p|4k|uhd|fhd)\s*$", "", value, flags=re.I)
    return value.strip()


def download(
    url: str,
    *,
    timeout: int = 120,
    user_agent: str = USER_AGENT,
    accept: str = "*/*",
) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": accept},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def download_m3u(config: dict) -> tuple[str, list[PlaylistChannel]]:
    errors: list[str] = []
    for url in config["m3u_urls"]:
        for attempt in range(2):
            try:
                text = download(
                    url,
                    timeout=config["m3u_timeout_seconds"],
                    user_agent=M3U_USER_AGENT,
                    accept="audio/x-mpegurl, application/vnd.apple.mpegurl, text/plain, */*",
                ).decode("utf-8-sig")
                if text.lstrip().startswith("#EXTM3U") and "#EXTINF" in text:
                    playlist = parse_m3u(text)
                    return url, playlist
                errors.append(f"{url}: la respuesta no es un M3U")
                break
            except urllib.error.HTTPError as exc:
                errors.append(f"{url}: HTTP {exc.code} {exc.reason}")
                if attempt == 0 and (exc.code == 429 or exc.code >= 500):
                    time.sleep(2)
                    continue
                break
            except (OSError, UnicodeError, RuntimeError, urllib.error.URLError) as exc:
                errors.append(f"{url}: {exc}")
                break
    raise RuntimeError("No se pudo descargar el M3U:\n  " + "\n  ".join(errors))


def parse_m3u(text: str) -> list[PlaylistChannel]:
    channels: list[PlaylistChannel] = []
    for line in text.splitlines():
        if not line.startswith("#EXTINF"):
            continue
        attributes = dict(re.findall(r'([\w-]+)="([^"]*)"', line))
        name = line.split(",", 1)[1].strip() if "," in line else ""
        if not name:
            continue
        channels.append(
            PlaylistChannel(
                name=name,
                clean_name=clean_channel_name(name),
                tvg_id=attributes.get("tvg-id", "").strip(),
                tvg_name=attributes.get("tvg-name", "").strip(),
                logo=attributes.get("tvg-logo", "").strip(),
                group=attributes.get("group-title", "").strip(),
            )
        )
    if not channels:
        raise RuntimeError("El M3U no contiene entradas #EXTINF válidas")
    return channels


def load_source(name: str, url: str, timeout: int) -> SourceData:
    compressed = download(url, timeout=timeout)
    xml = gzip.decompress(compressed) if compressed.startswith(b"\x1f\x8b") else compressed
    root = ET.fromstring(xml)
    if root.tag != "tv":
        raise RuntimeError(f"la raíz XMLTV de {name} no es <tv>")
    channels = {element.get("id", ""): element for element in root.findall("channel")}
    programs: dict[str, list[ET.Element]] = {}
    for program in root.findall("programme"):
        programs.setdefault(program.get("channel", ""), []).append(program)
    if not channels or not programs:
        raise RuntimeError(f"la fuente {name} no contiene canales y programas")

    id_normalized: dict[str, list[str]] = {}
    alias_normalized: dict[str, list[str]] = {}
    for channel_id, element in channels.items():
        id_normalized.setdefault(normalize(channel_id), []).append(channel_id)
        for display_name in element.findall("display-name"):
            if display_name.text:
                alias_normalized.setdefault(normalize(display_name.text), []).append(channel_id)

    return SourceData(name, channels, programs, id_normalized, alias_normalized)


def select_preferred(
    candidates: list[tuple[str, str]],
    source_priority: list[str],
    sources: dict[str, SourceData],
) -> tuple[str, str] | None:
    unique = list(dict.fromkeys(candidates))
    if not unique:
        return None
    with_programmes = [
        candidate
        for candidate in unique
        if sources[candidate[0]].programs.get(candidate[1])
    ]
    if with_programmes:
        unique = with_programmes
    priority = {name: index for index, name in enumerate(source_priority)}
    best_priority = min(priority.get(source, len(priority)) for source, _ in unique)
    best = [
        candidate
        for candidate in unique
        if priority.get(candidate[0], len(priority)) == best_priority
    ]
    return best[0] if len(best) == 1 else None


def find_match(
    channel: PlaylistChannel,
    sources: dict[str, SourceData],
    aliases: dict[str, tuple[str, str]],
    source_priority: list[str],
) -> Match | None:
    # 1. IDs y nombres literales. Se conserva la preferencia por tvg-id.
    identity_values = list(
        dict.fromkeys(
            value
            for value in (channel.tvg_id, channel.tvg_name, channel.clean_name)
            if value
        )
    )
    for value in identity_values:
        exact = [
            (source.name, value)
            for source in sources.values()
            if value in source.channels
        ]
        selected = select_preferred(exact, source_priority, sources)
        if selected:
            return Match(*selected, "id/nombre exacto")

    # 2. Coincidencia normalizada, respetando el mismo orden de los campos M3U.
    for value in identity_values:
        normalized_id = normalize(value)
        candidates = [
            (source.name, source_id)
            for source in sources.values()
            for source_id in source.id_normalized.get(normalized_id, [])
        ]
        selected = select_preferred(candidates, source_priority, sources)
        if selected:
            return Match(*selected, "id/nombre normalizado")

    # 3. Excepciones auditadas. Se prueban por tvg-id, tvg-name y nombre visible.
    mapping_keys = list(
        dict.fromkeys(
            normalize(value)
            for value in (channel.tvg_id, channel.tvg_name, channel.clean_name)
            if value
        )
    )
    mapped_fallback: Match | None = None
    for key in mapping_keys:
        mapped = aliases.get(key)
        if mapped and mapped[0] in sources and mapped[1] in sources[mapped[0]].channels:
            mapped_fallback = Match(mapped[0], mapped[1], "mapping verificado")
            if sources[mapped[0]].programs.get(mapped[1]):
                return mapped_fallback

    # 4. Alias exacto de tvg-name o nombre visible, tras normalizar escritura.
    candidates = [
        (source.name, source_id)
        for source in sources.values()
        for name in dict.fromkeys((channel.tvg_name, channel.clean_name))
        if name
        for source_id in source.alias_normalized.get(normalize(name), [])
    ]
    selected = select_preferred(candidates, source_priority, sources)
    if selected:
        return Match(*selected, "display-name")
    return mapped_fallback


def logical_key(channel: PlaylistChannel) -> str:
    if channel.tvg_id:
        return f"id:{channel.tvg_id}"
    name = channel.tvg_name or channel.clean_name
    return f"name:{normalize(name)}"


def add_playlist_metadata(element: ET.Element, channels: list[PlaylistChannel]) -> None:
    existing_names = {normalize(item.text or "") for item in element.findall("display-name")}
    for channel in channels:
        for name in (channel.tvg_name, channel.clean_name):
            normalized_name = normalize(name)
            if name and normalized_name not in existing_names:
                display = ET.SubElement(element, "display-name")
                display.text = name
                existing_names.add(normalized_name)
    if element.find("icon") is None:
        seen_logos: set[str] = set()
        for channel in channels:
            if channel.logo and channel.logo not in seen_logos:
                ET.SubElement(element, "icon", {"src": channel.logo})
                seen_logos.add(channel.logo)


def build_epg(
    playlist: list[PlaylistChannel],
    sources: dict[str, SourceData],
    aliases: dict[str, tuple[str, str]],
    source_priority: list[str],
    generator_info_url: str,
) -> tuple[ET.Element, dict[str, Match | None], int]:
    matches: dict[str, Match | None] = {}
    representatives: dict[str, PlaylistChannel] = {}
    grouped_channels: dict[str, list[PlaylistChannel]] = {}
    for channel in playlist:
        key = logical_key(channel)
        representatives.setdefault(key, channel)
        grouped_channels.setdefault(key, []).append(channel)
        if key not in matches or matches[key] is None:
            match = find_match(channel, sources, aliases, source_priority)
            if key not in matches or match is not None:
                matches[key] = match

    output = ET.Element(
        "tv",
        {
            "generator-info-name": "miEPG",
            "generator-info-url": generator_info_url,
        },
    )
    program_elements: list[ET.Element] = []
    used_output_ids: set[str] = set()
    seen_schedules: set[tuple[str, str, str]] = set()
    duplicate_schedules = 0

    for key, channel in representatives.items():
        match = matches[key]
        if match is None:
            continue
        source = sources[match.source]
        output_id = channel.tvg_id or match.source_id
        if output_id in used_output_ids:
            continue
        used_output_ids.add(output_id)

        channel_element = copy.deepcopy(source.channels[match.source_id])
        channel_element.set("id", output_id)
        add_playlist_metadata(channel_element, grouped_channels[key])
        output.append(channel_element)

        for program in source.programs.get(match.source_id, []):
            cloned = copy.deepcopy(program)
            cloned.set("channel", output_id)
            schedule = (output_id, cloned.get("start", ""), cloned.get("stop", ""))
            if schedule in seen_schedules:
                duplicate_schedules += 1
                continue
            seen_schedules.add(schedule)
            program_elements.append(cloned)

    program_elements.sort(
        key=lambda item: (
            item.get("start", ""),
            item.get("channel", ""),
            item.get("stop", ""),
            normalize(item.findtext("title", default="")),
        )
    )
    output.extend(program_elements)
    return output, matches, duplicate_schedules


def write_epg(root: ET.Element) -> ValidationSummary:
    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)
    temporary = OUTPUT_PATH.with_name(".epg.xml.tmp")
    try:
        tree.write(temporary, encoding="utf-8", xml_declaration=True, short_empty_elements=True)
        summary = validate_epg(temporary)
        temporary.replace(OUTPUT_PATH)
        return summary
    finally:
        if temporary.exists():
            temporary.unlink()


def print_diagnostics(
    playlist: list[PlaylistChannel],
    matches: dict[str, Match | None],
    sources: dict[str, SourceData],
) -> tuple[int, int, list[str]]:
    representatives: dict[str, PlaylistChannel] = {}
    for channel in playlist:
        representatives.setdefault(logical_key(channel), channel)

    matched = 0
    missing_ids: list[str] = []
    print("\nDiagnóstico de canales")
    print("=" * 78)
    for key, channel in representatives.items():
        match = matches[key]
        if match:
            count = len(sources[match.source].programs.get(match.source_id, []))
            print(
                f"OK  {channel.tvg_id or channel.name} -> [{match.source}] {match.source_id} "
                f"({match.method}, {count} programas)"
            )
            matched += 1
        else:
            channel_id = channel.tvg_id or channel.tvg_name or channel.name
            missing_ids.append(channel_id)
            print(
                f"XX  {channel.name} -> sin EPG "
                f"(tvg-id={channel.tvg_id or '<vacío>'}, tvg-name={channel.tvg_name or '<vacío>'})"
            )

    print("=" * 78)
    print(f"Canales M3U: {len(representatives)}")
    print(f"Canales con EPG: {matched}")
    print(f"Canales sin EPG: {len(missing_ids)}")
    if missing_ids:
        print("tvg-id/nombre sin EPG: " + ", ".join(missing_ids))
    return len(representatives), matched, missing_ids


def main() -> int:
    config, aliases = load_config()
    print("Descargando M3U...")
    m3u_url, playlist = download_m3u(config)
    print(f"M3U válido: {m3u_url}")
    print(f"Canales #EXTINF detectados: {len(playlist)}")

    sources: dict[str, SourceData] = {}
    source_errors: list[str] = []
    configured_sources = sorted(config["epg_sources"], key=lambda item: item["priority"])
    for source_config in configured_sources:
        name, url = source_config["name"], source_config["url"]
        print(f"Descargando EPG {name} (prioridad {source_config['priority']})...")
        try:
            source = load_source(name, url, config["epg_timeout_seconds"])
        except (OSError, ET.ParseError, RuntimeError, ValueError, EOFError, zlib.error) as exc:
            message = f"{name}: {exc}"
            source_errors.append(message)
            print(f"  ERROR: {message}", file=sys.stderr)
            continue
        sources[name] = source
        total_programs = sum(len(items) for items in source.programs.values())
        print(f"  Fuente descargada: {url}")
        print(f"  Canales detectados: {len(source.channels)} | programas: {total_programs}")

    if not sources:
        raise RuntimeError("Fallaron todas las fuentes EPG; se conserva el epg.xml anterior")

    source_priority = [item["name"] for item in configured_sources]
    root, matches, duplicate_schedules = build_epg(
        playlist,
        sources,
        aliases,
        source_priority,
        config["generator_info_url"],
    )
    print_diagnostics(playlist, matches, sources)
    if duplicate_schedules:
        print(f"Programas repetidos por canal y horario omitidos: {duplicate_schedules}")
    print(f"Programas importados: {len(root.findall('programme'))}")

    # El archivo anterior solo se reemplaza después de validar una salida XMLTV completa.
    summary = write_epg(root)
    print(
        f"\nGenerado {OUTPUT_PATH.name}: {len(root.findall('channel'))} canales, "
        f"{len(root.findall('programme'))} programas, {OUTPUT_PATH.stat().st_size} bytes"
    )
    print(
        f"Validación: XMLTV válido, {summary.channels} canales y "
        f"{summary.programmes} programas."
    )
    if source_errors:
        print("Fuentes que fallaron (se continuó con las restantes):")
        for error in source_errors:
            print(f"  - {error}")
    # Se publican resultados parciales válidos para no bloquear una fuente restante;
    # los canales pendientes quedan explícitos en el resumen y el log.
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ET.ParseError, OSError, RuntimeError, ValueError, urllib.error.URLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
