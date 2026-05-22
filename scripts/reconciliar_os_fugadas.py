"""
reconciliar_os_fugadas.py — Detecta y reconcilia OS cerradas sin pasar por POS.

Una OS en estado ENTREGADO/FINALIZADO debería tener SIEMPRE `venta_id`. Las que
no lo tienen se cerraron desde el dropdown de estado antes del fix OS->POS
(2026-05-22): no generaron venta, ni movimiento de caja, ni descuento de
inventario, y son invisibles en el módulo de Contabilidad.

Uso:
  # Listar las OS fugadas de un tenant (solo lectura, no toca nada):
  python scripts/reconciliar_os_fugadas.py --tenant <TENANT_ID>

  # Listar las de TODOS los tenants:
  python scripts/reconciliar_os_fugadas.py --todos

  # Documentar el motivo en las OS listadas. NO crea ventas: solo deja rastro
  # auditable de que se revisaron y por qué no llevan venta (cortesías,
  # garantías, errores de captura, etc.):
  python scripts/reconciliar_os_fugadas.py --tenant <ID> --marcar-revisado "Cortesía sin cobro"

Para CREAR la venta retroactiva de una OS concreta: abre el Punto de Venta
apuntando a esa orden (/punto-venta?orden_id=<id>) y cóbrala de forma normal —
`create_venta_handler` la sigue aceptando porque no existe venta previa para
ese `orden_id`, y así pasa por toda la lógica de stock/caja/contabilidad.

Requiere las variables de entorno MONGO_USER / MONGO_PASSWORD / MONGO_HOST
(o un archivo .env en la raíz del repo).
"""
import os
import sys
import argparse
from datetime import datetime, timezone
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

ESTADOS_CERRADOS = ["ENTREGADO", "FINALIZADO"]


def _client() -> MongoClient:
    user = os.environ.get("MONGO_USER")
    password = os.environ.get("MONGO_PASSWORD")
    host = os.environ.get("MONGO_HOST")
    db_name = os.environ.get("MONGO_DB", "siga")
    if not (user and password and host):
        print("[ERROR] Faltan MONGO_USER / MONGO_PASSWORD / MONGO_HOST (o .env).")
        sys.exit(1)
    uri = f"mongodb+srv://{user}:{password}@{host}/{db_name}?retryWrites=true&w=majority"
    return MongoClient(uri)


def _tenant_db(client, tenant_id):
    # get_tenant_db usa el mismo esquema: prefijo "t_" + id sin guiones.
    return client[f"t_{tenant_id.replace('-', '')}"]


def _listar_tenants(client):
    """Lee los tenants desde la colección talleres del DB _platform."""
    try:
        talleres = list(client["_platform"]["talleres"].find(
            {}, {"tenantId": 1, "nombreComercial": 1}))
        return [(t.get("tenantId"), t.get("nombreComercial", "?"))
                for t in talleres if t.get("tenantId")]
    except Exception as e:
        print(f"[WARN] No se pudo leer _platform.talleres: {e}")
        return []


def _orphans(db):
    """OS en estado cerrado sin venta_id — las fugadas."""
    return list(db["ordenes_servicio"].find({
        "estado": {"$in": ESTADOS_CERRADOS},
        "venta_id": {"$exists": False},
    }))


def reconciliar_tenant(db, etiqueta, marcar_revisado=None):
    orphans = _orphans(db)
    if not orphans:
        print(f"  [OK] {etiqueta}: sin OS fugadas.")
        return 0

    print(f"  [!!] {etiqueta}: {len(orphans)} OS fugada(s):")
    total = 0.0
    for o in orphans:
        folio = o.get("folio", "?")
        estado = o.get("estado", "?")
        monto = float(o.get("total", 0) or 0)
        total += monto
        cliente = (o.get("cliente_snapshot") or {}).get("nombre", "?")
        fecha = o.get("createdAt", "?")
        oid = str(o.get("_id"))
        print(f"     - {folio:<18} {estado:<11} ${monto:>11,.2f}  {cliente}")
        print(f"       orden_id={oid}  creada={fecha}")
    print(f"     TOTAL en OS fugadas: ${total:,.2f}")

    if marcar_revisado:
        ids = [o["_id"] for o in orphans]
        res = db["ordenes_servicio"].update_many(
            {"_id": {"$in": ids}},
            {"$set": {"reconciliacion": {
                "revisado_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "") + "Z",
                "motivo": marcar_revisado,
                "sin_venta_justificado": True,
            }}},
        )
        print(f"     [OK] {res.modified_count} OS marcadas como revisadas: \"{marcar_revisado}\"")
    return len(orphans)


def main():
    parser = argparse.ArgumentParser(
        description="Reconcilia OS cerradas (ENTREGADO/FINALIZADO) sin pasar por POS.")
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--tenant", help="Tenant ID concreto a revisar.")
    grupo.add_argument("--todos", action="store_true",
                       help="Revisar todos los tenants de _platform.talleres.")
    parser.add_argument("--marcar-revisado", metavar="MOTIVO",
                        help="Estampa el motivo en las OS fugadas (NO crea ventas).")
    args = parser.parse_args()

    client = _client()
    print("Conectado a MongoDB.\n")

    total_orphans = 0
    if args.todos:
        tenants = _listar_tenants(client)
        if not tenants:
            print("No se encontraron tenants en _platform.talleres.")
            sys.exit(1)
        for tid, nombre in tenants:
            total_orphans += reconciliar_tenant(
                _tenant_db(client, tid), f"{nombre} ({tid})", args.marcar_revisado)
    else:
        total_orphans += reconciliar_tenant(
            _tenant_db(client, args.tenant), args.tenant, args.marcar_revisado)

    print()
    if total_orphans == 0:
        print("Sin OS fugadas. Nada que reconciliar.")
    elif not args.marcar_revisado:
        print(f"{total_orphans} OS fugada(s) en total. Siguientes pasos:")
        print("  - Para COBRARLAS: abre POS en /punto-venta?orden_id=<id> y procesa la venta.")
        print("  - Para DOCUMENTARLAS sin venta: re-corre con --marcar-revisado \"<motivo>\".")


if __name__ == "__main__":
    main()
