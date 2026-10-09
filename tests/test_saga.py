"""Pruebas de los escenarios de fallo descritos en el análisis.

Cada prueba corresponde a un punto de fallo identificado en la situación
problema, de modo que el código sirva como evidencia del análisis.
"""
import pytest
from fastapi.testclient import TestClient

from app import notificaciones
from app.main import app
from app.pasarela import InterruptorCircuito, pasarela
from app.repositorio import repositorio

client = TestClient(app)


@pytest.fixture(autouse=True)
def reiniciar_estado():
    """Deja el sistema en su estado inicial antes de cada prueba."""
    repositorio.cargar_datos_iniciales()
    pasarela.procesados.clear()
    pasarela.reversados.clear()
    pasarela.interruptor = InterruptorCircuito()
    notificaciones.cola.pendientes.clear()
    notificaciones.cola.enviados.clear()
    yield


def _primer_cupo(profesional_id: str = "prof-001") -> dict:
    respuesta = client.get(f"/api/v1/profesionales/{profesional_id}/disponibilidad")
    assert respuesta.status_code == 200
    return respuesta.json()[0]


def test_busqueda_por_especialidad():
    respuesta = client.get("/api/v1/profesionales", params={"especialidad": "dermatologia"})
    assert respuesta.status_code == 200
    assert [p["nombre"] for p in respuesta.json()] == ["Laura Mejía"]


def test_reserva_exitosa_recorre_toda_la_saga():
    cupo = _primer_cupo()
    respuesta = client.post(
        "/api/v1/reservas",
        json={
            "paciente_id": "pac-001",
            "cupo_id": cupo["id"],
            "cupo_version": cupo["version"],
            "medio_pago": "tok-ok",
        },
    )
    assert respuesta.status_code == 201
    reserva = respuesta.json()
    assert reserva["estado"] == "CONFIRMADA"
    assert reserva["pago_id"]
    assert [p["paso"] for p in reserva["traza"]] == [
        "T1 bloquear cupo",
        "T2 registrar reserva",
        "T3 autorizar cobro",
        "T4 confirmar cita",
        "T5 notificar",
    ]
    assert respuesta.headers["X-Id-Correlacion"] == reserva["id_correlacion"]


def test_doble_reserva_del_mismo_horario_se_rechaza():
    """Bloqueo optimista: el segundo paciente recibe 409 y no obtiene la cita."""
    cupo = _primer_cupo()
    cuerpo = {
        "paciente_id": "pac-001",
        "cupo_id": cupo["id"],
        "cupo_version": cupo["version"],
        "medio_pago": "tok-ok",
    }
    primera = client.post("/api/v1/reservas", json=cuerpo)
    segunda = client.post("/api/v1/reservas", json={**cuerpo, "paciente_id": "pac-002"})

    assert primera.status_code == 201
    assert segunda.status_code == 409
    assert segunda.json()["codigo"] == "CONFLICTO_DE_VERSION"


def test_pago_rechazado_compensa_y_libera_el_horario():
    """El fallo en T3 dispara C2 y C1: no queda una reserva sin pago."""
    cupo = _primer_cupo()
    respuesta = client.post(
        "/api/v1/reservas",
        json={
            "paciente_id": "pac-001",
            "cupo_id": cupo["id"],
            "cupo_version": cupo["version"],
            "medio_pago": "tok-rechazo",
        },
    )
    assert respuesta.status_code == 402
    assert respuesta.json()["codigo"] == "PAGO_RECHAZADO"

    # El cupo vuelve a estar disponible para otros pacientes.
    disponibles = [c["id"] for c in _recargar_disponibilidad("prof-001")]
    assert cupo["id"] in disponibles

    # La reserva quedó marcada como compensada, con la traza de las reversas.
    reserva = list(repositorio.reservas.values())[0]
    assert reserva.estado.value == "COMPENSADA"
    assert [p.paso for p in reserva.traza][-2:] == ["C2 anular reserva", "C1 liberar cupo"]


