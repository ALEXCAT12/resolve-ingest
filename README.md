# resolve-ingest

Conversión automática de medios para **DaVinci Resolve en Linux**, pensada para
quien viene de Windows/macOS y se encuentra con que Resolve no abre H.264,
H.265, AAC, MP3, MKV, etc. Usa **ffmpeg** para convertir a formatos que Resolve
sí entiende y, si está disponible, importa el resultado en el proyecto abierto.

Es **un solo archivo Python** (`resolve-ingest.py`), sin dependencias aparte de
`ffmpeg`/`ffprobe` y Python 3.11+. No necesita `sudo` ni instalar paquetes.

---

## Lo primero: ¿esto es un plugin de Resolve?

**No.** Y conviene saber por qué, para no buscar algo que no existe:

- Resolve **no tiene un SDK de plugins para interceptar la importación** de
  archivos. Los plugins que existen son de efectos (OFX/Fusion) o paneles de
  Workflow Integration (Electron, habitualmente Studio). Ninguno se ejecuta
  "antes de que Resolve lea el archivo" ni puede transcodificarlo al vuelo.
- Por eso **no se puede hacer en un único archivo `.plugin`** que se meta en
  Resolve y convierta al importar. No es una limitación de este proyecto: es
  una limitación de la API de Resolve.

La alternativa práctica, que es la que implementa este proyecto, es:

```text
archivo incompatible
        │
        ▼
carpeta vigilada  ──►  ffprobe (¿Resolve lo soporta?)
        │                       │
        │                       ├── sí ──► se importa el original
        │                       │
        │                       └── no ──► ffmpeg ──► DNxHR/PCM ──► importar
        ▼
servicio de usuario (systemd) + API de scripts de Resolve
```

Además se incluye un **script para el menú de Resolve** (`Workspace > Scripts`)
que convierte los clips seleccionados y los vuelve a importar. Sigue sin poder
"arreglar" un clip ya montado en la timeline, porque la API no permite sustituir
medios de forma fiable; hay que reemplazarlo a mano.

---

## Qué hace

1. Vigila `~/Videos/ResolveIngest` (configurable).
2. Espera a que el archivo termine de copiarse (tamaño estable).
3. Lo analiza con `ffprobe` y decide:
   - **Compatible** → no lo toca, solo lo importa si corresponde.
   - **Contenedor raro pero códecs buenos** → remux a MOV sin recodificar.
   - **Audio incompatible** → copia el vídeo y convierte solo el audio a PCM.
   - **Vídeo incompatible** → transcodifica a DNxHR + PCM 24 bits/48 kHz.
   - **Audio suelto** → WAV PCM 24 bits/48 kHz.
   - **Imágenes y RAW nativos** (BRAW, R3D, etc.) → pasan sin tocar.
4. Deja el resultado en `~/Videos/ResolveIngest/Convertidos`.
5. Si Resolve está abierto y la edición permite scripting externo, lo importa al
   bin `Resolve Ingest`.
6. Si no se puede importar (Resolve cerrado o edición free), lo deja en
   `Convertidos` y en cola para más tarde; en free se importa con el script del
   menú de Resolve o a mano.

El original **nunca se borra** por defecto.

---

## Instalación

Requisitos:

- Linux con `ffmpeg` y `ffprobe` (en CachyOS/Arch: `sudo pacman -S ffmpeg`).
- Python 3.11 o superior.
- DaVinci Resolve instalado (probado con 21.x; debería servir 18+).
- `notify-send` es opcional, solo para avisos de escritorio.

```bash
git clone https://github.com/ALEXCAT12/resolve-ingest.git
cd resolve-ingest
chmod +x resolve-ingest.py

# Diagnóstico: comprueba ffmpeg, DNxHR, carpetas, Resolve y hace una conversión de prueba
./resolve-ingest.py doctor

# Instala el servicio de usuario, accesos directos y el script del menú de Resolve
./resolve-ingest.py install --resolve-script
```

No pide contraseña: todo va a `~/.local`, `~/.config` y systemd de usuario.

Comandos útiles:

```bash
systemctl --user status resolve-ingest
systemctl --user restart resolve-ingest
journalctl --user -u resolve-ingest -f

./resolve-ingest.py status
./resolve-ingest.py config
./resolve-ingest.py once archivo.mp4          # convierte ahora, sin esperar al servicio
./resolve-ingest.py scan                      # procesa todo lo que ya está en la carpeta
./resolve-ingest.py uninstall                 # quita servicio y accesos; conserva medios
./resolve-ingest.py uninstall --purge         # además borra config y estado
```

