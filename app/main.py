"""Servicio REST de reservas de citas de salud.

Expone como recursos los pasos de la saga descrita en el análisis. Cada
operación responde con códigos HTTP que distinguen el conflicto de reserva,
el pago rechazado y la indisponibilidad del servicio externo.
"""
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from app import notificaciones, saga
from app.modelos import Cupo, Error, Profesional, Reserva, SolicitudReserva
from app.pasarela import pasarela
from app.repositorio import repositorio

app = FastAPI(
    title="Servicio de reservas de citas de salud",
    version="1.0.0",
    description=(
        "Implementación del patrón Compensating Service Transaction sobre una "
        "interfaz REST. Entrega final del módulo Arquitectura de Software, "
        "Politécnico Grancolombiano, grupo B04-10."
    ),
    openapi_tags=[
        {"name": "Profesionales", "description": "Búsqueda de profesionales y disponibilidad."},
        {"name": "Reservas", "description": "Saga de reserva, consulta y cancelación."},
        {"name": "Operación", "description": "Estado interno para monitoreo y evidencias."},
    ],
)


@app.exception_handler(saga.ErrorSaga)
async def manejar_error_saga(_, exc: saga.ErrorSaga) -> JSONResponse:
    """Traduce los fallos de la saga al formato de error del contrato."""
    cuerpo = Error(codigo=exc.codigo, mensaje=exc.mensaje, id_correlacion=exc.id_correlacion)
    return JSONResponse(
        status_code=exc.http,
        content=cuerpo.model_dump(),
        headers={"X-Id-Correlacion": exc.id_correlacion},
    )


# --------------------------------------------------------------- profesionales
@app.get("/api/v1/profesionales", response_model=list[Profesional], tags=["Profesionales"])
def buscar_profesionales(
    especialidad: Optional[str] = Query(
        None, description="Filtra por especialidad, por ejemplo 'dermatologia'."
    )
) -> list[Profesional]:
    return repositorio.listar_profesionales(especialidad)


@app.get(
    "/api/v1/profesionales/{profesional_id}/disponibilidad",
    response_model=list[Cupo],
    tags=["Profesionales"],
    responses={404: {"model": Error}},
)
def consultar_disponibilidad(profesional_id: str) -> list[Cupo]:
    """Devuelve los cupos libres junto con su versión.

    El cliente debe enviar esa versión al reservar: así el servicio detecta si
    alguien tomó el horario entre la consulta y la reserva.
    """
    if profesional_id not in repositorio.profesionales:
        raise HTTPException(status_code=404, detail="El profesional no existe.")
    return repositorio.listar_cupos(profesional_id)


# -------------------------------------------------------------------- reservas
@app.post(
    "/api/v1/reservas",
    response_model=Reserva,
    status_code=201,
    tags=["Reservas"],
    responses={
        402: {"model": Error, "description": "El pago fue rechazado; la reserva se compensó."},
        409: {"model": Error, "description": "El cupo cambió o ya fue tomado."},
        503: {"model": Error, "description": "El servicio de pagos no está disponible."},
    },
)
def crear_reserva(
    solicitud: SolicitudReserva,
    response: Response,
    idempotency_key: Optional[str] = Header(
        None,
        alias="Idempotency-Key",
        description="Clave que evita que un reintento genere una segunda reserva o un doble cobro.",
    ),
) -> Reserva:
    reserva = saga.crear_reserva(
        paciente_id=solicitud.paciente_id,
        cupo_id=solicitud.cupo_id,
        cupo_version=solicitud.cupo_version,
        medio_pago=solicitud.medio_pago,
        clave_idempotencia=idempotency_key,
    )
    response.headers["X-Id-Correlacion"] = reserva.id_correlacion
    return reserva


@app.get(
    "/api/v1/reservas/{reserva_id}",
    response_model=Reserva,
    tags=["Reservas"],
    responses={404: {"model": Error}},
)
def consultar_reserva(reserva_id: str) -> Reserva:
    """Devuelve la reserva con la traza completa de la saga."""
    reserva = repositorio.reservas.get(reserva_id)
    if reserva is None:
        raise HTTPException(status_code=404, detail="La reserva no existe.")
    return reserva


@app.delete(
    "/api/v1/reservas/{reserva_id}",
    response_model=Reserva,
    tags=["Reservas"],
    responses={404: {"model": Error}},
)
def cancelar_reserva(reserva_id: str) -> Reserva:
    """Cancela la cita: reversa el cobro y libera el horario."""
    return saga.cancelar_reserva(reserva_id)


# -------------------------------------------------------------------- operación
@app.get("/api/v1/estado", tags=["Operación"])
def estado_operativo() -> dict:
    """Evidencia del interruptor de circuito y de la cola de notificaciones."""
    return {
        "interruptor_pagos": pasarela.interruptor.estado,
        "fallos_consecutivos": pasarela.interruptor.fallos,
        "notificaciones_pendientes": len(notificaciones.cola.pendientes),
        "notificaciones_entregadas": len(notificaciones.cola.enviados),
        "cupos_disponibles": sum(1 for c in repositorio.cupos.values() if c.disponible),
        "reservas": len(repositorio.reservas),
    }


@app.post("/api/v1/notificaciones/procesar", tags=["Operación"])
def procesar_notificaciones() -> dict:
    """Simula el trabajador que vacía la cola de mensajes."""
    return {"entregadas": notificaciones.cola.procesar()}
