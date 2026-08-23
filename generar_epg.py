#!/usr/bin/env python3
"""Genera una EPG XMLTV reducida a los canales de la lista M3U remota."""

from __future__ import annotations

import copy
import gzip
import re
import sys
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


IPNS_NAME = "k51qzi5uqu5dh5qej4b9wlcr5i6vhc7rcfkekhrxqek5c9lk6gdaiik820fecs"
M3U_URLS = (
    f"https://{IPNS_NAME}.ipns.inbrowser.link/hashes_kodi.m3u",
    # inbrowser.link entrega una app HTML; este gateway expone el mismo IPNS por HTTP.
    f"https://{IPNS_NAME}.ipns.dweb.link/hashes_kodi.m3u",
)
EPG_SOURCES = (
    (
        "DobleM",
        "https://raw.githubusercontent.com/davidmuma/EPG_dobleM/master/EPG_dobleM.xml.gz",
    ),
    (
        "Italia",
        "https://epgshare01.online/epgshare01/epg_ripper_IT1.xml.gz",
    ),
)
OUTPUT_PATH = Path(__file__).with_name("epg.xml")
USER_AGENT = "miEPG/1.0 (+GitHub Actions)"

# Excepciones comprobadas contra los channel id/display-name reales de las fuentes.
# La clave es el nombre limpio del M3U normalizado; el valor es (fuente, channel id).
VERIFIED_MAPPINGS = {
    "dazn 1 italia": ("Italia", "DAZN.1.it.it"),
    "real madrid tv": ("DobleM", "Real Madrid TV"),
    "sport tv2": ("DobleM", "PT | Sport TV 2"),
    "sport tv3": ("DobleM", "PT | Sport TV 3"),
    "sport tv7": ("DobleM", "PT | Sport TV 7"),
    "sport tv plus": ("DobleM", "PT | Sport TV +"),
    "tennis channel": ("DobleM", "Tennis Channel"),
}


@dataclass(frozen=True)
class PlaylistChannel:
    name: str
    clean_name: str
    tvg_id: str
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


def normalize(value: str) -> str:
    value = value.replace("+", " plus ")
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value).split())


def clean_channel_name(value: str) -> str:
    value = re.sub(r"\s+\*+\s*$", "", value.strip())
    value = re.sub(r"\s+(?:720p|1080p|2160p|4k|uhd|fhd)\s*$", "", value, flags=re.I)
    return value.strip()


def download(url: str, *, timeout: int = 120) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def download_m3u() -> tuple[str, str]:
    errors: list[str] = []
    for url in M3U_URLS:
        try:
            text = download(url).decode("utf-8-sig", "replace")
            if text.lstrip().startswith("#EXTM3U") and "#EXTINF" in text:
                return text, url
            errors.append(f"{url}: la respuesta no es un M3U")
        except (OSError, UnicodeError, urllib.error.URLError) as exc:
            errors.append(f"{url}: {exc}")
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
                logo=attributes.get("tvg-logo", "").strip(),
                group=attributes.get("group-title", "").strip(),
            )
        )
    if not channels:
        raise RuntimeError("El M3U no contiene entradas #EXTINF válidas")
    return channels


def load_source(name: str, url: str) -> SourceData:
    compressed = download(url)
    try:
        xml = gzip.decompress(compressed)
    except gzip.BadGzipFile:
        xml = compressed
    root = ET.fromstring(xml)
    channels = {element.get("id", ""): element for element in root.findall("channel")}
    programs: dict[str, list[ET.Element]] = {}
    for program in root.findall("programme"):
        programs.setdefault(program.get("channel", ""), []).append(program)

    id_normalized: dict[str, list[str]] = {}
    alias_normalized: dict[str, list[str]] = {}
    for channel_id, element in channels.items():
        id_normalized.setdefault(normalize(channel_id), []).append(channel_id)
        for display_name in element.findall("display-name"):
            if display_name.text:
                alias_normalized.setdefault(normalize(display_name.text), []).append(channel_id)

    return SourceData(name, channels, programs, id_normalized, alias_normalized)


