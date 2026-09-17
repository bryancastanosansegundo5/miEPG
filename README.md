# miEPG

Genera y mantiene `epg.xml`, una guía XMLTV filtrada para los canales del M3U
oficial. Está pensada para Kodi IPTV Simple Client. El M3U se descarga solo para
leer sus metadatos; el proyecto no lo guarda ni lo modifica.

## Flujo

1. Descarga la lista oficial desde IPNS:
   `https://k2k4r8lm8tkmuxbc8lkmq1in3v0oya1p6pe9o5bu0hu30br5ko08k2gb.ipns.inbrowser.link/data/listas/lista_kodi.m3u`
2. Si `inbrowser.link` entrega su página HTML de servicio en vez del M3U, prueba
   los gateways raw configurados en `config/fuentes_epg.json`.
3. De cada `#EXTINF` obtiene `tvg-id`, `tvg-name`, nombre visible, `group-title` y
   `tvg-logo`. Solo esos canales se buscan en las guías EPG.
4. Descarga DobleM e Italia, relaciona por ID exacto o normalizado, aliases
   configurados y `display-name`, y aplica la prioridad configurada si varias
   fuentes coinciden.
5. Copia al XMLTV los metadatos disponibles de canales y programas. Los
   `programme.channel` generados utilizan exactamente el `tvg-id` del M3U cuando
   existe.
6. Valida el XML en un archivo temporal y reemplaza `epg.xml` únicamente después
   de comprobar su estructura, fechas, referencias y duplicados. Si fallan todas
   las fuentes o el resultado queda vacío, conserva el `epg.xml` anterior.

## Fuentes y prioridad

Las fuentes están en `config/fuentes_epg.json`:

- **M3U:** el IPNS oficial indicado arriba; se prueban también gateways raw de
  ese mismo recurso cuando el gateway de navegador devuelve HTML.
- **DobleM:**
  `https://raw.githubusercontent.com/davidmuma/EPG_dobleM/master/EPG_dobleM.xml.gz`
- **Italia:**
  `https://epgshare01.online/epgshare01/epg_ripper_IT1.xml.gz`

DobleM tiene prioridad `1` e Italia prioridad `2`. Si varias fuentes tienen el
mismo canal, se prefiere una que contenga programas; si varias tienen guía para
él, gana la de menor número de prioridad. Si una fuente falla, se continúa con
las demás. Si todas fallan, la ejecución termina con error sin reemplazar el XML
anterior.

## Aliases

Los mappings verificados están separados de la lógica en
`config/aliases_epg.json`. Cada clave es el nombre normalizado del `tvg-id`,
`tvg-name` o nombre visible. El valor identifica la fuente y su `channel id` real.
Ejemplo:

```json
"dazn 1 italia": {
  "source": "Italia",
  "channel_id": "DAZN.1.it.it"
}
```

Para añadir un alias, agrega una entrada con el nombre normalizado y un
`channel_id` existente en la fuente seleccionada. La salida usa el `tvg-id` del
M3U como `<channel id>` para mantener la relación con Kodi.

## Ejecución local

Requiere Python 3.10 o posterior. Solo usa la biblioteca estándar; no hay que
instalar dependencias.

```bash
python generar_epg.py
python validar_epg.py
```

El generador imprime las fuentes descargadas, canales detectados/emparejados/sin
EPG y programas importados. Una fuente individual puede fallar sin detener las
demás. `validar_epg.py` también se puede ejecutar por separado para revisar el
archivo guardado.

## GitHub Actions

`.github/workflows/update-epg.yml` se ejecuta cada cuatro horas y también admite
`workflow_dispatch` desde **Actions**. No necesita instalar paquetes. Valida la
salida antes de publicar y crea el commit `chore: update EPG` solo cuando cambia
`epg.xml`. El workflow no tiene disparador `push`, así que su propio commit no
inicia otra ejecución.

El `epg.xml` publicado para Kodi queda disponible en:

```text
https://raw.githubusercontent.com/bryancastanosansegundo5/miEPG/main/epg.xml
```
