"""Demostración guiada de los escenarios analizados.

Ejecuta contra la aplicación en memoria, sin necesidad de levantar el servidor:

    python demo.py
"""
from fastapi.testclient import TestClient

from app.main import app
from app.repositorio import repositorio

client = TestClient(app)


def titulo(texto: str) -> None:
    print("\n" + "=" * 78)
    print(texto)
    print("=" * 78)


def mostrar(respuesta) -> dict:
    cuerpo = respuesta.json()
    print(f"  HTTP {respuesta.status_code}")
    if "traza" in cuerpo:
        print(f"  Reserva {cuerpo['id']} | estado: {cuerpo['estado']} | "
              f"correlacion: {cuerpo['id_correlacion']}")
        for paso in cuerpo["traza"]:
            detalle = f" ({paso['detalle']})" if paso.get("detalle") else ""
            print(f"    - {paso['paso']}: {paso['estado']}{detalle}")
    else:
        print(f"  {cuerpo}")
    return cuerpo


def cupo_libre(profesional_id: str = "prof-001") -> dict:
    return client.get(f"/api/v1/profesionales/{profesional_id}/disponibilidad").json()[0]


def reservar(cupo: dict, paciente: str, medio_pago: str, clave: str | None = None):
    cabeceras = {"Idempotency-Key": clave} if clave else {}
    return client.post(
        "/api/v1/reservas",
        json={
            "paciente_id": paciente,
            "cupo_id": cupo["id"],
            "cupo_version": cupo["version"],
            "medio_pago": medio_pago,
        },
        headers=cabeceras,
    )


def main() -> None:
    repositorio.cargar_datos_iniciales()

    titulo("ESCENARIO 1. Reserva exitosa: la saga recorre los cinco pasos")
    cupo = cupo_libre()
    print(f"  Cupo {cupo['id']} del {cupo['inicio']} en versión {cupo['version']}")
    mostrar(reservar(cupo, "pac-001", "tok-ok"))

    titulo("ESCENARIO 2. Doble reserva: el bloqueo optimista rechaza la segunda")
    repositorio.cargar_datos_iniciales()
    cupo = cupo_libre()
    print("  Paciente 1 reserva el cupo:")
    mostrar(reservar(cupo, "pac-001", "tok-ok"))
    print("  Paciente 2 intenta el mismo cupo con la versión que había leído:")
    mostrar(reservar(cupo, "pac-002", "tok-ok"))

    titulo("ESCENARIO 3. Pago rechazado: se compensan los pasos ya ejecutados")
    repositorio.cargar_datos_iniciales()
    cupo = cupo_libre()
    mostrar(reservar(cupo, "pac-001", "tok-rechazo"))
    reserva = list(repositorio.reservas.values())[-1]
    print(f"  Estado final de la reserva: {reserva.estado.value}")
    for paso in reserva.traza:
        print(f"    - {paso.paso}: {paso.estado}")
    disponibles = [c["id"] for c in client.get(
        "/api/v1/profesionales/prof-001/disponibilidad").json()]
    print(f"  ¿El cupo volvió a estar disponible? {'sí' if cupo['id'] in disponibles else 'no'}")

    titulo("ESCENARIO 4. Reintento con la misma clave: no hay doble cobro")
    repositorio.cargar_datos_iniciales()
    cupo = cupo_libre()
    primera = mostrar(reservar(cupo, "pac-001", "tok-ok", clave="clave-123"))
    print("  Se repite exactamente la misma petición:")
    segunda = mostrar(reservar(cupo, "pac-001", "tok-ok", clave="clave-123"))
    print(f"  ¿Misma reserva? {'sí' if primera['id'] == segunda['id'] else 'no'} | "
          f"¿Mismo pago? {'sí' if primera['pago_id'] == segunda['pago_id'] else 'no'} | "
          f"reservas creadas: {len(repositorio.reservas)}")

    titulo("ESCENARIO 5. Pasarela caída: reintento, compensación e interruptor")
    repositorio.cargar_datos_iniciales()
    for i in range(3):
        cupo = cupo_libre()
        print(f"  Intento {i + 1} con la pasarela fuera de servicio:")
        mostrar(reservar(cupo, f"pac-00{i + 1}", "tok-timeout"))
    print(f"  Estado operativo: {client.get('/api/v1/estado').json()}")
    print("  Nueva solicitud con un medio de pago válido, ya con el circuito abierto:")
    mostrar(reservar(cupo_libre("prof-002"), "pac-004", "tok-ok"))

    titulo("FIN DE LA DEMOSTRACIÓN")


if __name__ == "__main__":
    main()
