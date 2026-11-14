> **English:** The original English page is available at [getting-started.md](getting-started.md).

# Primeros pasos

El Hidratador convierte un ticket **en seco** (*dry*) —cincuenta palabras de intención— en un paquete de ejecución **húmedo** (*wet*):
contexto expandido, un grafo ordenado de trabajo, un límite de sandbox y un perfil de cómputo enrutado.
Esta página muestra el recorrido de cinco minutos. Verifica la máquina, observa una ejecución narrada y luego lleva un ticket real
a través de su ciclo de vida por ti mismo.

Todo lo que sigue se ejecuta contra un ledger temporal, por lo que nada de esto afecta tu propio estado.

---

## Paso 0 — Apuntar a un ledger temporal

El ledger es un único archivo de SQLite, ubicado por defecto en `~/.el-hidratador/ledger.sqlite`. Para una primera
ejecución, apúntalo a un directorio temporal:

```bash
export HIDRATADOR_DB="$(mktemp -d)/ledger.sqlite"
echo "$HIDRATADOR_DB"
```

```text
/var/folders/qm/xxxx/T/tmp.Abc123/ledger.sqlite
```

Elimina el directorio cuando hayas terminado.

---

## Paso 1 — Verificar la máquina

`doctor` responde una sola pregunta: ¿está esta máquina lista para ejecutar El Hidratador? Verifica el
intérprete, la compilación de SQLite, el archivo del ledger y sus permisos, el modo de journal, los adaptadores
opcionales, el espacio libre en disco y las dos variables de entorno. Genera un reporte. Nunca repara
nada.

```bash
hidratador doctor | jq '{healthy, failed, warnings, checks: [.checks[] | "\(.name): \(.status)"]}'
```

```json
{
  "checks": [
    "python: ok",
    "sqlite: ok",
    "ledger: warn",
    "journal-mode: warn",
    "adapter:git: ok",
    "adapter:gh: warn",
    "adapter:docker: ok",
    "disk: ok",
    "env:HIDRATADOR_DB: ok",
    "env:HIDRATADOR_GITHUB_DB: ok"
  ],
  "failed": [],
  "healthy": true,
  "warnings": [
    "ledger",
    "journal-mode",
    "adapter:gh"
  ]
}
```

Cada verificación incluye una cadena de `detail` que explica lo que se encontró y una cadena de `remedy` cuando hay
algo que hacer al respecto. El código de salida es `0` cuando ninguna verificación falla, y `2` cuando una falla. Una advertencia (`warn`) no es un
fallo: la ausencia de `gh` o `docker` solo significa que dichos adaptadores no pueden ejecutarse. El núcleo no necesita ninguno de los dos.

La advertencia del `ledger` es esperada aquí, ya que el archivo aún no existe. Créalo con:

```bash
hidratador init
```

```json
{
  "command": "init",
  "database": "/var/folders/qm/xxxx/T/tmp.Abc123/ledger.sqlite",
  "initialized": true,
  "ok": true,
  "schema_version": 3
}
```

---

## Paso 2 — Observar una ejecución narrada

Antes de operar nada por ti mismo, observa todo el ciclo de vida una vez. `demo` ejecuta un ticket completo
contra un ledger temporal en un directorio provisional y narra lo que va ocurriendo. Tu ledger no es
tocado.

```bash
hidratador demo --text
```

```text
El Hidratador demonstration
================================

Created the ticket M-1001 in status INBOX.
  Scope keys: repo/api, repo/edge
  A scope key is a resource, not a description. Two tickets that share a scope key cannot run at the same time.
Attached one context blob, content-addressed by its hash.
  Hash: 7c7bb3f8ba3b47d9 (66 bytes)
  Attached context is reference data. It is quoted inside explicit banners and never treated as an instruction.
Triaged the ticket: INBOX to READY.
  Status is now READY.
Took the execution lease. The task is IN_PROGRESS.
  Session: b2f2376aae62
  Lease expires at 2026-10-22T13:06:44.726Z.
  The lease reserves the scope. A second worker asking for the same scope is refused until the lease is released or expires.
Recorded the first checkpoint with evidence.
  Tokens reported so far: 900
  Evidence is hashed. Three checkpoints with the same evidence hash block the task as no_progress.
Requested review. The task is in REVIEW, not DONE.
  Finishing is not accepting. Only an operator decision can move work to DONE.
Accepted the work. The task is DONE.
  The acceptance gate is explicit and never bypassed.
Read the ledger back.
  Accepted work: 1
  Tokens reported by the worker: 1500
  Reported tokens are declared by the worker. They are not metered by a provider, and the measured cost stays null.
Read the audit log: 9 events.
  task.created -> context.attached -> task.triaged -> session.started -> session.checkpoint -> session.checkpoint -> session.finished -> operator.accept -> task.done

Ledger used: /var/folders/qm/xxxx/T/hidratador-demo-t00uri30/demo.sqlite
The demonstration ran against a scratch ledger. Your ledger was not touched.
```

