# Despliegue y operación

Guía completa para publicar FactX Agent detrás de una imagen privada, un túnel
de Cloudflare y una puerta de acceso que controlas tú.

---

## 0. Qué vas a montar

```
 Usuario  ──►  Cloudflare Edge  ──►  túnel saliente  ──►  contenedor en tu máquina
                (HTTPS, WAF)          (sin abrir puertos)      │
                                                               ├─ puerta de acceso
                                                               ├─ topes de gasto
                                                               └─ agentes LangGraph
```

Tienes **tres interruptores independientes**, de más suave a más contundente:

| Nivel | Acción | Efecto |
|---|---|---|
| Aplicación | Botón *Apagar* en `/admin` | 503 para todos, sesiones cerradas. El panel sigue accesible |
| Red | `docker compose stop cloudflared` | La URL deja de existir en internet |
| Máquina | `docker compose down` | No queda nada corriendo |

---

## 1. Cuentas necesarias (todas gratis, ninguna pide tarjeta)

| Servicio | Para qué | Dónde |
|---|---|---|
| Google AI Studio | `GOOGLE_API_KEY` (extracción de claims) | aistudio.google.com/apikey |
| Groq | `TRANSCRIPTION_API_KEY` (whisper-large-v3-turbo) | console.groq.com/keys |
| Cloudflare | túnel y dominio | dash.cloudflare.com |
| Gmail | contraseña de aplicación para SMTP | myaccount.google.com/apppasswords |
| Telegram | bot de avisos (opcional) | @BotFather |

> **DuckDuckGo** no necesita cuenta ni clave: es el buscador por defecto.
> Brave dejó de tener plan gratuito en febrero de 2026 — actívalo solo si
> aceptas que te facture.

---

## 2. Genera los secretos

```bash
cp .env.example .env

echo "SECRET_KEY=\"$(openssl rand -hex 32)\""
echo "ADMIN_TOKEN=\"$(openssl rand -hex 32)\""
echo "GATE_PASSWORD=\"$(openssl rand -base64 12)\""   # esta es la que repartes
```

Pega los tres valores en `.env` junto con las claves del paso 1.

> `SECRET_KEY` firma las cookies y los enlaces de aprobación. Si lo cambias,
> todas las sesiones y enlaces vivos dejan de valer al instante — es otro
> botón de pánico más.

---

## 3. Configura el bot de Telegram (opcional, muy recomendable)

Con ventanas de 10 minutos, el email puede llegar tarde. Telegram llega en segundos.

1. Habla con **@BotFather** → `/newbot` → copia el token en `TELEGRAM_BOT_TOKEN`.
2. Escribe algo a tu bot (si no, no puede contestarte).
3. Habla con **@userinfobot** → copia tu id numérico en `TELEGRAM_CHAT_ID`.

Recibirás un mensaje con botones **✅ Autorizar 10 min** / **❌ Rechazar**.

---

## 4. Publica la imagen en GHCR (privado y gratis)

Se hace solo: `.github/workflows/build-push.yml` construye y sube en cada push.

```bash
git push -u origin claude/jolly-newton-cl4o4o
```

La imagen aparece en `ghcr.io/neolafuente/fake_news_dector`. **Es privada por
defecto**; compruébalo en *Package settings* del repositorio.

Para que tu servidor pueda descargarla necesitas un token de lectura:

1. GitHub → Settings → Developer settings → **Personal access tokens (classic)**
2. Scope: solo `read:packages`
3. En la máquina que sirve la app:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u neolafuente --password-stdin
```

<details>
<summary>Construir localmente en vez de usar el CI</summary>

```bash
docker build -t ghcr.io/neolafuente/fake_news_dector:latest .
docker push ghcr.io/neolafuente/fake_news_dector:latest
```
La primera construcción tarda: la etapa `nli-builder` descarga torch para
exportar el modelo NLI. Ese peso **no** acaba en la imagen final.
</details>

---

## 5. Monta el túnel de Cloudflare

Un *named tunnel* da una URL **estática** que sobrevive a reinicios.

1. dash.cloudflare.com → **Zero Trust** → *Networks* → *Tunnels* → **Create a tunnel**
2. Tipo **Cloudflared**, ponle nombre (`factx`), copia el token → `CLOUDFLARE_TUNNEL_TOKEN` en `.env`
3. En *Public Hostname*:
   - Subdominio: `factx`, dominio: el tuyo
   - Service: **HTTP** → `app:8000`  ← el nombre del servicio en compose, no `localhost`
4. Pon esa URL en `PUBLIC_BASE_URL` del `.env` (se usa en los enlaces del email)

> Sin dominio propio, `cloudflared tunnel --url http://localhost:8000` da una URL
> aleatoria de `trycloudflare.com` que cambia en cada arranque. Sirve para
> probar, pero entonces hay que actualizar `PUBLIC_BASE_URL` cada vez.

---

## 6. Arranca

```bash
mkdir -p data          # aquí viven sesiones, contadores e interruptor
docker compose pull
docker compose up -d
docker compose logs -f app
```

Comprueba que responde:

