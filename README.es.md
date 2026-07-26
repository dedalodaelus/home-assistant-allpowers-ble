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
- Escrituras read-modify-write que conservan bits no relacionados o desconocidos.
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

## Seguridad de las escrituras

El protocolo agrupa varios controles dentro de los mismos campos de bits. Para no
modificar accidentalmente otra salida, la integración aplica estas reglas:

1. Los cambios de CA, CC y luz requieren un estado reciente y envían un comando
   combinado que conserva las demás salidas.
2. ECO, modo de trabajo, cargador de coche y temporizador requieren ajustes
   recientes y conservan todos los bits no relacionados.
3. Cada escritura abre una transacción versionada y espera una confirmación en
   notificación antes de reutilizar ese estado para otra escritura.
4. Las transacciones pendientes y la frescura se invalidan al desconectar o
   iniciar otra sesión GATT.
5. Si no existe una instantánea segura, la escritura se rechaza en vez de adivinar.

Consulta [Arquitectura](docs/architecture.md) y [Protocolo](docs/protocol.md).

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
campo `last_error` conserva la categoría del error con detalle saneado. No se
almacenan credenciales de nube.

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

Licencia MIT. Consulta [LICENSE](LICENSE).
