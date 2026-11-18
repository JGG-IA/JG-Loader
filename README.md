# JG-Loader

Plataforma web para descargar videos de YouTube (y +1000 sitios soportados por yt-dlp) en audio o video.

- **Audio:** MP3 320 kbps, M4A, OPUS, WAV, FLAC, AAC
- **Video MP4:** 360p, 480p, 720p, 1080p, 1440p, 4K, mejor calidad
- Vista previa (miniatura, título, duración), barra de progreso con velocidad y ETA, varias descargas a la vez
- Los archivos se borran solos pasada 1 hora

## Ejecutar en local

Requisitos: Python 3.10+ y **ffmpeg** instalado.

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Abre http://localhost:8000

## Con Docker

```bash
docker build -t jg-loader .
docker run -p 8000:8000 jg-loader
```

## Configuración (variables de entorno)

| Variable | Por defecto | Descripción |
|---|---|---|
| `DOWNLOAD_DIR` | `./downloads` | Carpeta temporal de archivos |
| `FILE_TTL_SECONDS` | `3600` | Tiempo antes de borrar los archivos |
| `MAX_CONCURRENT` | `3` | Descargas simultáneas máximas |

## API

- `POST /api/info` `{url}` → título, miniatura, duración
- `POST /api/download` `{url, format}` → `{id}`
- `GET /api/progress/{id}` → estado y porcentaje
- `GET /api/file/{id}` → el archivo final

## Notas para producción

- Actualiza yt-dlp a menudo (`pip install -U yt-dlp`): YouTube cambia su sitio con frecuencia.
- Las IPs de servidores en la nube suelen recibir bloqueos de YouTube ("Sign in to confirm you're not a bot"). Puede que necesites cookies (`cookiefile` en las opciones de yt-dlp) o un proxy residencial.
- Pon un proxy inverso (Nginx/Caddy) con HTTPS y limita las peticiones por IP.
- Los trabajos se guardan en memoria: usa un solo proceso de uvicorn, o pasa a Redis + una cola de tareas si quieres escalar.
- Descargar contenido de YouTube puede ir contra sus Términos de Servicio y los derechos de autor. Úsalo solo con contenido propio o con permiso.

---
Desarrollado por [Julián García](https://jgg-ia.github.io/portafolio/)
