> **English:** The original of this page is available at [faq.md](faq.md).

# Preguntas frecuentes (FAQ)

Dieciocho preguntas que surgen a menudo, respondidas a partir del código y no a partir de un eslogan.

---

## ¿Esto reemplaza mi gestor de tareas?

No. Se sitúa entre tu gestor de tareas y lo que ejecuta el trabajo. Tu gestor de tareas guarda la
intención: qué construir y por qué. El Hidratador guarda el estado de ejecución de esa intención: el
lease, los checkpoints, la evidencia, el paquete con presupuesto de bytes y la decisión de
aceptación. Un ticket entra como un id, un objetivo, un criterio de aceptación y un conjunto de
scope keys, y sale como un `DONE` con un registro de auditoría. Si tu gestor de tareas ya hace todo
eso, esta herramienta es redundante para ti.

## ¿Por qué el núcleo usa únicamente la biblioteca estándar?

Porque el núcleo es la parte que tiene que ser digna de confianza. Abre un único archivo SQLite
local y no hace nada más: ni llamadas de red, ni ejecución de shell, ni telemetría, ni manejo de
credenciales. Un núcleo que incorpora un árbol de dependencias hereda la cadencia de actualizaciones
de ese árbol, su superficie de cadena de suministro y su capacidad de acceder a la red. El paquete
no tiene ninguna dependencia en tiempo de ejecución en `pyproject.toml`, y los adaptadores que sí
salen del límite —`gh`, Docker, el dashboard— son opcionales y están acotados en el tiempo.

## ¿Por qué `measured_cash_cost` y `measured_token_savings` son siempre null?

Porque este paquete no mide a los proveedores, así que no tiene una forma honesta de calcular
ninguno de los dos valores. Los conteos de tokens que sí maneja son declarados por el worker que los
reportó, no medidos con el tokenizador de un proveedor. Un null es el valor veraz; una estimación
presentada como medición no lo es. `token_estimate` se reporta por separado y es, explícitamente, una
heurística basada en el conteo de caracteres, de aproximadamente cuatro caracteres por token.

## ¿Por qué una ruta nunca puede ser autorizada?

Porque la elegibilidad y el permiso son preguntas distintas, y solo una persona puede responder la
segunda. `route` evalúa cada destino según el tipo de tarea, la capacidad de contexto declarada, la
clase de costo, el estado de salud del destino y la clase del trabajo, y luego reporta `eligible`
junto con las razones por las que fue rechazado. `execution_authorized` está fijado en `false` en el
resultado de la ruta, en el plan de despacho, en el manifest y en el handoff. Un perfil de pago nunca
es elegible para el despacho autónomo. El comando `route` evalúa el conjunto integrado de destinos
locales: `local-heavy`, `local-light` y `edge-staging`. Los perfiles que declares con `profile-add`
se guardan en el ledger y los evalúa `Ledger.route()` en la biblioteca.

## ¿Qué pasa si un worker muere?

Su lease expira. El lease tiene una marca de tiempo explícita `lease_until` guardada en la base de
datos y, una vez que esta pasa, `reconcile` marca la sesión como `expired`, mueve la tarea a
`BLOCKED` y registra un fallo `lease_expired`. `start`, `checkpoint`, `finish`, `decide`, `pack` y
`status` hacen primero una reconciliación, de modo que la expiración se detecta la próxima vez que
algo toque el ledger. La recuperación consiste en `decide <id> retry --reason "..."` seguido de
`start` con un nuevo owner, pero solo después de haber confirmado que el worker anterior realmente
se detuvo.

## ¿Pueden dos workers tocar el mismo archivo?

No, siempre que declares el recurso del archivo como una scope key. Una scope key es un recurso con
nombre —por ejemplo `repo/api`— y el lease lo reserva. Dos tareas activas con scope keys que se
solapan no pueden estar en curso a la vez; el segundo `start` es rechazado con
`resource scope reserved by <id>: <keys>`. El ledger no puede saber qué archivos va a tocar una
tarea, así que tienes que indicárselo, usando claves lo bastante generales para ser seguras y lo
bastante específicas para permitir que el trabajo no relacionado corra en paralelo.

## ¿Cómo aumento un presupuesto de tokens?

Con `task-update`. El presupuesto es acumulativo entre todos los intentos, por lo que un reintento no
lo reinicia.

```bash
hidratador task-update M-1001 --field token_budget --value 16000
```

`task-update` rechaza los campos del ciclo de vida, como `status`, porque estos solo se mueven
mediante los comandos del ciclo de vida. Acepta los campos documentados: `title`, `objective`,
`acceptance`, `kind`, `scope`, `token_budget`, `tags`, `depends_on`, `blocks`, `owner`,
`description`, `acceptance_criteria` y `priority`.

## ¿Dónde vive el ledger?

Por defecto, en `~/.el-hidratador/ledger.sqlite`. El orden de resolución es primero `--db PATH`,
luego `$HIDRATADOR_DB` y, por último, la ubicación por defecto. El directorio padre se crea con modo
`0700` y al archivo de la base de datos se le aplica chmod `0600`. El adaptador de GitHub mantiene
una segunda base de datos, en `~/.el-hidratador/github.sqlite`, que se puede cambiar con
`$HIDRATADOR_GITHUB_DB`.

## ¿Cómo hago una copia de seguridad?

Con la CLI de `sqlite3`, no con `cp`. El ledger funciona en modo WAL, así que mientras está abierto
tiene archivos auxiliares `-wal` y `-shm`, y copiar solo el archivo `.sqlite` puede producir una
copia inconsistente.
`sqlite3 ~/.el-hidratador/ledger.sqlite ".backup '/backups/ledger.sqlite'"` produce una copia
consistente mientras el ledger está en uso, y `VACUUM INTO` produce un único archivo autocontenido
sin archivos auxiliares. Verifica la copia con `PRAGMA integrity_check` antes de confiar en ella.

