# SEO Operating System (SEO-OS)

Plataforma SEO unificada, local-first, $0/mes. Ver [PROMPT_MAESTRO.md](PROMPT_MAESTRO.md)
para la especificación completa (arquitectura, reglas, fases, agentes).

> 🚀 **¿Vas a instalar esto?** Empieza por [SELFHOST.md](SELFHOST.md) — guía
> corta de auto-hospedaje, pensada para seguirla directo o pegarla en un chat
> con una IA.
>
> 📖 **¿Quieres saber qué hace todo el sistema y cómo?** Ver
> [FEATURES.md](FEATURES.md) — referencia completa de cada módulo, de dónde
> saca cada dato real, y por qué está diseñado así.
>
> Lo de abajo es el historial completo de cómo se construyó cada feature
> (útil para seguir desarrollando, no para instalar por primera vez).

**Estado actual: Fases 0, 1, 2, 3 y 4 completas**, más features añadidas a
pedido del usuario (nuevo proyecto, análisis rápido de URL, eliminar
proyecto). Dashboard funcional con datos reales de Google Search Console,
crawler técnico propio, análisis de canibalización, GEO/AEO, Contenido &
E-E-A-T, un asistente de IA (DeepSeek) integrado, Keywords (Trends + intent),
Competidores (matriz + keyword gap), Backlinks (Bing Webmaster, anchors,
tóxicos + disavow — ver § Remoción de Moz), SEO Local (NAP + schema
LocalBusiness), alertas
por Telegram, reportes HTML/PDF y auth opcional por token — todo validado
contra `jcreparaciones.com` y sus competidores reales en producción.

### Gap real cerrado: "Keywords Principales" solo mostraba el top 10, no las 149 reales de jc (2026-09-13)

Pedido del usuario: re-auditar que todo lo analizado entre al reporte (mismo
tipo de verificación que ya había encontrado el gap de security_headers/
indexnow). Cruzando cada endpoint de dashboard contra los imports de
`reports.py`, todo lo demás ya estaba cubierto — el único hueco real: la
sección "Keywords Principales" cortaba a `[:10]` sin importar cuántas
queries reales hubiera en Search Console. Contra jc real: **149 keywords
con posición/clics/impresiones reales, solo 10 visibles en el reporte** —
139 nunca aparecían ahí, solo en la pestaña Rankings del dashboard (que no
se copia/pega en una conversación de IA, a diferencia del reporte).