Esa narración representa el vocabulario de todo el sistema, en orden. Léela una vez más y presta atención a
las tres frases que nunca cambian: una `scope key` es un recurso, el contexto es un dato de referencia y
finalizar no es aceptar.

---

## Paso 3 — Impulsar un ticket real

Seis comandos. Cada uno imprime exactamente un objeto JSON.

### 3.1 Agregar el ticket

Un ticket contiene un objetivo, el criterio de aceptación que lo evalúa, el `scope` de recursos
que toca y el presupuesto de tokens que puede gastar.

```bash
hidratador task-add M-1001 \
  --title "Add request id echo endpoint" \
  --objective "Expose GET /healthz that returns the request id assigned by the edge proxy." \
  --acceptance "GET /healthz returns 200 with the X-Request-Id header; a unit test covers it." \
  --scope repo/api --scope repo/edge \
  --budget 4000 --tags api,health --priority 2 --owner terminal-1
```

```json
{
  "command": "task-add",
  "created": true,
  "idempotent": false,
  "ok": true,
  "task_id": "M-1001"
}
```

El ticket se encuentra ahora en `INBOX`. `--scope` es repetible y cada valor es una **`scope key`**: un
recurso que solo un trabajador puede retener a la vez. `--seed-key` haría que el comando fuera idempotente,
por lo que una ejecución repetida devuelve el ID de la tarea original en lugar de crear un duplicado.

### 3.2 Clasificarlo (*Triage*)

```bash
hidratador --compact triage M-1001 | jq '{id, status, scope}'
```

```json
{
  "id": "M-1001",
  "status": "READY",
  "scope": [
    "repo/api",
    "repo/edge"
  ]
}
```

La clasificación (*triage*) mueve de `INBOX` a `READY`, y se rechaza si la tarea no tiene un `scope`. Un ticket no se puede ejecutar
hasta que sus recursos estén nombrados.

### 3.3 Iniciarlo y tomar un `lease`

```bash
hidratador start M-1001 --owner terminal-1 --lease 900
```

```json
{
  "command": "start",
  "lease_until": 1792674407.926934,
  "lease_until_iso": "2026-10-22T13:06:47.926Z",
  "ok": true,
  "session_id": "30789e01508a4a14a5df6ca35be86392",
  "task_id": "M-1001",
  "token_budget": 4000,
  "tokens_used": 0
}
```

La tarea está en `IN_PROGRESS`. El **`lease`** dura 900 segundos y reserva las `scope keys`. A un segundo
trabajador que solicite `repo/api` se le denegará hasta que este `lease` se libere o expire. Captura el
ID de sesión:

```bash
export SESSION=30789e01508a4a14a5df6ca35be86392
```

### 3.4 Registrar un `checkpoint`

Un `checkpoint` consta de cuatro hechos: qué se hizo, la evidencia, el siguiente paso y el recuento acumulativo
de tokens reportados por el trabajador.

```bash
hidratador checkpoint "$SESSION" \
  --summary "Route table registered" \
  --evidence "diff 4f1c2d: router.py adds GET /healthz" \
  --next "Read the request id header" \
  --tokens 900
```

```json
{
  "command": "checkpoint",
  "evidence_hash": "a4efe85688de6e3e24e58f3a431ea19c88489cf788eb0a1f410e46b78ae30f38",
  "ok": true,
  "session_id": "30789e01508a4a14a5df6ca35be86392",
  "stagnant": 0,
  "status": "active",
  "tokens": 900
}
```

La evidencia se codifica mediante hash. Tres `checkpoints` seguidos con el mismo hash significan que el trabajador está en un bucle,
y la tarea se bloquea como `no_progress`. El recuento de tokens es acumulativo y nunca disminuye; el
`checkpoint` también renueva el `lease`.

