# All You Can Cache — Tarea 1 Sistemas Distribuidos UDP 2026-2

Sistema distribuido de caché para consultas del fútbol chileno. Obtiene datos en tiempo real desde Soccerway y los sirve con baja latencia usando Redis como caché.

---

## Integrantes

| Nombre | Correo |
|---|---|
| Benjamín Casanova | benjamin.casanova@mail.udp.cl |
| Lucas Echeverría | lucas.echeverria@mail.udp.cl |

---

## Cómo funciona

Cuando un usuario hace una consulta, el sistema primero revisa si la respuesta ya está guardada en Redis (cache hit). Si está, responde de inmediato en milisegundos. Si no está (cache miss), va a buscar los datos a Soccerway usando un navegador automatizado (Playwright), guarda el resultado en Redis para futuras consultas, y responde.

```
Usuario → cache-service → ¿está en Redis?
                              ├── SÍ  → responde en ~10ms
                              └── NO  → scraper-service → Soccerway (~15-20 seg)
                                              ↓
                                        guarda en Redis
                                              ↓
                                        responde al usuario
                                              ↓
                                  metrics-service registra el evento
```

El sistema tiene cuatro servicios independientes:

| Servicio | Puerto | Qué hace |
|---|---|---|
| **redis** | 6379 | Guarda las respuestas en memoria con tiempo de expiración (TTL) |
| **scraper-service** | 8000 | Abre Soccerway con un navegador y extrae los datos |
| **cache-service** | 8001 | Recibe las consultas, decide si va al scraper o responde desde Redis |
| **metrics-service** | 8002 | Registra cada hit, miss y latencia para análisis posterior |
| **traffic-generator** | — | Simula usuarios haciendo consultas para experimentos |

---

## Requisitos

- Docker
- Docker Compose

No se necesita instalar Python, Redis ni ninguna otra dependencia de forma manual. Todo corre dentro de contenedores.

---

## Levantar el sistema

**Paso 1 — Clonar el repositorio**

Usar el repositorio del grupo del curso:
```bash
git clone https://giteit.udp.cl/CIT2011/2026-2/seccion-2/grupo-10.git
cd grupo-10
```

Si no hay acceso al GitLab institucional, clonar desde GitHub:
```bash
git clone https://github.com/TH3L1AR7/Sistemas-distribuidos-tarea-1.git
cd Sistemas-distribuidos-tarea-1
```

**Paso 2 — Configurar variables de entorno**
```bash
cp .env.example .env
```

**Paso 3 — Levantar los servicios**
```bash
docker compose up redis scraper-service metrics-service cache-service
```

La primera vez tarda un par de minutos porque Docker descarga las imágenes. El scraper tarda ~30 segundos adicionales en arrancar porque inicializa Playwright con Chromium.

Cuando veas esto en los logs, el sistema está listo:
```
scraper-service-1   | INFO: Application startup complete.
cache-service-1     | INFO: Application startup complete.
metrics-service-1   | INFO: Application startup complete.
```

---

## Verificar que funciona

Abre otra terminal y ejecuta los siguientes comandos:

```bash
# Verificar que el cache-service está activo
curl http://localhost:8001/health
# Respuesta esperada: {"status":"ok"}
```

```bash
# Primera consulta — va a Soccerway (tarda ~15-20 segundos)
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{"query_type": "Q5", "params": {}}'
# Respuesta esperada: {"source":"scraper","data":[...tabla de posiciones...]}
```

```bash
# Misma consulta — responde desde Redis (instantáneo)
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{"query_type": "Q5", "params": {}}'
# Respuesta esperada: {"source":"cache","data":[...mismos datos...]}
```

```bash
# Ver resumen de métricas acumuladas
curl http://localhost:8002/summary
# Respuesta esperada: {"hits":1,"misses":1,"hit_rate":0.5,...}
```

---

## Tipos de consulta disponibles

El cache-service acepta 5 tipos de consulta. Los códigos de equipo están en `shared/teams.json`.

**Q1 — Próximos partidos de un equipo**
```bash
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{"query_type": "Q1", "params": {"team": "colocolo"}}'
```

**Q2 — Últimos partidos de un equipo**
```bash
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{"query_type": "Q2", "params": {"team": "udechile"}}'
```

**Q3 — Historial de enfrentamientos entre dos equipos**
```bash
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{"query_type": "Q3", "params": {"team_a": "colocolo", "team_b": "udechile"}}'
```

**Q4 — Partidos en un rango de fechas**
```bash
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{"query_type": "Q4", "params": {"date_from": "2026-09-01", "date_to": "2026-09-15"}}'
```

**Q5 — Tabla completa de posiciones**
```bash
curl -X POST http://localhost:8001/query \
  -H "Content-Type: application/json" \
  -d '{"query_type": "Q5", "params": {}}'
```

---

## Correr experimentos

Con los servicios levantados, en otra terminal:

```bash
# Limpiar métricas anteriores
curl -X DELETE http://localhost:8002/events

# Correr el generador de tráfico
docker compose run --rm traffic-generator
```

El generador envía 200 solicitudes al sistema y al terminar imprime un resumen con hit rate, throughput y latencias. Los resultados también se guardan automáticamente en `experiments/results/` como CSV y JSON.

Para cambiar la configuración entre experimentos:

```bash
# 1. Editar .env con los nuevos valores
nano .env

# 2. Reiniciar Redis para aplicar el nuevo tamaño/política
docker compose up -d redis

# 3. Limpiar métricas
curl -X DELETE http://localhost:8002/events

# 4. Correr el experimento
docker compose run --rm traffic-generator
```

---

## Variables de configuración

El archivo `.env` controla el comportamiento del sistema. Copiar desde `.env.example` como punto de partida.

| Variable | Default | Descripción |
|---|---|---|
| `CACHE_SIZE` | `5mb` | Tamaño máximo de la caché en Redis. Cuando se llena, Redis elimina claves según la política de evicción. |
| `EVICTION_POLICY` | `allkeys-lru` | Cómo Redis decide qué eliminar cuando la caché está llena. `allkeys-lru` elimina lo usado hace más tiempo; `allkeys-lfu` elimina lo menos frecuente. |
| `DISTRIBUTION` | `uniform` | Distribución de tráfico del generador. `uniform` distribuye consultas aleatoriamente; `zipf` concentra el tráfico en pocos equipos populares. |
| `TOTAL_REQUESTS` | `200` | Total de solicitudes por experimento. |
| `ARRIVAL_RATE` | `2` | Tasa de arribo en requests por segundo. No subir de 4 o el scraper se satura. |
| `ZIPF_PARAM` | `1.5` | Concentración del tráfico en Zipf. Valores más altos concentran más en pocos equipos. |
| `CACHE_TTL_Q1` | `3600` | Segundos que se guarda en caché Q1 (próximos partidos). |
| `CACHE_TTL_Q2` | `300` | Segundos que se guarda en caché Q2 (últimos partidos). Corto porque los resultados cambian tras cada partido. |
| `CACHE_TTL_Q3` | `1800` | Segundos que se guarda en caché Q3 (historial). |
| `CACHE_TTL_Q4` | `600` | Segundos que se guarda en caché Q4 (por fecha). |
| `CACHE_TTL_Q5` | `900` | Segundos que se guarda en caché Q5 (tabla de posiciones). |

---

## Herramientas de desarrollo

- Claude (Anthropic) como asistente para debugging, configuración de Docker y revisión de código
