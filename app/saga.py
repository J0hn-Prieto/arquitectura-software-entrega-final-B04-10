"""Orquestador de la saga de reserva.

Implementa el patrón Compensating Service Transaction (Erl, 2009): la reserva
se descompone en transacciones locales y, si un paso falla de forma definitiva,
se ejecutan en orden inverso las compensaciones de los pasos ya realizados.

    T1 bloquear el cupo        C1 liberar el cupo
    T2 registrar la reserva    C2 anular la reserva
    T3 autorizar el cobro      C3 reversar el cobro
    T4 confirmar la cita       -- punto sin retorno
    T5 notificar al paciente   -- se encola, no bloquea la operación
"""
import uuid
from datetime import datetime
from typing import Callable

from app import notificaciones
from app.modelos import EstadoReserva, PasoSaga, Reserva
from app.pasarela import (
    CircuitoAbierto,
    PagoRechazado,
    PasarelaNoDisponible,
    pasarela,
)
from app.repositorio import (
    ConflictoDeVersion,
    CupoNoDisponible,
    repositorio,
)


class ErrorSaga(Exception):
    """Fallo controlado de la saga, con el código que se devuelve al cliente."""

    def __init__(self, codigo: str, mensaje: str, http: int, id_correlacion: str):
        super().__init__(mensaje)
        self.codigo = codigo
        self.mensaje = mensaje
        self.http = http
        self.id_correlacion = id_correlacion


def _anotar(reserva: Reserva, paso: str, estado: str, detalle: str | None = None) -> None:
    reserva.traza.append(
        PasoSaga(paso=paso, estado=estado, detalle=detalle, momento=datetime.now())
    )


def crear_reserva(
    paciente_id: str,
    cupo_id: str,
    cupo_version: int,
    medio_pago: str,
    clave_idempotencia: str | None = None,
) -> Reserva:
    id_correlacion = uuid.uuid4().hex[:12]

    # Recepción idempotente: la misma clave devuelve la reserva ya creada.
    if clave_idempotencia and clave_idempotencia in repositorio.idempotencia:
        return repositorio.reservas[repositorio.idempotencia[clave_idempotencia]]

    cupo = repositorio.cupos.get(cupo_id)
    if cupo is None:
        raise ErrorSaga("CUPO_NO_EXISTE", f"No existe el cupo {cupo_id}.", 404, id_correlacion)
    profesional = repositorio.profesionales[cupo.profesional_id]

    compensaciones: list[tuple[str, Callable[[], None]]] = []

    # ----------------------------------------------------------- T1: bloquear
    try:
        cupo = repositorio.bloquear_cupo(cupo_id, cupo_version)
    except ConflictoDeVersion as exc:
        raise ErrorSaga("CONFLICTO_DE_VERSION", str(exc), 409, id_correlacion) from exc
    except CupoNoDisponible as exc:
        raise ErrorSaga(
            "CUPO_NO_DISPONIBLE", f"El cupo {cupo_id} ya fue reservado.", 409, id_correlacion
        ) from exc

    reserva = Reserva(
        id=f"res-{uuid.uuid4().hex[:8]}",
        paciente_id=paciente_id,
        cupo_id=cupo_id,
        profesional_id=cupo.profesional_id,
        inicio=cupo.inicio,
        valor=profesional.valor_consulta,
        estado=EstadoReserva.PENDIENTE,
        id_correlacion=id_correlacion,
        traza=[],
    )
    _anotar(reserva, "T1 bloquear cupo", "OK", f"versión {cupo.version}")
    compensaciones.append(("C1 liberar cupo", lambda: repositorio.liberar_cupo(cupo_id)))

    # ---------------------------------------------------------- T2: registrar
    repositorio.reservas[reserva.id] = reserva
    _anotar(reserva, "T2 registrar reserva", "OK", "estado PENDIENTE")

    def anular_reserva() -> None:
        reserva.estado = EstadoReserva.COMPENSADA

    compensaciones.append(("C2 anular reserva", anular_reserva))

    # ------------------------------------------------------------- T3: cobrar
    clave_pago = clave_idempotencia or reserva.id
    try:
        pago_id = pasarela.autorizar(medio_pago, reserva.valor, clave_pago)
    except (PagoRechazado, PasarelaNoDisponible, CircuitoAbierto) as exc:
        _anotar(reserva, "T3 autorizar cobro", "FALLO", str(exc))
        _compensar(reserva, compensaciones)
        codigo = {
            PagoRechazado: "PAGO_RECHAZADO",
            PasarelaNoDisponible: "PASARELA_NO_DISPONIBLE",
            CircuitoAbierto: "CIRCUITO_ABIERTO",
        }[type(exc)]
        http = 402 if isinstance(exc, PagoRechazado) else 503
        notificaciones.cola.encolar(
            paciente_id,
            "No fue posible completar tu reserva",
            "El horario quedó disponible nuevamente. Puedes intentar con otro medio de pago.",
            id_correlacion,
        )
        raise ErrorSaga(codigo, str(exc), http, id_correlacion) from exc

    reserva.pago_id = pago_id
    _anotar(reserva, "T3 autorizar cobro", "OK", pago_id)
    compensaciones.append(("C3 reversar cobro", lambda: pasarela.reversar(pago_id)))

    # --------------------------------------------- T4: confirmar (punto pivote)
    reserva.estado = EstadoReserva.CONFIRMADA
    _anotar(reserva, "T4 confirmar cita", "OK", "punto sin retorno")

    # ----------------------------------------------------------- T5: notificar
    notificaciones.cola.encolar(
        paciente_id,
        "Tu cita quedó confirmada",
        f"Cita con {profesional.nombre} el {reserva.inicio:%d/%m/%Y a las %H:%M}.",
        id_correlacion,
    )
    _anotar(reserva, "T5 notificar", "ENCOLADO", "entrega asíncrona")

    if clave_idempotencia:
        repositorio.idempotencia[clave_idempotencia] = reserva.id
    return reserva


def cancelar_reserva(reserva_id: str) -> Reserva:
    """Cancelación solicitada por el paciente: ejecuta las compensaciones."""
    reserva = repositorio.reservas.get(reserva_id)
    if reserva is None:
        raise ErrorSaga("RESERVA_NO_EXISTE", f"No existe la reserva {reserva_id}.", 404, "-")
    if reserva.estado in (EstadoReserva.CANCELADA, EstadoReserva.COMPENSADA):
        return reserva

    if reserva.pago_id:
        pasarela.reversar(reserva.pago_id)
        _anotar(reserva, "C3 reversar cobro", "OK", reserva.pago_id)
    repositorio.liberar_cupo(reserva.cupo_id)
    _anotar(reserva, "C1 liberar cupo", "OK")
    reserva.estado = EstadoReserva.CANCELADA
    notificaciones.cola.encolar(
        reserva.paciente_id,
        "Tu cita fue cancelada",
        "Se reversó el cobro y el horario quedó disponible.",
        reserva.id_correlacion,
    )
    return reserva


def _compensar(reserva: Reserva, compensaciones: list[tuple[str, Callable[[], None]]]) -> None:
    """Ejecuta en orden inverso las compensaciones de los pasos cumplidos."""
    for nombre, accion in reversed(compensaciones):
        try:
            accion()
            _anotar(reserva, nombre, "OK")
        except Exception as exc:  # la compensación también puede fallar
            _anotar(reserva, nombre, "FALLO", str(exc))
    reserva.estado = EstadoReserva.COMPENSADA