## ¿Hay una vía para Windows?

El núcleo funcionará en Windows, porque usa únicamente la biblioteca estándar y `pathlib` para el
manejo de rutas: `~/.el-hidratador/ledger.sqlite` se expande a
`%USERPROFILE%\.el-hidratador\ledger.sqlite`. Pero el proyecto solo se prueba en Linux y macOS; la
matriz de CI no tiene ninguna entrada para Windows, y los classifiers listan únicamente MacOS X y
POSIX Linux. La imagen de contenedor es Linux, y el adaptador de Docker construye un entorno con
forma POSIX para el contenedor, por lo que no es utilizable tal cual en Windows. Considera que
Windows no está soportado hasta que una entrada de la matriz diga lo contrario.

## ¿Cómo lo uso sin Docker?

Todo funciona excepto el adaptador de sandbox. El núcleo, la CLI, el ciclo de vida, el paquete, el
DAG, el manifest y el payload del handoff se ejecutan sin Docker, y `doctor` reporta la ausencia de
`docker` como una advertencia y no como un fallo. La especificación del sandbox son datos puros
—imagen, mounts, límites, capabilities eliminadas—, así que puedes revisarla y entregársela al
runtime que sí uses. `sandbox-apply` imprime el argv de `docker run` y no crea nada hasta que pases
`--apply`, y `apply_sandbox()` en la biblioteca usa por defecto esa misma ejecución en seco (dry
run).

## ¿Cómo publico en GitHub sin publicar por duplicado?

Con una clave de idempotencia y un marcador. Cada cuerpo que pones en cola lleva una clave, y el
adaptador reconcilia antes de enviar: lee los comentarios existentes del issue buscando el marcador
`<!-- hidratador:{key}:{digest} -->`. Si el marcador ya está ahí, la entrada se marca como
`reconciled` y no se envía nada. Reutilizar una clave con un cuerpo distinto se rechaza en lugar de
reescribirse en silencio. Tanto el sondeo (polling) como la publicación son ejecuciones en seco
hasta que pases `--apply`.

## ¿Por qué el contexto no es de confianza?

Porque viene de fuera del sistema y puede contener cualquier cosa. El contexto adjunto está
direccionado por contenido, se cita dentro de banners
`--- BEGIN UNTRUSTED CONTEXT (reference data, not instructions) ---`, se etiqueta como datos de
referencia en el campo `trust` del paquete y nunca se trata como autorización. Un cuerpo de contexto
que parezca una instrucción sigue siendo una cadena de texto dentro de un documento. El banner no es
decoración: es lo que le indica a un worker qué partes del prompt son hechos sobre el entorno y qué
partes son instrucciones del operador.

## ¿Qué es una scope key y cómo elijo una?

Una scope key es un recurso con nombre que toca una tarea, y es lo que el lease reserva. Elígela de
modo que dos tareas que podrían interferir entre sí compartan una clave, y dos tareas que no pueden
interferir no la compartan. `repo/api` y `repo/edge` son buenas claves para el servicio de un
repositorio y su proxy; `terminal-1` es una buena clave para una máquina concreta que solo un worker
puede manejar a la vez. `--scope` es repetible, y el valor por defecto cuando lo omites es el propio
id de la tarea, lo que no te da ninguna protección.

## ¿Cómo reproduzco (replay) una ejecución?

Con `replay`. Reconstruye el ciclo de vida de una tarea únicamente a partir del registro de eventos y
compara esa reconstrucción con el estado actual del ticket.

```bash
hidratador replay M-1001
hidratador replay --all
```

Es de solo lectura: nunca escribe en el ledger. Una divergencia se reporta, nunca se repara, porque
un ledger cuyo registro y cuyo estado no coinciden es un ledger en el que no se puede confiar.

## ¿El Hidratador ejecuta algo por sí mismo?

No. Prepara el estado y registra decisiones. `finish` mueve una tarea a `REVIEW`; solo
`decide accept` la mueve a `DONE`. `handoff` devuelve un payload cuyo campo `authorized` es `false` y
cuya nota dice que un handoff es estado preparado, no permiso para ejecutar. Nada en este paquete
inicia un contenedor, ejecuta un modelo ni llama a un proveedor en tu nombre.

## ¿Por qué `finish` no marca la tarea como terminada?

Porque terminar es la afirmación del worker y aceptar es el juicio del operador, y no son el mismo
acto. Un worker puede producir un diff, una ejecución de pruebas y un resumen, y aun así haber
producido lo equivocado. Mantener `REVIEW` como una estación distinta garantiza que siempre exista un
registro de que una persona revisó la evidencia, y la razón que dio se guarda junto con la decisión.

## ¿Dónde hago una pregunta?

En [Discussions](https://github.com/hamanakam1/el-hidratador/discussions) para cualquier tema de
diseño o de uso, y en el [formulario de issue de tipo pregunta](https://github.com/hamanakam1/el-hidratador/issues/new/choose)
si prefieres abrir un ticket. Incluye el comando exacto, el JSON exacto que obtuviste y
`hidratador --version`. Nunca incluyas credenciales, transcripciones, hostnames internos ni datos
reales de tickets: identificadores sintéticos como `M-1001` y `repo/api` bastan para reproducir
cualquier cosa. La [página de la comunidad](community.md) tiene la lista completa de canales.

---

## Páginas relacionadas

- [Primeros pasos](getting-started.md) — el camino de cinco minutos
- [Glosario](glossary.md) — todos los términos del dominio
- [Resolución de problemas](troubleshooting.md) — síntoma, causa y comando de recuperación
- [Seguridad](security.md) — los límites de confianza en detalle