### 3.5 Solicitar revisión

```bash
hidratador finish "$SESSION" \
  --evidence "GET /healthz returns 200 with X-Request-Id; unit test green"
```

```json
{
  "command": "finish",
  "ok": true,
  "status": "REVIEW",
  "task_id": "M-1001"
}
```

La tarea está en `REVIEW`. No está terminada. `finish` no puede producir `DONE`.

### 3.6 Aceptar el trabajo

```bash
hidratador decide M-1001 accept --reason "Reviewed the diff and the test output"
```

```json
{
  "command": "decide",
  "decision": "accept",
  "ok": true,
  "status": "DONE",
  "task_id": "M-1001"
}
```

La decisión pasa por `ACCEPTED` y llega a `DONE`, añadiéndose dos eventos de auditoría:
`operator.accept` y `task.done`. Las otras dos decisiones son `retry`, que devuelve la tarea a
`READY` manteniendo intacto su recuento acumulativo de tokens, y `cancel`, que la mueve a `CANCELLED`.

---

## Paso 4 — Leer el ledger de vuelta

```bash
hidratador --compact status | jq '{tasks_by_status, metrics}'
```

```json
{
  "metrics": {
    "accepted": 1,
    "measured_cash_cost": null,
    "measured_token_savings": null,
    "note": "Reported tokens are declared by workers, not metered by a provider.",
    "reported_tokens": 900
  },
  "tasks_by_status": {
    "DONE": 1
  }
}
```

Dos campos se mantienen como `null` a propósito: este paquete no mide a los proveedores, por lo que no puede reportar el costo en efectivo
o el ahorro de tokens. `reported_tokens` es lo que el trabajador declaró.

El registro de auditoría almacena cada mutación:

```bash
hidratador --compact events | jq -r '.events[] | "\(.seq)\t\(.type)"'
```

```text
1 task.created
2 task.triaged
3 session.started
4 session.checkpoint
5 session.finished
6 operator.accept
7 task.done
```

---

## El vocabulario, en el orden en que lo encuentras

| Palabra | Qué significa aquí |
|---------|-------------------|
| dry ticket | Intención sin contexto adjunto: objetivo, aceptación, `scope`, presupuesto |
| hydration | Expandir ese ticket en un paquete con presupuesto de bytes |
| packet | La estructura hidratada: objetivo, aceptación, `scope`, último `checkpoint`, contexto citado |
| station | Una estación entre `INBOX`, `READY`, `IN_PROGRESS`, `BLOCKED`, `REVIEW`, `ACCEPTED`, `DONE`, `CANCELLED` |
| scope key | Un recurso que toca una tarea; dos tareas activas no pueden retener el mismo |
| lease | Una reclamación con límite de tiempo sobre una tarea, por defecto 900 segundos |
| session | Un intento bajo un `lease` |
| checkpoint | Un paso registrado: resumen, evidencia, siguiente paso, tokens acumulativos |
| evidence hash | SHA-256 de la evidencia; tres hashes idénticos seguidos bloquean la tarea |
| failure | Un rechazo registrado con una categoría y una huella digital (`fingerprint`) |
| fingerprint | `sha256(category:summary)[:16]`, la clave que agrupa fallos recurrentes |
| operator decision | `accept`, `retry` o `cancel`, lo único que puede terminar el trabajo |
| acceptance gate | `finish` produce `REVIEW`; solo `decide accept` produce `DONE` |

El [glosario](glossary.md) contiene la lista completa.

---

## Dónde ir después

- [Install](install.md) — cada ruta de instalación y cómo verificarla
- [CLI reference](cli-reference.md) — cada comando, bandera y código de salida
- [Architecture](architecture.md) — las cinco capas y el modelo de datos
- [Glossary](glossary.md) — cada término de dominio, definido una sola vez
- [FAQ](faq.md) — las preguntas que surgen más a menudo
- [Troubleshooting](troubleshooting.md) — síntoma, causa y comando de recuperación
- [Failure playbook](failure-playbook.md) — una página por categoría de fallo
- [Operations](operations.md) — ubicación del ledger, respaldos y el runbook de tareas atascadas
- [Security](security.md) — los límites de confianza y lo que deliberadamente no se hace
- [Development](development.md) — trabajar en el proyecto mismo
- [Community](community.md) — hacer una pregunta, reportar un error, proponer un cambio