---

## Uso

### 1. Carpeta de ingesta (lo normal)

Arrastra el archivo a:

```text
~/Videos/ResolveIngest
```

En unos segundos aparecerá convertido en `Convertidos/` y, si tu edición permite
importación externa (normalmente Studio), también en el bin `Resolve Ingest`.

También hay un acceso directo **"Abrir carpeta de ingesta para Resolve"** en el
menú de aplicaciones.

### 2. Clic derecho → "Convertir para DaVinci Resolve"

El instalador crea una entrada de aplicación para vídeo/audio. En GNOME,
KDE/Dolphin, Nautilus, etc. aparece en **Abrir con**.

Si el servicio ya está corriendo, el archivo se **encola** en la carpeta
vigilada en vez de lanzar un segundo ffmpeg en paralelo.

### 3. Desde dentro de Resolve

Después de `install --resolve-script`, reinicia Resolve y ve a:

```text
Workspace > Scripts > Utility > Resolve Ingest
```

- Si **seleccionas clips** en el Media Pool, convierte los incompatibles y los
  importa al bin `Resolve Ingest`. Los que ya eran compatibles no se duplican.
- Si **no seleccionas nada**, importa todo lo que haya en `Convertidos` al bin
  `Resolve Ingest`. Es la forma rápida de pasar la carpeta de ingesta a Resolve
  en la edición free.

Este script se ejecuta **dentro** de Resolve, así que no depende del scripting
externo.

---

## Qué considera "compatible"

La política es conservadora: ante la duda, convierte. Así se evita el clásico
"Media Offline" a mitad de proyecto.

| Tipo | Se deja tal cual | Se convierte |
|---|---|---|
| Vídeo | DNxHR/DNxHD, ProRes*, CineForm, XDCAM MPEG-2, DV, sin comprimir | H.264/H.265 en free, AV1, VP9, MPEG-4, etc. |
| Audio | PCM (WAV/AIFF/MOV/MXF) | AAC, MP3, FLAC, Opus, Vorbis, AC-3, WMA, etc. |
| Contenedor | MOV, MXF, MP4, WAV, AIFF | MKV, WebM, AVI, TS, WMV, FLV, OGG, etc. |
| Imágenes | JPG, PNG, TIFF, EXR, DPX, DNG | — (pasan sin tocar) |
| RAW | BRAW, R3D, ARRI, etc. | — (pasan sin tocar) |

\* ProRes se acepta por defecto porque suele funcionar en Resolve Linux. Si tu
versión/edición no lo abre, pon `accept_prores = false` en la configuración.

En **Studio** se consideran compatibles H.264/H.265 y AAC/AC-3/MP3/FLAC. En la
**free** se transcodifican, que es justo el caso habitual en Linux.

### Perfiles DNxHR

`video_profile = "auto"` elige:

| Resolución | Perfil | Uso |
|---|---|---|
| ≤ 1080p | `dnxhr_hq` | 8 bits 4:2:2, buena calidad |
| ≤ 4K | `dnxhr_sq` | 8 bits 4:2:2, equilibrio tamaño/calidad |
| > 4K | `dnxhr_lb` | 8 bits 4:2:2, para editar sin llenar el disco |

Puedes forzar `dnxhr_lb`, `dnxhr_sq`, `dnxhr_hq`, `dnxhr_hqx` (10 bits) o
`dnxhr_444`. Ojo: DNxHR ocupa mucho. A modo orientativo, 1 hora de 1080p30 en
HQ ronda las decenas de GB; 4K puede ser varios cientos de GB. Para HDR/Log,
usa `dnxhr_hqx` y configura el color management en Resolve.

---

## Configuración

Archivo: `~/.config/resolve-ingest/config.toml`

Se crea solo la primera vez. Después de cambiarlo:

```bash
systemctl --user restart resolve-ingest
```

Lo más útil:

```toml
[paths]
inbox = "~/Videos/ResolveIngest"
# output = "~/Videos/OtroDisco/Convertidos"

[transcode]
video_profile = "auto"       # auto | dnxhr_lb | dnxhr_sq | dnxhr_hq | dnxhr_hqx | dnxhr_444
audio_rate = 48000
stability_seconds = 5        # espera antes de tocar un archivo
accept_prores = true

[resolve]
auto_import = "auto"          # auto = solo en Studio; true/false para forzar
bin_name = "Resolve Ingest"
edition = "auto"             # auto | free | studio
scripting_timeout = 30

[behavior]
keep_original = true
move_original = false        # mover originales a _originales
notify = true
retry_failed = false
```

