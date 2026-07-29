# Cómo auto-hospedar SEO-OS

Esta guía está pensada para pegarse completa en un chat con una IA (Claude,
ChatGPT, etc.) y pedirle "sígueme estos pasos para instalar esto en mi
máquina" — o para seguirla tú mismo. Todo lo que hace falta está aquí, sin
necesitar leer el resto del repo.

## Qué es esto

**SEO-OS** es una plataforma SEO local-first y gratuita (\$0/mes): auditorías
técnicas, rankings reales de Google Search Console, seguimiento de qué
responden las IAs (Gemini/Claude/DeepSeek) sobre tu negocio, GEO/AEO,
backlinks, SEO local y reportes — corre en tu propia máquina, con tu propia
base de datos SQLite, sin depender de un SaaS de terceros. Backend en
FastAPI + SQLite, frontend en HTML/JS vanilla sin build step.

## Requisitos

- **Python 3.12** (3.14 todavía no tiene wheels de `pydantic-core` — no uses
  una versión más nueva).
- Nada más para arrancar. Todas las integraciones externas (Search Console,
  PageSpeed, Serper, IA, etc.) son **opcionales** — sin configurarlas, el
  dashboard funciona igual y cada sección avisa claramente qué le falta.

## Instalación (5 pasos, ~5 minutos)

```bash
# 1. Clonar y entrar al proyecto
git clone <URL-DEL-REPO>
cd seo-os

# 2. Entorno virtual + dependencias
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Variables de entorno (puede quedar vacío, se configura todo desde la UI)
cp .env.example .env

# 4. Crear la base de datos
python -m backend.db.migrations

# 5. Levantar el servidor
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Abre **http://127.0.0.1:8000** en el navegador. Ya está funcionando.

## ⚠️ Primer paso después de instalar: revisa los proyectos sembrados

La migración inicial siembra 4 proyectos reales del autor original (JC
Reparaciones, Komaromi, Fixio, Fixio Tech) como datos de ejemplo — están
definidos en `backend/db/migrations.py` (`SEED_PROJECTS`). Si estás
auto-hospedando esto para TU propio negocio:

1. Usa **➕ Nuevo proyecto** (arriba a la izquierda) para registrar tu propio
   sitio — solo pide nombre, URL, y opcionalmente la propiedad de Search
   Console y competidores.
2. Los proyectos de ejemplo se pueden borrar con **🗑️ Eliminar** (borrado
   lógico, no destruye nada mientras decides si los quieres mantener como
   referencia).

## Configurar las integraciones (la parte fácil)

**No hace falta editar `.env` a mano.** Abre **⚙️ Configuración** en el
dashboard y pega ahí las API keys que quieras activar — se guardan en la
base de datos local (`data/seo.db`, nunca sale de tu máquina) y tienen
prioridad sobre `.env`. Cada campo explica para qué sirve y dónde conseguir
la key. Nada de esto es obligatorio: sin una key configurada, esa sección
del dashboard simplemente avisa "sin configurar" en vez de romperse.

| Integración | Para qué | Dónde conseguirla |
|---|---|---|
| **DeepSeek** | Asistente de IA del dashboard (chat, generar schema, resúmenes) | [platform.deepseek.com](https://platform.deepseek.com) |
| **Gemini / Claude** | AI Visibility — qué responden esos modelos si les preguntas por tu negocio | [aistudio.google.com](https://aistudio.google.com) / [console.anthropic.com](https://console.anthropic.com) |
| **Serper** | Ranking real en Google (SERP en vivo, no solo el promedio de GSC), Local Pack de Maps | [serper.dev](https://serper.dev) (2500 consultas gratis) |
| **PageSpeed Insights** | Core Web Vitals reales por página | [Google Cloud Console](https://console.cloud.google.com) → habilitar "PageSpeed Insights API" |
| **Bing Webmaster** | Backlinks (histórico, anchors, tóxicos) | [bing.com/webmasters](https://www.bing.com/webmasters) |
| **Search Console (autónomo)** | Rankings/clics/impresiones reales sin depender de nada externo | Ver sección siguiente — es el único que requiere un archivo, no solo una key |
| **GA4** | Conversiones/tráfico orgánico real | ID numérico de propiedad en GA4 → Administrar |
| **Telegram** | Alertas automáticas | Hablar con `@BotFather` en Telegram |
| **IndexNow** | Avisar a Bing/Yandex cuando cambia contenido | Se genera sola, ver el campo en Configuración |

### Search Console autónomo (el único setup con archivo, no solo una key)

1. Google Cloud Console → crear proyecto → habilitar "Google Search Console API".
2. Crear una **Service Account** → descargar su JSON de credenciales.
3. En Search Console, agregar el email de esa service account como usuario
   (permiso "Restringido" alcanza) en cada propiedad que quieras auditar.
4. Guardar el JSON como `credentials/gsc-service-account.json` (esa carpeta
   ya está en `.gitignore` — nunca se sube a git).
5. Reiniciar el servidor. El collector de GSC lo detecta solo.

## Verificar que quedó bien

```bash
python -m pytest tests/ -v
```

Corre contra una base de datos SQLite temporal aislada — nunca toca
`data/seo.db`. Si todo pasa, la instalación está sana.

## Primeros pasos en el dashboard

1. Selecciona tu proyecto en el selector de arriba.
2. Botón **▶ Ejecutar auditoría** — crawlea el sitio y corre todos los
   análisis (podés elegir qué pasos correr con **⚙️ Pasos**, y qué período de
   Search Console traer, sin esperar la auditoría completa cada vez).
3. Explora las tabs: Rankings, Técnico, GEO, Contenido, Keywords,
   Competidores, Backlinks, Local.
4. **📄 Reporte** genera un HTML imprimible con todo lo medido, listo para
   pegar en una IA o mandar a un cliente.

## Más allá de esta guía

- `README.md` — historial completo de cómo se construyó cada feature, con el
  razonamiento y los bugs reales que se encontraron y corrigieron por el
  camino. Útil si vas a seguir desarrollando sobre esto.
- `PROMPT_MAESTRO.md` — la especificación completa (arquitectura, reglas de
  honestidad de datos, fases).