```bash
curl -s http://localhost:8000/health   # solo desde la máquina anfitriona
# {"status":"ok","web_enabled":false}
```

`web_enabled:false` es lo correcto: **la web arranca apagada a propósito**.

---

## 7. Operación diaria

### Antes de una demo

1. Abre `https://factx.tudominio.com/admin`
2. Pega tu `ADMIN_TOKEN` (se queda guardado en ese navegador)
3. Pulsa **Encender**
4. Pasa a los usuarios la URL y el `GATE_PASSWORD`

### Cuando alguien entra

1. El usuario mete su nombre y la credencial
2. Te llega un aviso por email y Telegram
3. Pulsas **Autorizar** → entra con una sesión de 10 minutos
4. Lo ves en el panel con su cuenta atrás y sus análisis consumidos

### Al terminar

Pulsa **🛑 PARADA DE EMERGENCIA**: apaga la web y expulsa a todo el mundo de golpe.

### Desde la terminal, sin navegador

```bash
BASE=https://factx.tudominio.com
TOK=tu_admin_token

curl -sX POST $BASE/admin/api/web   -H "X-Admin-Token: $TOK" -H 'Content-Type: application/json' -d '{"enabled":true}'
curl -sX POST $BASE/admin/api/panic -H "X-Admin-Token: $TOK"
curl -s     $BASE/admin/api/state   -H "X-Admin-Token: $TOK" | jq
```

---

## 8. Control de gasto

Ninguna llamada externa se hace sin reservar antes una ranura de presupuesto.
Los contadores viven en SQLite y se reinician a las 00:00 UTC — **reiniciar el
contenedor no regala cuota nueva**.

| Variable | Por defecto | Qué corta |
|---|---|---|
| `MAX_TRANSCRIPT_CHARS` | 6000 | Tokens que llegan al LLM |
| `MAX_FACTS_PER_RUN` | 8 | Claims por análisis |
| `MAX_SEARCHES_PER_RUN` | 8 | Búsquedas por análisis |
| `MAX_RUNS_PER_SESSION` | 3 | Análisis por sesión de 10 min |
| `DAILY_LLM_CALL_BUDGET` | 80 | Llamadas al LLM al día |
| `DAILY_SEARCH_BUDGET` | 150 | Búsquedas al día |
| `DAILY_TRANSCRIPTION_BUDGET` | 60 | Transcripciones al día |
| `MAX_AUDIO_SECONDS` | 180 | Duración de audio aceptada |
| `MAX_UPLOAD_MB` | 20 | Tamaño de subida |

El consumo del día se ve en vivo en `/admin`.

**Dónde estaría el riesgo si te saltas esto:**

- **Gemini**: los alias *Flash* tienen del orden de 20 peticiones/día gratis;
  los **Flash-Lite** unos 500. Por eso `GEMINI_MODEL` apunta a Flash-Lite.
  Mientras no actives la facturación en Google Cloud, el free tier no puede
  cobrarte: corta con error 429.
- **Groq**: 2.000 transcripciones/día y unas 8 h de audio. Sin tarjeta.
- **DuckDuckGo**: gratis, sin clave.
- **Brave**: `$5/1.000 búsquedas` **con cargo automático**. Solo si pones
  `SEARCH_PROVIDER="brave"`.

---

## 9. Problemas frecuentes

| Síntoma | Causa | Solución |
|---|---|---|
| `Faltan variables obligatorias` al arrancar | `.env` incompleto | Rellena `SECRET_KEY`, `GATE_PASSWORD`, `ADMIN_TOKEN` |
| El email no llega | Gmail rechaza la contraseña normal | Usa una **contraseña de aplicación** de 16 caracteres |
| Todo devuelve 503 | La web está apagada | Púlsale a *Encender* en `/admin` |
| La sesión se cae al recargar | `COOKIE_SECURE=true` sobre http | En local pon `COOKIE_SECURE=false` |
| Los enlaces del email apuntan a localhost | `PUBLIC_BASE_URL` sin actualizar | Pon la URL pública real |
| `no such file /models/nli-onnx/model.onnx` | Imagen construida a medias | Reconstruye sin caché: `docker build --no-cache .` |
| El túnel no conecta con la app | Service mal puesto | Debe ser `http://app:8000`, no `localhost` |

---

## 10. Notas de seguridad

- La web **arranca siempre apagada** (`WEB_ENABLED_ON_BOOT=false`). Un reinicio
  no deseado deja el servicio cerrado, no abierto.
- Los enlaces de aprobación son HMAC firmados, caducan en 15 minutos y **solo
  funcionan una vez**.
- Revocar una sesión es inmediato: la cookie solo transporta un identificador y
  la verdad está en la base de datos, que se consulta en cada petición.
- `/docs` y `/openapi.json` están detrás de la puerta: no se puede inventariar
  la API sin sesión.
- El contenedor corre como usuario sin privilegios (uid 10001) y `/data` es lo
  único que necesita escribir.
- `AUTH_REQUESTS_PER_HOUR` limita por IP para que nadie te inunde el correo.
- El `.env` está en `.gitignore` y en `.dockerignore`: no entra ni en el repo ni
  en ninguna capa de la imagen.