Si quieres convertir sin importar nada, pon `auto_import = false`. Con `"auto"`
(el valor por defecto) solo intenta importar en Studio, que es donde el
scripting externo suele estar disponible.

---

## Importación automática: requisitos y límites

Para que el servicio importe solo:

1. Resolve debe estar abierto y con un proyecto abierto.
2. Debe estar permitida la API externa:
   `Preferencias > Sistema > General > External scripting using = Local`.

**Importante:** el scripting externo es, en la práctica, una función de
**Studio**. La edición free de Linux suele escuchar solo scripts internos (los
del menú `Workspace > Scripts`), no conexiones desde un servicio. Por eso:

- En **Studio**, el flujo es totalmente automático: dejas el archivo en la
  carpeta y aparece en el proyecto.
- En **free**, la conversión sigue siendo automática, pero la importación no:
  abre Resolve y ejecuta `Workspace > Scripts > Utility > Resolve Ingest` sin
  seleccionar nada para importar todo `Convertidos`, o arrastra el archivo
  convertido al Media Pool.

Si Resolve estaba cerrado en **Studio**, los archivos quedan en cola (`status`
muestra "pendientes de importar") y se importan cuando lo abras. En **free** se
quedan en `Convertidos` hasta que ejecutes el script del menú o los importes a
mano.

---

## Por qué no "arregla" clips ya importados

La API de Resolve permite importar, mover a bins, etc., pero no ofrece un
"reemplaza este medio por este otro" seguro para clips ya colocados en la
timeline. `RelinkClips` cambia la carpeta de búsqueda, no sustituye un archivo
por otro con otro nombre/códec. Por eso el flujo correcto es **convertir antes
de importar** (carpeta de ingesta) o usar el script del menú y luego reemplazar
en la timeline si hace falta.

---

## Solución de problemas

- **No pasa nada al dejar un archivo**: revisa que la extensión sea de
  vídeo/audio/imagen, que no empiece por `.` y que no sea un `.part`. Espera
  `stability_seconds`. Mira `./resolve-ingest.py status` y el log.
- **El servicio no arranca**: `systemctl --user status resolve-ingest` y
  `journalctl --user -u resolve-ingest -n 80`.
- **"No pude importar en Resolve"**: abre Resolve, comprueba el proyecto y la
  opción de External scripting. Ejecuta `./resolve-ingest.py doctor` con
  Resolve abierto.
- **ProRes se ve offline**: `accept_prores = false` y vuelve a convertirlo con
  `./resolve-ingest.py once --force archivo.mov`.
- **H.264 debería funcionar (Studio) pero lo convierte**: pon
  `edition = "studio"` o revisa que la detección sea correcta con `doctor`.
- **Se llena el disco**: usa `video_profile = "dnxhr_lb"`, cambia `output` a
  otro disco, o borra `Convertidos` cuando ya hayas importado.
- **Un archivo quedó marcado como fallido**: revisa el error en `status`; si
  corregiste el archivo, `once --force` o cambia su mtime. También puedes poner
  `retry_failed = true`.
- **Audio desincronizado**: suele pasar con VFR de móviles; el script fuerza
  CFR cuando detecta VFR, pero algunos archivos muy rotos necesitan tratamiento
  manual.

---

## Compartirlo

El proyecto vive en <https://github.com/ALEXCAT12/resolve-ingest>. Comparte ese
enlace o el archivo `resolve-ingest.py` junto con este README y la licencia. No
hay rutas absolutas obligatorias: la configuración se crea en el HOME de cada
usuario. En otras distros solo cambia la forma de instalar `ffmpeg` y, si
Resolve no está en `/opt/resolve`, la importación automática usará el Python del
sistema con las variables de la API; la conversión funciona igual.

Licencia MIT. Úsalo, modifícalo y compártelo.

---

## Limitaciones conocidas

- No intercepta el diálogo de importación de Resolve (no existe ese hook).
- No sustituye clips ya montados en la timeline.
- No hace tone mapping HDR→SDR ni gestiona LUTs; para HDR usa HQX y color
  management.
- No soporta medios cifrados/DRM.
- Las imágenes/RAW se pasan sin validar códec a códec; si Resolve no abre un
  formato exótico, conviértelo aparte.
- Convertir a DNxHR es costoso en CPU y disco; es el precio de la compatibilidad.
