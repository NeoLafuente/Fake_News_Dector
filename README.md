# Agent Fact Detection System

Sistema agéntico que extrae audio de una URL o un archivo, lo transcribe y
evalúa la veracidad de las afirmaciones del transcript mediante un flujo
LangGraph.

Se publica como imagen Docker privada detrás de una **puerta de acceso que
controla el propietario**: la web solo funciona cuando él la enciende, cada
usuario necesita su autorización explícita y las sesiones duran 10 minutos.

---

## Arquitectura

### Fase 1 · Ingesta y transcripción
- Descarga el audio con `yt-dlp` y lo normaliza con `ffmpeg` a 16 kHz mono.
- Transcribe con **whisper-large-v3-turbo** a través de una API compatible con
  OpenAI (Groq por defecto).

### Fase 2 y 3 · Ecosistema de agentes
- **Fact Extractor** — extrae afirmaciones verificables con **Gemini Flash-Lite**
  (una sola llamada estructurada por análisis).
- **Evidence Search** — recupera evidencia con **DuckDuckGo** (gratis, sin clave)
  o Brave si se activa expresamente.
- **NLI Evaluator** — `cross-encoder/nli-deberta-v3-base` servido con
  **ONNX Runtime** en int8, con voto ponderado por confianza.

### Fase 4 · Control de acceso y de gasto
- Interruptor general, credencial compartida, aprobación por email y Telegram,
  sesiones de 10 minutos revocables y panel de administración.
- Topes diarios y por sesión sobre LLM, búsquedas, transcripciones y audio.

> **Por qué la transcripción y el NLI no corren en local:** la versión anterior
> cargaba Whisper `large-v3` y el cross-encoder sobre PyTorch, lo que producía
> una imagen de ~12 GB imposible de servir en un tier gratuito. Moviendo la
> transcripción a una API y exportando el NLI a ONNX int8, la imagen baja a
> menos de 1 GB sin cambiar el modelo NLI del proyecto.

---

## Modelo de seguridad

```
Usuario ──► ¿web encendida? ──no──► 503
               │sí
               ▼
        credencial (GATE_PASSWORD)
               │
               ▼
     aviso al propietario  ──►  📧 email  +  📱 Telegram  +  🖥️ panel /admin
               │
        [Autorizar] / [Rechazar]
               │autorizado
               ▼
      sesión de 10 min, revocable en cualquier momento
```

| Quieres… | Dónde |
|---|---|
| Encender o apagar la web | `/admin` → botón *Encender* / *Apagar* |
| Autorizar a un usuario | Botón del email, de Telegram o del panel |
| Cerrar la sesión de alguien | `/admin` → *Cerrar* en esa fila |
| Echar a todo el mundo y apagar | `/admin` → **🛑 PARADA DE EMERGENCIA** |

Garantías:

- La web **arranca apagada** tras cada reinicio (`WEB_ENABLED_ON_BOOT=false`).
- Los enlaces de aprobación son HMAC firmados, caducan y son de un solo uso.
  Abrirlos no decide nada: la autorización es un POST confirmado, para que un
  escáner de correo o una previsualización de Telegram no pueda aprobar por ti.
- La revocación es inmediata: la cookie solo lleva un identificador y la validez
  se comprueba en base de datos en cada petición.
- `/docs`, `/openapi.json` y todos los endpoints quedan detrás de la puerta.
- Las reservas de cuota son atómicas y las cabeceras de proxy solo se creen si
  `TRUST_PROXY_HEADERS` lo autoriza.

---

## Puesta en marcha

El procedimiento completo — cuentas, túnel, registro privado y operación diaria —
está en **[DEPLOY.md](DEPLOY.md)**. Resumen:

```bash
cp .env.example .env
echo "SECRET_KEY=\"$(openssl rand -hex 32)\""
echo "ADMIN_TOKEN=\"$(openssl rand -hex 32)\""
echo "GATE_PASSWORD=\"$(openssl rand -base64 12)\""
# pega los tres valores y las claves de Gemini/Groq en .env

docker compose pull && docker compose up -d
```

Después abre `https://tu-dominio/admin`, introduce el `ADMIN_TOKEN` y pulsa
**Encender**.

### Desarrollo local

```bash
uv venv && source .venv/bin/activate
uv pip install -r requirements-serve.txt

# El modelo NLI hay que exportarlo una vez (requiere torch y optimum):
uv pip install "optimum[onnxruntime]" torch sentencepiece
python scripts/export_nli_onnx.py --output ./models/nli-onnx

NLI_MODEL_DIR=./models/nli-onnx SECURITY_DB_PATH=./data/security.db \
COOKIE_SECURE=false PUBLIC_BASE_URL=http://localhost:8000 \
python src/app.py
```

`ffmpeg` debe estar en el PATH. `requirements.txt` sigue conteniendo el entorno
completo de investigación (notebooks, MLflow, Optuna); `requirements-serve.txt`
es lo único que entra en la imagen.

---

## Endpoints

Todos exigen una sesión autorizada salvo donde se indique.

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/` | App si hay sesión, pantalla de acceso si no *(público)* |
| `GET` | `/health` | Estado del servicio *(público)* |
| `POST` | `/auth/request` | Solicita acceso con la credencial *(público)* |
| `GET` | `/auth/status` | Consulta el estado de la solicitud *(público)* |
| `GET` | `/auth/decide` | Enlace firmado de autorizar/rechazar *(propietario)* |
| `POST` | `/process_url` | Pipeline completo desde URL o archivo |
| `POST` | `/transcribe_only` | Solo transcripción, sin agentes |
| `POST` | `/analyze_text` | Pipeline de agentes sobre texto |
| `GET` | `/admin` | Panel de control *(pide `ADMIN_TOKEN`)* |
| `POST` | `/admin/api/web` | Enciende o apaga la web |
| `POST` | `/admin/api/panic` | Parada de emergencia |

---

## Control de gasto

Ninguna llamada externa se emite sin reservar antes una ranura de presupuesto.
Los contadores son diarios (UTC), viven en SQLite y **no se reinician al
reiniciar el contenedor**. Se ven en vivo en `/admin`.

Configuración por defecto: 8 claims y 8 búsquedas por análisis, 3 análisis por
sesión, 80 llamadas al LLM / 150 búsquedas / 60 transcripciones al día, audio de
como máximo 3 minutos. Todo ajustable en el `.env`.

Los proveedores elegidos no pueden facturarte mientras no actives la
facturación: Gemini Flash-Lite y Groq cortan con `429` al agotar su cuota
gratuita, y DuckDuckGo no requiere clave. **Brave sí cobra** desde 2026 y por eso
está desactivado por defecto.
