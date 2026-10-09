"""Servicio de notificaciones desacoplado por cola.

El envío no se hace dentro del flujo de la reserva: el mensaje se deja en una
cola y se procesa aparte. Así, si el proveedor de mensajería no responde, la
reserva no se ve afectada y la confirmación no se pierde.
"""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Mensaje:
    destinatario: str
    asunto: str
    cuerpo: str
    id_correlacion: str
    creado: datetime = field(default_factory=datetime.now)
    entregado: bool = False


@dataclass
class ColaNotificaciones:
    pendientes: deque[Mensaje] = field(default_factory=deque)
    enviados: list[Mensaje] = field(default_factory=list)

    def encolar(self, destinatario: str, asunto: str, cuerpo: str, id_correlacion: str) -> Mensaje:
        mensaje = Mensaje(destinatario, asunto, cuerpo, id_correlacion)
        self.pendientes.append(mensaje)
        return mensaje

    def procesar(self) -> int:
        """Entrega los mensajes pendientes. Lo ejecutaría un trabajador aparte."""
        procesados = 0
        while self.pendientes:
            mensaje = self.pendientes.popleft()
            mensaje.entregado = True
            self.enviados.append(mensaje)
            procesados += 1
        return procesados


cola = ColaNotificaciones()