def test_pasarela_caida_responde_503_y_compensa():
    cupo = _primer_cupo()
    respuesta = client.post(
        "/api/v1/reservas",
        json={
            "paciente_id": "pac-001",
            "cupo_id": cupo["id"],
            "cupo_version": cupo["version"],
            "medio_pago": "tok-timeout",
        },
    )
    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "PASARELA_NO_DISPONIBLE"
    assert cupo["id"] in [c["id"] for c in _recargar_disponibilidad("prof-001")]


def test_interruptor_de_circuito_se_abre_tras_fallos_consecutivos():
    """Tras varios fallos deja de invocarse la pasarela: se corta la cascada."""
    for cupo in _recargar_disponibilidad("prof-001")[:3]:
        client.post(
            "/api/v1/reservas",
            json={
                "paciente_id": "pac-001",
                "cupo_id": cupo["id"],
                "cupo_version": cupo["version"],
                "medio_pago": "tok-timeout",
            },
        )
    estado = client.get("/api/v1/estado").json()
    assert estado["interruptor_pagos"] == "ABIERTO"

    cupo = _recargar_disponibilidad("prof-002")[0]
    respuesta = client.post(
        "/api/v1/reservas",
        json={
            "paciente_id": "pac-003",
            "cupo_id": cupo["id"],
            "cupo_version": cupo["version"],
            "medio_pago": "tok-ok",
        },
    )
    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "CIRCUITO_ABIERTO"


def test_reintento_con_la_misma_clave_no_genera_doble_cobro():
    """Recepción idempotente: dos peticiones iguales producen una sola reserva."""
    cupo = _primer_cupo()
    cuerpo = {
        "paciente_id": "pac-001",
        "cupo_id": cupo["id"],
        "cupo_version": cupo["version"],
        "medio_pago": "tok-ok",
    }
    cabeceras = {"Idempotency-Key": "clave-unica-123"}
    primera = client.post("/api/v1/reservas", json=cuerpo, headers=cabeceras)
    segunda = client.post("/api/v1/reservas", json=cuerpo, headers=cabeceras)

    assert primera.status_code == 201
    assert segunda.json()["id"] == primera.json()["id"]
    assert segunda.json()["pago_id"] == primera.json()["pago_id"]
    assert len(repositorio.reservas) == 1
    assert len(pasarela.procesados) == 1


def test_cancelacion_reversa_el_cobro_y_libera_el_cupo():
    cupo = _primer_cupo()
    reserva = client.post(
        "/api/v1/reservas",
        json={
            "paciente_id": "pac-001",
            "cupo_id": cupo["id"],
            "cupo_version": cupo["version"],
            "medio_pago": "tok-ok",
        },
    ).json()

    respuesta = client.delete(f"/api/v1/reservas/{reserva['id']}")
    assert respuesta.status_code == 200
    assert respuesta.json()["estado"] == "CANCELADA"
    assert reserva["pago_id"] in pasarela.reversados
    assert cupo["id"] in [c["id"] for c in _recargar_disponibilidad("prof-001")]


def test_notificaciones_se_entregan_fuera_del_flujo_de_reserva():
    cupo = _primer_cupo()
    client.post(
        "/api/v1/reservas",
        json={
            "paciente_id": "pac-001",
            "cupo_id": cupo["id"],
            "cupo_version": cupo["version"],
            "medio_pago": "tok-ok",
        },
    )
    assert client.get("/api/v1/estado").json()["notificaciones_pendientes"] == 1
    assert client.post("/api/v1/notificaciones/procesar").json()["entregadas"] == 1
    assert client.get("/api/v1/estado").json()["notificaciones_pendientes"] == 0


def _recargar_disponibilidad(profesional_id: str) -> list[dict]:
    return client.get(f"/api/v1/profesionales/{profesional_id}/disponibilidad").json()
