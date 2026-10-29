import json

from bson import ObjectId

from src.handlers.clientes.fusion_manager import fusionar_cliente_handler

TENANT = "tallertest"
ADMIN = {"custom:tenant_id": TENANT, "cognito:groups": "ADMIN", "email": "dueno@taller.mx"}


def _evento(conservado_id, duplicado_id, claims=ADMIN):
    return {
        "pathParameters": {"id": conservado_id},
        "body": json.dumps({"duplicado_id": duplicado_id}),
        "requestContext": {"authorizer": {"claims": claims}},
    }


def _sembrar(db):
    """Juan se dio de alta en Norte y lo volvieron a capturar en Sur."""
    norte = db.clientes.insert_one({
        "nombre": "Juan", "apellido_paterno": "Pérez", "telefono": "5512345678",
        "rfc": "XAXX010101000", "limite_credito": 5000.0, "dias_credito": 15,
        "sucursal_id": "norte", "vehiculos_resumen": [{"id": "v1", "placas": "AAA111"}],
    }).inserted_id
    sur = db.clientes.insert_one({
        "nombre": "Juan", "apellido_paterno": "Perez", "apellido_materno": "López",
        "telefono": "5512345678", "email": "juan@mail.com", "rfc": "PELJ800101AB1",
        "tipo_persona": "FISICA", "regimen_fiscal": "605", "limite_credito": 10000.0,
        "sucursal_id": "sur", "vehiculos_resumen": [{"id": "v2", "placas": "BBB222"}],
    }).inserted_id
    n, s = str(norte), str(sur)
    db.vehiculos.insert_many([
        {"_id": ObjectId(), "cliente_id": n, "placas": "AAA111"},
        {"_id": ObjectId(), "cliente_id": s, "placas": "BBB222"},
    ])
    db.ordenes_servicio.insert_many([
        {"folio": "OS-1", "cliente_snapshot": {"id": n, "nombre": "Juan"}, "sucursal_id": "norte"},
        {"folio": "OS-2", "cliente_snapshot": {"id": s, "nombre": "Juan", "apellido_paterno": "Perez"}, "sucursal_id": "sur"},
        {"folio": "OS-LEGADO", "cliente_id": s},
    ])
    db.cotizaciones.insert_one({"cliente_snapshot": {"id": s}})
    db.ventas.insert_one({"cliente_id": s, "cliente_nombre": "Juan Perez López", "saldo_pendiente": 800.0, "total": 800.0})
    db.citas.insert_one({"clienteId": s, "clienteNombre": "Juan Perez"})
    return n, s


def test_fusion_mueve_todo_y_archiva(mock_db):
    db = mock_db[f"t_{TENANT}"]
    n, s = _sembrar(db)

    resp = fusionar_cliente_handler(_evento(n, s), None)
    assert resp["statusCode"] == 200, resp["body"]
    data = json.loads(resp["body"])["data"]
    assert data["movidos"] == {"vehiculos": 1, "ordenes_servicio": 2, "cotizaciones": 1, "ventas": 1, "citas": 1}

    assert db.clientes.count_documents({}) == 1
    assert db.vehiculos.count_documents({"cliente_id": n}) == 2
    assert db.ordenes_servicio.count_documents({"cliente_snapshot.id": s}) == 0
    assert db.ordenes_servicio.find_one({"folio": "OS-LEGADO"})["cliente_id"] == n
    assert db.ventas.find_one({})["cliente_id"] == n
    assert db.citas.find_one({})["clienteId"] == n

    juan = db.clientes.find_one({"_id": ObjectId(n)})
    # Hereda lo que le faltaba; no pisa lo que ya tenía.
    assert juan["email"] == "juan@mail.com"
    assert juan["apellido_materno"] == "López"
    assert juan["rfc"] == "PELJ800101AB1" and juan["regimen_fiscal"] == "605"
    assert juan["apellido_paterno"] == "Pérez"
    assert juan["sucursal_id"] == "norte"
    # Se queda la línea de crédito más amplia.
    assert juan["limite_credito"] == 10000.0 and juan["dias_credito"] == 15
    assert {v["id"] for v in juan["vehiculos_resumen"]} == {"v1", "v2"}

    # Los snapshots movidos ya traen los datos del que se conserva.
    os2 = db.ordenes_servicio.find_one({"folio": "OS-2"})
    assert os2["cliente_snapshot"]["apellido_paterno"] == "Pérez"
    assert db.ventas.find_one({})["cliente_nombre"] == "Juan Pérez López"

    archivo = db.clientes_fusionados.find_one({})
    assert archivo["cliente_original_id"] == s
    assert archivo["fusionado_en"] == n
    assert archivo["fusionado_por"] == "dueno@taller.mx"
    assert archivo["movidos"]["ventas"] == 1

    # Repetirla no rompe nada: el duplicado ya no existe.
    assert fusionar_cliente_handler(_evento(n, s), None)["statusCode"] == 404


def test_fusion_solo_admin_y_validaciones(mock_db):
    db = mock_db[f"t_{TENANT}"]
    n, s = _sembrar(db)
    asesor = {"custom:tenant_id": TENANT, "cognito:groups": "ASESOR"}
    assert fusionar_cliente_handler(_evento(n, s, asesor), None)["statusCode"] == 403
    assert fusionar_cliente_handler(_evento(n, n), None)["statusCode"] == 400
    assert fusionar_cliente_handler(_evento(n, "no-es-id"), None)["statusCode"] == 400
    assert fusionar_cliente_handler(_evento(n, str(ObjectId())), None)["statusCode"] == 404
    assert db.clientes.count_documents({}) == 2


def test_fusion_bloquea_flotillas_distintas(mock_db):
    db = mock_db[f"t_{TENANT}"]
    n, s = _sembrar(db)
    db.clientes.update_one({"_id": ObjectId(n)}, {"$set": {"flotilla_id": "f1"}})
    db.clientes.update_one({"_id": ObjectId(s)}, {"$set": {"flotilla_id": "f2"}})
    assert fusionar_cliente_handler(_evento(n, s), None)["statusCode"] == 409
    assert db.ventas.find_one({})["cliente_id"] == s
