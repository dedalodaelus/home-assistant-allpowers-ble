# ALLPOWERS BLE para Home Assistant

[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.7%2B-41BDF5.svg)](https://www.home-assistant.io/)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://www.hacs.xyz/)
[![Licencia: MIT](https://img.shields.io/badge/Licencia-MIT-yellow.svg)](LICENSE)

[English](README.md)

Integración local de Home Assistant para estaciones de energía portátiles
ALLPOWERS compatibles. Utiliza la pila Bluetooth oficial de Home Assistant, por lo
que funciona tanto con un adaptador local como mediante un **Bluetooth Proxy de
ESPHome conectable**.

No utiliza la nube del fabricante, no accede directamente a BlueZ y no necesita un
componente ESPHome específico para la estación.

> [!IMPORTANT]
> La **ALLPOWERS R600** es el objetivo verificado. Un nombre parecido no demuestra
> compatibilidad. Antes de crear la entrada, la integración comprueba activamente
> el servicio GATT, las características y una trama de estado con checksum válido.

## Funciones principales

- Descubrimiento Bluetooth y configuración completa desde la interfaz.
- Comunicación local mediante Bluetooth de Home Assistant o ESPHome Bluetooth Proxy.
- Varias estaciones simultáneas, cada una con su propia entrada y conexión.
- Sensores, controles, versiones, RSSI y contadores de salud de la conexión.
- Escrituras protegidas con instantáneas recientes; los ajustes conservan bits desconocidos y las salidas solo preservan estados verificados.
- Reconexión resolviendo de nuevo el mejor adaptador o proxy disponible.
- Parser incremental para notificaciones fragmentadas, concatenadas o con ruido.
- Diagnósticos de entrada y dispositivo con la dirección Bluetooth redactada.
- Traducciones en inglés y español.

## Compatibilidad

| Modelo o anuncio | Estado | Observaciones |
|---|---:|---|
| ALLPOWERS R600 (`R600*`, `AP R*`) con firma de revisión verificada (`hardware_version=1.2`, `raw_hardware_version=0x12`) | Verificado | Objetivo principal de desarrollo y protocolo con controles de escritura habilitados. |
| AP S300 y unidades similares `AP S*` | Experimental de solo lectura | Solo se aceptan tras una prueba activa GATT y de protocolo. Se exponen entidades de telemetría, pero no controles de escritura. |
| AP S500 / AP S700 V2 | Rechazado | Utilizan una revisión de protocolo diferente. |
| Anuncio genérico `ALLPOWERS*` o servicio FFF0 | Experimental de solo lectura | La configuración continúa únicamente si la validación de protocolo termina correctamente. Se expone telemetría, pero no controles de escritura. |

Consulta [Compatibilidad](docs/compatibility.md) antes de declarar compatible otro
modelo.

## Requisitos

- Home Assistant **2026.7.0 o posterior**.
- HACS para el método de instalación recomendado.
- Una ruta Bluetooth conectable:
  - adaptador Bluetooth disponible para Home Assistant, o
  - ESPHome Bluetooth Proxy con conexiones activas habilitadas.
- Estación encendida y con una señal BLE estable.

Un proxy exclusivamente pasivo puede detectar el anuncio, pero no mantener la
conexión GATT necesaria.

## Instalación mediante HACS

1. Abre **HACS** en Home Assistant.
2. En el menú, selecciona **Repositorios personalizados**.
3. Añade `https://github.com/dedalodaelus/home-assistant-allpowers-ble` como
   repositorio de tipo **Integración**.
4. Instala **ALLPOWERS BLE**.
5. Reinicia Home Assistant.
6. Abre **Ajustes → Dispositivos y servicios**. Acepta el dispositivo descubierto
   o selecciona **Añadir integración → ALLPOWERS BLE**.

### Instalación manual

Copia únicamente este directorio en la configuración de Home Assistant:

```text
custom_components/allpowers_ble/
```

Reinicia Home Assistant y añade la integración desde **Dispositivos y servicios**.

## Bluetooth Proxy

Configuración mínima habitual en ESPHome:

```yaml
esp32_ble_tracker:

bluetooth_proxy:
  active: true
```

Home Assistant elige la mejor ruta conectable disponible. En cada reconexión, la
integración vuelve a resolver el adaptador o proxy y no queda fijada de forma
permanente a uno concreto.

## Límite de confianza BLE

Esta integración asume un entorno local de Home Assistant de confianza:

- los comandos de escritura solo se aceptan desde usuarios con permisos sobre entidades;
- la proximidad BLE limita el alcance, pero el acceso por radio cercano sigue siendo parte del riesgo;
- los proxies ESPHome son relés de transporte y deben gestionarse como infraestructura local de confianza;
- no existe una frontera de credenciales en nube que rotar si el acceso local se compromete.

No uses esta integración como enclavamiento duro de seguridad para cargas críticas desatendidas.

## Entidades

| Plataforma | Entidades |
|---|---|
| Sensor | Batería, potencia de entrada, potencia de salida, tiempo restante, RSSI, versiones, reconexiones, errores de protocolo y reinicios del watchdog |
| Sensor binario | Conectado, telemetría disponible, ajustes disponibles, entrada activa, salida activa y estado de salidas |
| Interruptor | Salida CA, salida CC, luz, modo ECO y cargador de coche experimental |
| Selector | Modo de trabajo y tiempo de apagado ECO |
| Botón | Actualizar estado, reconectar y enviar keepalive de ajustes |
| Número | Intervalo de estado e intervalo de keepalive |

Los valores y controles pasan a no disponibles cuando la información necesaria
está obsoleta. Así se evita mostrar como actual un estado antiguo o construir una
escritura a partir de datos de una sesión anterior.

Los perfiles experimentales funcionan en modo de solo lectura: no crean entidades
de control de escritura hasta validar explícitamente las capacidades por modelo y
revisión de hardware.
Los contadores de diagnóstico son valores de sesión y no exponen una clase de
estado para estadísticas de largo plazo.

## Seguridad de las escrituras

El protocolo agrupa varios controles dentro de los mismos campos de bits. Para no
modificar accidentalmente otra salida, la integración aplica estas reglas:

1. Los cambios de CA, CC y luz requieren un estado reciente y envían un comando
   combinado que conserva las demás salidas.
   La preservación solo está garantizada para estados de salida documentados;
   no se afirma seguridad para semánticas no documentadas del comando de salida.
2. ECO, modo de trabajo, cargador de coche y temporizador requieren ajustes
   recientes y conservan todos los bits no relacionados.
3. Cada escritura abre una transacción versionada y espera una confirmación en
   notificación antes de reutilizar ese estado para otra escritura.
4. Las transacciones pendientes y la frescura se invalidan al desconectar o
   iniciar otra sesión GATT.
5. El cliente BLE aplica capacidades de escritura por perfil y revisión en el
   borde de transporte para salidas, ajustes y keepalive de ajustes.
6. Si no existe una instantánea segura o el perfil activo no autoriza la
   operación, la escritura se rechaza en vez de adivinar.

Cualquier garantía futura de seguridad en escritura debe apoyarse en evidencia
capturada del hardware objetivo y en pruebas de regresión equivalentes.

Consulta [Arquitectura](docs/architecture/README.md) y [Protocolo](docs/protocol.md).

## Ejemplos de automatización conservadora

Usa automatizaciones que fallen de forma segura cuando la telemetría esté obsoleta
o no disponible, y evita control autónomo de cargas críticas.

Ejemplo 1: notificar cuando cae la telemetría en lugar de forzar escrituras.

```yaml
automation:
   - alias: allpowers_telemetry_unavailable
      triggers:
         - trigger: state
            entity_id: binary_sensor.allpowers_telemetry_available
            to: "off"
            for: "00:01:00"
      actions:
         - action: persistent_notification.create
            data:
               title: Telemetría ALLPOWERS no disponible
               message: Revisa ruta BLE, disponibilidad del proxy y estado de la estación.
```

Ejemplo 2: condicionar una activación no crítica de salida CA.

```yaml
automation:
   - alias: allpowers_enable_ac_non_critical
      triggers:
         - trigger: state
            entity_id: binary_sensor.allpowers_connected
            to: "on"
      conditions:
         - condition: state
            entity_id: binary_sensor.allpowers_telemetry_available
            state: "on"
         - condition: numeric_state
            entity_id: sensor.allpowers_battery
            above: 40
      actions:
         - action: switch.turn_on
            target:
               entity_id: switch.allpowers_ac_output
```

Prueba siempre estas automatizaciones manualmente con cargas no críticas antes de activarlas.

## Retirada y recuperación

Para retirar la integración limpiamente:

1. Desactiva o elimina automatizaciones que usen entidades ALLPOWERS.
2. Elimina la entrada de configuración ALLPOWERS BLE en Dispositivos y servicios.
3. Si instalaste con HACS, desinstala ALLPOWERS BLE en HACS y reinicia Home Assistant.
4. Si instalaste manualmente, borra `custom_components/allpowers_ble` y reinicia.
5. Comprueba que no quedan entidades ni dispositivos huérfanos y limpia helpers si hace falta.

Si planeas reinstalar, guarda antes un diagnóstico saneado para comparar historial
de ruta y detección de modelo tras la recuperación.

## Contrato de comunicación y clase IoT

El manifiesto declara `iot_class: local_polling` porque la operación normal
depende de solicitudes periódicas locales de estado y, al mismo tiempo,
consume notificaciones push.

- Ruta de polling: el cliente envía una solicitud de estado cada
   `status_interval` configurado.
- Ruta push: las notificaciones de estado y ajustes pueden llegar en cualquier
   momento y actualizan entidades sin esperar a la siguiente solicitud.
- Ruta de recuperación: el tráfico de watchdog y reconexión solo recupera la
   salud del transporte, no acelera el polling normal.

La base de evidencia y justificación de esta clasificación está trazada en la
issue #55 y en la decisión de arquitectura 0003.

Cada operación periódica tiene disparador y límite inferior explícitos:

| Operación periódica | Disparador | Valor inicial | Límite inferior |
|---|---|---:|---:|
| Solicitud de estado | Sesión conectada y sin solicitud en el intervalo | 20 s | 10 s |
| Keepalive de ajustes (opcional) | Activado y sin keepalive en el intervalo (más un envío inicial tras ajustes frescos) | 540 s | 60 s |
| Reintento de reconexión con backoff | Fallo de conexión o desconexión | 1 s inicial, tope 60 s | Piso de jitter 0 s, mínimo configurable del tope 5 s |
| Reconexión por watchdog de telemetría | Sin telemetría fresca en la ventana watchdog | 45 s | Debe ser mayor que stale timeout |
| Reconexión por watchdog de transporte | Sin paquetes BLE en la ventana watchdog | 45 s | Debe ser mayor que stale timeout |

Intervalos agresivos pueden saturar el adaptador local o la capacidad de
conexión del Bluetooth Proxy, especialmente con varios dispositivos. Mantén los
valores por defecto salvo que tengas una razón medida para ajustarlos.

## Opciones

| Opción | Valor inicial | Restricción |
|---|---:|---|
| Intervalo de solicitud de estado | 20 s | 10–120 s |
| Caducidad de telemetría | 30 s | Debe superar el intervalo de estado |
| Tiempo del watchdog de telemetría y transporte | 45 s | Debe superar la caducidad de telemetría |
| Retardo máximo de reconexión | 60 s | 5–300 s |
| Caducidad de ajustes | 600 s | 60–3600 s |
| Keepalive de ajustes | Desactivado | Experimental |
| Intervalo de keepalive | 540 s | 60–540 s; la caducidad de ajustes debe ser mayor |
| Cargador de coche | Desactivado | Experimental y requiere activación expresa |

Los cambios de opciones se aplican en caliente, sin recargar la integración.

## Diagnósticos

Los diagnósticos incluyen contadores de conexión, paquetes válidos, descartes del
parser, errores de protocolo, errores de escritura y watchdog (total, telemetría
y transporte), además de las últimas marcas temporales, estado en caché,
clasificación del modelo y opciones.
Los identificadores Bluetooth se redactan de forma recursiva en campos
estructurados y cadenas anidadas. Los nombres del dispositivo y el título de la
entrada se sustituyen por un marcador de redacción en los diagnósticos. El
campo `last_error` conserva la categoría del error con detalle saneado y se
publica en los diagnósticos. No se almacenan credenciales de nube.

Para activar registros de depuración de forma temporal:

```yaml
logger:
  logs:
    custom_components.allpowers_ble: debug
    bleak_retry_connector: debug
```

Consulta [Resolución de problemas](docs/troubleshooting.md).

## Desarrollo

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements_test.txt
pre-commit install
make all
```

Para construir el ZIP de una release HACS:

```bash
python scripts/build_release.py --clean
```

## Soporte y reportes

- Reporta fallos y compatibilidad de modelos con los formularios públicos de
   [GitHub Issues](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/new/choose)
- Reporta vulnerabilidades de forma privada mediante
   [GitHub Security Advisories](https://github.com/dedalodaelus/home-assistant-allpowers-ble/security/advisories/new)

El proyecto es una integración personalizada mantenida por la comunidad. No forma
parte de Home Assistant Core ni ha sido auditado o soportado por el proyecto Home
Assistant.

## Estado del proyecto

Las releases se promueven mediante pull requests revisadas desde `devel` hacia
`main`, con validación de CI, validación de repositorio y revisión de documentación
de seguridad antes de publicar. Las correcciones urgentes de producción pueden
apuntar a `main` solo desde ramas `hotfix/*` creadas desde `main`, y después deben
propagarse de vuelta a `devel`. Los objetivos de calidad describen evidencia y
pruebas actuales; no suponen un programa formal de certificación de Home Assistant.

No se permiten commits directos sobre `devel` ni sobre `main`. Las pull requests hacia `main` solo se permiten desde `devel` o desde ramas `hotfix/*` creadas desde `main`.

Licencia MIT. Consulta [LICENSE](LICENSE).
