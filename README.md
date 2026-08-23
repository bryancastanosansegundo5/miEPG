# miEPG

Generador automático de una EPG XMLTV reducida a los canales del M3U remoto. La
EPG resultante está pensada para usarse con Kodi IPTV Simple Client.

## Funcionamiento

`generar_epg.py` realiza estas operaciones:

1. Descarga el M3U publicado mediante IPNS.
2. Lee nombre, `tvg-id`, `tvg-logo` y grupo de todas las entradas `#EXTINF`.
3. Descarga las guías DobleM e Italia de EPGshare.
4. Asocia por ID exacto, ID normalizado, mappings comprobados o `display-name`.
5. Conserva solo los canales y programas necesarios y crea `epg.xml`.
6. Muestra `OK` para cada asociación encontrada y `XX` para las no resueltas.

Las variantes 720p/1080p y los asteriscos se ignoran al identificar el canal.
Cuando el M3U tiene `tvg-id`, ese mismo valor se usa como `channel id` en el XMLTV.
Cuando está vacío, se conserva el ID real de la fuente y el nombre limpio del M3U
se incluye como `display-name`; no se inventan IDs.

La dirección original `inbrowser.link` devuelve una aplicación HTML que necesita
un navegador. El script la prueba primero y, si no recibe un M3U, usa el gateway
HTTP `ipns.dweb.link` para descargar exactamente el mismo nombre IPNS.

## Generación local

Se necesita Python 3.10 o posterior; no hay dependencias externas:

```bash
python generar_epg.py
```

El comando termina con código `0` si todas las entradas tienen EPG y con código
`2` si genera el archivo pero queda alguna entrada `XX`. Un fallo de descarga o de
XML termina con código `1`.

## Automatización

El workflow `.github/workflows/actualizar-epg.yml` se ejecuta cada cuatro horas y
también permite ejecución manual desde la pestaña **Actions**. Tiene permiso de
escritura y solo hace commit/push cuando `epg.xml` cambia.

Después de subir el repositorio, la URL para Kodi será:

```text
https://raw.githubusercontent.com/bryancastanosansegundo5/miEPG/main/epg.xml
```

## Fuentes

- M3U: `https://k51qzi5uqu5dh5qej4b9wlcr5i6vhc7rcfkekhrxqek5c9lk6gdaiik820fecs.ipns.inbrowser.link/hashes_kodi.m3u`
- DobleM: `https://raw.githubusercontent.com/davidmuma/EPG_dobleM/master/EPG_dobleM.xml.gz`
- Italia: `https://epgshare01.online/epgshare01/epg_ripper_IT1.xml.gz`
