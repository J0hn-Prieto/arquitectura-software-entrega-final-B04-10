"""Servicio externo de pagos simulado.

Representa la pasarela: un servicio que no controlamos, que puede rechazar la
operación o no responder a tiempo. Se incluyen el tiempo de espera y el
reintento con espera creciente, y el interruptor de circuito que evita seguir
llamando a un servicio que ya acumula fallos.
"""
import time
import uuid
from dataclasses import dataclass, field


class PagoRechazado(Exception):
    """La pasarela respondió, pero no autorizó el cobro."""


class PasarelaNoDisponible(Exception):
    """La pasarela no respondió dentro del tiempo de espera."""


class CircuitoAbierto(Exception):
    """El interruptor está abierto: no se invoca la pasarela."""


@dataclass
class InterruptorCircuito:
    """Implementación mínima del patrón Circuit Breaker."""

    umbral_fallos: int = 3
    espera_segundos: float = 30.0
    fallos: int = 0
    abierto_desde: float | None = None

    def permitir(self) -> bool:
        if self.abierto_desde is None:
            return True
        if time.monotonic() - self.abierto_desde >= self.espera_segundos:
            # Estado semiabierto: se deja pasar un intento de prueba.
            self.abierto_desde = None
            self.fallos = self.umbral_fallos - 1
            return True
        return False

    def registrar_exito(self) -> None:
        self.fallos = 0
        self.abierto_desde = None

    def registrar_fallo(self) -> None:
        self.fallos += 1
        if self.fallos >= self.umbral_fallos:
            self.abierto_desde = time.monotonic()

    @property
    def estado(self) -> str:
        if self.abierto_desde is not None:
            return "ABIERTO"
        return "CERRADO" if self.fallos == 0 else "CERRADO (con fallos)"


@dataclass
class Pasarela:
    interruptor: InterruptorCircuito = field(default_factory=InterruptorCircuito)
    # Cobros ya procesados: clave de idempotencia -> id del pago.
    procesados: dict[str, str] = field(default_factory=dict)
    reversados: set[str] = field(default_factory=set)
    intentos_por_llamada: int = 3
    espera_base: float = 0.05

    def autorizar(self, medio_pago: str, valor: int, clave_idempotencia: str) -> str:
        """T3 de la saga. Devuelve el identificador del pago.

        El token del medio de pago decide el comportamiento, para poder
        demostrar cada escenario sin depender de un servicio real:
        'tok-rechazo' rechaza el cobro y 'tok-timeout' simula indisponibilidad.
        """
        if clave_idempotencia in self.procesados:
            # Recepción idempotente: el reintento no genera un segundo cobro.
            return self.procesados[clave_idempotencia]

        if not self.interruptor.permitir():
            raise CircuitoAbierto(
                "El interruptor de circuito está abierto para el servicio de pagos."
            )

        ultimo_error: Exception | None = None
        for intento in range(1, self.intentos_por_llamada + 1):
            try:
                pago_id = self._invocar(medio_pago, valor)
            except PasarelaNoDisponible as exc:
                # Fallo transitorio: se reintenta con espera creciente.
                ultimo_error = exc
                self.interruptor.registrar_fallo()
                if intento < self.intentos_por_llamada:
                    time.sleep(self.espera_base * (2 ** (intento - 1)))
                continue
            except PagoRechazado:
                # No es transitorio: reintentar no cambia el resultado.
                self.interruptor.registrar_exito()
                raise
            else:
                self.interruptor.registrar_exito()
                self.procesados[clave_idempotencia] = pago_id
                return pago_id

        raise PasarelaNoDisponible(str(ultimo_error))

    def reversar(self, pago_id: str) -> None:
        """C3: compensación del cobro. Idempotente."""
        self.reversados.add(pago_id)

    # ------------------------------------------------------------- simulación
    def _invocar(self, medio_pago: str, valor: int) -> str:
        if medio_pago == "tok-rechazo":
            raise PagoRechazado(f"Fondos insuficientes para un cobro de {valor}.")
        if medio_pago == "tok-timeout":
            raise PasarelaNoDisponible("La pasarela no respondió a tiempo.")
        return f"pago-{uuid.uuid4().hex[:8]}"


pasarela = Pasarela()