def only_unique(candidates: list[tuple[str, str]]) -> tuple[str, str] | None:
    unique = list(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else None


def find_match(channel: PlaylistChannel, sources: dict[str, SourceData]) -> Match | None:
    # 1. ID literal. Es el caso normal y evita falsos positivos por nombres parecidos.
    if channel.tvg_id:
        exact = [
            (source.name, channel.tvg_id)
            for source in sources.values()
            if channel.tvg_id in source.channels
        ]
        selected = only_unique(exact)
        if selected:
            return Match(*selected, "id exacto")

        normalized_id = normalize(channel.tvg_id)
        candidates = [
            (source.name, source_id)
            for source in sources.values()
            for source_id in source.id_normalized.get(normalized_id, [])
        ]
        selected = only_unique(candidates)
        if selected:
            return Match(*selected, "id normalizado")

    # 2. Excepciones auditadas. Se prueban por tvg-id y por nombre limpio.
    mapping_keys = [normalize(channel.tvg_id), normalize(channel.clean_name)]
    for key in mapping_keys:
        mapped = VERIFIED_MAPPINGS.get(key)
        if mapped and mapped[0] in sources and mapped[1] in sources[mapped[0]].channels:
            return Match(mapped[0], mapped[1], "mapping verificado")

    # 3. Alias exacto tras quitar solo calidad/asteriscos y normalizar escritura.
    clean = normalize(channel.clean_name)
    candidates = [
        (source.name, source_id)
        for source in sources.values()
        for source_id in source.alias_normalized.get(clean, [])
    ]
    selected = only_unique(candidates)
    if selected:
        return Match(*selected, "display-name")
    return None


def logical_key(channel: PlaylistChannel) -> str:
    return f"id:{channel.tvg_id}" if channel.tvg_id else f"name:{normalize(channel.clean_name)}"


def add_playlist_metadata(element: ET.Element, channel: PlaylistChannel) -> None:
    existing_names = {normalize(item.text or "") for item in element.findall("display-name")}
    if normalize(channel.clean_name) not in existing_names:
        display = ET.Element("display-name")
        display.text = channel.clean_name
        element.insert(0, display)
    if channel.logo and element.find("icon") is None:
        ET.SubElement(element, "icon", {"src": channel.logo})


def build_epg(
    playlist: list[PlaylistChannel], sources: dict[str, SourceData]
) -> tuple[ET.Element, dict[str, Match | None]]:
    matches: dict[str, Match | None] = {}
    representatives: dict[str, PlaylistChannel] = {}
    for channel in playlist:
        key = logical_key(channel)
        representatives.setdefault(key, channel)
        matches.setdefault(key, find_match(channel, sources))

    output = ET.Element(
        "tv",
        {
            "generator-info-name": "miEPG",
            "generator-info-url": "https://github.com/bryancastanosansegundo5/miEPG",
        },
    )
    program_elements: list[ET.Element] = []
    used_output_ids: set[str] = set()

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
        add_playlist_metadata(channel_element, channel)
        output.append(channel_element)

        for program in source.programs.get(match.source_id, []):
            cloned = copy.deepcopy(program)
            cloned.set("channel", output_id)
            program_elements.append(cloned)

    program_elements.sort(key=lambda item: (item.get("start", ""), item.get("channel", "")))
    output.extend(program_elements)
    return output, matches


def write_epg(root: ET.Element) -> None:
    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)
    tree.write(OUTPUT_PATH, encoding="utf-8", xml_declaration=True, short_empty_elements=True)


def print_diagnostics(
    playlist: list[PlaylistChannel],
    matches: dict[str, Match | None],
    sources: dict[str, SourceData],
) -> int:
    ok = 0
    missing = 0
    logical_ok: set[str] = set()
    logical_missing: set[str] = set()
    print("\nDiagnóstico de canales")
    print("=" * 78)
    for channel in playlist:
        key = logical_key(channel)
        match = matches[key]
        if match:
            count = len(sources[match.source].programs.get(match.source_id, []))
            print(
                f"OK  {channel.name} -> [{match.source}] {match.source_id} "
                f"({match.method}, {count} programas)"
            )
            ok += 1
            logical_ok.add(key)
        else:
            print(
                f"XX  {channel.name} -> sin coincidencia "
                f"(tvg-id={channel.tvg_id or '<vacío>'})"
            )
            missing += 1
            logical_missing.add(key)

    print("=" * 78)
    print(f"Entradas M3U: {len(playlist)} | OK: {ok} | XX: {missing}")
    print(
        f"Canales lógicos: {len(logical_ok) + len(logical_missing)} | "
        f"con EPG: {len(logical_ok)} | sin EPG: {len(logical_missing)}"
    )
    return missing


def main() -> int:
    print("Descargando M3U...")
    m3u_text, m3u_url = download_m3u()
    print(f"M3U válido: {m3u_url}")
    playlist = parse_m3u(m3u_text)
    print(f"Entradas #EXTINF: {len(playlist)}")

    sources: dict[str, SourceData] = {}
    for name, url in EPG_SOURCES:
        print(f"Descargando EPG {name}...")
        source = load_source(name, url)
        sources[name] = source
        total_programs = sum(len(items) for items in source.programs.values())
        print(f"  {len(source.channels)} canales, {total_programs} programas")

    root, matches = build_epg(playlist, sources)
    missing = print_diagnostics(playlist, matches, sources)
    write_epg(root)
    print(
        f"\nGenerado {OUTPUT_PATH.name}: {len(root.findall('channel'))} canales, "
        f"{len(root.findall('programme'))} programas, {OUTPUT_PATH.stat().st_size} bytes"
    )
    return 0 if missing == 0 else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ET.ParseError, OSError, RuntimeError, urllib.error.URLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
