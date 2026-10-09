"""Esquema canónico de datos.

Define una sola representación de profesional, disponibilidad, reserva y pago
para todos los servicios, de modo que no sea necesario transformar los mensajes
en cada integración.
"""
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class EstadoReserva(str, Enum):
    """Estados por los que pasa una reserva dentro de la saga."""

    PENDIENTE = "PENDIENTE"          # horario bloqueado, pago aún no autorizado
    CONFIRMADA = "CONFIRMADA"        # pago autorizado y cita confirmada
    CANCELADA = "CANCELADA"          # cancelada por el paciente, con compensación
    COMPENSADA = "COMPENSADA"        # un paso falló y se revirtieron los anteriores


class Profesional(BaseModel):
    id: str = Field(..., examples=["prof-001"])
    nombre: str = Field(..., examples=["Laura Mejía"])
    especialidad: str = Field(..., examples=["dermatologia"])
    valor_consulta: int = Field(..., description="Valor en pesos colombianos", examples=[60000])


class Cupo(BaseModel):
    id: str = Field(..., examples=["cupo-001"])
    profesional_id: str = Field(..., examples=["prof-001"])
    inicio: datetime = Field(..., examples=["2026-10-15T10:00:00"])
    disponible: bool = True
    version: int = Field(
        1, description="Número de versión usado para el bloqueo optimista."
    )


class SolicitudReserva(BaseModel):
    """Cuerpo de la petición POST /api/v1/reservas."""

    paciente_id: str = Field(..., examples=["pac-001"])
    cupo_id: str = Field(..., examples=["cupo-001"])
    cupo_version: int = Field(
        ...,
        description=(
            "Versión del cupo que el cliente leyó. Si cambió, el servicio "
            "rechaza la operación con 409 (bloqueo optimista)."
        ),
        examples=[1],
    )
    medio_pago: str = Field(
        ...,
        description=(
            "Token del medio de pago. Los valores 'tok-rechazo' y 'tok-timeout' "
            "simulan un rechazo y una caída de la pasarela."
        ),
        examples=["tok-ok"],
    )


class PasoSaga(BaseModel):
    """Traza de un paso de la saga, guardada para poder compensar y auditar."""

    paso: str
    estado: str
    detalle: Optional[str] = None
    momento: datetime


class Reserva(BaseModel):
    id: str
    paciente_id: str
    cupo_id: str
    profesional_id: str
    inicio: datetime
    valor: int
    estado: EstadoReserva
    id_correlacion: str = Field(
        ..., description="Identificador que relaciona todos los pasos de la operación."
    )
    pago_id: Optional[str] = None
    traza: list[PasoSaga] = []


class Error(BaseModel):
    """Representación uniforme de los errores en todos los recursos."""

    codigo: str = Field(..., examples=["CUPO_NO_DISPONIBLE"])
    mensaje: str
    id_correlacion: Optional[str] = None
