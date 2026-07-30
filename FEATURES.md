# SEO-OS — qué hace y cómo funciona

Referencia completa del sistema: cada feature, qué mide, de dónde saca el
dato real, y qué archivo lo implementa. Pensada para que una IA (o una
persona nueva en el proyecto) entienda todo el sistema sin tener que leer
el código fuente completo. Para instalarlo, ver [SELFHOST.md](SELFHOST.md).
Para el historial de cómo se construyó cada feature (bugs reales encontrados,
decisiones de diseño), ver [README.md](README.md).

## La idea central

SEO-OS es un dashboard SEO local-first (\$0/mes, tu propia base de datos
SQLite, sin SaaS de terceros) que reemplaza herramientas como SEMrush o
Ahrefs para varios sitios a la vez. La regla que gobierna todo el proyecto:
**nunca fabricar un dato**. Cada número que se muestra viene de una API real
(Google Search Console, PageSpeed Insights, Serper, Bing Webmaster, GA4, un
crawler propio, o un modelo de IA consultado en vivo) o dice explícitamente
"sin datos" / "sin configurar" — nunca un promedio inventado, nunca una
estimación disfrazada de hecho.

## Arquitectura

- **Backend**: Python, FastAPI, SQLAlchemy Core (no ORM) sobre SQLite en
  modo WAL. Sin async donde no hace falta — los collectors son funciones
  síncronas, aisladas, ejecutables por separado (`python -m backend.collectors.X`).
- **Frontend**: HTML + CSS + JavaScript vanilla, sin build step, sin
  framework, sin CDN (todo el JS corre tal cual en el navegador).
- **Base de datos**: un único archivo SQLite (`data/seo.db`), 17 tablas.
  Es multi-proyecto: un mismo servidor gestiona varios sitios (`projects`),
  cada uno con su propio histórico.
- **Patrón collector → analyzer → issue**:
  1. Un **collector** (`backend/collectors/*.py`) llama a una API externa
     real, guarda la respuesta cruda en `snapshots` (auditoría: qué se pidió,
     cuándo, si falló), y persiste los datos ya parseados en su tabla.
  2. Un **analyzer** (`backend/analyzers/*.py`) lee esos datos ya guardados
     (nunca llama a una API él mismo) y genera `MagoIssue` — hallazgos con
     severidad, esfuerzo estimado e impacto — que se guardan en `issues`.
  3. El **Action Plan** (`analyzers/opportunities.py`) prioriza todas las
     issues abiertas por impacto/esfuerzo.
- **Degradación con gracia (regla S3)**: si una API no está configurada o
  falla, el collector devuelve `status="skipped"` o `"error"` con un mensaje
  claro — nunca tumba el resto de la auditoría ni rompe el dashboard.

## Módulos, uno por uno

### 🕷️ Crawler técnico propio
**Qué hace**: visita las páginas del sitio (1 req/s, User-Agent
identificable, respeta robots.txt) y extrae: status code, redirects, title,
meta description, H1, canonical, meta robots, X-Robots-Tag, schema.org
(JSON-LD), Open Graph, security headers, cache/compression, enlaces
internos, y calcula un score de legibilidad (Fernández-Huerta) y E-E-A-T
básico por página.
**Cómo**: `backend/collectors/crawler.py` → guarda cada página en `pages`.
**Lo analiza**: `analyzers/technical.py` (semáforo 🔴🟡🟢 por página) y
`analyzers/content.py` (legibilidad, E-E-A-T, content decay).
**Disparo**: manual o como parte de "▶ Ejecutar auditoría".

### 🔑 Rankings y Keywords (Google Search Console)
**Qué hace**: trae clics, impresiones, CTR y posición REALES por
keyword+página, para un período elegible (7d a 16 meses — antes fijo a 28
días). Base para canibalización, CTR-0, y el score de "Clics (28d)".
**Cómo**: `backend/collectors/gsc.py`, vía Search Console API con una
service account (`credentials/gsc-service-account.json`). Guarda en
`gsc_queries` (agregado por período) y `gsc_daily` (real por día, usado para
detectar caída de tráfico).
**Detalle importante**: cada corrida sobreescribe el período anterior (no
acumula duplicados) — filtra siempre por la fecha más reciente
(`latest_gsc_query_date()`).

