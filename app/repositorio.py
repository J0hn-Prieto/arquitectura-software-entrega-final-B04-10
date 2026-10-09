"""Almacenamiento en memoria.

Hace las veces de las bases de datos de los servicios de agenda y de reservas.
Lo importante para el análisis no es la tecnología de persistencia, sino que
cada escritura sobre un cupo verifique la versión leída por el cliente.
"""
from datetime import datetime, timedelta
from threading import Lock
from typing import Optional

from app.modelos import Cupo, Profesional, Reserva


class ConflictoDeVersion(Exception):
    """El cupo cambió entre la lectura y la escritura (bloqueo optimista)."""


class CupoNoDisponible(Exception):
    """El cupo ya fue tomado por otra reserva."""


class Repositorio:
    def __init__(self) -> None:
        self._lock = Lock()
        self.profesionales: dict[str, Profesional] = {}
        self.cupos: dict[str, Cupo] = {}
        self.reservas: dict[str, Reserva] = {}
        # Claves de idempotencia ya procesadas -> id de la reserva creada.
        self.idempotencia: dict[str, str] = {}
        self.cargar_datos_iniciales()

    # ------------------------------------------------------------------ datos
    def cargar_datos_iniciales(self) -> None:
        self.profesionales.clear()
        self.cupos.clear()
        self.reservas.clear()
        self.idempotencia.clear()

        datos = [
            ("prof-001", "Laura Mejía", "dermatologia", 60000),
            ("prof-002", "Andrés Patiño", "medicina general", 45000),
            ("prof-003", "Carolina Ruiz", "pediatria", 70000),
        ]
        for pid, nombre, especialidad, valor in datos:
            self.profesionales[pid] = Profesional(
                id=pid, nombre=nombre, especialidad=especialidad, valor_consulta=valor
            )

        base = datetime(2026, 10, 15, 8, 0)
        contador = 1
        for pid in self.profesionales:
            for hora in range(4):
                cid = f"cupo-{contador:03d}"
                self.cupos[cid] = Cupo(
                    id=cid,
                    profesional_id=pid,
                    inicio=base + timedelta(hours=hora),
                    disponible=True,
                    version=1,
                )
                contador += 1

    # --------------------------------------------------------------- consultas
    def listar_profesionales(self, especialidad: Optional[str] = None) -> list[Profesional]:
        valores = list(self.profesionales.values())
        if especialidad:
            objetivo = especialidad.strip().lower()
            valores = [p for p in valores if p.especialidad.lower() == objetivo]
        return valores

    def listar_cupos(self, profesional_id: str, solo_disponibles: bool = True) -> list[Cupo]:
        cupos = [c for c in self.cupos.values() if c.profesional_id == profesional_id]
        if solo_disponibles:
            cupos = [c for c in cupos if c.disponible]
        return sorted(cupos, key=lambda c: c.inicio)

    # ---------------------------------------------------------- bloqueo de cupo
    def bloquear_cupo(self, cupo_id: str, version_leida: int) -> Cupo:
        """T1 de la saga: toma el cupo solo si nadie lo modificó antes.

        Esta comprobación es el bloqueo optimista: si la versión que trae el
        cliente ya no es la vigente, la operación se rechaza en lugar de
        sobrescribir la reserva de otro paciente.
        """
        with self._lock:
            cupo = self.cupos.get(cupo_id)
            if cupo is None:
                raise KeyError(cupo_id)
            if cupo.version != version_leida:
                raise ConflictoDeVersion(
                    f"El cupo {cupo_id} está en la versión {cupo.version}, "
                    f"no en la {version_leida}."
                )
            if not cupo.disponible:
                raise CupoNoDisponible(cupo_id)
            cupo.disponible = False
            cupo.version += 1
            return cupo

    def liberar_cupo(self, cupo_id: str) -> None:
        """C1: compensación de bloquear_cupo. Es idempotente."""
        with self._lock:
            cupo = self.cupos.get(cupo_id)
            if cupo is None or cupo.disponible:
                return
            cupo.disponible = True
            cupo.version += 1


repositorio = Repositorio()