- Subido a top 40 (mismo criterio de truncación ya usado en "Preguntas
  reales de Google": top 40 + nota "Mostrando 40 de N" cuando hay más).
- La pestaña Rankings sigue teniendo su propio gráfico de tendencia diaria
  (clics/impresiones/CTR/posición en el tiempo) que el reporte, siendo
  texto/tablas, no reproduce — decisión de diseño existente, no un gap: el
  dato agregado (`clics 28d`) ya vive en Scorecards.
- Test de regresión con 55 keywords sintéticas verificando que la #40
  aparece y la #55 no. Verificado en vivo contra jc real: "Mostrando 40 de
  149".
- Suite: 800 → 801.

### Bug real: reporte tumbaba con 500 por un `effort="30min"` fuera de contrato (2026-09-12)

Encontrado durante un escaneo completo con todas las herramientas contra jc:
`/api/dashboard/jc/report` devolvía **500 Internal Server Error** al entrar.

- Causa raíz: `build_backlinks_issues()` (`backend/analyzers/backlinks.py`)
  crea el issue resumen de "N backlinks tóxicos" con `effort="30min"` — un
  valor FUERA del contrato `Effort = Literal["5min", "1h", "1d"]`
  (`backend/analyzers/mago.py`). Python no valida `Literal` en tiempo de
  ejecución, así que esto se guardó en la tabla `issues` sin ningún error
  durante meses — recién truena cuando `get_action_plan()` ordena por
  `effort_rank[issue.effort]`, un dict que solo tiene esas 3 claves reales
  (confirmado con un grep: TODO el resto del proyecto, sin excepción, solo
  usa esas 3). `KeyError: '30min'` tumbaba el reporte entero.
- Corregido en 3 capas: (1) causa raíz — `effort="30min"` → `"1h"` en
  `backlinks.py`; (2) datos ya guardados — 2 filas reales de jc en `issues`
  con `effort='30min'` corregidas directo en `data/seo.db`; (3) endurecido
  `effort_rank[...]` → `effort_rank.get(..., 1)` en los DOS lugares que
  ordenan por effort (`mago.sort_by_priority` y
  `routes_dashboard.get_action_plan`) — un valor fuera de contrato futuro
  degrada con gracia en vez de tumbar la página.
- Test de regresión: `build_backlinks_issues()` ahora se verifica contra el
  set real de valores válidos, no solo un valor de ejemplo — falla si
  alguien reintroduce un effort fuera de contrato.
- Verificado en vivo: `/report` volvía 500 antes del fix, 200 después,
  mismo servidor, mismos datos.

### Volumen de búsqueda real y citas locales — 2 nuevas APIs de DataForSEO (2026-08-29/30)

Pedido del usuario tras investigar el catálogo de DataForSEO ("ambas"), cierra
dos huecos más que el reporte declaraba en "Lo que NO medimos todavía".

**Volumen de búsqueda real (Google Ads)** — `backend/collectors/search_volume.py`,
`keywords_data/google_ads/search_volume/live`. Reemplaza el "interés relativo
0-100" de Trends por búsquedas/mes REALES, con histórico de 12 meses,
competencia y CPC. Verificado en vivo contra jc antes de escribir el parseo:
"reparacion de celulares cali" = 170 búsquedas/mes reales, competencia LOW.
Costo plano $0.09 por consulta de hasta 1.000 keywords (no por keyword) — no
se generan issues (dato de contexto, no un "problema" en sí mismo).

**Citas locales / listados de negocio** — `backend/collectors/
business_listings.py`, `business_data/business_listings/search/live`.
Busca el nombre del negocio y separa: listado propio (por dominio), citas
reales en otros directorios (mismo teléfono, dominio distinto — señal
positiva) y posibles listados duplicados (nombre casi idéntico, sin
teléfono/dominio que lo confirme — bandera para revisión manual, nunca
certeza). Genera un issue medium por cada posible duplicado encontrado.

- **Bug real encontrado y corregido en la MISMA verificación en vivo** (no
  después): buscar solo por título, sin filtro geográfico, trajo negocios
  con el mismo nombre genérico "JC Reparaciones" en México, Argentina y
  Paraguay — la primera versión los marcó a los 12 como "posible duplicado"
  por comparar solo el título normalizado. Corregido exigiendo que la
  dirección del candidato comparta al menos un token de ciudad/región con
  la dirección del listado PROPIO ya encontrado — sin listado propio
  hallado (sin ancla de ubicación), no se marca ningún duplicado en vez de
  arriesgar ruido global.
- **Hallazgo real que sobrevivió la corrección**: jc SÍ tiene un posible
  listado duplicado real en Cali — "Jc Reparaciones" en "Cra. 103 #13 L,
  Ciudad Jardín, Cali, Valle del Cauca", sin teléfono ni dominio, dirección
  distinta a la real (Av. 5a Nte. #20-13) — vale la pena que el usuario lo
  revise en Google Business Profile.
- Ambos wireados end-to-end: collector → endpoint dashboard → botón/tabla
  en 🛰️ DataForSEO → sección del reporte (con `available`/`empty_reason`
  honesto) → los 2 ítems correspondientes de "Lo que NO medimos todavía"
  ahora son condicionales a `settings.has_dataforseo` (mismo patrón que
  ChatGPT/Perplexity).
- Tests: 26 nuevos (7 search_volume, 19 business_listings incluyendo la
  regresión del bug de ubicación) + 4 de reporte. Suite: 769 → 799.
- Verificado en vivo end-to-end contra jc real (servidor levantado, botones
  reales): volumen real de "reparacion iphone cali" = 30 búsquedas/mes;
  listados: 1 propio + 0 citas + 1 posible duplicado real (tras el fix).

### Páginas huérfanas: reconciliar issues de URLs que ya no existen (2026-08-28)

Hallazgo colateral del fix de `noindex_justified` de arriba, implementado
como tarea aparte a pedido del usuario: 10 issues "critical" de jc eran de
URLs con la estructura VIEJA de los templates ciudad×servicio (antes de que
el sitio migrara de `/reparacion-iphone-{ciudad}` a `/reparacion-iphone/
{ciudad}`) — el crawler ya no las descubre, y la reconciliación normal
(`reconcile_page_issues`) solo toca URLs presentes en el crawl MÁS
RECIENTE, así que esas issues quedaban abiertas para siempre sin ninguna
vía de cerrarse, sin importar cuántas veces se re-crawleara el sitio.

- Nuevo `_check_page_still_alive(url)` en `crawler.py`: HEAD real (GET si el
  servidor no soporta HEAD, 405/501) antes de tocar nada — **nunca se marca
  huérfana una página solo por ausencia en el crawl** (podría ser un límite
  de `max_pages`, no que la página desapareció). Si termina en una URL
  DISTINTA a la pedida (redirect permanente, el caso real de jc) también
  cuenta como "ya no es esta URL".
- `TechnicalCrawler._reconcile_orphaned_pages()`: al final de cada
  `persist_analysis`, revisa hasta `MAX_ORPHAN_CHECKS_PER_RUN=30` páginas de
  `pages` ausentes del crawl actual (límite duro, regla P5 — no dispara
  cientos de peticiones extra en una corrida; las que sobren se revisan en
  la siguiente). Confirmadas muertas: `is_orphaned=True` (nunca se borra la
  fila, para no perder historial) + se cierran sus issues abiertas
  reutilizando `reconcile_page_issues` con `fresh_keys` vacío — 'resolved'
  ya significa exactamente "el detector dejó de verlo", semántica correcta
  también para "la página misma ya no existe".
- Si una página marcada huérfana vuelve a aparecer en un crawl futuro, el
  upsert normal de `persist_analysis` la desmarca sola (`is_orphaned=False`
  en cada escritura fresca) — no hace falta ningún paso manual.
- `is_orphaned=False` agregado como filtro a los 5 lugares que leen `pages`
  para scores/listados visibles al usuario: `get_technical` (pestaña
  Técnico), `get_content` (pestaña Contenido), `get_scorecards`,
  `run_opportunities_analysis` y `build_project_context` (Asistente IA) —
  una página confirmada muerta no debe seguir contando en el score técnico
  ni aparecer en tablas de páginas activas. Dejado SIN tocar a propósito:
  `site_health.py` (cobertura sitemap↔crawleado — ahí "en pages pero no
  descubierta" es información real, no ruido a esconder), lookups por
  `page_id` puntual, y los collectors de indexación/competidores (fuera del
  alcance de este pedido).
- Migración `pages.is_orphaned/orphaned_checked_at/orphaned_reason`
  (idempotente, ver `migrations.py`).
- Tests: 9 nuevos (`_check_page_still_alive` en sus 4 casos reales — 200,
  404, redirect a otra URL, HEAD no soportado — más la integración completa
  vía `persist_analysis`: marca huérfana y cierra el issue, NO marca si
  sigue viva, se desmarca sola al reaparecer, y respeta el límite por
  corrida). `time.sleep` mockeado en los tests (si no, el test del límite
  tardaba 30s reales). Suite: 761 → 769.
- Aplicado en vivo contra jc real (tras migrar `data/seo.db`): re-crawl
  completo — ver resultado abajo.

### Bug real corregido: `noindex_justified` nunca se poblaba (190 issues "critical" que eran noindex a propósito) (2026-08-28)

Otra sesión de Claude auditando el reporte de jc reportó "190 issues críticas
= todas noindex de ciudades sin volumen — es intencional (mitiga el ~93% de
duplicado entre plantillas de ciudad), ¿es un falso positivo del Action
Plan?". Investigación propia: SÍ es un bug real de SEO-OS, no solo un falso
positivo a ignorar — `PageData.noindex_justified` (en `technical.py`) existe
desde antes, y `evaluate_indexability()` ya tenía la rama correcta (noindex
justificado → amarillo "justificado", no rojo) — pero **nada lo poblaba
nunca**, así que esa rama jamás se activaba y CUALQUIER noindex, incluida
una decisión de diseño a propósito, salía crítico.

- Nueva `is_noindex_justified(url, patterns)` en `technical.py` — glob
  simple (`fnmatch`) sobre el PATH de la URL contra
  `project.config["noindex_justified_patterns"]` (mismo patrón sin-UI que
  `banned_phrases`/`geo_grid`). Función única compartida — antes iba a
  quedar duplicada entre el crawler y los otros 3 lugares que reconstruyen
  `PageData` desde la tabla `pages`.
- Encontrados y corregidos, por trazar TODOS los consumidores de
  `page_data_from_stored_row()` (no solo el primero obvio): el propio
  crawler (`persist_analysis`), `calculate_technical_score()`
  (`opportunities.py`, 2 call sites: `run_opportunities_analysis` y
  `routes_dashboard.get_scorecards`), `context.py`
  (`build_project_context`, usado por el Asistente IA), y
  `_rebuild_technical_report()` (`routes_dashboard.get_technical`, la
  pestaña Técnico del dashboard). Sin esto, la pestaña Técnico seguía
  mostrando rojo aunque el Action Plan ya mostrara la severidad corregida —
  dos vistas del mismo dato en desacuerdo.
- **Hallazgo no obvio documentado con test propio**: el `technical_score`
  (número del SEO Score) NO cambia con este fix — solo cuenta % de celdas
  VERDES, y ni amarillo (justificado) ni rojo (sin justificar) son verde.
  El efecto real y observable es la SEVERIDAD del issue en el Action Plan
  (`critical` → `medium`) y el color de celda en la pestaña Técnico
  (`red` → `yellow`) — quien toque este código después no debe asumir que
  el score también sube.
- `project.config["noindex_justified_patterns"]` de jc, determinado
  inspeccionando `lib/cities.ts`/`INDEXED_CITY_SLUGS` Y `next.config.js` del
  repo real de jcreparaciones.com (no adivinado): 5 plantillas de
  servicio×ciudad marcan `noindex` para cualquier ciudad fuera de
  `INDEXED_CITY_SLUGS`. **Vuelta real durante la verificación en vivo**: el
  primer intento asumió que las 5 plantillas usaban el mismo formato de URL
  con slash (`/reparacion-iphone/{ciudad}`, la carpeta de ruta real en
  `app/`) — cierto para `reparacion-celulares`, pero `reparacion-iphone`,
  `reparacion-samsung`, `reparacion-macbook` y `microsoldadura` en realidad
  se enlazan y se guardan en `pages.url` con GUIÓN
  (`/reparacion-iphone-{ciudad}`), que `next.config.js` redirige 301 a la
  ruta con slash — dos formas de URL reales para la misma plantilla. Los
  patrones finales cubren ambas formas: `/reparacion-celulares/*`,
  `/reparacion-iphone/*` + `/reparacion-iphone-*`, y así para
  samsung/macbook/microsoldadura (9 patrones en total). Una URL indexada que
  calce el glob no se ve afectada: el gate solo entra en juego cuando
  `is_indexable=False` en primer lugar.
- **Hallazgo colateral, fuera de alcance de este fix**: 10 de los issues
  "critical" restantes son páginas con URL en formato guión
  (`/reparacion-iphone-candelaria-valle`) que YA NO existen en el sitio real
  (la URL viva es la de slash) — quedaron huérfanas en `pages`/`issues` de
  un crawl anterior a que el sitio migrara a la ruta con slash, y como el
  crawler actual ya no las descubre, nunca se reconcilian (la reconciliación
  solo toca URLs que SÍ aparecen en el crawl más reciente). No es un bug de
  `noindex_justified` — es que no hay limpieza de páginas huérfanas cuando
  una URL desaparece del sitio. Anotado para un fix aparte, no corregido
  aquí (fuera del alcance de "cerrar las 190 críticas falsas").
- Tests: 11 nuevos (`is_noindex_justified`, `page_data_from_stored_row` con
  y sin patrones, `analyze_page` bajando de critical a medium, el
  `technical_score` documentado sin cambio, integración vía
  `run_opportunities_analysis`, y el endpoint `/technical` mostrando
  amarillo). Suite: 750 → 761.
- Re-crawleado jc completo (500 páginas) para reconciliar las 190 issues
  viejas contra la config real: **68 issues bajaron de critical a medium**
  (las páginas de ciudad noindex a propósito, ahora correctamente marcadas
  "justificado"), 258 issues viejas se cerraron por reconciliación normal.
  Critical abiertas: 190 → 105, de las cuales 95 son issues reales sin
  relación con este fix (94 de meta description, 1 de H1) y 10 son las
  páginas huérfanas con URL vieja descritas arriba (no tocadas a propósito).

### ChatGPT y Perplexity reales en AI Visibility, vía DataForSEO AI Optimization API (2026-08-28)

Pedido del usuario: "también tenemos que utilizar la herramienta para ver
qué está diciendo la IA o qué señales tenemos que mandarle a la IA". Cierra
exactamente el hueco que el reporte venía declarando en "Lo que NO medimos
todavía": *"Qué responde ChatGPT (OpenAI) o Perplexity en vivo... no
consultamos esas dos APIs específicas todavía"*.

- Investigado y verificado en vivo ANTES de escribir el parseo (misma
  disciplina de esta sesión): DataForSEO tiene una API completa,
  `ai_optimization/{platform}/llm_responses/live`, que consulta ChatGPT,
  Claude, Gemini y Perplexity de verdad, con `web_search=true` opcional. Se
  integraron solo ChatGPT y Perplexity (Claude/Gemini ya se consultan
  directo contra su propia API — habría sido redundante duplicarlos).
- **Bug real encontrado en vivo, no de la documentación**: con
  `max_output_tokens` puesto, el modelo de razonamiento "o4-mini" devuelve
  40501 "Invalid Field" — se corrigió no enviando ese parámetro (se deja el
  default del proveedor).
- Integrado en el mismo pipeline de `ai_visibility.py` (no una tabla ni
  pestaña nueva) — `chat_gpt`/`perplexity` se suman a `_PROVIDERS` con la
  MISMA credencial de DataForSEO (no hace falta una API key propia de
  OpenAI/Perplexity), y siguen el mismo contrato `ask(credenciales, prompt)
  -> texto` que Gemini/Claude/DeepSeek, así que reutilizan TODA la lógica
  existente: `mentions_business`, comparativo, issues del Action Plan,
  tabla del dashboard, sección del reporte.
- El ítem de "Lo que NO medimos todavía" ahora es condicional: dice
  "medido" cuando DataForSEO está configurado, y mantiene el texto honesto
  original cuando no lo está.
- **Hallazgo real de la primera corrida en vivo contra jc**: ChatGPT (con
  búsqueda web real) conoce el negocio con detalle exacto — fundador,
  año de fundación (2006), certificación IRP de Apple, dirección física,
  +15.000 equipos reparados — y cita `jcreparaciones.com` como fuente
  directa. Perplexity también la menciona. 16 respuestas guardadas en total
  (gemini + deepseek + chat_gpt + perplexity — claude se saltó con gracia,
  sin key configurada).
- Tests: 8 nuevos en `test_ai_visibility.py` (parseo real, regresión del
  bug de `max_output_tokens`, regresión de body-array-plano, error de
  status_code, respuesta sin items de mensaje, `_configured_providers` con
  y sin DataForSEO, corrida completa guardando el provider correcto).
  Suite: 742 → 750.
- Costo real observado: ~$0.02/consulta ChatGPT (o4-mini), ~$0.006/consulta
  Perplexity (sonar) — sigue siendo disparo MANUAL, nunca entra en la
  auditoría automática (mismo criterio de costo que rank_tracking/
  local_rank/ai_overview_check).

### Backlinks vía DataForSEO como segunda fuente, mezclada con Bing (2026-08-28)

Pedido del usuario: "backlinks para DataForSEO". El collector de backlinks ya
tenía diseño multi-fuente desde el principio (regla del proyecto: no atarse
a una sola API de pago) — DataForSEO se sumó como segunda fuente con el
mismo contrato de fila (`BacklinkRow`) que Bing Webmaster, en vez de crear
una tabla/pipeline paralela.

- `backend/collectors/backlinks.py`: nueva `_fetch_dataforseo_backlinks()`
  contra `/v3/backlinks/backlinks/live` (verificado en vivo contra
  jcreparaciones.com antes de escribir el parseo — mismo bug real de
  body-como-array-plano de esta sesión, no `{"tasks": [...]}`).
- `BacklinksCollector.collect()` corre Bing y DataForSEO en paralelo lógico
  (cada uno degrada solo si falla, igual que antes) — con solo una de las
  dos credenciales configuradas sigue funcionando con esa única fuente; solo
  se salta si NINGUNA está configurada.
- Cada fila queda etiquetada con su `source` real (columna "Fuente" ya
  existente en el dashboard) — sin tabla ni pestaña duplicada.
- **Hallazgo real de la primera corrida en vivo contra jc**: 39 backlinks
  totales (antes solo Bing daba visibilidad parcial), 8 de ellos (20.5%) con
  el anchor "High Quality Dofollow Backlinks DA 50 PA 40 Premium PBN Network
  Service... Buy Backlinks Online Cheap" — un caso de PBN/venta de enlaces
  obvio a simple vista que el detector heurístico actual (solo TLD) NO
  marca como tóxico todavía — limitación conocida y ya documentada en
  `toxic_backlinks.py` (Nivel 2, apagado por defecto), no un bug nuevo.
- Tests: 4 nuevos en `test_backlinks.py` (parseo real, item sin anchor
  —null en enlaces de imagen—, status_code de error de DataForSEO, y ambas
  fuentes combinadas) + 5 tests existentes corregidos para fijar
  `has_dataforseo=False` explícito (si no, el mock de `settings` sin ese
  atributo seteado es un MagicMock truthy y el collector intentaba llamar a
  DataForSEO en tests pensados solo para Bing). Suite: 738 → 742.
- Verificado en vivo contra jc real (botón "Actualizar backlinks" desde el
  dashboard): `39 backlinks · 2 tóxicos · fuentes: bing, dataforseo`.

### Pestaña 🛰️ DataForSEO dedicada — se movieron las 4 secciones a un solo lugar (2026-08-28)

Pedido directo del usuario tras la entrega anterior: las 4 secciones de
DataForSEO quedaron dispersas en 4 pestañas distintas (Técnico, GEO, Local,
Competidores) — quería una sola pestaña que agrupe todo lo que se consulta
gracias a DataForSEO.

- Nueva pestaña `🛰️ DataForSEO` con las 4 secciones movidas ahí (no
  duplicadas — se quitaron de sus ubicaciones anteriores): Crawler propio vs
  renderizado con JS, AI Overview real de Google, Geo-grid rank tracking,
  Gap real de keywords vs competidor.
- Banner explicativo arriba de la pestaña aclarando que TODO ahí sale de la
  API paga de DataForSEO y que ninguno se dispara solo en la auditoría
  automática (cuestan cupo real).
- **AI Visibility (Gemini/Claude/DeepSeek) se queda en 🤖 GEO** — no es
  DataForSEO, es una llamada directa a cada proveedor de IA. Mismo criterio
  para `security_headers`/`indexnow` (HTTP directo/IndexNow ping, no
  DataForSEO): siguen sin vista en el dashboard, fuera del alcance de este
  pedido.
- Cambio de solo HTML — los `id` de cada control no cambiaron, así que la
  lógica de `app.js` (loaders, botones, `loadAll()`) no necesitó tocarse.
  Verificado en vivo: la pestaña nueva muestra las 4 tablas con datos reales
  de jc, y confirmado por JS que ya no quedó rastro de esas secciones en las
  4 pestañas originales.

### Vista en el dashboard para los 4 collectors de DataForSEO que solo vivían en el reporte (2026-08-28)

Bug real reportado por el usuario ("los datos de DataForSEO no se visualizan"):
`competitor_keywords`, `geo_grid`, `js_rendering_check` y `ai_overview_check`
ya tenían datos reales guardados (verificado con jc: geo-grid con 2 keywords
al 22.2% de visibilidad, AI Overview con 1/2 keywords activando Overview
real, etc.), pero la ÚNICA forma de verlos era abrir el reporte HTML largo —
no existía ni un botón para dispararlos ni una tabla para leerlos desde el
dashboard mismo.

- **Técnico**: nueva sección "Crawler propio vs renderizado con JS" con
  input de URLs opcional + botón, justo debajo de la tabla del semáforo.
- **GEO**: nueva sección "AI Overview real de Google" con input de keywords
  + `location_code` + botón, debajo de AI Visibility.
- **Local**: nueva sección "Geo-grid rank tracking" con input de keywords +
  botón, debajo del Local Pack.
- **Competidores**: nueva sección "Gap real de keywords vs competidor
  (DataForSEO Labs)" con input de `location_code` + botón, debajo del
  Keyword Gap de texto existente — aclarado en el propio texto de la UI que
  es un dato distinto (ranking real vs comparación de título/H1).
- Los 4 se cargan automáticamente en `loadAll()` igual que el resto del
  dashboard, y cada uno tiene su propio botón de disparo manual porque los 4
  cuestan cupo real de DataForSEO por consulta (mismo criterio que
  rank_tracking/local_rank/serp_compare: nunca se disparan solos en la
  auditoría automática).
- Verificado en vivo contra `jcreparaciones.com` real (servidor levantado,
  las 4 tablas muestran datos reales, sin errores de consola).
- `security_headers`/`indexnow` quedan con el mismo problema (solo viven en
  el reporte) — no se tocaron en este cambio, el pedido fue específicamente
  sobre "datos de DataForSEO".

### Reporte HTML dividido en 34 secciones copiables por separado (2026-08-28)

Bug real reportado por el usuario: el reporte HTML había crecido a ~880KB de
texto (34 secciones — Técnico, Backlinks, Geo-grid, AI Overview, etc.) en un
solo bloque. El único botón para llevarlo a una IA era "Copiar todo", así que
cada consulta a Claude sobre el reporte quemaba cientos de miles de tokens
de golpe, y no había forma de ver de un vistazo qué secciones existían antes
de decidir cuál pegar.

- Cada `<h2>` ahora vive dentro de `<section id="...">` con su propio botón
  "📋 Copiar sección" — copia SOLO esa sección (con el título como primera
  línea, para que quien la pegue en una IA sepa de inmediato qué está viendo
  sin tener que pegar las otras 33). El botón temporalmente oculta su propio
  texto antes de leer `innerText` para no colarse en lo copiado (clonar el
  nodo no sirve: un clon desconectado del DOM no tiene layout, así que
  `innerText` devuelve vacío/inconsistente según el navegador).
- Un índice (`<details class="toc-box">`, abierto por defecto) justo debajo
  del banner de frescura lista las 34 secciones con enlaces `#ancla` — antes
  de esto no había ninguna vista de conjunto del reporte completo.
- El botón "Copiar todo" original se mantiene para cuando sí se necesita
  todo.
- Efecto secundario esperado: los tests que hacían `html.find("título de
  sección")` para ubicar una sección ahora encontraban primero la MISMA
  cadena de texto en el índice (que aparece antes en el documento). Se
  corrigieron buscando `<h2>{emoji} título` en vez del texto plano, que solo
  aparece una vez en el documento.
- Verificado en vivo generando el reporte real de `jcreparaciones.com`: las
  34 secciones y sus 34 anclas del índice coinciden exactamente, y
  `copySection()` sobre la sección de geo-grid devuelve el título primero y
  sin ruido del botón.
- Test suite: 738 → 738 (sin tests nuevos — se reescribieron 14 existentes
  para apuntar al `<h2>` real en vez del texto plano ahora ambiguo).

### SEO Score global con desglose por componente (2026-07-15)

- **`calculate_content_score()`** (nuevo en `backend/analyzers/opportunities.py`):
  promedio del `eeat_score` (0-100, ya calculado por página en el crawler)
  entre las páginas que lo tienen. Se persiste como score `kind="content"`,
  igual que `technical`/`geo`/`local`.
- **`calculate_seo_score()` expandido**: antes promediaba solo Técnico + GEO.
  Ahora combina Técnico, Contenido, GEO y Local — excluyendo del promedio
  cualquiera que no tenga datos todavía (regla P1: nunca se rellena con 0).
  (Nota 2026-07-18: originalmente incluía un quinto componente, "Autoridad"
  vía Domain Authority de Moz — removido junto con el resto de la
  integración Moz, ver § Remoción de Moz más abajo.)
- **`GET /api/dashboard/{slug}/scorecards`** ahora devuelve también
  `score_breakdown` (los 4 componentes), `content_score`, `local_score`
  y `seo_score_delta` (vs. la auditoría anterior, `None` en
  la primera medición — nunca "vs promedio del sector").
- **Panel visual "🎯 SEO Score global"** en el dashboard (debajo de los
  scorecards): número grande con badge de tendencia (↑/↓/→) y una barra por
  componente. Componentes sin datos se muestran como "sin datos", nunca como
  una barra vacía interpretable como 0.

### Comparador de auditorías (2026-07-15)

- **`GET /api/dashboard/{slug}/compare-audits`** (`from_date`/`to_date`
  opcionales, `YYYY-MM-DD`): diff real entre dos días de auditoría. Ancla los
  "días de auditoría" a las fechas donde se guardó un score `kind="seo"` —
  es el que escribe `run_opportunities_analysis` en cada auditoría completa.
  Sin al menos 2 fechas distintas, responde `available: false` con el motivo
  en vez de inventar una comparación (regla P1).
- Devuelve **`score_deltas`** (los 6 kinds de `SCORE_KINDS_LABELS`, cada uno
  con `from`/`to`/`delta` — `delta` es `None` si falta cualquiera de los dos
  extremos, nunca se calcula contra un 0 implícito), **`issues_resolved`**
  (status≠open con `resolved_at` dentro del rango — solo issues que el
  usuario marcó explícitamente, el sistema no auto-detecta "ya no aparece"),
  **`issues_new`** (por `detected_at`) y **`pages_new`** (por `first_seen`
  en `pages`; no existe forma de detectar páginas *desaparecidas* porque
  `pages` no está versionado por fecha — limitación real, no se finge lo
  contrario).
- `SCORE_KINDS_LABELS` se movió de `reports.py` a
  `backend/analyzers/opportunities.py` para que ambos (reporte HTML y este
  comparador) compartan la misma fuente sin import circular.
- **Modal "🕐 Comparar auditorías"** en el dashboard: scores con badge de
  color, lista de issues resueltas/nuevas con icono de severidad, páginas
  nuevas. Validado con datos reales de `jcreparaciones.com`
  (2026-07-11 → 2026-07-15): SEO Score bajó 74→69 (cambio de metodología al
  incorporar Contenido/Autoridad), GEO subió 90→100, 103 issues nuevas
  (principalmente canibalización recién detectada), 15 páginas nuevas.

### Core Web Vitals reales vía PageSpeed Insights (2026-07-15)

- **`backend/collectors/pagespeed.py`** (nuevo): la clave `PAGESPEED_API_KEY`
  llevaba semanas configurada sin ningún feature encima — era la deuda
  técnica más grande pendiente. Endpoint real verificado contra
  `jcreparaciones.com` y `wikipedia.org`: `GET /pagespeedonline/v5/
  runPagespeed` (**no** `runPagespeedInsights`, que da 404 real — el nombre
  intuitivo está mal).
- Distingue **lab data** (Lighthouse simulado, `lighthouseResult` — siempre
  disponible: Performance/Accessibility/Best Practices/SEO 0-100 + LCP/CLS/
  TBT/FCP/Speed Index) de **field data** (CrUX, usuarios reales,
  `loadingExperience.metrics`). Verificado real: `jcreparaciones.com` no
  tiene field data (`metrics: {}`, tráfico insuficiente para que Chrome
  reporte), `wikipedia.org` sí — `field_data_available` se calcula por si
  `metrics` viene con contenido, nunca por la sola presencia de la clave
  `loadingExperience` (que existe igual, vacía).
- Nueva tabla `pagespeed` (`project_id`, `date`, `strategy` — solo "mobile"
  por ahora, upsert idempotente por los 3). `GET /api/dashboard/{slug}/
  pagespeed` expone la última medición + histórico corto.
- Wireado en el collector manual (`POST /api/collect/pagespeed/{slug}`), el
  scheduler diario y el botón "▶ Ejecutar auditoría" (paso nuevo: "Midiendo
  Core Web Vitals…", ~20s porque Lighthouse corre de verdad en el servidor
  de Google).
- Nueva sección en la tab "🔧 Técnico": 4 scorecards de Lighthouse + 3
  tarjetas de CWV con semáforo de color (umbrales oficiales de Google: LCP
  ≤2.5s/≤4s, CLS ≤0.1/≤0.25, TBT ≤200ms/≤600ms) + la nota honesta de field
  data. Validado real: `jcreparaciones.com` → Performance 96-98,
  Accessibility 97, Best Practices 92, SEO 100, LCP ~2.6s (umbral "mejorable"
  por poco), CLS 0.026, TBT 0ms.
- **Bug real encontrado de paso**: `StaticFiles` no mandaba `Cache-Control`,
  así que el navegador podía cachear JS/CSS editados por horas (heurística
  de RFC 7234 sobre `Last-Modified`) — una pestaña abierta seguía corriendo
  código viejo sin ningún error visible, ni con recarga normal. Fix:
  `_NoCacheStaticFiles` en `backend/main.py` fuerza `Cache-Control: no-cache`
  (revalida por ETag, no evita el caché — sigue siendo rápido, solo nunca
  miente sobre si el archivo cambió).

### Indexación real vía Search Console URL Inspection API (2026-07-15)

- **`backend/collectors/indexation.py`** (nuevo): a diferencia del semáforo
  técnico (que solo *infiere* indexabilidad de robots meta/canonical/status
  code observados por nuestro propio crawler), esto le pregunta a Google
  directamente qué hizo con cada URL — reutiliza `_build_service()` de
  `gsc.py` (mismo service account, mismo scope `webmasters.readonly`; la
  URL Inspection API vive en el mismo cliente `searchconsole` v1). Cuota
  real verificada en docs oficiales: 600 QPM / 2000 QPD por sitio, de sobra
  para los 15-50 páginas de estos proyectos.
- Verificado en vivo contra `jcreparaciones.com` antes de escribir el
  collector: página real indexada → `verdict="PASS"`,
  `coverageState="Submitted and indexed"`; URL inventada → `verdict=
  "NEUTRAL"`, `coverageState="URL is unknown to Google"`. Se persisten
  ambos campos literales, nunca reinterpretados (regla P1).
- Nueva tabla `indexation_status` (upsert por `project_id`+`url` — estado
  actual, no histórico). `GET /api/dashboard/{slug}/indexation` expone
  resumen por verdict + detalle por URL.
- Wireado en el collector manual (`POST /api/collect/indexation/{slug}`,
  1 req/s propio, corre después del crawler porque depende de `pages`), el
  scheduler diario y el botón "▶ Ejecutar auditoría".
- Nueva sección "🔎 Indexación real (Google)" en la tab Técnico: scorecards
  por verdict + tabla con el texto real de Search Console por página.
- **Hallazgo real de la primera corrida contra `jcreparaciones.com`**: 13/15
  páginas indexadas, pero `/contacto` está "Crawled - currently not
  indexed" (Google la visitó y decidió no indexarla) y
  `/microsoldadura-cali` da "URL is unknown to Google" — Google nunca la ha
  visto. Información que Search Console tiene pero que hasta ahora
  requería revisarla a mano, página por página, en la interfaz web.

### Botones "Copiar todo" para pegar en una IA (2026-07-16)

- **Problema real reportado por el usuario**: el semáforo técnico
  (Título/Desc/H1/Schema/OG/Canonical/Index) se pinta con `::before` de CSS
  (🟢/🟡/🔴) — un pseudo-elemento, no texto real del DOM. Seleccionar y
  copiar la tabla con el mouse traía celdas completamente vacías;
  imposible pegarla en un chat de IA para pedir ayuda. Confirmado real con
  `get_page_text` del navegador: las celdas salían en blanco.
- Causa raíz más profunda: `TechnicalReport` (en `backend/analyzers/
  technical.py`) ya calculaba una razón legible por celda (ej. "Title de 71
  caracteres, fuera de rango aceptable") pero solo la guardaba para celdas
  no verdes, dentro de los issues — la información para construir un texto
  útil ni siquiera llegaba al frontend. Se agregó `row_detail: dict[str,
  str]` a `TechnicalReport`, con la razón de las 7 celdas de cada página
  (incluidas las verdes), expuesto ahora en `GET /api/dashboard/{slug}/
  technical`.
- **Tres botones "📋 Copiar"** nuevos, todos generan texto plano (no HTML,
  no colores) listo para pegar en un chat de IA:
  - Reporte HTML completo (`backend/reports.py`): botón junto a "Imprimir".
  - Modal Action Plan: botón junto a "Exportar CSV".
  - Tab Técnico: botón sobre la tabla de semáforo, con motivo real por
    celda (`[OK]`/`[REVISAR]`/`[FALTA]` + la razón).
- **Bug real encontrado al probarlo**: `navigator.clipboard.writeText()`
  puede fallar con `NotAllowedError` incluso en `localhost` según permisos
  del navegador — verificado real, no supuesto. Los tres botones usan un
  fallback de dos niveles: Clipboard API moderna primero,
  `document.execCommand('copy')` (textarea oculto) si falla.
- **Segundo bug real de caché encontrado de paso**: el endpoint del
  reporte (`GET /api/dashboard/{slug}/report`) es HTML generado
  dinámicamente en cada request, pero no mandaba `Cache-Control` — mismo
  problema de fondo que `_NoCacheStaticFiles` (heurística de RFC 7234), pero
  esta ruta no pasa por `StaticFiles`. Fix: `Cache-Control: no-store`
  explícito en la respuesta (no solo `no-cache`, porque no hay nada que
  valga la pena revalidar contra un ETag — cada request ya recalcula todo).

### Agregar un sitio nuevo (no solo los 4 sembrados)

Hasta acá la plataforma solo servía para los 4 proyectos sembrados en Fase 0.
Ahora hay dos formas de analizar un sitio nuevo:

1. **➕ Nuevo proyecto** (botón en el header): registra un sitio de forma
   permanente (URL, nombre, país, idioma, competidores). Queda guardado —
   puedes auditarlo, ver su histórico, chatear con la IA sobre él, igual que
   cualquier proyecto sembrado. El slug se genera automáticamente del
   dominio. Probado end-to-end: crear proyecto → "Ejecutar auditoría" →
   scores reales, sobre `example.com`.
2. **⚡ Análisis rápido de URL** (botón en el header): pega cualquier URL
   pública y obtén un reporte al instante (title/meta/H1/schema/GEO,
   Formato Mago) **sin guardar nada** — ni proyecto ni histórico. Pensado
   para revisar una página suelta sin comprometerte a monitorearla.

**Nota de seguridad de la Fase "Análisis rápido"**: como esta es la primera
vez que una URL llega directo del usuario en cada request (antes las URLs
solo venían de `projects` ya registrados), se agregó un guard SSRF
(`backend/analyzers/url_safety.py`) que resuelve el DNS y bloquea IPs
privadas/loopback/reservadas — incluido el endpoint de metadata de la nube
(`169.254.169.254`) — antes de hacer cualquier fetch. Probado con intentos
reales contra `127.0.0.1` y el endpoint de metadata: ambos bloqueados.

### Qué hace la Fase 4 — Reportes HTML/PDF + auth por token

- **Reporte** (botón "📄 Reporte" en el dashboard, o
  `GET /api/dashboard/{slug}/report`): documento HTML standalone con
  scorecards, Action Plan completo, resumen técnico/GEO/Local/Backlinks.
  Botón "Imprimir / Guardar como PDF" integrado — usa el motor de impresión
  nativo del navegador en vez de una librería de PDF nueva (regla P8: lista
  de dependencias cerrada). Reutiliza las mismas funciones ya probadas de
  `routes_dashboard.py`, no duplica queries.
- **Rediseñado (2026-07-14) tras revisar un reporte de otra herramienta que
  el usuario compartió como referencia** — ese reporte se veía pulido pero
  inventaba datos (score "vs promedio del sector" que no existe
  públicamente, conteo de reseñas de Google sin API conectada, "visibilidad
  en ChatGPT" sin haberlo consultado en vivo). El rediseño copia el nivel de
  pulido visual pero con la regla P1 aplicada estricta:
  - **Resumen ejecutivo redactado por IA** (DeepSeek) — pero SOLO narra
    hechos ya calculados (scores reales, issue crítica principal, keyword
    más cercana al top 10); nunca inventa una cifra nueva. Degrada a lista
    de hechos crudos sin narrar si DeepSeek no está configurado.
  - **Keywords principales y "oportunidades cerca del Top 10"** (posición
    11-20): 100% de Search Console real, no "posición estimada".
  - **Ideas de contenido con copy de IA**: toma oportunidades reales
    (keywords top-10 con 0 clics, related queries de Trends) y le pide a
    DeepSeek un título + meta description — con la regla explícita de no
    inventar precios/garantías/datos del negocio que no se le dan. Validado
    con datos reales: encontró que "data recovery" rankea #1.7 en
    jcreparaciones.com pero tiene 0 clics, y generó copy real para
    corregirlo.
  - **Delta de score "vs auditoría anterior"** (real, de la tabla `scores`)
    — nunca "vs promedio del sector".
  - **Sección explícita "Lo que NO medimos todavía"**: reseñas de Google,
    qué responde ChatGPT/Perplexity en vivo, posición real de competidores
    (Custom Search API cerrada para proyectos nuevos, confirmado contra la
    documentación oficial de Google) — declarado en vez de omitido en
    silencio o inventado.
- **Auth por Bearer token** (`backend/api/auth.py`): sin `AUTH_TOKEN`
  configurado, no cambia nada (uso local normal). Si cambias `HOST` para
  exponerlo en tu red, `config.py` ahora exige `AUTH_TOKEN` antes de
  arrancar (regla S8) y todas las rutas `/api/*` piden
  `Authorization: Bearer <token>` (comparación en tiempo constante). El
  HTML/JS estático no queda protegido por este token (limitación conocida:
  `StaticFiles` es una sub-app ASGI aparte) — es la barrera mínima razonable
  para no dejar la API abierta en LAN, no protección de nivel empresarial.

### Qué hace la Fase 4 — Alertas por Telegram

- **Solo excepciones, no un reporte diario de "todo bien"**: el scheduler
  diario (`backend/scheduler.py`) corre crawler + geo + opportunities + local
  para cada proyecto activo y, al final, revisa dos cosas vía
  `backend/alerts.py`: (1) ¿algún score bajó ≥10 puntos vs. el día anterior?
  (2) ¿se creó alguna issue **crítica nueva** en esta corrida? Si hay algo,
  envía un solo mensaje consolidado por Telegram; si no hay nada, no envía nada.
- **Degradación elegante**: sin `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` en
  `.env`, todo sigue funcionando — el scheduler corre igual, solo no manda
  mensajes. Nunca falla por falta de configuración.
- **Botón "🔔 Alertas" en el header**: muestra si Telegram está configurado y
  tiene un botón "Enviar mensaje de prueba" para verificar el setup sin
  esperar al scheduler diario.
- **Setup**: habla con `@BotFather` en Telegram para crear un bot y obtener el
  token; escríbele un mensaje a tu bot nuevo; abre
  `https://api.telegram.org/bot<TOKEN>/getUpdates` para obtener tu `chat_id`.
  Instrucciones completas en `.env.example`.

### Ideas de keywords (preguntas/búsquedas relacionadas de Google Trends)

- **`fetch_related_queries()` en `backend/collectors/trends.py`**: usa
  `pytrends.related_queries()` (top + rising) sobre tus keywords que ya
  rankean, mismo patrón de batching/rate-limit/fallback geográfico que el
  collector de Trends existente. Se guardan como filas nuevas en `keywords`
  (`source='trends_related'`) — no pisa nada de lo que ya había.
- **Nueva sección en la tab Keywords**: "💡 Ideas de keywords", separada de
  la tabla de lo que ya rankeas — son candidatas para contenido nuevo, no
  keywords que ya tengas ganadas.
- **Validado con datos reales el 2026-07-13** contra `jcreparaciones.com`:
  29 ideas guardadas a partir de 3 keywords semilla. Hallazgo honesto real:
  keywords muy específicas/locales (`"jc reparaciones"`, `"reparacion iphone
  cali"`) casi nunca tienen suficiente volumen para que Google calcule
  relacionadas — usa términos más amplios (`"reparacion de celulares"`,
  `"iphone"`) para obtener resultados. Y el resultado mezcla ruido genuino
  con hallazgos útiles (ej. seed `"phone repair"` trajo tanto `"how to repair
  zipper"` como `"phone repair near me"`) — es una herramienta de
  descubrimiento que necesita criterio humano, no una lista curada.

### Collector autónomo de Search Console (resuelve el hueco de datos manuales)

- **`backend/collectors/gsc.py`**: usa una service account (server-to-server,
  sin login interactivo) para traer rankings reales por su cuenta — hasta
  ahora `gsc_daily`/`gsc_queries` solo se llenaban una vez, a mano, vía
  `scripts/bootstrap_data.py`. Ventana móvil de 30 días (con 3 días de
  margen por la latencia real de Search Console), upsert idempotente —
  correrlo a diario mantiene los datos frescos sin duplicar filas.
- **Validado contra credenciales reales el 2026-07-12**: JC Reparaciones y
  Komaromi Print Service trajeron datos reales (31 días, 139 y 151 queries
  respectivamente — Komaromi pasó de tener **cero** datos de GSC a tener su
  dashboard completo). SoyFixio y SoyFixio Tech fallan con 403 real porque el
  email de la cuenta de servicio (`ver GOOGLE_APPLICATION_CREDENTIALS`)
  todavía no se agregó como usuario en esas dos propiedades de Search
  Console — error honesto y accionable, no un dato inventado.
  Setup completo en la sección "Traer más datos reales de Search Console" más abajo.
- Encadenado en el botón "▶ Ejecutar auditoría" y en el snapshot diario del
  scheduler (`backend/scheduler.py`), antes del análisis de oportunidades
  (canibalización/CTR-0 necesitan los datos de GSC frescos primero).

### Qué hace la Fase 4 — SEO Local (NAP + schema)

- **Sin nueva colección de red**: reutiliza el snapshot más reciente del
  crawler propio (`backend/analyzers/local_seo.py`) — el crawler ahora
  también extrae el JSON-LD `LocalBusiness` (`name`/`telephone`) de cada
  página, además de lo que ya extraía.
- **Consistencia NAP (teléfono)**: compara el teléfono declarado en schema
  (prioritario) contra los detectados por regex en el texto de cada página.
  Si aparece más de un número distinto en el sitio, es un issue real (Google
  penaliza NAP inconsistente en resultados locales).
- **Cobertura de schema LocalBusiness**: % de páginas crawleadas con ese
  schema presente.
- **Local Score** (0-100): penaliza teléfono inconsistente (-40), ausencia
  total de teléfono (-30), o ausencia de schema LocalBusiness (-30).
- **Validado contra `jcreparaciones.com` real**: Local Score 70/100 — el
  teléfono `316 333 3400` es consistente en 4 páginas, pero **ninguna página
  tiene schema LocalBusiness todavía** (hallazgo real, no inventado).
- **Fuera de alcance a propósito**: dirección (requiere geocoding real, un
  regex de direcciones colombianas da demasiados falsos positivos/negativos)
  y **citations** (Google Business Profile, Yelp, Facebook, directorios) —
  requieren una API externa no configurada en este entorno. El tab lo dice
  explícito en vez de fingir esos datos.

### Qué hace la Fase 4 — Backlinks (histórico: Moz + Bing Webmaster)

> **Nota 2026-07-18**: la parte de Moz descrita en esta sección se removió
> por completo — ver § Remoción de Moz más abajo. Se deja este relato
> histórico de cómo se construyó y validó originalmente, pero hoy Bing
> Webmaster es la única fuente activa.

- **Fuentes (histórico)**: Mozscape (`MOZ_ACCESS_ID` + `MOZ_SECRET_KEY`, ojo: la API
  legacy de Moz, no la v2/Link Explorer moderna — se detecta por el prefijo
  `mozscape-` del Access ID) y Bing Webmaster Tools API
  (`BING_WEBMASTER_API_KEY`). **Degradación elegante** (regla S3): sin
  ninguna de las dos, la tab Backlinks muestra "sin configurar" con
  instrucciones — nunca inventa datos (regla P1).
- **Validado contra credenciales reales el 2026-07-11** (no solo mockeado):
  Domain Authority 4 y Page Authority 20 reales de `jcreparaciones.com` vía
  Mozscape; Bing Webmaster confirmado funcional (backlinks reales cuando el
  sitio esté verificado en esa cuenta). Ambas integraciones fallaron con 400
  Bad Request en el primer intento (parámetros adivinados sin docs en mano)
  y se corrigieron con la documentación oficial real — no quedó nada sin probar.
- **División de responsabilidades real** (no la asumida originalmente): Moz
  (Mozscape) da Domain Authority / Page Authority de tu propio dominio — no
  una lista de backlinks (ese endpoint de Mozscape requiere bitflags de
  columnas sin documentación pública accesible ahora mismo). Bing Webmaster
  da la lista real de backlinks con anchor text de la home. Ver el docstring
  de `backend/collectors/backlinks.py` para el detalle completo de ambos fixes.
- **Distribución de anchor text** (`backend/analyzers/backlinks.py`): agrupa
  por anchor, calcula % del total, marca "sobre-optimizado" si un anchor pasa
  el 30% (señal clásica de riesgo de penalización tipo Penguin).
- **Detección de tóxicos**: usa una heurística de TLDs asociados a spam
  (`.xyz`, `.top`, `.win`, etc.) marcada explícitamente como heurística débil
  — Mozscape no expone `spam_score` (eso es de la Moz API v2 moderna) y Bing
  tampoco lo reporta.
- **Disavow.txt**: botón que genera el archivo en el formato oficial de
  Google Search Console (`domain:ejemplo.com`), deduplicado por dominio, con
  advertencia de revisar antes de subir.
- **Common Crawl queda fuera de esta versión a propósito**: no ofrece una
  consulta directa "qué dominios enlazan a X" sin descargar su dataset de
  grafo web (cientos de GB en S3) — fuera de alcance de costo/tiempo para una
  herramienta local. Documentado en el docstring de
  `backend/collectors/backlinks.py` en vez de fingir una integración que no
  puede funcionar a costo cero.

### Qué hace la Fase 3 (Keywords + Competidores)

- **Google Trends** (`backend/collectors/trends.py`): batches de 5 keywords,
  delay de 18s, geo primario `CO-VAC` con fallback a `CO` si el departamento
  no tiene volumen suficiente — pitfall real verificado: "reparar iphone"
  devuelve vacío en Valle del Cauca pero sí tiene datos a nivel país.
- **Escaneo de competidores** (`backend/collectors/competitor.py`): reutiliza
  el crawler propio contra un dominio YA REGISTRADO en `projects.competitors`
  (nunca una URL arbitraria). Calcula su score técnico y GEO.
- **Matriz competitiva + Keyword Gap** (`backend/analyzers/competitors.py`):
  compara tu sitio contra cada competidor. El gap se basa en el title/H1 real
  de sus páginas (no en su ranking, que no podemos ver) y lo dice explícito
  en cada resultado.
- **Clasificación de intent por IA**: botón en la tab Keywords que clasifica
  tus top keywords en informacional/comercial/transaccional/navegacional/local.

**Tres bugs reales de honestidad de datos que encontré y corregí validando
contra competidores reales** (no estaban en el plan — surgieron al correr el
collector contra sitios de verdad):
1. `capriservicios.com` estaba completamente inalcanzable (timeout de red) —
   el collector calculaba igual un GEO Score "optimista" a partir de un
   robots.txt vacío por defecto. Ahora reporta `None` con nota explícita.
2. `myphonedoctor.co` bloqueaba con 403 en robots.txt — un 403 se estaba
   interpretando igual que "sin restricciones" (score falso de 70). Ahora
   distingue "confirmado ausente" (404) de "no lo sabemos" (403/500/timeout).
3. Esa misma página de bloqueo devolvía una página de challenge de Cloudflare
   ("Checking your browser...") cuyo title/H1 se colaba como si fuera
   contenido real del competidor en el keyword gap. Ahora se excluye
   cualquier página con status HTTP distinto de 200 del análisis de contenido.

### Qué hace la Fase 1 (además de la Fase 0)

- **Canibalización de keywords**: detecta cuando varias páginas compiten por
  la misma query en GSC. Encontró 4 casos reales, incluido un `www` sin
  canonicalizar y dos URLs duplicadas con guion vs slash.
- **Zero-impression pruning + CTR-0 en top-10**: candidatas a mejorar meta o
  consolidar contenido.
- **GEO/AEO** (`backend/collectors/geo.py` + `backend/analyzers/geo.py`):
  descarga `llms.txt`/`llms-full.txt`/`robots.txt` reales, arma la matriz de
  AI crawlers (GPTBot, ClaudeBot, PerplexityBot, OAI-SearchBot, CCBot) y
  calcula un GEO Score 0-100. `jcreparaciones.com` sacó 90/100.
- **Contenido & E-E-A-T** (`backend/analyzers/content.py`): legibilidad
  Fernández-Huerta (adaptación española del Flesch Reading Ease), score
  E-E-A-T (autor, fecha, HTTPS, contacto, thin content) y content decay sobre
  el histórico de `gsc_daily` — si no hay al menos 60 días cargados, lo dice
  explícitamente en vez de opinar sin datos suficientes.
- **SEO Score combinado** en las scorecards: promedio de score técnico (%
  celdas verdes del semáforo) y GEO Score, mostrando "N/A" si falta alguno.
- **Export CSV** del Action Plan, con mitigación de CSV injection (un título
  crawleado que empiece con `=`, `+`, `-` o `@` se neutraliza antes de escribirse).
- Un solo botón **"Ejecutar auditoría"** encadena crawler → GEO → oportunidades.

### Qué hace la Fase 2 (IA con DeepSeek)

- **Chat con contexto real** (botón "🤖 Asistente IA"): la IA responde usando
  el SEO Score, GEO Score, top issues y top keywords reales del proyecto
  activo — nunca inventa métricas, y si un dato no está cargado lo dice
  explícitamente.
- **"Corregir con IA"** en issues de categoría `meta` dentro del Action Plan:
  genera 2 propuestas de meta description (120-160 chars, con CTA) sin
  inventar precios que no le diste.
- **Generador de schema JSON-LD** por página, en la tab Contenido: elige tipo
  (LocalBusiness/Service/FAQPage/Product/Organization) y genera el schema
  usando solo los datos reales de la página — omite campos que no tiene, no
  los inventa.
- **Costo estimado visible** en cada respuesta (tokens + USD aproximado) — es
  una estimación, no un cobro exacto; verifica precios reales en
  platform.deepseek.com.
- **Rate limit de 10 solicitudes de IA por minuto** por proyecto, para que un
  bug de frontend no te vacíe los créditos.
- **Scheduler diario opt-in** (`backend/scheduler.py`): nunca se activa solo.
  Actívalo con `POST /api/scheduler/start` (body opcional `{"hour": 6, "minute": 0}`).
- **Gráfico de evolución de Scores**: cada vez que corres una auditoría se
  guarda un punto histórico (SEO/GEO/Técnico). Con 2+ auditorías en días
  distintos, el gráfico aparece automáticamente arriba de las tabs.

**Para activar el asistente de IA**, agrega tu key de DeepSeek a `.env` (nunca
la pegues en el chat de Claude ni en ningún otro lugar fuera de este archivo
local):

```bash
# En .env, agrega:
DEEPSEEK_API_KEY=tu_key_real_aqui
```

Consigue una key en [platform.deepseek.com](https://platform.deepseek.com).
Reinicia el servidor después de agregarla. Sin key configurada, el chat y los
botones de IA muestran un error claro (503) en vez de romperse — el resto del
dashboard sigue funcionando normalmente.

**Verificación de seguridad (regla P3)**: la API key nunca se sirve al
navegador. Se probó explícitamente: ni en `index.html`, ni en ningún archivo
`.js`, ni en el body de ninguna respuesta `/api/*`, ni en los logs del
servidor — todas las llamadas a DeepSeek pasan por el backend.

## Setup en 5 minutos

```bash
# 1. Entorno virtual — usa Python 3.12 (3.14 aún no tiene wheels de pydantic-core)
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Variables de entorno
cp .env.example .env
# Fase 0 funciona sin llenar ninguna key. DEEPSEEK_API_KEY es para Fase 2.

# 3. Migraciones + seed de los 4 proyectos reales
python -m backend.db.migrations

# 4. (Opcional) recargar datos reales de GSC de ejemplo ya incluidos
python -m scripts.bootstrap_data scripts/gsc_bootstrap_jc.json

# 5. Levantar el servidor
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Abre `http://127.0.0.1:8000`. Selecciona "JC Reparaciones" en el header:
vas a ver clics/impresiones reales de los últimos 28 días (tab Rankings) y,
si ya corriste el crawler, el semáforo técnico real (tab Técnico).

## Ejecutar una auditoría técnica

Desde el dashboard: botón **"▶ Ejecutar auditoría"** en el header (crawlea el
sitio activo, 15 páginas por defecto, ~1 página/segundo).

Desde terminal, de forma aislada (regla S7 — todo collector es standalone):

```bash
python -m backend.collectors.crawler --site jc --max-pages 20
```

## Traer más datos reales de Search Console

La conexión a GSC vía MCP vive en la sesión de Claude, no en tu Python local.
El flujo hoy: pide a Claude que traiga datos de `get_gsc_performance` para el
rango de fechas que quieras, los guarda en un JSON (ver formato en
`scripts/gsc_bootstrap_jc.json`) y corre:

```bash
python -m scripts.bootstrap_data scripts/tu_archivo.json
```

Es idempotente: correrlo dos veces no duplica filas (upsert por fecha/query).

### Para que el collector de GSC sea autónomo (sin pasar por Claude)

1. Google Cloud Console → crear proyecto → habilitar "Google Search Console API".
2. Crear una Service Account → descargar su JSON de credenciales.
3. En Search Console, agregar el email de la service account como usuario
   (permiso "Restringido" alcanza para lectura) en cada propiedad que quieras auditar.
4. Guardar el JSON en `credentials/gsc-service-account.json` (ya está en `.gitignore`).
5. `backend/collectors/gsc.py` (Fase 2) lo usará automáticamente si existe
   (`settings.has_gsc_credentials`).

## Correr los tests

```bash
python -m pytest tests/ -v
```

Los tests corren contra una base de datos SQLite temporal aislada
(`tests/conftest.py`) — nunca tocan `data/seo.db`. 57 tests en Fase 0:
analyzers (semáforo 🔴🟡🟢 + bugs conocidos), collectors (parseo offline con
fixtures, sin llamadas a internet), API (caso feliz + 404 + 422) y resiliencia
de collectors (un fallo de red nunca tumba la app).

## Estructura

Ver §2 de `PROMPT_MAESTRO.md`. Resumen:

- `backend/` — FastAPI + SQLite (SQLAlchemy Core) + collectors + analyzers
- `frontend/` — HTML/CSS/JS vanilla, sin build step, sin CDN (gráfico SVG hecho a mano)
- `data/seo.db` — histórico completo (gitignored, es lo más valioso del proyecto)
- `scripts/` — bootstrap de datos GSC

## Notas de esta build

- **Sin Chart.js vendorizado todavía**: este entorno de construcción no tuvo
  salida a internet para descargarlo a `frontend/vendor/`. El gráfico de
  tendencia usa un SVG hecho a mano (`frontend/js/charts.js`) que cumple la
  regla P9 (cero CDN) sin depender de conectividad. Si más adelante quieres
  Chart.js real: descarga `chart.umd.min.js` a `frontend/vendor/`, impórtalo
  en `index.html`, y reemplaza `renderLineChart` por las llamadas a `Chart()`.
- **Python 3.14 no es compatible** con las versiones fijadas de pydantic
  (pydantic-core aún no tiene wheels para 3.14). Usa Python 3.12.

### sameAs faltante en LocalBusiness — y un hallazgo colateral inesperado (2026-07-26)

Del análisis de repos de mercado (`/SKILL/Seo-Promt-Master-main`, MIT): su overlay
de "negocio local" señala que `sameAs` (enlace a Google Business Profile,
Facebook, etc.) es "el equivalente local del trabajo de autoridad de entidad".
`schema_validation.py` ya lo pedía para `Organization` pero no para
`LocalBusiness`. Se agregó como recomendado.

**Bug real encontrado al implementar esto**: `validate_pages_schema()` calculaba
`missing_recommended` en `validate_schema_node()` pero lo **descartaba** en la
agregación — nunca llegaba a ningún lado (ni issue, ni dashboard). Agregar
`sameAs` a la tabla de reglas habría quedado inerte. Se agregó `recommended_groups`
como su propia lista, con `build_schema_issues()` emitiéndolas en severidad
`medium` (no bloquean el rich result, solo lo mejoran — no deben pesar como lo
requerido).

**Hallazgo colateral verificado en vivo, no buscado**: al confirmar el fix contra
jc, la issue mostró más campos faltantes de los esperados en 13 páginas. Investigando,
resultó que esas páginas tienen **dos bloques JSON-LD con el mismo `@id`**
(`https://jcreparaciones.com/#business`): uno completo (con `sameAs`, `image`,
etc. — el inyectado globalmente desde `layout.tsx`) y otro parcial duplicado.
Dos declaraciones con el mismo `@id` es ambiguo para Google sobre cuál es la
canónica. Es un hallazgo real del código de jcreparaciones.com, no de SEO-OS —
se documenta aquí porque lo destapó la verificación en vivo de este fix, pero
no se tocó ese código (fuera de alcance de este pase).

Suite: 564 tests.

### CTR real contra el baseline propio, y saber cuándo callarse (2026-07-26)

`backend/analyzers/ctr.py`. La petición era "CTR esperado vs real por posición".
La forma habitual de hacerlo es aplicar una curva de industria ("posición 1 =
28%"), pero eso es el promedio de OTROS sitios, otro país y otro tipo de SERP:
presentarlo como la referencia del sitio sería exactamente una estimación
disfrazada de hecho. El baseline sale de los propios datos de Search Console.

Al medir la curva real de jcreparaciones.com apareció algo que cambió el
diseño: la **mediana de CTR es 0.00% en TODOS los tramos** y hay **2 clics en
138 filas** keyword+página. Comparar cada keyword contra esa mediana no habría
marcado nada — no se puede estar por debajo de cero. Con esa muestra, cualquier
veredicto de "CTR bajo" sería ruido presentado como hallazgo.

Así que el analyzer hace tres cosas y ninguna es inventar un incumplimiento:

1. **Publica la curva del sitio** por tramo (1-3, 4-10, 11-20, 21+) como dato.
   Real de jc: 0.51% en 1-3 · 0.00% en 4-10 · 4.35% en 11-20 · 0.00% en 21+.
2. **Declara si la muestra alcanza.** Bajo 30 clics emite un aviso explícito de
   que no se puede comparar por keyword, en vez de opinar igual.
3. **Señala candidatas**, no culpables: keywords en primera página con muchas
   impresiones y cero clics (jc: 'data recovery' pos 1.6 con 59 impresiones,
   'phone repair' pos 1.0 con 42). Y si la muestra no alcanza, **la propia
   issue lo dice**: sin ese matiz, un lector — o una IA — leería "CERO clics"
   como un defecto probado del snippet.

Hay un test que falla si alguien agrega una tabla de CTR por industria al
módulo, para que la regla no se erosione con el tiempo.

Suite: 549 tests.

### Preguntas reales que la gente busca en Google (2026-07-26)

Nueva sección en la tab Keywords: `backend/collectors/question_ideas.py`.
Petición del usuario: capturar preguntas reales que la gente escribe en
Google sobre las keywords del sitio, para poder responderlas en el sitio y
capturar esas búsquedas — no inventar preguntas, usar las reales.

Se probaron 3 fuentes contra datos reales de jc antes de elegir una (regla
de este proyecto: verificar con datos reales antes de diseñar):
- Serper `peopleAlsoAsk`: confirmado vacío en es/CO (ya documentado en
  rank_tracking.py).
- Preguntas con forma real dentro de `gsc_queries`: solo 6 en total, 1-2
  impresiones cada una — muestra insuficiente.
- Google Autocomplete (`suggestqueries.google.com/complete/search`, público,
  sin API key) sembrado con la keyword real + un prefijo de pregunta ("por
  qué", "qué hacer si", etc.) sí da resultados reales y relevantes en es/CO.

**Bug real encontrado y arreglado**: para queries con tilde, Google responde
con `charset=ISO-8859-1` en el header, pero `httpx.Response.json()` ignora
ese header y siempre asume UTF-8 — revienta con `'utf-8' codec can't decode
byte 0xe9... invalid continuation byte'` en CUALQUIER pregunta con tilde (es
decir, casi todas: "por qué", "qué", "cómo", "cuánto", "cuándo", "dónde").
Se reprodujo con las 28 combinaciones reales (4 seeds × 7 prefijos) contra el
endpoint real de Google antes de arreglarlo. Fix: `_fetch_suggestions()`
decodifica con `response.encoding` (que sí lee bien el header) antes de
parsear el JSON, con fallback a latin-1 si ni eso alcanza.

**Hallazgo real de calidad, no un bug**: con la keyword "data recovery" (una
de las keywords reales con más impresiones de jc) el Autocomplete trae
preguntas sobre el software EaseUS/Stellar Data Recovery ("como instalar
easeus data recovery wizard"), no sobre el servicio de recuperación de datos
que ofrece jc — es un término ambiguo en inglés con dos intenciones de
búsqueda distintas y el filtro actual (marcador de pregunta + alguna palabra
de la keyword semilla) no puede distinguirlas. Se documenta como limitación
conocida en vez de intentar arreglarlo sin que el usuario lo haya pedido.

Disparo manual (no en la secuencia automática), mismo criterio que
rank_tracking/local_rank/serp_compare por el costo de red real ante Google.
Reutiliza la tabla `keywords` con `source='question_ideas'` — mismo patrón
que `trends_related`. `already_has_real_data` cruza contra `gsc_queries` de
forma honesta: solo dice que Google ya mostró impresiones para algo similar,
nunca que el sitio ya responde la pregunta.

Verificado en vivo contra jc: 60-79 preguntas reales guardadas con seeds
reales del negocio ("iphone no prende", "celular mojado", "bateria iphone",
etc.), coincidencias correctas con páginas existentes del sitio.

Suite: 575 tests.

### AI Visibility: qué responden Gemini/Claude/DeepSeek en vivo (2026-07-27)

Petición del usuario tras agregar sus API keys de Gemini y Claude en
Configuración: "probar que es o que muestra la ia de nosotros". La GEO
section ya medía si los BOTS de IA pueden CRAWLEAR el sitio (robots.txt/
llms.txt) — eso es acceso, no lo mismo que "qué responde el modelo si le
pregunto". Lo segundo requiere consultar la API real de cada proveedor y
mostrar la respuesta tal cual, sin resumirla ni calificarla.

`backend/collectors/ai_visibility.py`: hasta 1 prompt de marca ("¿qué sabes
de {negocio}?") + hasta 3 de categoría (las keywords reales de mayor
impresiones en GSC, convertidas en pregunta de recomendación — mismo
criterio de sembrar con datos reales que question_ideas.py/local_rank.py).
Un proveedor sin API key configurada se salta con gracia (S3); disparo
manual, no en la secuencia automática (costo real de pago).

**Bug real encontrado y arreglado ANTES de construir nada encima**: la
primera versión marcaba `mentions_business=True` para el prompt de MARCA
cada vez que el modelo respondía — pero el prompt de marca ya CONTIENE el
nombre del negocio, así que el modelo lo repite aunque diga literalmente
"no tengo información sobre este negocio". Verificado en vivo: DeepSeek
respondió "No tengo información específica sobre 'JC Reparaciones'..." y el
substring match lo marcaba como "SÍ te menciona" — exactamente lo opuesto
del hallazgo real. Fix: `mentions_business` ahora es `NULL` para prompts de
marca (no es una señal ahí) y solo se calcula para prompts de categoría,
donde el nombre no estaba en la pregunta y su aparición sí es un dato real.

Verificado en vivo con las API keys reales del usuario: Gemini devolvió
HTTP 429 (cuota de su cuenta agotada — key válida, no es un bug: un 401/403
habría sido key inválida) y DeepSeek respondió completo en las 4 preguntas
(1 marca + 3 categoría), con 0 menciones reales de la marca en las
respuestas de categoría — un hallazgo honesto, no un error del collector.

Suite: 587 tests.

### AI Visibility: prompts curados en español/Cali + fix del tipo "comparison" (2026-07-27)

Feedback real del usuario tras ver el primer resultado de AI Visibility: los
prompts de categoría se generaban mecánicamente desde las keywords de mayor
impresión en GSC, y eso trajo ruido genérico en inglés ("data recovery",
"phone repair") que no refleja cómo pregunta un cliente real en Cali — nada
que ver con marca/reputación local. El usuario también hizo una corrección
importante que quedó documentada: **consultar un LLM no lo entrena ni deja
rastro** — cada llamada es independiente. Esta herramienta sirve para
**monitorear una línea base** (correr hoy, volver a correr en 30-60 días) y
ver si el trabajo de backlinks/GBP/reseñas empieza a aparecer en modelos que
sí navegan en vivo (Perplexity, ChatGPT con búsqueda, AI Overviews) — no para
"enseñarle" nada a Gemini/Claude/DeepSeek con las preguntas mismas.

Cambios en `backend/collectors/ai_visibility.py`:
- `_build_prompts()` ahora prefiere `project.config["ai_visibility_prompts"]`
  (lista curada a mano, `[{"type", "text"}, ...]`) si existe — permite
  preguntas reales por proyecto (idioma, ciudad, competidores reales) sin
  tocar código. Sin esa config, sigue funcionando con el criterio mecánico
  anterior (keywords de GSC), para proyectos que aún no tienen nada curado.
- Nuevo tipo de prompt **`comparison`** ("¿tú o [competidor real], cuál es
  mejor para X?") — mide si el modelo te posiciona contra la competencia
  real, no solo si "te conoce" o "te recomienda" en abstracto.
- Se sembró `jc.config.ai_visibility_prompts` con 14 preguntas reales en
  español (3 marca, 9 categoría, 2 comparación — usando "Capri Servicios",
  un competidor ya registrado del proyecto, no uno inventado).

**Bug real encontrado y arreglado antes de que se acumulara en el reporte**:
tanto `reports.py` como el frontend (tab GEO) trataban cualquier prompt que
no fuera "brand" como "categoría" — así que un prompt de `comparison`
("¿JC Reparaciones o Capri Servicios...?") mostraba "— no te menciona" como
si fuera una señal real. Pero el nombre de marca YA está en la pregunta de
comparación, igual que en la de marca — el mismo falso positivo que se
corrigió para "brand" en la versión anterior, que se había colado de nuevo
por un tipo de prompt nuevo. Fix: `mentions_business` se trata igual
(sin señal, `None`) para `brand` Y `comparison`; solo `category` es una
señal real. Verificado en vivo contra jc con las 14 preguntas nuevas: 0
menciones reales en las 9 de categoría — línea base honesta para comparar
dentro de 30-60 días.

Suite: 593 tests.

### "Clics (28d)" sumaba la tabla completa, no 28 días (2026-07-27)

El usuario reportó que el dashboard decía "33 Clics (28d)" pero Search
Console mostraba otra cosa al comparar directamente. Confirmado real: bug
propio, introducido el mismo día. `get_scorecards()` sumaba
`func.sum(gsc_daily.c.clicks)` **sin ningún filtro de fecha** — antes pasaba
casi desapercibido porque el collector de GSC solo traía ~28-30 días, así
que `gsc_daily` nunca acumulaba mucho más que eso. Pero al agregar el
selector de período (7d-16 meses, mejora de hoy mismo) probé la ventana de
365 días contra jc para verificarla — eso dejó un año completo de filas
reales (108 filas, 2025-07-24 a 2026-07-24) permanentemente en `gsc_daily`,
y el "28d" pasó a mostrar la suma de todo ese año (33 clics) en vez de los
28 días reales (14 clics).

El mismo bug, con el mismo nombre de variable (`clicks_28d`) copiado sin el
filtro, existía también en `analyzers/context.py` — el contexto que arma el
asistente de IA para el chat, así que la IA también venía citando el número
inflado.

Fix: nuevo helper `gsc_daily_totals_last_n_days(conn, project_id, days=28)`
en `backend/db/database.py` — ancla la ventana al **último día real con
datos** (no a "hoy", por el lag de 2-3 días de Search Console) y filtra
correctamente. Reemplaza el cálculo inline en ambos sitios. Los otros dos
lugares que leen `gsc_daily` sin filtrar (`opportunities.py` para el
detector de caída de tráfico, que necesita ≥60 días de histórico a propósito;
`routes_dashboard.py` para el gráfico de evolución del tab Rankings, que debe
mostrar todo el histórico disponible) están bien como están — no son el
mismo bug, ahí "sin filtrar" es el comportamiento correcto.

4 tests nuevos en `tests/test_database.py`, incluyendo uno que reproduce
exactamente el escenario real (365 días de historial, solo deben sumarse los
últimos 28). Verificado en vivo contra jc: 33 → 14 clics, 2480 → 884
impresiones.

Suite: 597 tests.

### El reporte no traía 4 tabs completas del dashboard (2026-08-02)

El usuario preguntó, con toda razón: "¿el reporte trae TODO lo que veo en el
dashboard, o hay que revisar otras opciones aparte?" — la respuesta honesta
era que NO. Comparando las funciones que `reports.py` importa contra todas
las que existen en `routes_dashboard.py`, faltaban 4 secciones completas:
**GA4** (conversiones orgánicas), **Contenido/E-E-A-T** (legibilidad, fechas
de actualización por página), **Preguntas reales** (Google Autocomplete,
§ mejoras 2026-07-27) y **Competidores** (matriz + keyword gap). Ninguna se
conectó al reporte cuando se construyó cada feature — el mismo patrón de bug
ya documentado arriba ("El reporte medía cosas que nunca mostraba"), que
volvió a aparecer en features agregadas después de esa corrección.

Se agregaron las 4, siguiendo el mismo criterio del resto del reporte:
reutilizar las funciones ya probadas de `routes_dashboard.py` (una sola
fuente de verdad por sección), degradar con gracia si no hay datos
(`empty_reason`), y nunca reinterpretar el dato — el keyword gap deja
explícito que es "una señal de qué tema parece targetear el competidor
según su title/H1", no un ranking confirmado.

Verificado en vivo contra jc: las 4 secciones renderizan con datos reales
(75 preguntas reales, 126 páginas con legibilidad/E-E-A-T, matriz de 5
competidores con notas honestas sobre los que no se pudieron escanear) — y
GA4 muestra correctamente "sin configurar" en vez de una tabla vacía, porque
ese proyecto no tiene `GA4_PROPERTY_ID` configurado.

Suite: 598 tests.

### 4 capacidades nuevas revisando el plugin claude-seo-ai (2026-08-02)

El usuario instaló el plugin de Claude Code `claude-seo-ai` (invocado como
`/seo audit <url>`) y pidió revisarlo para mejorar SEO-OS. En vez de solo leer
las descripciones de sus ~20 módulos, se leyó el código fuente real (skills,
agentes, y sobre todo los scripts `.mjs` con la lógica determinística) para
encontrar algo genuinamente portable, no solo inspiración. Se encontraron y
portaron 4 cosas, cada una verificada en vivo contra jcreparaciones.com antes
de confiar en ella — regla de siempre en este proyecto, aplicada también a
código que no se escribió originalmente aquí.

**1. Bug real, no mejora — FAQPage/HowTo ya no dan rich result.** Google
eliminó el rich result de FAQPage y HowTo en agosto 2023. `schema_validation.py`
seguía diciendo "sin `mainEntity` Google NO muestra el rich snippet" — una
afirmación falsa desde 2023, sin importar qué tan completo esté el schema.
Verificado contra jc: 93 páginas reales con FAQPage. Fix: nuevo
`DEPRECATED_RICH_RESULT_TYPES`, severidad bajada de `high` a `medium`, mensaje
corregido (sigue siendo útil para que una IA extraiga la respuesta, pero no
hay snippet de Google que desbloquear).

**2. Extraíbilidad de respuestas por IA** (adaptado del módulo M11). Detecta
encabezados en forma de pregunta y si el párrafo siguiente da una respuesta
directa (no un "en este artículo veremos..." de relleno) en la banda ideal de
40-60 palabras — eso es lo que un motor de IA puede citar sin leer todo el
artículo. El original usa regex en inglés ("how/what/why..."); se adaptó a
español desde cero, no es una traducción literal. **Bug real encontrado en la
propia adaptación**, antes de confiar en ella: "¿Cómo saber si la batería del
iPhone está degradada?" es una pregunta real (termina en "?") pero "saber" es
infinitivo, así que la exclusión de título-guía ("Cómo cambiar la batería" =
guía, no pregunta) la descartaba antes de mirar el signo de pregunta. Fix: el
"?" final siempre gana. Verificado en vivo contra
`jcreparaciones.com/cuanto-dura-bateria-iphone`: 10 preguntas reales
detectadas, respuestas extraídas correctamente (ej. "¿Cuánto cuesta cambiar
la batería?" → "$150.000-$220.000... toma 1 hora..." — respuesta real, no
inventada). También detectó un hueco real en `celular-mojado-cali`: "QUÉ
HACER CUANDO TU CELULAR SE MOJA" sin ningún párrafo después.

**3. Densidad de datos/hechos** (adaptado del módulo M12). Cuenta párrafos
largos sin ningún número (afirmaciones vagas), frases de autoridad sin fuente
("el mejor", "los expertos dicen"), señal de datos propios ("en nuestra
encuesta encontramos que"), y enlaces salientes a fuentes de autoridad real
(.gov/.edu/wikipedia). Deliberadamente conservador: de las 5 métricas, solo
"afirmación vaga sin fuente" genera una issue — las demás son dato, no
defecto (un párrafo sin números es normal en la mayoría del contenido real).
Verificado en vivo: jc no usa lenguaje superlativo vago (0 en todas las
páginas, confirmado que el regex sí funciona con casos sintéticos), y sí tiene
una señal real de datos propios en `/blog` ("Analizamos si conviene comprarla
o esperar").

**4. Frescura real: schema vs header HTTP** (adaptado del módulo M13). Cruza
`dateModified` del schema.org contra el header HTTP `Last-Modified` real del
servidor — detecta cuando el schema dice "actualizado hace poco" pero el
servidor no cambió nada en meses. Requirió una excepción deliberada y
documentada a la regla de `schema_validation.py` de "solo nombres de campo,
nunca valores": para cruzar fechas hace falta el valor real. **Limitación real
descubierta verificando esto contra jc**: su sitio (Next.js/Vercel) no manda
header `Last-Modified` en absoluto — común en sitios modernos con SSR/edge
cache. Sin ambas señales no hay nada que cruzar, así que en jc esto
correctamente no genera ninguna issue (verificado que la extracción de fechas
del schema SÍ funciona con datos reales en páginas de blog: `datePublished`/
`dateModified` = "2026-06-02" extraídos correctamente). El camino de detectar
un mismatch real se probó con datos sintéticos en los tests, no fue posible
contra jc por esta limitación real de su stack.

Los 4 hallazgos (y otros que no se implementaron todavía: score dual
Search-SEO/AI-Visibility sin mezclar, severity gating con tope duro a F,
detección de sitios JS-only sin renderizar) están documentados para
retomarlos si se decide seguir por ahí.

Suite: 611 tests.

### El dato crudo de M11/M12/M13 tampoco tenía sección propia en el reporte (2026-08-02)

El usuario preguntó, tras portar las 4 mejoras de arriba: "¿ya registra lo
nuevo el reporte?". Verificado: parcial. Las ISSUES de extraíbilidad/densidad
de datos SÍ salían en el Action Plan (cuando hay un problema real), pero el
dato crudo por página (cuántas preguntas tiene cada una, densidad numérica,
frescura) no tenía su propia sección — a diferencia de Contenido/E-E-A-T, que
sí muestra el número aunque no sea un problema.

Se agregó `🧠 Extraíbilidad por IA y densidad de datos` al reporte y a
`GET /api/dashboard/{slug}/content` (misma fuente para dashboard y reporte,
mismo criterio de siempre). Muestra agregados del sitio (preguntas totales
sin respuesta, afirmaciones vagas totales) y una tabla por página. La columna
de frescura compara schema vs header HTTP solo cuando AMBOS existen — el
reporte lo explica explícitamente para que "—" en la mayoría de las filas de
jc no se lea como un defecto (su stack Next.js/Vercel no manda ese header).

Suite: 612 tests.

### AI Visibility: de texto crudo a issue accionable en el Action Plan (2026-08-23)

El usuario pidió "una herramienta que permita medir cómo nos está viendo la
IA para alimentarla mejor" — ya existía (AI Visibility, tab GEO), así que la
pregunta real era otra: hoy solo muestra la respuesta cruda y si te menciona,
sin convertir eso en una recomendación como sí hace el resto del software.

Límite honesto que hay que respetar aquí (P1): consultar un LLM no lo
entrena ni deja rastro — cada llamada es independiente. Así que la issue
nueva **nunca** afirma que una acción específica "hará que la IA te
mencione" (esa relación causal no se puede sostener). Lo que sí es honesto:
señalar el hecho observado (0 menciones reales en las preguntas de
categoría — solo esas cuentan, brand/comparison no son señal, ver bug de
2026-07-27) y apuntar a las señales que SÍ están bajo control y que este
mismo software ya mide: backlinks, NAP/GBP, extraíbilidad de contenido
(M11/M12). Nunca promete que se moverá la aguja, solo que es lo único
accionable que existe.

`backend/analyzers/ai_visibility.py` (nuevo) + `record_issue`/
`reconcile_project_issues` en el collector — mismo patrón que usa
`opportunities.py` para issues a nivel proyecto (no por página). Idempotente:
correr dos veces no duplica, y si una corrida posterior sí trae una mención
real, la issue vieja se cierra sola.

**Bug real encontrado en la verificación en vivo, no relacionado con lo
anterior**: Gemini empezó a devolver 404 — `gemini-2.0-flash` fue retirado
por Google (su propia API lo dice: "no longer available... use
models/gemini-3.6-flash"). Corregido. Verificado de nuevo en vivo: Gemini ya
responde — y reveló un hallazgo real por su cuenta: confunde a "JC
Reparaciones" con OTRO negocio del mismo nombre genérico (describe
reparación de electrodomésticos de línea blanca, no de celulares/
computadores) — una alucinación real, documentada aquí, no perseguida
todavía (el prompt de marca no genera issue, por diseño — ver arriba).

Suite: 617 tests.

### TF-IDF: qué términos comparte el top-10 real y nos faltan (2026-08-25)

Petición del usuario tras revisar el listado de funciones de Seobility (un
competidor comercial): tienen una "herramienta TF*IDF" que SEO-OS no tenía.
Se implementó extendiendo `serp_compare.py` (ya fetch-eaba el top-10 real +
nuestra página) en vez de duplicar el trabajo de red — el mismo dato ya
disponible, un análisis nuevo encima.

TF-IDF real (no una aproximación): document frequency + term frequency sobre
el texto completo de cada página del top-10 real, comparado contra el
nuestro. Se excluye la keyword semilla (obvio que debe aparecer) y cualquier
término que solo aparezca en UNA página del top-10 (ruido de esa página
sola, no señal compartida). Sin dependencia nueva — stopwords en español
hardcodeadas, todo en Python estándar.

**Honestidad (P1), el límite más importante de este módulo**: esto es
vocabulario COMPARTIDO entre páginas que ya rankean — una correlación de
cobertura temática, nunca una causa. Google no puntúa coincidencia de
TF-IDF; usar estos términos puede reflejar un tema más completo, no
garantiza nada. El disclaimer lo dice explícito en la UI.

**Bug real encontrado verificando en vivo contra jc, arreglado antes de
confiar en el resultado**: sin quitar `<nav>/<header>/<footer>/<aside>`
antes de extraer el texto, el "gap" salía lleno de boilerplate de plantilla
("síguenos", "contáctanos", horario) que 2 de 3 páginas comparten por usar
un template parecido — no por cobertura temática real. Se corrigió quitando
esos tags antes de extraer texto (mismo criterio que M11/M12). Límite
honesto que SIGUE sin resolver, documentado en el código: sitios que ponen
horario/dirección fuera de esas etiquetas semánticas (widgets sin HTML5
correcto) todavía pueden colar alguna palabra suelta — resolverlo del todo
necesitaría un extractor de "contenido principal" (tipo Mozilla Readability),
fuera de alcance de este pase.

Verificado en vivo contra las 2 keywords reales de jc con top-10 guardado:
para "servicio tecnico apple cali" surgieron términos genuinos como
"autorizado", "reemplazo", "dispositivo", "cita" — vocabulario real de
servicio técnico Apple que jc no usa todavía.

Suite: 623 tests.

### Share of Voice sobre el propio baseline de CTR (2026-08-25)

Petición del usuario tras revisar el pricing de Semrush (un competidor
comercial): tienen "Share of Voice" y SEO-OS no. Se implementó reutilizando
el CTR-por-tramo real del sitio que ya calculaba `ctr.py` (§ mejoras
2026-07-26) — misma regla del proyecto de no inventar una curva de CTR de
industria, solo que aplicada a un metric nuevo.

Qué mide `calculate_share_of_voice()` (`backend/analyzers/ctr.py`): qué % de
la visibilidad máxima REALISTA del sitio se captura hoy, donde "máxima
realista" es el escenario de que TODAS las keywords rankearan en el tramo
donde el sitio de hecho tiene su mejor CTR observado — nunca un 100%
inventado ni una curva ajena. Reutiliza el mismo gate de fiabilidad de
`analyze_ctr()` (mínimo 30 clics): si la muestra no alcanza, se declara
`available: false` con el motivo, igual que la sección de CTR de al lado.

Se calcula en `run_opportunities_analysis()` sobre los mismos `query_rows`
de GSC ya cargados (sin fetch nuevo), se guarda en el mismo snapshot
`opportunities` que el CTR, expuesto en `/api/dashboard/{slug}/share-of-voice`
y con sección propia en el reporte. Verificado en vivo contra jc: con solo 3
clics reales en la ventana, correctamente declara "no alcanza" en vez de
mostrar un número sin sustento.

Suite: 629 tests.

### Auditoría "¿queda algo por fuera del reporte?": 4 huecos más (2026-08-25)

Pedido explícito del usuario: revisar TODO lo que el sistema analiza (los 20
`collector` distintos que escriben en `snapshots`, más los ~30 endpoints de
`routes_dashboard.py`) contra lo que el reporte HTML realmente muestra, igual
que la auditoría de GA4/Contenido/Preguntas/Competidores del 2026-07-25 y la
de M11/M12 del 2026-08-02 — mismo patrón de bug, buscado de forma sistemática
esta vez en vez de uno a uno según se fuera notando.

Se recorrieron los 20 nombres de `collector` y los ~30 `@router.get` de
`routes_dashboard.py` verificando cuáles llegaban al HTML. Cuatro huecos
reales, los 4 verificados en vivo contra jc antes de darlos por corregidos:

1. **Headers de seguridad HTTP** (`security_headers.py`): corría en cada
   auditoría, pero solo generaba una issue cuando algo faltaba — con TODO
   bien configurado (caso real de jc: HSTS con preload, CSP, Permissions-Policy,
   etc.) el reporte no decía absolutamente nada al respecto. Nuevo endpoint
   `/api/dashboard/{slug}/security-headers` + sección propia con el estado de
   cada header (✅/❌), no solo los problemas.
2. **IndexNow**: el chequeo (¿está publicado el key file?) corre en cada
   auditoría automáticamente, pero no tenía NINGUNA exposición — ni cuando
   está mal configurado. Nuevo endpoint `/api/dashboard/{slug}/indexnow` +
   sección con los pasos exactos para activarlo si no lo está (jc no lo tiene
   configurado, verificado en vivo).
3. **Comparación detallada de competidor** (`schema_gap` + E-E-A-T/contenido
   lado a lado): `build_competitor_comparison()` ya calculaba esto, pero solo
   se exponía bajo demanda vía `/competitors/{domain}/detail` — la matriz del
   reporte nunca mostraba el detalle. Verificado en vivo contra jc:
   ifixit.com usa `NewsArticle` y jc no, dato real que estaba invisible.
   Ahora el bloque de Competidores incluye esta comparación para cada
   competidor escaneado.
4. **Técnico por tipo de check**: el resumen sumaba los 8 checks del semáforo
   (title, meta, h1, schema, og, canonical, indexable, x_robots) en un solo
   conteo aplanado — "🟢 856 · 🟡 154 · 🔴 22" no decía si el problema estaba
   concentrado en un check o disperso. Verificado en vivo contra jc: 100 de
   129 páginas en amarillo por meta description y 20 en rojo por
   indexabilidad — informacion que el conteo aplanado escondía por completo.
   Ahora hay una tabla por check.

Suite: 633 tests.

### M11 detecta respuestas que son solo CTA de marketing (2026-08-26)

Salió de una auditoría manual del usuario contra jcreparaciones.com (fuera de
SEO-OS, con un prompt de 8 categorías de problemas que Search Console/Semrush
no detectan directamente). Hallazgo real: `/por-que-no-carga-mi-celular-cali`
tiene el H1 "¿por qué no carga mi celular?" y el párrafo siguiente tiene
texto real (no cae en `question_headings_without_answer`, que exige párrafo
vacío o frase de relleno tipo "en este artículo veremos...") pero solo
repite síntomas y empuja WhatsApp — nunca nombra una causa. Una IA no tiene
nada citable ahí, y M11 no lo veía.

Se agregó `question_headings_answered_with_cta_only` a
`_analyze_answer_extractability()` (`backend/collectors/crawler.py`): cuenta
encabezados-pregunta cuya respuesta contiene una frase de llamado a la acción
(cotización, escríbenos, whatsapp, agenda tu cita, etc.) SIN ningún número
(precio, tiempo, cifra) al lado — el gate del número es lo que evita marcar
como problema un párrafo que sí cotiza o da un dato concreto junto al CTA.
Contador separado de `without_answer` (mismo patrón que M11/M12 siempre): el
fix es distinto — no es quitar el CTA, es agregar el dato que falta.

Verificado en vivo contra jc: `/por-que-no-carga-mi-celular-cali` →
`question_headings_answered_with_cta_only: 1` (el hallazgo real de la
auditoría manual), `/celular-mojado-cali` (que sí usa un bloque "Respuesta
corta:") → 1 de 11 preguntas, proporción mucho más sana.

Suite: 635 tests.

### Contenido casi-duplicado entre páginas de plantilla (2026-08-26)

Segunda categoría portada de la auditoría manual del usuario contra
jcreparaciones.com. Hallazgo real: `lib/cities.ts` tenía un campo
`localContext` escrito a mano específicamente "para diferenciar contenido
entre ciudades... evita que Google marque las páginas como duplicadas", pero
`lib/renderBrandPage.tsx` nunca lo usaba — 31 ciudades × varias marcas
renderizando el mismo texto con solo el nombre de la ciudad cambiando.
Medido en ese momento con Python fuera de SEO-OS: 93.5% de similitud cruda
entre `/reparacion-iphone/palmira` y `/reparacion-iphone/tulua`.

`backend/analyzers/duplicate_content.py` (nuevo): agrupa páginas por
"plantilla" sin conocer de antemano cuál segmento de la URL es la variable —
genera todas las firmas posibles reemplazando UN segmento a la vez por `*`
(`/reparacion-iphone/palmira` → `/reparacion-iphone/*` y `/*/palmira`); dos
URLs que comparten una firma comparten plantilla. Con 3+ páginas en el mismo
grupo, compara `body_text_sample` (ya guardado en el snapshot del crawler,
sin fetch nuevo) por pares con `difflib.SequenceMatcher` — si el promedio
supera 85%, genera UNA issue por grupo (no por página, es un problema de la
plantilla entera), citando el par MÁS distinto de la muestra como evidencia
(no el más parecido, para no exagerar). Corre dentro de
`run_opportunities_analysis()`, mismo snapshot que CTR/Share of Voice.

Verificado en vivo contra el crawl real de jc (snapshot del 2026-08-21, 100
páginas): detectó exactamente los mismos 2 grupos encontrados a mano —
`/reparacion-iphone/*` (12 páginas, 93.3% de similitud promedio) y
`/reparacion-samsung/*` (11 páginas, 95.5%) — y los creó como issues "high"
reales en el Action Plan de jc.

Suite: 642 tests.

### Reglas de negocio prohibidas por proyecto (2026-08-26)

Tercera y última categoría portada de la auditoría manual contra
jcreparaciones.com. Hallazgo real: CLAUDE.md de ese proyecto prohíbe "nunca
escribir 'diagnóstico gratis'... ninguna variación en ningún archivo", y una
FAQ en `/reparacion-iphone-cali` la mencionaba (uso contrastivo, pero la
regla es literal y sin excepción). Ya corregido en el sitio real por el
usuario tras la auditoría manual.

Por qué es POR PROYECTO y no una lista genérica: "diagnóstico gratis" solo
está prohibido en JC Reparaciones porque cobran la revisión — otro proyecto
de SEO-OS puede ofrecerlo de verdad. La lista vive en
`project.config["banned_phrases"]`, mismo patrón ya establecido para
`ai_visibility_prompts` (`backend/collectors/ai_visibility.py`): configurado
a mano por el dueño del proyecto directamente en la columna `config`, sin
UI nueva — igual que ese otro campo, que tampoco la tiene.

`backend/analyzers/business_rules.py` (nuevo): `build_banned_phrase_issues()`
genera una issue "high" por cada frase encontrada. La detección ocurre en
`_extract_page_data()` (`backend/collectors/crawler.py`) sobre el texto
COMPLETO de la página (antes de recortar a los 5000 caracteres de
`body_text_sample` que usa el resto de analyzers) — una frase prohibida
puede estar en cualquier parte, no solo al principio. Case-insensitive.
Explícitamente NO corre sobre sitios de competidores (no tendría sentido
exigirles las reglas de negocio de otra empresa).

Alcance honesto: esto solo cubre reglas de tipo "frase literal prohibida"
(como "diagnóstico gratis"). La otra regla real de CLAUDE.md que apareció en
la misma auditoría — "nunca atribuir 'Sin servicio' a la antena, es siempre
falla de placa" — es una regla de ATRIBUCIÓN CAUSAL, no de presencia de una
palabra (mencionar "antena" como una causa técnica real, ej. en una página de
reballing, es legítimo) — necesitaría un chequeo distinto, no se intentó
forzar aquí.

Suite: 646 tests.

### Backlinks tóxicos por patrón, no por score de terceros (2026-08-26)

Cuarta categoría portada de la auditoría manual (spec detallada del usuario,
mismo patrón que `duplicate_content.py` y `business_rules.py`): clasificar
backlinks sin depender de un score pagado (Semrush Toxic Score, Ahrefs) ni
de un LLM juzgando — reglas determinísticas (regex, listas, comparación de
dominio), rápidas y gratis de correr a escala.

`backend/analyzers/toxic_backlinks.py` (nuevo): 5 señales Nivel 1 sin red
(anchor tóxico, TLD tóxico, patrón de ruta de granja de enlaces, plataforma
gratuita, directorio genérico) que clasifican cada backlink en
toxic/review/clean. El **whitelist del proyecto gana siempre** — sin él,
cualquier directorio local legítimo con baja autoridad terminaría marcado
igual que un PBN. Un backlink **nofollow** con señales tóxicas baja a
`review`, no `toxic`: no pasa autoridad, así que el riesgo real es menor
aunque el dominio parezca spam. Nivel 2 opcional (`apply_domain_content_check`,
apagado por defecto): descarga la home del dominio y busca las mismas
frases en su propio contenido, cacheado por dominio — nunca corre en el
pipeline estándar, es un paso separado bajo demanda.

Mismo gate de fiabilidad que `calculate_share_of_voice()`: con menos de 10
backlinks, `available: False` con la razón, nunca un `%` inventado sobre
una muestra chica. Reutiliza `generate_disavow_file()` ya existente para el
formato oficial de Google (dedup por dominio). Nuevo endpoint
`/api/dashboard/{slug}/toxic-backlinks`. Verificado: jc no tiene backlinks
cargados hoy (el collector corrió pero Bing no reportó ninguno) — el
endpoint declara `available: False` correctamente en vez de fabricar un
resultado; probado también con datos sintéticos realistas end-to-end.

Suite: 661 tests.

### Backlinks tóxicos: faltaba la sección en el reporte (2026-08-26)

Mismo bug de siempre, esta vez detectado por el usuario apenas terminó la
implementación de arriba: `toxic_backlinks_report()` tenía endpoint propio
pero no llegaba al reporte estático. Se agregó sección "☠️ Backlinks
tóxicos por patrón" junto a la de Backlinks existente — mismo criterio
honesto que Share of Voice (declara `available: false` con la muestra
insuficiente en vez de inventar un %), tabla con dominio/clasificación/
señales/acción recomendada para los backlinks marcados (tope de 20 filas).

Suite: 663 tests.

### AI Visibility comparativo: cuándo la IA menciona al competidor y no a ti (2026-08-26)

Pedido explícito del usuario tras preguntar cómo analizar mejor a la
competencia. AI Visibility ya medía SI la IA te menciona; nunca comparaba
contra los competidores registrados en las MISMAS preguntas.

`competitor_mentions` (nueva columna JSON en `ai_visibility_checks`): qué
dominios de `project.competitors` aparecen en la respuesta. Deliberadamente
**solo se calcula en prompts de categoría** (ej. "¿cuál es el mejor taller
en Cali?"), nunca en marca ni comparación — ahí el nombre (propio o del
competidor) ya estaba en la pregunta, así que contarlo repetiría lo que ya
sabíamos, no sería una señal nueva (mismo criterio que `mentions_business`,
establecido el 2026-07-27). El match de dominio a nombre "hablado" es una
heurística aproximada por diseño (`_competitor_tokens`: dominio completo +
raíz sin TLD + raíz con guiones como espacios) — declarada así en el código,
no hay forma honesta de adivinar el nombre comercial exacto solo del
dominio.

`build_ai_visibility_comparison()` (`backend/analyzers/ai_visibility.py`):
sobre las preguntas de categoría, cuántas veces te mencionan a ti vs a cada
competidor. `build_ai_visibility_competitor_issues()`: issue nueva SOLO
cuando un competidor aparece en más respuestas que tú — un hecho
comparativo con el conteo exacto, nunca una afirmación causal de qué lo
explica (mismo límite honesto de `build_ai_visibility_issues` original:
consultar un LLM no lo entrena, no hay acción que garantice más menciones).

Sección nueva en el reporte, junto a AI Visibility. Verificado en vivo
contra los 18 checks reales de categoría ya guardados de jc: 0 menciones
propias, ningún competidor registrado apareció tampoco (honesto — esos
checks son anteriores a esta función, no tienen el dato retroactivo).

Suite: 671 tests.

### Páginas 404 dejan de analizarse como si fueran contenido real (2026-08-28)

Encontrado por el usuario en una sesión separada, auditando manualmente un
reporte de SEO-OS contra jcreparaciones.com: 2 URLs muertas
(`/cambio-bateria-macbook-cali`, `/reparacion-iphone`, sin ningún link
entrante real desde hace tiempo) devolvían el HTML de la página de error
COMPARTIDA de Next.js — y el crawler la analizaba como si fuera contenido
real de CADA URL rota, generando issues falsas de meta description corta,
noindex, doble sufijo de marca, legibilidad 25.6, contenido delgado y sin
fecha — todas describiendo el MISMO template genérico repetido, no un
problema real. 12 issues falsas confirmadas abiertas en jc con este patrón.

Causa raíz: `crawl_site()`/`_extract_page_data()` nunca miraban
`response.status_code` — cualquier HTML que httpx trajera (200 o 404) se
analizaba igual. `TechnicalCrawler.persist_analysis()` ahora comprueba
`status_code >= 400` antes de correr el análisis técnico/de contenido: para
una página rota, NO genera `report.issues` ni `content_issues` (readability,
E-E-A-T, M11, M12, frescura, frases prohibidas), y guarda `title`/
`meta_description`/`h1`/`word_count`/`readability_score` como `None` en vez
del texto de la plantilla de error — evita contaminar promedios y agregados
del reporte con copias del mismo template. El hallazgo de "esta URL da 404"
en sí ya se reportaba correctamente, una sola vez y agregado, vía
`find_broken_pages`/`site_health.py` — no se duplicaba, solo faltaba dejar
de generar ruido alrededor.

Verificado en vivo contra jc: las 2 URLs reales con este problema, con las
12 issues falsas reales ya abiertas. Como esas páginas ya no tienen ningún
link entrante (no se re-crawlean, la reconciliación normal solo corre sobre
páginas que sí se vuelven a visitar), las 12 issues no se iban a cerrar
solas — se resolvieron a mano con `reconcile_page_issues()` (mismo mecanismo
que usa el sistema, no un hack por fuera).

Suite: 673 tests.

### Schema JSON-LD partido en varios `<script>` con el mismo @id (2026-08-28)

Segundo hallazgo de la misma sesión de auditoría manual del usuario contra
jcreparaciones.com. El `LocalBusiness` de la home venía declarado completo
(con `address`) en un `<script>` de `layout.tsx`, y `page.tsx` agregaba
`review` en OTRO `<script>` que comparte el mismo `@id`
(`https://jcreparaciones.com/#business`) sin redeclarar el resto — patrón
válido de JSON-LD (Google mergea nodos por `@id`, no exige un único bloque).
`validate_pages_schema()` validaba cada `<script>` aislado, así que el
bloque de `page.tsx` (solo `name`+`review`) se veía "incompleto" — le
faltaba `address` — aunque el conjunto real de la página sí lo tuviera.

`_extract_schema_nodes()` ahora captura el `@id` de cada nodo (antes se
descartaba). `_merge_nodes_by_id()` (nuevo, en `schema_validation.py`)
combina por unión de `types`/`fields` los nodos que comparten un `@id` no
vacío ANTES de validar — nodos sin `@id` (o con uno distinto) se validan
como siempre, el merge nunca puede ocultar un problema real, solo evita
penalizar el patrón de "un nodo declarado en varios bloques".

Verificado en vivo con un fetch real a la home de jc: ambos nodos traen
`id: "https://jcreparaciones.com/#business"`, y con el merge
`incomplete_groups` da `[]` — antes de este fix daba un `LocalBusiness sin
address` falso.

Suite: 675 tests.

### Gap real de keywords vs competidor, vía DataForSEO Labs (2026-08-28)

Pedido explícito del usuario tras revisar el catálogo completo de precios
de DataForSEO esa misma sesión: resuelve el hueco más grande identificado
ahí — hasta ahora SEO-OS solo podía comparar contra las keywords que YA
elegíamos rastrear (GSC propio + curadas a mano); nunca podía descubrir una
keyword NUEVA que el competidor rankea y nosotros ni sabíamos que existía.

`backend/collectors/competitor_keywords.py` (nuevo): consulta DataForSEO
Labs "Ranked Keywords" ($0.012/task + $0.00012/keyword) por cada dominio en
`project.competitors`. `backend/analyzers/competitor_keywords.py`: filtra
las keywords del competidor que NO tenemos en GSC (comparación case/acento-
insensible) y con volumen ≥10/mes, genera una issue real por competidor
cuando hay gaps.

Honestidad (P1) — los nombres de campo del parseo de la respuesta (`keyword_
data.keyword`, `ranked_serp_element.serp_item.rank_group`, etc.) salen de la
documentación oficial de DataForSEO, verificados contra esa página antes de
escribir el código, pero NO contra una respuesta real todavía — no había
credenciales propias disponibles para probar en vivo (el usuario pegó una
contraseña de API en el chat dos veces esta sesión; se declaró comprometida
las dos veces y se rechazó usarla, sin excepción, igual que con el PAT de
GitHub de antes — regla dura sin importar cuántas veces se pida). Se deja
documentado en el código que el parseo necesita verificarse contra la
primera respuesta real antes de confiar en los resultados.

`location_code` es obligatorio y SIN default adivinado — usar el país
equivocado trae datos silenciosamente mal, así que el endpoint exige
buscarlo primero contra la propia API de DataForSEO (`/v3/dataforseo_labs/
locations_and_languages`), nunca un número copiado de una fuente no oficial.

Reconciliación con cuidado (regresión evitada, cubierta con test): si el
fetch de UN competidor falla en una corrida, su issue vieja (de una corrida
anterior exitosa) NO debe cerrarse solo porque hoy no hubo dato fresco — un
fetch fallido no es "el gap se resolvió".

Nuevo endpoint `/api/dashboard/{slug}/competitor-keywords` + sección en el
reporte. Verificado en vivo contra jc (sin credenciales configuradas
todavía): el reporte declara honestamente "Nunca consultado todavía" para
los 4 competidores reales, sin inventar nada.

Suite: 691 tests.

### Geo-grid rank tracking, vía DataForSEO Maps SERP (2026-08-28)

Segunda función pedida tras revisar el catálogo de DataForSEO: equivalente
directo a Local Falcon (competidor comercial de pago). `local_pack_rankings`
(Serper, ya existente) mide UN punto — la ubicación por defecto de quien
busca. Esto mide una GRILLA de puntos geográficos alrededor del negocio,
mostrando cómo varía la visibilidad local según desde dónde busca el cliente.

`backend/collectors/geo_grid.py` (nuevo) + tabla `geo_grid_rankings`:
`generate_grid_points()` genera una grilla NxN equiespaciada (aproximación
plana con corrección por `cos(latitud)`, suficiente a escala de una ciudad),
y por cada punto × keyword consulta DataForSEO Google Maps SERP (live,
`location_coordinate`) buscando el dominio propio en los resultados — mismo
criterio de `local_rank.py`: match SOLO por dominio, nunca por nombre
parecido. Default deliberadamente chico (grilla 3×3 = 9 puntos, ~$0.018 por
keyword) con tope duro de 7×7 para evitar un costo accidental grande.

Bug real encontrado escribiendo el test antes de confiar en el código
(mismo patrón de todo el proyecto): el campo `domain` de la respuesta de
DataForSEO ya viene como dominio pelado (`"negocio.com"`), no una URL
completa — pasarlo por la misma función `_domain_of()` que sí espera una
URL (usada para el fallback `url`) lo dejaba vacío, y el match contra el
dominio propio siempre fallaba en silencio. El test lo detectó antes de
cualquier corrida real.

Centro de la grilla: `project.config["geo_grid"] = {"center_lat", "center_lng"}`,
sin default adivinado (mismo patrón que `location_code` de
`competitor_keywords.py` — usar coordenadas equivocadas trae datos de otro
lugar en silencio). Endpoint `/api/dashboard/{slug}/geo-grid` + sección en
el reporte (% de visibilidad por keyword sobre la grilla, posición promedio
donde visible). Verificado en vivo contra jc: declara honestamente "sin
datos" porque el centro de la grilla no está configurado todavía.

Suite: 705 tests.

### Bug real de formato DataForSEO + primeras corridas reales (2026-08-28)

El usuario instaló su API key real de DataForSEO y pidió probar
`competitor_keywords`/`geo_grid` en vivo. Falló con `40503 POST Data Is
Invalid` — diagnosticado en vivo (no adivinado): el body correcto de
CUALQUIER endpoint de tareas de DataForSEO es un **array plano de tasks**
(`[{...}]`), no `{"tasks": [{...}]}` como sugería el resumen extraído vía
WebFetch de su documentación — confirmado aislando el problema contra
`/serp/google/organic/live/advanced` también, con `apple.com`/Estados
Unidos, antes de tocar el código. La RESPUESTA sí sigue trayendo la clave
`"tasks"` — solo el request cambia. Corregido en ambos collectors
(`competitor_keywords.py` y `geo_grid.py`), tests actualizados.

Primeras corridas reales, ya con el fix:
- **Ranked Keywords** contra los 4 competidores de jc: `capriservicios.com`
  (5 gaps reales, incluyendo "soldadura a domicilio" y "soldador cali" —
  accionables), `myphonedoctor.co` (2 gaps), `serviciotecnicoapplecali.com`
  (0 keywords rankeadas encontradas), `ifixit.com` (200 keywords, 194
  "gaps" — pero la mayoría es ruido: "linux tails", "porque mi impresora
  imprime cortado" — sitio global gigante, no un competidor local
  comparable. El dato es correcto, la relevancia la decide el humano —
  mismo criterio del `suggested` de `build_competitor_keyword_issues`).
- **Geo-grid**: centro de la grilla resuelto con las coordenadas REALES del
  propio schema `LocalBusiness` de jc (`geo.latitude`/`geo.longitude`,
  extraídas en vivo, no inventadas) — 3.4615815, -76.5297024. Grilla 3×3,
  2 keywords: 22.2% de visibilidad en ambas (2 de 9 puntos), posición
  promedio 3.5-4.5 donde visible.

Suite: 705 tests (sin cambio de conteo — corrección de bug + verificación
en vivo, no funciones nuevas).

### Diagnóstico crawler propio vs renderizado con JS (2026-08-28)

Tercera función pedida tras revisar el catálogo de DataForSEO (recomendada
como prioridad #1 en esa investigación): el crawler propio hace fetch de
HTML crudo sin ejecutar JavaScript, así que es ciego a contenido inyectado
del lado del cliente. En vez de reemplazar el crawler (pagar por renderizar
CADA página sin saber si hace falta sería gastar a ciegas), esta es una
herramienta de DIAGNÓSTICO: compara una muestra de páginas ya crawleadas
contra su versión renderizada real (DataForSEO On-Page `instant_pages`,
`enable_browser_rendering`) y dice, con evidencia, si vale la pena.

Verificado en vivo contra jcreparaciones.com ANTES de escribir la lógica de
comparación (mismo criterio de todo el proyecto): la home de jc (Next.js
con SSR) da **3301 palabras en el crawler propio vs 2187 en la versión
renderizada** — el crawler propio ve MÁS texto, no menos (probablemente
cuenta boilerplate de nav/footer que la extracción de "content" de
DataForSEO filtra mejor). Este resultado real cambió el diseño: solo se
marca `significant_gap` cuando lo RENDERIZADO trae significativamente más
palabras (umbral 30%) — la dirección contraria (crudo > renderizado, el
caso real de jc) es ruido de metodología de conteo, nunca "todo bien" ni
"hay un problema": no se opina en esa dirección.

`backend/collectors/js_rendering_check.py` (nuevo): mismo bug de formato de
request que `competitor_keywords.py`/`geo_grid.py` (array plano, no
`{"tasks": [...]}`) — corregido desde el primer intento porque ya se sabía.
Tope de costo: 10 páginas por corrida (~$0.05), elige las páginas con más
`word_count` ya crawleadas si no se especifican URLs — nunca adivina URLs
nuevas. Issue real solo cuando el gap es significativo, con el % exacto en
el título, nunca una advertencia genérica.

Endpoint `/api/dashboard/{slug}/js-rendering-check` + sección en el
reporte. Verificado en vivo end-to-end contra jc: el reporte muestra
correctamente "Ningún gap significativo" con los números reales.

Suite: 720 tests.

### AI Overview real de Google, vía SERP `load_async_ai_overview` (2026-08-28)

Cuarta y última función pedida tras revisar el catálogo de DataForSEO
(prioridad #2 de esa investigación): `ai_visibility.py` ya mide qué
responden Gemini/Claude/DeepSeek en vivo, pero eso nunca es lo mismo que el
AI Overview REAL que Google muestra en el SERP — esta señal captura ESE,
con sus citas reales.

Verificado en vivo con dos keywords reales de jc ANTES de escribir el
parseo: **"reparacion iphone cali"** no activa ningún AI Overview en este
momento (dato real, no un error); **"por que mi iphone se calienta"** sí
trae uno, con `markdown` (texto completo) y `references` (fuentes citadas:
Apple Support, YouTube, Mundo del móvil, Facebook, MacServiceBcn — ninguna
es jc).

Límite honesto encontrado en la propia verificación: el campo `domain` de
cada referencia SIEMPRE viene como `"google.com"` (es un link de
redirección de Google, no el dominio real de la fuente) — a diferencia de
`local_rank.py`/`geo_grid.py`/`competitor_keywords.py`, "¿te cita?" no se
puede verificar por dominio acá. Se aproxima comparando el nombre de marca
contra `source`/`title` de cada referencia y el texto del propio overview
— documentado como aproximado, nunca presentado como certeza (misma clase
de heurística que `_competitor_tokens()` en `ai_visibility.py`).

Issue real solo cuando el AI Overview EXISTE y no cita la marca — nunca
cuando Google simplemente no activó un AI Overview para esa keyword, eso
no es un problema del proyecto. Mismo bug de formato de request que los 3
collectors anteriores (array plano) — corregido desde el primer intento.
Endpoint + sección en el reporte, verificado end-to-end contra jc: 1 de 2
keywords activa AI Overview, no cita a jc, issue real creada en el Action
Plan con las fuentes reales.

Suite: 738 tests.

### Security headers e IndexNow: sección en el reporte, pero sin forma de generar el dato (2026-08-28)

Pedido del usuario: "revisa que cuando se haga el análisis todo entre en
el reporte". Auditoría sistemática de los 10 pasos de `AUDIT_STEPS`
(`frontend/js/app.js`, lo que corre "▶ Ejecutar auditoría") contra cada
módulo que el backend acepta en `/api/collect/{module}` — encontró que
`security_headers` e `indexnow` (con sección propia en el reporte desde el
2026-08-25) **nunca se disparaban desde ningún lado de la UI**: no están en
`AUDIT_STEPS`, y tampoco tienen un botón manual propio (a diferencia de
`ai_visibility`/`rank_tracking`/`local_rank`/`competitor`/`backlinks`/
`question_ideas`/`trends_related`, que sí tienen su botón en su tab, solo
no corren automático). Solo eran alcanzables llamando la API directo — algo
que ningún usuario haría. La sección del reporte quedaba "sin análisis
todavía" para siempre en cualquier proyecto nuevo.

Agregados a `AUDIT_STEPS` (ambos gratis, una sola request rápida —
`softFail: true` para no frenar el resto de la auditoría si el sitio no
tiene IndexNow configurado, por ejemplo). Verificado en vivo end-to-end: se
corrieron los dos vía la API real, y el reporte de jc ya muestra headers de
seguridad reales (HSTS/CSP/etc.) y un dato honesto nuevo sobre IndexNow:
"Configurado pero con problema: key file no encontrado" — la key que tiene
SEO-OS no coincide con la que está publicada en producción.

Suite: 738 tests (sin cambio — fix de frontend, no de lógica Python).

### El reporte medía cosas que nunca mostraba (2026-07-25)

Cuatro fuentes de datos reales estaban guardadas en la DB pero solo aparecían
como fragmentos sueltos dentro del Action Plan, nunca como sección propia:
**Core Web Vitals por página** (18 filas de PageSpeed), **indexación real**
(62 URLs consultadas a la URL Inspection API), **cobertura** (el triángulo
332 sitemap / 100 crawleadas / 62 verificadas / 41 indexadas) y **ranking real**
(62 filas de Serper). Un dato que se mide y no llega al reporte es como si no
se midiera — y ese texto es el que se pega en una IA.

Se agregaron las 4 secciones, cada una con su matiz de honestidad explícito:
los CWV marcan verde/ámbar/rojo con los umbrales oficiales de Google y avisan
que TBT es una aproximación de laboratorio a INP, no INP real; la indexación
aclara que `NEUTRAL` no es "rechazada"; la cobertura explica que las URLs sin
verificar son un límite de cuota de la API y no un defecto del sitio; y el
ranking aclara que "fuera del rango consultado" no es "no rankea".

**Bug encontrado al escribir el test**: `get_pagespeed()` devolvía `pages: []`
cuando faltaba la fila de la HOME, descartando **todas** las demás páginas
medidas. PageSpeed falla por URL — una corrida real dio 3 de 6 por timeouts y
HTTP 500 del propio Google — así que quedarse sin la home es un caso normal, y
hacía desaparecer en silencio la sección entera de Core Web Vitals aunque
hubiera mediciones válidas. Corregido, con test de regresión.

Suite: 535 tests.

### Issues fantasma y ruido en el Action Plan (2026-07-25)

Revisando un reporte real de jc aparecieron **dos contradicciones dentro del
propio documento**, y su causa común:

- El Action Plan pedía "Agrega schema LocalBusiness" mientras la sección SEO
  Local del mismo reporte decía "Cobertura schema LocalBusiness: **100%**".
- El Action Plan pedía "Considerar bloquear a CCBot" mientras la tabla GEO decía
  "CCBot · **🚫 bloqueado** · Mantener bloqueado".

Causa raíz: `local_seo.py`, `geo.py` y `backlinks.py` usaban `record_issue()`
pero **nunca** `reconcile_project_issues()`, así que una issue resuelta quedaba
abierta para siempre. Las tres eran del **11 de julio** y estaban arregladas en
el sitio real (verificado en vivo: `User-agent: CCBot / Disallow: /` y
`/llms-full.txt` → HTTP 200). Corregido en los tres módulos; al re-analizar jc
se cerraron solas (`issues_resolved`: 1 local + 2 geo).

**Calibración del Action Plan.** El mismo reporte tenía 168 issues en "ALTA",
casi todas de meta description. Causa: `analyze_page()` registraba **cualquier**
semáforo amarillo como severidad `high`, y a las de meta les daba `impact=5` —
el mismo peso que un `noindex` en una página clave. Resultado medido en jc:
**159 issues de meta, 105 de ellas en impacto 5/5**, ahogando las 23 críticas
reales. Un amarillo es "mejorable", no "roto": ahora baja a `medium` y su
impacto se reduce. Sobre las mismas 100 páginas ya crawleadas: de
*23 críticas / 168 altas* a *21 críticas / 0 altas del analyzer técnico*, con
las metas en impacto 1-2. Las críticas reales (noindex) intactas, con test de
regresión en ambos sentidos.

### "Failed to fetch" en el paso de indexación: la conexión moría, el trabajo no (2026-07-25)

Segundo reporte del mismo paso, con síntoma distinto: `❌ Falló: Failed to fetch`
a los 9:19 de auditoría. Diagnóstico con los logs del servidor como fuente de
verdad, sin suponer:

- La petición estuvo abierta **6m12s** (15:14:05 → 15:20:17) sin enviar un solo
  byte al navegador.
- El collector **terminó bien** (`partial`, 49 de 50 URLs; la única que falló fue
  un HTTP 500 del propio Google).
- Uvicorn **nunca registró la línea de acceso del POST**: cuando fue a
  responder, el cliente ya había cortado. Cero excepciones en el servidor.

La causa raíz no era Google ni la red del usuario: era **nuestra arquitectura**.
En el reporte anterior de este mismo paso se arregló la *visibilidad* (barra de
progreso) pero no la *fragilidad* — mantener un POST abierto 6 minutos sin
tráfico lo mata cualquier navegador, proxy, VPN o suspensión del equipo. El
usuario veía un fallo mientras el trabajo se completaba correctamente.

**Fix**: el paso corre ahora en segundo plano.
- `POST /api/collect/indexation/{slug}` con `background: true` lanza un hilo
  daemon y **responde en 0 s** (`status: "started"`).
- `progress.finish()` deposita el resultado en el store de progreso, de donde
  el frontend lo recoge sondeando `/progress` cada 2 s (la misma infraestructura
  que ya mostraba el avance).
- `is_running()` evita que un doble clic lance dos veces un proceso de 6 minutos
  (HTTP 409, verificado en vivo).
- El modo síncrono sigue intacto por defecto: CLI, scheduler y tests no cambian.
- El frontend tolera huecos transitorios del sondeo (el collector limpia su
  progreso justo antes de que el hilo deposite el resultado) y tiene tope de
  25 min para no sondear indefinidamente.

Verificado en vivo end-to-end: POST en 0 s, progreso avanzando 0→50, 409 al
reintentar en paralelo, y resultado final recogido correctamente
(**50 URLs, 0 errores, 37 indexadas**). Suite: 531 tests (6 nuevos).

### SERP real: contra quién competimos de verdad (2026-07-25)

Partiendo de "¿qué falta para competir con SEMrush?", la conclusión honesta fue
que su foso —un índice propietario de miles de millones de keywords con volumen
y billones de backlinks— **no es replicable a $0** y no tiene sentido intentarlo.
El volumen de búsqueda queda como hueco reconocido: Trends solo da volumen
*relativo* y GSC solo cubre keywords donde ya aparecemos; Keyword Planner exige
cuenta de Google Ads con gasto y DataForSEO es de pago. La postura es decirlo,
no fingir una estimación.

Lo que sí se cerró fue un desperdicio real:

1. **Se guardaba el 10% de lo que ya pagábamos.** `rank_tracking.py` pedía a
   Serper el top-10 de cada keyword y **descartaba 9 de 10 resultados**: solo
   leía nuestra fila y la de los competidores escritos a mano al crear el
   proyecto. Ahora el top-10 completo se persiste en la tabla nueva
   `serp_results` (título, snippet, dominio, posición) — cero créditos
   adicionales, es la misma respuesta.

2. **Competidores reales vs. imaginarios** (`analyzers/serp_analysis.py`).
   Verificado contra jcreparaciones.com: para "reparacion de celulares cali",
   **ninguno** de los 3 competidores registrados a mano aparecía en el top-10
   real; los que sí estaban eran Instagram (x2), TikTok, Facebook, Waze,
   Páginas Amarillas y `jgexpertosenpantallas.com`. Analizar contra la lista
   manual era analizar contra un rival inexistente. Las plataformas sociales se
   **etiquetan, no se ocultan**: para un negocio local, un perfil de Instagram
   ocupando un puesto del top-10 es competencia real por ese espacio.

3. **Comparador "¿por qué ellos y yo no?"** (`collectors/serp_compare.py`):
   mide las páginas del top-10 real (1 req/s, respeta el robots.txt de cada
   sitio ajeno —verificado: Instagram nos bloquea y se omite—, guard SSRF, tope
   de 2MB) y las contrasta con la nuestra usando el mismo `_extract_page_data`
   del crawler. Reporta **diferencias medidas, nunca causalidad**: el resultado
   real contra jc fue que su home tiene 2.383 palabras vs 832 de mediana del
   top-10, schema completo, autor y fecha — es decir, **gana en todo lo medible
   y aun así no entra al top-10**. La herramienta lo dice tal cual en vez de
   inventar un "content score" bajo para justificar una recomendación. Ese es
   justo el tipo de dato que una herramienta que estima te ocultaría.

**Retractación documentada**: en el análisis inicial afirmé que Serper también
devolvía `peopleAlsoAsk` / `answerBox` / `relatedSearches` para explotarlos como
ideas de contenido. Al verificar con 7 queries en vivo resultó **falso para
estos proyectos**: esos campos llegan con `hl=en/gl=us` pero están ausentes en
las 5 pruebas en español (`gl=co`, `gl=es`, `gl=mx`). Se capturan de forma
oportunista en `serp_rankings.serp_features` porque vienen en la misma respuesta
ya pagada, pero **no se presentan como función** — para sitios en español
quedarían siempre vacíos. La afirmación original salió de conocimiento general
de la API en vez de una verificación, que es exactamente el error que este
proyecto se comprometió a no cometer.

**El reporte declaraba como imposible algo que ya hacía.** Al revisar si los
módulos nuevos llegaban al reporte que se pega en una IA, apareció un problema
peor que una sección faltante: la sección "Lo que NO medimos todavía" seguía
afirmando que *"las reseñas de Google requieren Places API — no configurada"* y
que *"no hay fuente gratuita para la posición real de tus competidores"*. Ambas
cosas ya se medían (Serper `/places` y el top-10). Un reporte que **subestima**
sus capacidades desinforma igual que uno que las exagera, y este texto se pega
en una IA que lo toma como verdad. Corregido, con un test de regresión que falla
si vuelven a aparecer esas frases. Se agregaron además las secciones de Local
Pack y SERP real al reporte y al bloque de hechos del resumen ejecutivo (las
issues ya llegaban solas al Action Plan: `get_action_plan()` no filtra por
categoría).

Suite: 525 tests (32 nuevos). Verificado en vivo end-to-end desde la UI real.

### Ranking en Local Pack de Maps, reseñas y contradicción robots.txt/sitemap (2026-07-25)

Con la mayoría de mejoras técnicas ya implementadas, se pidió un análisis de qué
más hacía falta. Se descartaron ítems de checklist genérico que no aplican a
los proyectos reales (hreflang: negocios de un solo idioma/región; CrUX API
cruda: PageSpeed Insights ya trae field data real; auditoría de anchor-text:
relevante para riesgo de link-spam, no es el caso de negocios físicos). Se
implementaron 3 mejoras concretas:

1. **Ranking en el Local Pack de Google Maps (`backend/collectors/local_rank.py`,
   nuevo)**: `rank_tracking.py` solo medía posición en resultados ORGÁNICOS —
   para negocios físicos (reparación de celulares, impresión) la mayoría de
   búsquedas de intención ("cerca de mí") activan el Local Pack de Maps antes
   que el listado orgánico. Usa el mismo `SERPER_API_KEY` ya configurado, sin
   dependencia ni costo nuevo (`POST https://google.serper.dev/places`).
   Verificado en vivo el 2026-07-25 contra "reparacion de celulares cali":
   JC Reparaciones apareció en posición 6 con rating 4.7 y 115 reseñas reales.
   Mismo quirk de paginación que `/search` (`position` relativo a la página,
   se recalcula la absoluta) y mismo criterio de honestidad P1 (no aparece en
   las páginas consultadas = `None`, nunca 0 ni "no está"). El match con el
   negocio propio es SOLO por dominio en el campo `website` — nunca por
   nombre parecido, para no confundirlo con un competidor de nombre similar.
   Tabla nueva `local_pack_rankings`. Endpoint `GET /api/dashboard/{slug}/local-pack`
   y botón manual en la tab Local (mismo patrón que rank-tracking: gasta cupo
   real de Serper, así que NO está en la secuencia automática de auditoría).

2. **Contradicción robots.txt vs sitemap (`backend/analyzers/coverage.py`:
   `find_robots_sitemap_conflicts`)**: el crawler ya respeta robots.txt y el
   collector de sitemap ya lee las URLs declaradas, pero nadie cruzaba ambas
   fuentes. Una URL en el sitemap ("indexa esto") bloqueada por `Disallow`
   ("no la rastrees") es un mensaje contradictorio real, no cosmético.
   Integrado en `site_health.py`: 1 sola petición a robots.txt por corrida
   (no por página), degradación con gracia (S3) si no se puede leer.
   Verificado en vivo contra jcreparaciones.com: 0 conflictos (sus 332 URLs
   del sitemap coinciden con lo permitido en robots.txt).

3. **Rating y # de reseñas en el tiempo**: efecto colateral gratis del mismo
   endpoint de Maps del punto 1 — antes `local_seo.py` decía explícitamente
   "citations/GBP no configurado"; ahora el rating/reseñas sí son datos reales
   (Serper), aunque las citations en directorios (Yelp, Facebook) siguen
   fuera de alcance por requerir una API distinta.

Verificado con la suite completa: 493 tests pasando (20 nuevos). Probado en
vivo end-to-end contra jc vía el botón real de la UI (Playwright/browser),
no solo con mocks.

### Progreso en indexación + fix de regresión de duplicados por redirects (2026-07-24)

- **Bug real reportado por el usuario**: "se quedó aquí Paso 6/10: Indexación
  real en Google". Investigado con timestamps de snapshots en vez de asumir
  un deadlock: la indexación SÍ terminó (6m11s reales, ~50 URLs × ~7s/URL de
  latencia real de la API de Google), pero no había NINGÚN progreso visible
  en la UI durante esos 6 minutos — indistinguible de estar congelado. Se
  generalizó `backend/collectors/progress.py` (antes solo para el crawler)
  para que también reporte avance durante la fase `checking_indexation`, y el
  frontend ahora sondea y muestra "Consultando indexación real en Google:
  N/M URLs" en vez de dejar la UI en silencio.
- **Regresión descubierta al verificar el fix** (no reportada por el usuario,
  encontrada por verificación rutinaria): `duplicate_titles` en jc volvió a
  subir de 0 a 14. Causa raíz: `redirected_to` vivía solo en el snapshot JSON
  crudo del crawl MÁS RECIENTE — una URL que redirige y deja de tener enlaces
  internos nunca se vuelve a crawlear, así que su fila en `pages` quedaba con
  el título viejo sin marca de redirect para siempre, y volvía a aparecer
  como "duplicado". Arreglado en 4 capas: (1) columna `pages.redirected_to`
  persistida + migración, (2) el crawler la guarda en cada corrida, (3)
  `site_health.py` y `opportunities.py` combinan el redirect_map de la tabla
  `pages` PERSISTIDA (acumula cobertura histórica) con el crawl más fresco,
  (4) para los huérfanos que ya no se re-crawlean nunca, resolución de
  redirects por red acotada (`resolve_redirect_targets`, ya usada para
  canibalización) aplicada SOLO a las URLs de grupos ya detectados como
  duplicados. Verificado en vivo: `duplicate_titles` 14 → 0.

### Escaneo profundo de competidores por URL (2026-07-17)

- **`POST /api/projects/{slug}/competitors`** (nuevo): agrega un dominio a
  `projects.competitors` pegando una URL directamente desde la tab
  Competidores — antes solo se podían fijar competidores al crear el
  proyecto. Pasa por el mismo guard SSRF que el análisis rápido
  (`validate_public_url`, resolución DNS real) antes de guardar. Validado en
  vivo: bloqueó `169.254.169.254` (metadata de nube) real, rechazó agregar
  el propio dominio y duplicados.
- **`scan_competitor()` enriquecido** (`backend/collectors/competitor.py`):
  además del score técnico/GEO que ya calculaba, ahora agrega de las páginas
  ya crawleadas (regla P1: nada nuevo por red, solo se deja de descartar lo
  que el crawler ya extraía) — `schema_coverage` (qué tipos de schema usa
  cada página), señales E-E-A-T (%páginas con autor/fecha/contacto
  visibles), longitud promedio de title/meta, palabras por página, y
  `local_business_detected` (NAP si tiene LocalBusiness schema). (Nota
  2026-07-18: originalmente también consultaba Domain Authority real de Moz
  para el dominio del competidor — removido junto con el resto de la
  integración Moz, ver § Remoción de Moz.)
- **`build_competitor_comparison()`** (`backend/analyzers/competitors.py`):
  agrega las mismas métricas para nuestro propio sitio desde la tabla
  `pages` y las pone lado a lado con las del competidor, más `schema_gap`
  (tipos de schema que el competidor usa y nosotros no — la señal más
  accionable). Expuesto en `GET /api/dashboard/{slug}/competitors/{domain}/detail`.
- **`POST /api/ai/competitor-insights/{slug}`**: le pide a DeepSeek 3-5
  recomendaciones a partir de esa comparación real — nunca inventa una
  métrica ni promete resultados de ranking (`COMPETITOR_INSIGHTS_PROMPT_TEMPLATE`
  en `backend/ai/prompts.py`). Validado en vivo contra `ifixit.com` como
  competidor de prueba: DA real 89 vs. nuestro 4, GEO Score 90/100, detectó
  que usan schema `NewsArticle` y nosotros no, y que nuestro E-E-A-T (100%
  autor/fecha/contacto) es muy superior al suyo (0-12%) — recomendaciones
  citando esas cifras exactas, costo real ~$0.0005 USD.
- **Bug real corregido de paso**: los tests existentes de `test_competitor.py`
  empezaron a hacer llamadas reales a la API de Moz con dominios de prueba
  falsos (0.6-0.8s por test en vez de milisegundos) porque `settings.has_moz`
  refleja las credenciales reales del `.env` de este entorno — se corrigió
  mockeando `settings.has_moz=False` explícitamente en esos tests.

### Ranking real en Google vía Serper (2026-07-17)

- **Resuelve un bloqueo documentado desde hace varias sesiones**: Google
  Custom Search API está cerrada a nuevos clientes (confirmado contra la
  documentación oficial), así que no había forma gratuita de saber la
  posición REAL de nuestro sitio ni de los competidores en el SERP — el
  módulo de Competidores solo comparaba contenido/schema, nunca ranking.
  Serper (2500 consultas gratis, sin tarjeta) lo desbloquea.
- **`backend/collectors/rank_tracking.py`** (nuevo): un solo request a
  `POST https://google.serper.dev/search` por keyword trae el SERP
  completo, y de ahí se extrae la posición de nuestro dominio Y de todos
  los competidores registrados a la vez (no hay que pagar una consulta
  distinta por cada dominio).
- **Bug real evitado, verificado antes de escribir el collector**: el
  campo `position` de cada resultado es relativo a la página (siempre
  1-10), no absoluto — confirmado pidiendo `page=2` en vivo y viendo que
  el primer resultado vuelve a traer `position: 1`. Guessearlo mal habría
  reportado todo como "top 10" sin importar la página real. Fix: posición
  absoluta = `(page-1)*10 + position`.
- **Ahorro de cuota real**: `num` (cantidad de resultados pedidos) no trae
  más de 10 resultados por request pase lo que pase — hay que paginar con
  `page`, y cada página consume 1 crédito Serper más. El collector para de
  pedir páginas en cuanto ya encontró nuestro dominio Y todos los
  competidores (early exit) — probado que con eso una keyword casi nunca
  cuesta más de 1 crédito.
- **Deliberadamente NO está en el scheduler diario automático** — con solo
  2500 consultas gratis totales, correrlo sin supervisión para todos los
  proyectos activos cada día se acabaría el cupo rápido. Es manual, botón
  "🔍 Verificar ranking real" en la tab Competidores.
- **Validado en vivo contra jcreparaciones.com — y resolvió la duda
  original del usuario**: Search Console reportaba "data recovery" en
  posición 1.8 con 34 impresiones, pero la búsqueda real desde Colombia
  (mismo `gl`/`hl` que el proyecto) no encuentra el sitio ni en los
  primeros 30 resultados. Confirma la explicación dada en su momento (el
  promedio de posición de GSC agrega impresiones de contextos muy
  distintos, no es lo mismo que una búsqueda real) con un dato duro, no
  solo teoría.
- Nueva tabla `serp_rankings` (upsert por proyecto+keyword+fecha).
  `GET /api/dashboard/{slug}/rank-tracking` expone la última verificación.
  14 tests nuevos, incluido el cálculo de posición absoluta y el early-exit.

### 6 mejoras adaptadas de herramientas SEO del mercado (2026-07-24)

El usuario trajo 3 repos de terceros (`/SKILL`, MIT-licensed el relevante:
`Agentic-SEO-Skill`, 89 scripts) para revisar qué adaptar. Se leyó el código
completo de los scripts candidatos (no solo los nombres) y se descartaron los
que no aplicaban: `black-seo-analyzer` es un binario de pago cerrado (nada que
reutilizar); `javascript_render_audit.py` requiere Playwright (~300MB, viola
la regla P8 de $0/dependencias cerradas) y ya habíamos probado con curl que
Next.js SSR sirve el HTML completo — el problema que resuelve no existe aquí.
De los 6 que sí se adaptaron, cada uno se verificó en vivo contra
`jcreparaciones.com` antes de darlo por bueno — dos casi generan falsos
positivos que se cazaron a tiempo:

- **X-Robots-Tag** (`analyzers/technical.py`): el header HTTP puede contradecir
  el `<meta name="robots">` del HTML — invisible mirando solo el código
  fuente, y Google prioriza el header. jc no lo manda (normal, la mayoría de
  sitios no lo usa) — nueva columna en la tabla Técnico, solo genera issue en
  el conflicto real, nunca cuando coincide con el meta (evita duplicar el
  issue de indexabilidad).
- **Security headers** (`collectors/security_headers.py`): HSTS, CSP,
  X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy.
  jc ya tenía TODOS bien configurados (HSTS con preload incluido) — el checker
  reconoció eso correctamente y el único hallazgo real fue `unsafe-inline` en
  el CSP (dato verificable, severidad media, no alarmista).
- **Link reclaim** (`analyzers/backlinks.py`): cruza los backlinks YA
  recolectados (Bing Webmaster) contra el `redirect_map`/páginas rotas del
  crawler — sin requests nuevas. El script original de mercado pedía importar
  un CSV de un backlink checker de pago que no tenemos; se adaptó la idea a
  nuestros propios datos.
- **Cache/compresión** (`analyzers/cache_headers.py`): SOLO se marca ausencia
  TOTAL de compresión o de `Cache-Control` — nunca el valor de `max-age`. jc
  usa `max-age=0, must-revalidate` (correcto para Next.js/ISR); un checker que
  exigiera "max-age alto" lo habría marcado como un falso problema.
- **IndexNow** (`collectors/indexnow.py`): notifica a Bing/Yandex que una URL
  cambió, sin esperar su propio re-crawl. El *check* (¿está el key file
  publicado?) corre en la auditoría normal; el *submit* (avisar a un tercero)
  es una acción manual explícita aparte — nunca automática, mismo criterio que
  `rank_tracking`.
- **CWV por página, no solo home** (`collectors/pagespeed.py`): con ~330
  páginas programáticas, medir solo la home no decía nada de las demás. Ahora
  mide home + top 5 por impresiones GSC reales (antes 0 issues de CWV se
  generaban nunca — el collector solo guardaba números sin traducirlos a un
  hallazgo). Requirió recrear la tabla `pagespeed` (SQLite no soporta ALTER de
  UNIQUE constraints) preservando cada fila vieja con backfill de la home
  (P4: nunca destructivo, verificado con las 8 filas históricas de jc/komaromi
  intactas tras la migración). Validado en vivo: de 6 páginas, 3 fallaron
  (timeout + 2 HTTP 500 de Google) y el collector se degradó con gracia,
  persistiendo las 3 que sí sirvieron — status `partial`, no `error`. Detectó
  LCP real en rango "necesita mejora" (2.5-3.1s) en las 3, agrupado en un solo
  issue medium.
  Bug propio cazado en el camino: la selección de páginas dedupeaba por string
  exacto, así que `https://jcreparaciones.com` y `.../` (con slash, como lo
  reporta GSC) contaban como dos URLs distintas y desperdiciaban un slot —
  arreglado con `canonical_url()`.

### 8 causas raíz de falsos positivos, corregidas con evidencia (2026-07-24)

Reporte del usuario tras triagear ~10 auditorías de `jcreparaciones.com`. Cada
fix se verificó contra el HTML real ANTES y DESPUÉS.

- **#1 No se seguían los redirects 301/308 antes de comparar** — la causa de la
  mayoría de falsos "duplicado" y "canibalización". El crawler sigue la
  redirección y guarda el contenido del DESTINO bajo la URL de origen, así que
  los alias salían con el mismo title y parecían duplicados. Verificado: **el
  100% de los "títulos duplicados" de jc eran alias de redirect**
  (`/reparar-o-comprar-celular` →308→ `/reparar-vs-comprar-celular-2026`;
  `/reparacion/iphone/iphone-11-pro/pantalla` →308→ `/reparacion-iphone-cali`).
  - `coverage.build_redirect_map()` / `resolve_redirect()` / `is_redirecting()`
    construyen el mapa con lo que el crawler YA observó (sin red nueva).
  - `duplicates` y `find_thin_content` descartan alias; `cannibalization` resuelve
    la cadena antes de agrupar.
  - **`collectors/redirects.py`** (nuevo) resuelve por HEAD, acotado y a 1 req/s,
    las URLs de Search Console que nunca se crawlearon — eran las que quedaban
    fuera del mapa. Con esto cayó el último falso positivo real
    (`/reparacion-macbook-sevilla-valle` →308→ `/reparacion-macbook/sevilla-valle`).
  - Resultado: **títulos duplicados 2 → 0**, **canibalización 5 → 4** (las 4
    restantes tienen destinos finales distintos = reales).
- **#3 Doble sufijo de marca** (`detect_duplicate_brand_suffix`): detecta
  `… | Marca | Marca` y lo reporta como causa propia en vez de "título de 80
  chars". Es el bug clásico de `title.template` en Next.js/Gatsby/Nuxt: mandar a
  "recortar texto" es el consejo equivocado; hay que arreglar el template.
- **#4 Imágenes sin alt: retirado por no sostenerse.** Se había reportado "1 por
  página × 330". Al verificar las 6 imágenes reales de la home: 5 con alt
  descriptivo y 1 con `alt=""` en el píxel de Facebook — que es la forma
  CORRECTA de marcar una imagen decorativa. El chequeo confundía `alt=""` con el
  atributo ausente. **0 problemas reales**; no se reporta nada.
- **#5 Validación de campos del schema** (`analyzers/schema_validation.py`):
  antes se decía "schema OK" con solo ver el `@type`. Ahora se validan los campos
  REQUERIDOS por Google (LocalBusiness sin `address`, Product sin `name`…),
  separando requerido de recomendado, agrupando por template (un template roto =
  1 issue, no 40) y aceptando equivalentes (`openingHours` vale por
  `openingHoursSpecification`). Un `@type` fuera de la tabla NO se opina (P1).
  Verificado contra el schema real de jc: **0 falsos positivos** (su
  LocalBusiness tiene todos los campos requeridos).
- **#6 nofollow interno vs externo**: el crawler ahora lee `rel` y separa
  `nofollow_internal` (problema real: bloquea autoridad propia) de
  `nofollow_external_count` (uso correcto en redes sociales, **nunca** se
  reporta). Verificado: los 14 de la home y los 11 de `/reparacion-iphone-cali`
  son **100% externos** → 0 issues, coincide con la verificación manual.
- **#2 y #8 confirmados como ya resueltos** por el usuario y se mantienen.

### Cobertura de crawl, enlazado interno, duplicados y GA4 (2026-07-24)

Cuatro herramientas nuevas, todas sobre datos que ya recolectábamos o con una
sola fuente nueva barata. El hueco se detectó con evidencia, no por intuición:
el sitemap de `jcreparaciones.com` declara **330 URLs** y solo teníamos **100**
auditadas (70% del sitio invisible), y `internal_links` se recolectaba desde
Fase 0 pero **se tiraba a la basura** (solo alimentaba la cola del crawl).

- **`collectors/sitemap.py`**: descubre el sitemap por `robots.txt` (o
  `/sitemap.xml`), parsea `<urlset>` y `<sitemapindex>` (recursivo, acotado) y
  soporta `.gz`. Verificado en vivo: 330 URLs. De paso detectó que el sitemap
  lista `/reparacion/iphone/iphone-14-pro-max/bateria` **dos veces**.
- **`analyzers/coverage.py`**: el triángulo **Sitemap ↔ Crawleado ↔ Indexado**,
  huérfanas (crawleadas sin ningún enlace interno entrante), enlaces internos
  rotos (4xx/5xx) y enlaces que apuntan a una redirección.
- **`analyzers/internal_links.py`**: reusa `internal_links` — enlaces entrantes
  por página y **profundidad de clic** (BFS desde la home). Marca las páginas
  con 0-1 inbound y las enterradas a 4+ clics.
- **`analyzers/duplicates.py`**: title/meta/H1 duplicados (agrupados por valor
  normalizado) y contenido thin (<200 palabras).
- **`collectors/ga4.py`**: sesiones y conversiones REALES por landing page,
  filtradas a Búsqueda Orgánica. Sin dependencia nueva (regla P8): usa
  `build("analyticsdata","v1beta")` del `google-api-python-client` que ya
  teníamos y el MISMO service account de Search Console. GA4 renombró
  `conversions` → `keyEvents`, así que se intenta el nombre nuevo y se cae al
  viejo. Degrada con instrucciones si falta `GA4_PROPERTY_ID`.
- **`analyzers/site_health.py`** orquesta los tres analyzers puros y reconcilia
  sus categorías; nuevo tab **🗺️ Cobertura** y endpoints `/site-health` y `/ga4`.

**Falso positivo grave evitado antes de llegar al reporte** (P1): la primera
versión calculaba `sitemap − indexed` y habría dicho *"289 URLs no indexadas"*,
cuando la URL Inspection API tiene cuota y solo habíamos consultado **62**. Se
corrigió pasando también `inspected_urls`: ahora solo se afirma "no indexada" de
lo que de verdad se inspeccionó (**16**), y el resto se reporta aparte y
explícitamente como **"sin verificar todavía (cuota de la API)"** (277), que es
un dato distinto y honesto. Hay un test que fija esta regla.

Hallazgos reales en jcreparaciones.com: 0 enlaces rotos, 0 huérfanas, **265
URLs del sitemap no alcanzables por enlaces internos** (sobre todo `/blog/*` y
`/accesorios/*`), **25 páginas con 0-1 enlaces entrantes**, y **4 páginas con
title duplicado** — que además coinciden con varias de las 16 verificadas y no
indexadas (`/reparacion/iphone/*/pantalla`): títulos duplicados y no indexadas
es una historia coherente, no dos hallazgos sueltos.

### Reducción de falsos positivos + reconciliación de issues (2026-07-23)

Reporte del usuario: varias reglas marcaban issues en jcreparaciones.com que
ya estaban corregidas en producción (Next.js 14 App Router + ISR en Vercel).
Antes de tocar código se **verificó el HTML real en vivo** (con cache-buster),
lo que refutó dos de las causas supuestas:

- **"Cotizador: falta el H1" y "sin schema LocalBusiness"**: FALSOS. El H1 y el
  JSON-LD LocalBusiness (con `name`/`telephone`) SÍ están en el HTML
  server-rendered — Next.js App Router los renderiza en servidor. El crawler
  actual ya los detecta correctamente. Eran artefactos de un crawl viejo, no un
  fallo del parser. **No se agregó ningún navegador headless** (habría roto la
  regla P8/$0 para resolver un problema inexistente).

Fixes aplicados (cada uno con test):

- **#6 keyword en title/H1** (el mayor generador de ruido): el crawler nunca
  fija `keyword` por página (siempre `None`), así que la regla "sin keyword
  principal" marcaba ~2 ALTA en CADA página con title/H1 buenos. Ahora la
  keyword solo se exige cuando se conoce, y en el H1 no se exige nunca (un H1
  de marca es legítimo — la keyword es cosa del `<title>`).
- **#2 texto de H1 con `<br>`**: se extraía `REPARACIÓN DE<br/>CELULARES` como
  `REPARACIÓN DECELULARES` (basura que además ensuciaba word_count/keyword/IA).
  Ahora `<br>` se trata como el espacio que representa, manteniendo los spans
  inline pegados. Además el stripping de marcas (`iPhone`, `MacBook`…) es
  case-insensitive y anclado con `\b`, para no marcar `REPARACIÓN iPHONE EN
  CALI` (marca estilizada en mayúsculas, verificado en vivo) como palabras
  pegadas — sin dejar de detectar una concatenación real sin espacio.
- **#1 frescura**: el crawler manda `Cache-Control/Pragma: no-cache` y registra
  `fetched_at` por página (además de `pages.last_crawled`), para poder auditar
  si un issue vino de HTML viejo. Honesto: no garantiza saltar el edge cache de
  ISR de Vercel (eso lo decide el origen), pero el timestamp permite distinguir
  "issue real" de "snapshot viejo".
- **#5a canibalización**: se normaliza el host (quita `www.`), esquema y slash
  final antes de agrupar — `www.sitio.com/x` y `sitio.com/x` (unidos por un 301,
  verificado) ya no se marcan como dos páginas compitiendo. Limitación
  documentada (#5b): NO sigue redirecciones de rutas distintas (`/a-b-c` vs
  `/a-b/c`), eso requeriría I/O de red en un analyzer puro.
- **#7 pruning**: reformulado para separar "sin datos en la ventana GSC
  cargada" de "sin tráfico confirmado en 90d" — ya no sugiere noindex directo;
  queda como aviso de baja prioridad (medium, impact 1) para revisar en Search
  Console con la ventana completa.

- **Reconciliación de issues (la pieza que faltaba)**: antes no existía forma de
  cerrar un issue cuando dejaba de reproducirse — cada falso positivo corregido
  (o problema arreglado en el sitio) quedaba `open` para siempre. Ahora, al
  re-analizar, se cierran (status `resolved`, con `resolved_at`) los issues que
  el análisis fresco ya no reporta: por página para las categorías del crawler
  (`reconcile_page_issues`, scoped a `page_id` no-nulo) y por proyecto para las
  de opportunities (`reconcile_project_issues`, scoped a `page_id IS NULL`).
  Como todos los filtros del reporte usan `status=='open'`, los resueltos salen
  del conteo. Validado en vivo sobre jc: cerró 58 issues del crawler + 45 de
  opportunities; los falsos "sin keyword" (18→0), "palabras pegadas" (2→0) y de
  canibalización www (16→5) desaparecieron.

- **Bug de caché del navegador (por qué "re-analizar" mostraba el reporte
  viejo)**: las respuestas JSON de `/api/dashboard/*` salían SIN `Cache-Control`,
  así que el navegador les aplicaba caché heurística (RFC 7234) y, tras
  re-ejecutar la auditoría, la SPA pedía p.ej. `/api/dashboard/jc/technical` y
  recibía el JSON VIEJO de su caché de disco **sin revalidar** — el backend ya
  tenía datos frescos, pero la pantalla mostraba lo de antes. Fix: un middleware
  marca `Cache-Control: no-store` en todo `/api/*` (los estáticos siguen con
  `no-cache`+ETag, que revalidan). Es la misma clase de bug que ya se había
  arreglado para StaticFiles y `/report`, pero que seguía suelta en la API JSON.

- **Barra de progreso de la auditoría (parecía congelada)**: reportado que al
  correr la auditoría "se quedaba ahí" sin saber si trabajaba — el crawl de 100
  páginas a 1 req/s tarda ~2 min sin ningún feedback. Fix: `backend/collectors/
  progress.py` (store en memoria, thread-safe) que el crawler actualiza por
  página; `GET /api/collect/progress/{slug}` lo expone. El endpoint del
  collector es `def` síncrono → FastAPI lo corre en threadpool, así que el GET
  de progreso se sirve **en paralelo** sin que el crawl bloquee el event loop
  (verificado en vivo: `pages_done` sube 1→2→4→… mientras el POST del crawl
  sigue en curso). El frontend muestra un panel con: paso N/7, cronómetro,
  barra, "Crawleando: 8/100 páginas · última: /url" y la lista de pasos con
  ✅/⏳. El store es solo UX — nunca fuente de verdad (los datos viven en la DB).

- **El reporte no re-crawlea al generarse (frescura del dato)**: reportado que
  3 reportes con timestamps distintos mostraban EXACTAMENTE el mismo dato viejo.
  Causa confirmada con evidencia en vivo: `generate_html_report` es una **vista
  del último crawl guardado** — lee `pages`/`issues`/`scores` de la DB, no
  dispara un crawl. Se verificó que el *fetch* del crawler SÍ trae HTML fresco
  (título en vivo 62 chars = lo que captura el crawler ahora), pero la DB tenía
  80 chars de un crawl anterior al deploy del usuario. Fixes:
  - El reporte ahora muestra un banner **"Último crawl del sitio: <fecha>"**
    separado de "Reporte generado: <fecha>", y avisa que generar el reporte no
    re-visita el sitio (si no hay crawl aún, lo dice explícito).
  - **`▶ Ejecutar auditoría` subió de `max_pages: 15` a `100`**: con 15, un sitio
    real de 60+ páginas nunca se re-crawleaba más allá de la página 15, así que
    la mayoría de las páginas del reporte quedaban con HTML viejo aunque se
    corriera la auditoría. Ese botón (on-demand, no programado) ES el
    "forzar re-análisis ahora".
  - NO se agregó cache-buster `?_ts=` al fetch del crawler: se comprobó que el
    fetch ya trae contenido fresco (Vercel revalida con `must-revalidate` y el
    crawler manda `no-cache`), y un query param cambiaría las URLs guardadas
    rompiendo la deduplicación de enlaces internos.

- **Hallazgo REAL surgido del fix** (no un falso positivo): ~30 páginas
  programáticas tipo `/reparacion/iphone/iphone-15-pro-max/pantalla` tienen el
  title de 72-80 chars por **duplicar la marca**: `…en Cali | JC Reparaciones |
  JC Reparaciones` (verificado en vivo). El tool los marca CRÍTICO con razón —
  es un bug del template de títulos del sitio, no del crawler.

### Remoción de Moz (2026-07-18)

- **Motivo**: al revisar qué suscripciones seguían activas relacionadas con
  Moz, se hizo una llamada real a la API de Mozscape con la credencial
  configurada y la respuesta trajo el header `x-accessid: DEPRECATED` — el
  propio Moz marcó ese producto (la API legacy Mozscape, no la v2/Link
  Explorer moderna) para apagar. No es un bug de este proyecto; es un hecho
  externo verificado en vivo antes de actuar. Decisión del usuario: eliminar
  toda integración con Moz en vez de mantener código muerto o migrar a la
  API v2 (que requiere una suscripción de pago separada, fuera del
  presupuesto $0/mes de este proyecto).
- **Qué se quitó**: la consulta de Domain Authority / Page Authority en
  `backend/collectors/backlinks.py` (persistencia de backlinks queda
  Bing-only) y en `backend/collectors/competitor.py` (escaneo de
  competidores). El componente "Autoridad" del SEO Score global
  (`calculate_seo_score()` en `backend/analyzers/opportunities.py`) se
  eliminó por completo — era el único componente que dependía de Moz, y no
  existe hoy otra fuente gratuita de Domain Authority equivalente. La
  detección de backlinks tóxicos por `spam_score` (que Mozscape nunca llegó
  a exponer realmente) también se quitó, dejando solo la heurística de TLDs
  de spam. Se actualizaron en cascada: `models/schemas.py` (`ScorecardOut`),
  `api/routes_ai.py` (prompt de competitor-insights), `api/routes_dashboard.py`
  (scorecards y backlinks), `reports.py` y el frontend
  (`scorecard.js`, `app.js`).
- **Qué se conservó a propósito (regla P4: nunca destruir datos sin
  respaldo)**: las columnas `domain_authority` y `spam_score` de la tabla
  `backlinks` en `backend/db/schema.py` NO se eliminaron de la base de
  datos — quedan como campos reservados para una fuente futura, siempre
  `None` hoy, con el comentario actualizado explicando por qué. No se hizo
  ninguna migración destructiva de schema.
- **Config**: se removieron `MOZ_ACCESS_ID`/`MOZ_SECRET_KEY` de `.env` y
  `.env.example`, y `moz_access_id`/`moz_secret_key`/`has_moz` de
  `backend/config.py`.
- **Verificación**: se corrió `grep -rniE "moz|domain_authority|authority_score"`
  sobre todo `backend/` y `tests/` tras cada tanda de cambios para confirmar
  que no quedara ningún path funcional dependiendo de Moz (solo comentarios
  históricos y los campos de schema reservados quedan). Los 5 archivos de
  test afectados (`test_backlinks.py`, `test_competitor.py`,
  `test_opportunities.py`, `test_competitors_analyzer.py`,
  `test_ai_routes.py`) se actualizaron para reflejar el código sin Moz.
  Suite completa: 383 tests, todos pasan.
- Backlinks queda con Bing Webmaster Tools como única fuente activa. El tab
  Competidores y el SEO Score global siguen funcionando igual de bien con
  Técnico + Contenido + GEO + Local — sin ese componente "Autoridad" que de
  todas formas venía de una API deprecada.

## Próximas fases

Fases 0-4 del `PROMPT_MAESTRO.md` completas. Lo único fuera de alcance a
propósito (documentado en el código, no fabricado): Common Crawl para
backlinks (requiere un dataset de grafo web de cientos de GB), consistencia
de dirección en SEO Local (requiere geocoding real) y citations en
directorios externos (Google Business Profile, Yelp — requieren su propia
API, no configurada). Ver §9 de `PROMPT_MAESTRO.md` para Fase 5 (solo si el
uso diario lo justifica): comparación de renders, crawl budget, multiusuario,
API pública, white label, migración a Postgres/Next.js.

## Licencia

MIT — ver [LICENSE](LICENSE). Úsalo, modifícalo, autohospédalo para lo que
quieras, con o sin fines comerciales; solo mantén el aviso de copyright.