### 🎯 Ranking real en Google (Serper)
**Qué hace**: consulta el SERP real (no el promedio de 28 días de GSC) para
keywords específicas — posición absoluta, quién más aparece en el top-10
(competidores reales, no solo los que registraste a mano), plataformas
sociales que ocupan espacio.
**Cómo**: `backend/collectors/rank_tracking.py` (guarda el top-10 completo
en `serp_results`) y `serp_compare.py` (compara tu página contra el top-10
de una keyword puntual). Disparo manual — consume créditos de la API.
**Lo analiza**: `analyzers/serp_analysis.py` — por qué el top-10 te gana
(diferencias objetivas: extensión, schema, autoría — nunca afirma "por qué
Google rankea", eso no es observable desde fuera).

### 🗺️ Local Pack de Google Maps
**Qué hace**: posición real en el pack de mapas para una keyword local,
rating y número de reseñas reales del listado.
**Cómo**: `backend/collectors/local_rank.py` vía Serper `/places`. Guarda en
`local_pack_rankings`.

### 💡 Ideas de keywords y preguntas reales
Dos fuentes distintas, ambas honestas sobre qué NO es una búsqueda real:
- **Trends relacionadas** (`collectors/trends.py`, vía `pytrends`): keywords
  relacionadas a las tuyas, con retry/backoff ante el 429 real de Google.
- **Preguntas reales** (`collectors/question_ideas.py`): usa Google
  Autocomplete (público, sin key) sembrado con tus keywords reales de GSC +
  prefijos de pregunta en español ("por qué", "qué hacer si"...) — para
  encontrar preguntas reales que la gente escribe, y poder responderlas en
  el sitio. Cruza contra `gsc_queries` para marcar honestamente si ya hay
  impresiones reales de algo similar (sin afirmar que el sitio "ya responde"
  la pregunta).

### 🧠 AI Visibility
**Qué hace**: consulta en vivo la API real de Gemini/Claude/DeepSeek con una
pregunta de marca, preguntas de categoría, y preguntas de comparación contra
un competidor — y muestra la respuesta REAL, tal cual, marcando si te
menciona (solo es una señal real en las de categoría; en marca/comparación
el nombre ya iba en la pregunta, así que no cuenta).
**Cómo**: `backend/collectors/ai_visibility.py`, llamadas REST directas
(sin SDK) a cada proveedor. Las preguntas pueden ser curadas a mano por
proyecto (`project.config["ai_visibility_prompts"]`) o generadas desde tus
keywords reales de GSC si no hay curación.
**Importante**: esto NO entrena a los modelos ni deja rastro — cada consulta
es independiente. Sirve para monitorear una línea base a lo largo de meses,
no para ver resultados inmediatos.
**No confundir con GEO** (abajo): eso mide si los BOTS pueden crawlear tu
sitio; esto mide qué dice el modelo si le preguntas.

### 🤖 GEO / AEO (acceso de crawlers de IA)
**Qué hace**: revisa `robots.txt` y `llms.txt` para ver si GPTBot,
OAI-SearchBot, ClaudeBot, PerplexityBot y CCBot tienen permiso de crawlear
el sitio — la base técnica para que el contenido pueda llegar a aparecer
citado en respuestas de IA.
**Cómo**: `backend/collectors/geo.py` + `analyzers/geo.py`.

### 📝 Contenido y E-E-A-T
**Qué hace**: legibilidad (Fernández-Huerta), señales de E-E-A-T (fecha de
publicación/actualización visible, autoría), y detección de caída de
tráfico real (compara el promedio de clics entre la primera y segunda mitad
del histórico disponible — necesita ≥60 días cargados o avisa que no
alcanza).
**Cómo**: `analyzers/content.py`, sobre datos ya crawleados + `gsc_daily`.

### 🧩 Duplicados y thin content
**Qué hace**: detecta títulos/meta descriptions duplicados entre páginas y
contenido delgado (muy poco texto real).
**Cómo**: `analyzers/duplicates.py`.

### 🔀 Canibalización de keywords
**Qué hace**: detecta cuando dos páginas tuyas compiten por la misma
keyword real en GSC, resolviendo primero si es solo un redirect viejo (no
canibalización real) usando el mapa de redirects acumulado de todos los
crawls anteriores.
**Cómo**: `analyzers/cannibalization.py` + `analyzers/coverage.py`
(`build_redirect_map`).

### 👆 CTR real por posición
**Qué hace**: publica la curva de CTR REAL de tu sitio por tramo de posición
(1-3, 4-10, 11-20, 21+) — nunca contra una curva de industria genérica.
Declara explícitamente cuando la muestra de clics no alcanza para opinar
por keyword (mínimo 30 clics), y solo entonces señala candidatas puntuales
(mucha impresión, cero clics).
**Cómo**: `analyzers/ctr.py`, sobre `gsc_queries`.

### 🎯 Oportunidades / Action Plan
**Qué hace**: junta todas las issues abiertas de todos los analyzers,
prioriza por impacto/esfuerzo, exportable a CSV. Incluye zero-impression
pruning (páginas indexables sin impresiones en la ventana cargada — nunca
recomienda noindex directo, solo "revisa en Search Console con 90d
completos").
**Cómo**: `analyzers/opportunities.py`.

### 🔍 Indexación real (URL Inspection API)
**Qué hace**: consulta a Google directamente qué hizo con cada URL
(indexada, "crawled - currently not indexed", bloqueada, etc.) — no una
inferencia propia.
**Cómo**: `backend/collectors/indexation.py`. Corre en segundo plano (puede
tardar ~6 min para 50 URLs, ~6-7s por llamada) para no cortar la conexión
del navegador.

### 🗺️ Cobertura (sitemap ↔ crawleado ↔ indexado)
**Qué hace**: el triángulo completo — cuántas URLs hay en el sitemap,
cuántas alcanzó el crawler, cuántas se verificaron contra Google, cuántas
están indexadas. Detecta huérfanas (sin enlaces internos), rotas, y
contradicciones robots.txt vs sitemap.
**Cómo**: `backend/collectors/sitemap.py` + `analyzers/coverage.py` +
`analyzers/internal_links.py`.

### ⚡ Core Web Vitals (PageSpeed Insights)
**Qué hace**: LCP, CLS, TBT reales por página (no solo la home), con los
umbrales oficiales de Google. Avisa si no hay datos de campo (CrUX) — en
ese caso todo es de laboratorio, no tráfico real.
**Cómo**: `backend/collectors/pagespeed.py`.

### 🏷️ Validación de schema.org
**Qué hace**: no solo detecta que existe un `LocalBusiness`/`Organization`/
etc. — valida que tenga los campos REQUERIDOS (sin los cuales Google no
muestra el rich result) y los RECOMENDADOS (mejoran pero no bloquean),
distinguiendo severidad entre ambos.
**Cómo**: `analyzers/schema_validation.py`, sobre los nodos JSON-LD que ya
extrajo el crawler.

### 🔒 Seguridad (headers, cache, IndexNow)
**Qué hace**: security headers (CSP, HSTS, etc.), cache/compresión, y check
de configuración de IndexNow (aviso a Bing/Yandex de cambios — el envío en
sí es manual, nunca automático).
**Cómo**: `analyzers/security_headers.py`, `analyzers/cache_headers.py`,
`backend/collectors/indexnow.py`.

### 📍 SEO Local
**Qué hace**: consistencia de NAP (nombre/dirección/teléfono) entre
páginas, cobertura de schema LocalBusiness.
**Cómo**: `analyzers/local_seo.py`.

### 🔗 Backlinks
**Qué hace**: histórico de backlinks propios (Bing Webmaster — Moz se
retiró, su credencial disponible estaba deprecada), distribución de
anchors, detección de tóxicos, genera `disavow.txt`, y cruza backlinks
propios con URLs rotas/redirigidas (link reclaim: backlinks que apuntan a
algo que ya no existe).
**Cómo**: `backend/collectors/backlinks.py` + `analyzers/backlinks.py`.

### 🎯 Competidores
**Qué hace**: escanea el sitio de un competidor (mismo crawler técnico),
compara scores, detecta keyword gaps (contenido que ellos tienen y tú no —
basado en contenido real, no en ranking, eso lo cubre el SERP real de
arriba). Clasificación de intent por IA sobre tus keywords reales.
**Cómo**: `backend/collectors/competitor.py` + `analyzers/competitors.py`.

### 📊 GA4 (conversiones reales)
**Qué hace**: tráfico y conversiones orgánicas reales por landing page.
**Cómo**: `backend/collectors/ga4.py`, mismo service account que GSC.

### 🎯 SEO Score global
**Qué hace**: combina los scores de Técnico, Contenido, GEO y Local en un
solo número — promedio simple de los componentes que SÍ tienen datos
(nunca rellena con 0 un componente sin medir). Histórico con gráfico de
evolución y comparador entre dos auditorías (qué issues se resolvieron,
cuáles son nuevas).
**Cómo**: `analyzers/opportunities.py::calculate_seo_score`, tabla `scores`.

### 🤖 Asistente de IA (DeepSeek)
**Qué hace**: chat contextualizado con los datos reales del proyecto activo
(nunca inventa cifras nuevas, solo redacta prosa a partir de lo ya
calculado), botón "Corregir" para metas/titles, generador de schema.org,
clasificación de intent, ideas de contenido para el reporte, resúmenes
ejecutivos.
**Cómo**: `backend/ai/engine.py` (abstracción de proveedor) +
`ai/providers/deepseek.py` + `analyzers/context.py` (arma el contexto real).
La key nunca llega al navegador — todo pasa por el backend.

### ⚙️ Configuración (Settings UI)
**Qué hace**: pantalla dentro del dashboard para pegar las API keys sin
editar `.env` a mano — pensado para que auto-hospedar esto sea fácil. Un
valor guardado aquí gana sobre `.env` mientras exista.
**Cómo**: tabla `app_settings` (key-value) + `backend/settings_store.py`
(`get_secret()`/`set_secret()`) + `routes_settings.py`. Nunca expone el
valor real de vuelta, solo si está configurado y de dónde viene.

### 🔔 Alertas (Telegram)
**Qué hace**: notifica cambios importantes (nuevas issues críticas, caídas
de score) por Telegram.
**Cómo**: `backend/alerts.py`.

### 📄 Reportes HTML/PDF
**Qué hace**: documento standalone e imprimible con TODO lo medido —
scorecards, Action Plan, técnico, GEO, AI Visibility, local, SERP real,
backlinks, CTR, y una sección explícita de "lo que NO medimos todavía"
(honestidad: nunca se presenta como medido algo que no se midió, ni se
subestima algo que sí se mide). Ideas de contenido redactadas por IA a
partir de oportunidades reales.
**Cómo**: `backend/reports.py`, reutiliza las mismas funciones que
`routes_dashboard.py` — una sola fuente de verdad por sección. Sin
dependencia nueva de PDF: usa "Imprimir → Guardar como PDF" del navegador.

### ⚡ Análisis rápido de URL (ad-hoc)
**Qué hace**: analiza cualquier URL sin necesidad de registrarla como
proyecto — con guard SSRF (bloquea IPs privadas/localhost) para que no se
pueda usar para escanear la red interna de quien lo hospeda.
**Cómo**: `analyzers/quick_analysis.py` + `analyzers/url_safety.py`.

## Auditoría orquestada ("▶ Ejecutar auditoría")

El botón principal corre, en orden: crawler → sitemap → GEO → PageSpeed →
GSC → indexación (en segundo plano) → GA4 → oportunidades → local →
cobertura. El botón **"⚙️ Pasos"** deja elegir cuáles correr (útil para
probar uno solo sin esperar los ~10-15 minutos de la secuencia completa) y
qué período de Search Console traer. Los collectors con costo real de API
por consulta (rank tracking, local rank, SERP compare, AI Visibility,
preguntas reales) son deliberadamente **manuales** — nunca corren solos en
la secuencia automática, para no gastar cuota sin que el usuario lo decida.

## Multi-proyecto

Un mismo servidor gestiona varios sitios (tabla `projects`), cada uno con
su propio histórico completo, competidores registrados, y configuración. Se
puede registrar uno nuevo desde el dashboard ("➕ Nuevo proyecto") o
convertir un análisis rápido de URL en proyecto explorable.

## Principios de diseño (por qué es así)

1. **Nunca fabricar un dato** (regla P1). Si no hay fuente real, se declara
   "sin datos" — nunca un promedio, estimación o inferencia presentada como
   hecho.
2. **Degradar con gracia** (regla S3). Un collector sin configurar o que
   falla nunca rompe el resto del dashboard.
3. **Cada collector es standalone** (regla S7): `python -m
   backend.collectors.<módulo> --site <slug>` corre aislado, sin depender
   del servidor web.
4. **Snapshot antes que análisis** (regla S2): los datos crudos se guardan
   ANTES de procesarlos — si el análisis tiene un bug, el dato crudo
   sobrevive para reprocesar.
5. **Idempotencia** (regla S5): correr un collector dos veces nunca duplica
   filas (upsert por claves únicas reales, ej. proyecto+fecha+keyword).
6. **Local-first y $0/mes**: SQLite propio, sin build step en el frontend,
   sin dependencia de un SaaS — todo corre en tu máquina.

## Tests

597 tests (`python -m pytest tests/ -v`), corriendo contra una base de
datos SQLite temporal aislada (nunca tocan `data/seo.db`). Cubren:
analyzers con fixtures offline, collectors con APIs externas mockeadas
(regla QA: nunca una llamada de red real desde la suite), API (caso feliz +
404 + 422), y resiliencia (un fallo de red nunca tumba la app).

## Ver también

- [SELFHOST.md](SELFHOST.md) — instalación y configuración paso a paso.
- [README.md](README.md) — historial de desarrollo: cada feature con el
  razonamiento detrás y los bugs reales que se encontraron y corrigieron.
- [PROMPT_MAESTRO.md](PROMPT_MAESTRO.md) — especificación original completa
  (arquitectura, reglas, fases).
