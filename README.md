# Servicio REST de reservas de citas de salud

Implementación del patrón **Compensating Service Transaction** (Erl, 2009) sobre una interfaz REST,
desarrollada como entrega final del módulo **Arquitectura de Software** del Politécnico Grancolombiano.

**Grupo B04-10:** Ana Minu Cedeño · John Alexander Prieto Leiva · Luis Alejandro Góngora Arias · Samuel Eduardo Palacios Solís
**Docente:** Jesús Antonio Figueroa Guerrero

---

## Qué resuelve

El análisis de las entregas anteriores identificó cuatro puntos de fallo en una plataforma de reserva
de citas de salud. Este servicio los atiende con mecanismos concretos:

| Punto de fallo | Mecanismo implementado | Dónde está en el código |
|---|---|---|
| Dos pacientes reservan el mismo horario | Bloqueo optimista con número de versión | `app/repositorio.py` → `bloquear_cupo` |
| La reserva queda sin pago o el pago sin cita | Saga con transacciones locales y compensaciones | `app/saga.py` |
| Un reintento genera un doble cobro | Recepción idempotente con `Idempotency-Key` | `app/saga.py`, `app/pasarela.py` |
| La pasarela lenta arrastra a toda la plataforma | Tiempo de espera, reintento y interruptor de circuito | `app/pasarela.py` |
| Las confirmaciones se pierden | Cola de notificaciones fuera del flujo de la reserva | `app/notificaciones.py` |
| No se puede rastrear dónde falló una reserva | Identificador de correlación y traza de la saga | `app/saga.py`, cabecera `X-Id-Correlacion` |

## La saga

```
T1 bloquear el cupo        ──►  C1 liberar el cupo
T2 registrar la reserva    ──►  C2 anular la reserva
T3 autorizar el cobro      ──►  C3 reversar el cobro
T4 confirmar la cita            (punto sin retorno)
T5 notificar al paciente        (asíncrono, por cola)
```

Si un paso falla de forma definitiva, las compensaciones se ejecutan **en orden inverso**.

---

## Cómo ejecutarlo

Requiere Python 3.10 o superior.

```bash
git clone <URL-DEL-REPOSITORIO>
cd plataforma-citas-soa
python -m venv .venv
```

Activar el entorno virtual:

```bash
.venv\Scripts\activate
```

En Linux o macOS: `source .venv/bin/activate`

Instalar las dependencias y levantar el servicio:

```bash
pip install -r requirements.txt
```

```bash
uvicorn app.main:app --reload
```

El servicio queda en `http://127.0.0.1:8000`. La documentación interactiva (Swagger UI), generada
automáticamente a partir del contrato, está en `http://127.0.0.1:8000/docs`.

## Pruebas automáticas

```bash
python -m pytest -v
```

Las nueve pruebas de `tests/test_saga.py` corresponden una a una a los escenarios del análisis:
reserva exitosa, doble reserva rechazada, pago rechazado con compensación, pasarela caída,
interruptor de circuito abierto, reintento idempotente, cancelación con reversa y entrega de
notificaciones.

## Demostración de los escenarios

```bash
python demo.py
```

Ejecuta los cinco escenarios en orden e imprime la traza de cada saga.

---

## Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/v1/profesionales` | Busca profesionales, con filtro `?especialidad=` |
| `GET` | `/api/v1/profesionales/{id}/disponibilidad` | Cupos libres con su número de versión |
| `POST` | `/api/v1/reservas` | Ejecuta la saga de reserva |
| `GET` | `/api/v1/reservas/{id}` | Reserva con la traza completa de la saga |
| `DELETE` | `/api/v1/reservas/{id}` | Cancela: reversa el cobro y libera el cupo |
| `GET` | `/api/v1/estado` | Estado del interruptor y de la cola (monitoreo) |
| `POST` | `/api/v1/notificaciones/procesar` | Vacía la cola de notificaciones |

### Códigos de respuesta

| Código | Significado |
|---|---|
| `201` | Reserva creada y confirmada |
| `402` | Pago rechazado. La reserva se compensó y el horario quedó libre |
| `409` | El cupo cambió de versión o ya fue tomado |
| `503` | La pasarela no respondió o el interruptor está abierto |

### Ejemplo de petición

```bash
curl -X POST http://127.0.0.1:8000/api/v1/reservas -H "Content-Type: application/json" -H "Idempotency-Key: clave-123" -d "{\"paciente_id\":\"pac-001\",\"cupo_id\":\"cupo-001\",\"cupo_version\":1,\"medio_pago\":\"tok-ok\"}"
```

### Medios de pago para probar

| Token | Comportamiento |
|---|---|
| `tok-ok` | El cobro se autoriza |
| `tok-rechazo` | La pasarela rechaza el cobro → respuesta `402` y compensación |
| `tok-timeout` | La pasarela no responde → reintentos, `503` y compensación |

---

## Estructura del proyecto

```
plataforma-citas-soa/
├── app/
│   ├── main.py            API REST: recursos, códigos de respuesta y contrato
│   ├── modelos.py         Esquema canónico de datos
│   ├── saga.py            Orquestador: transacciones locales y compensaciones
│   ├── repositorio.py     Persistencia en memoria y bloqueo optimista
│   ├── pasarela.py        Pasarela simulada, reintento e interruptor de circuito
│   └── notificaciones.py  Cola de mensajes asíncrona
├── tests/
│   └── test_saga.py       Pruebas de los escenarios de fallo
├── evidencias/            Capturas y salidas de ejecución
├── demo.py                Demostración guiada de los escenarios
├── requirements.txt
└── README.md
```

## Alcance

Es un prototipo académico: la persistencia es en memoria y la pasarela de pagos está simulada,
de modo que los escenarios de fallo puedan reproducirse sin depender de un servicio externo.
La autenticación, el registro de servicios y el despliegue quedan fuera del alcance y se analizan
en el documento de la entrega.

## Referencias

- Erl, T. (2009). *SOA design patterns*. Prentice Hall.
- Fielding, R. T. (2000). *Architectural styles and the design of network-based software architectures* [Tesis doctoral]. University of California, Irvine.
- Garcia-Molina, H. y Salem, K. (1987). Sagas. *ACM SIGMOD Record, 16*(3), 249–259.
- Hohpe, G. y Woolf, B. (2003). *Enterprise integration patterns*. Addison-Wesley.
- Nygard, M. T. (2018). *Release it!* (2.ª ed.). Pragmatic Bookshelf.
