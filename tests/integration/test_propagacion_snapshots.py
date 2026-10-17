"""Renombrar un cliente / corregir un vehículo se refleja en su historial (reporte 2026-10-16)."""
import json
from bson import ObjectId
from src.handlers.clientes.clientes_manager import update_cliente_handler
from src.handlers.vehiculos.vehiculos_manager import update_vehiculo_handler, create_vehiculo_handler
from src.handlers.proveedores.proveedores_manager import update_proveedor_handler
from src.handlers.contabilidad.contabilidad_manager import _construir_contexto_ventas

TENANT = "tallertest"


def _ev(body, path=None):
    e = {"body": json.dumps(body),
         "requestContext": {"authorizer": {"claims": {"custom:tenant_id": TENANT, "sub": "u1",
                                                      "cognito:groups": "ADMIN"}}}}
    if path:
        e["pathParameters"] = path
    return e


def _db(mock_db):
    return mock_db[f"t_{TENANT}"]


def test_renombrar_cliente_actualiza_os_ventas_citas_y_cotizaciones(mock_db):
    db = _db(mock_db)
    cid = str(db.clientes.insert_one({"nombre": "Luiz", "apellido_paterno": "Alberto", "telefono": "111"}).inserted_id)
    snap = {"id": cid, "nombre": "Luiz", "apellido_paterno": "Alberto", "telefono": "111"}
    os_id = db.ordenes_servicio.insert_one({"folio": "OS-1", "cliente_snapshot": dict(snap), "kilometraje": 90000}).inserted_id
    db.cotizaciones.insert_one({"folio": "COT-1", "tipo": "CLIENTE", "cliente_snapshot": dict(snap)})
    db.ventas.insert_one({"folio": "V-1", "cliente_id": cid, "cliente_nombre": "Luiz"})
    db.citas.insert_one({"clienteId": cid, "clienteNombre": "Luiz Alberto"})
    otro = db.ordenes_servicio.insert_one({"folio": "OS-2", "cliente_snapshot": {"id": "otro", "nombre": "Pedro"}}).inserted_id

    resp = update_cliente_handler(_ev({"nombre": "Luis", "telefono": "222"}, {"id": cid}), None)
    assert resp["statusCode"] == 200

    os_doc = db.ordenes_servicio.find_one({"_id": os_id})
    assert os_doc["cliente_snapshot"]["nombre"] == "Luis"
    assert os_doc["cliente_snapshot"]["telefono"] == "222"
    assert os_doc["kilometraje"] == 90000
    assert db.cotizaciones.find_one({"folio": "COT-1"})["cliente_snapshot"]["nombre"] == "Luis"
    assert db.ventas.find_one({"folio": "V-1"})["cliente_nombre"] == "Luis Alberto"
    assert db.citas.find_one({"clienteId": cid})["clienteNombre"] == "Luis Alberto"
    assert db.ordenes_servicio.find_one({"_id": otro})["cliente_snapshot"]["nombre"] == "Pedro"


def test_corregir_vehiculo_actualiza_copias_sin_tocar_kilometraje(mock_db):
    db = _db(mock_db)
    cid = str(db.clientes.insert_one({"nombre": "Ana"}).inserted_id)
    vid = str(db.vehiculos.insert_one({"marca": "Nissan", "modelo": "Platina", "placas": "ABC-1",
                                       "cliente_id": cid}).inserted_id)
    db.ordenes_servicio.insert_one({"folio": "OS-1", "vehiculo_id": vid,
                                    "vehiculo_snapshot": {"marca": "Nissan", "modelo": "Platina",
                                                          "placas": "ABC-1", "kilometraje": 120000}})
    db.ventas.insert_one({"folio": "V-1", "vehiculo_id": vid, "vehiculo_snapshot": None})
    db.citas.insert_one({"vehiculoId": vid, "vehiculoDesc": "Nissan Platina (ABC-1)"})

    resp = update_vehiculo_handler(_ev({"placas": "ADH-017-F", "numero_economico": " e37 "}, {"id": vid}), None)
    assert resp["statusCode"] == 200, resp["body"]

    snap = db.ordenes_servicio.find_one({"folio": "OS-1"})["vehiculo_snapshot"]
    assert snap["placas"] == "ADH-017-F"
    assert snap["numero_economico"] == "E37"
    assert snap["kilometraje"] == 120000
    assert db.ventas.find_one({"folio": "V-1"})["vehiculo_snapshot"] is None
    assert db.citas.find_one({"vehiculoId": vid})["vehiculoDesc"] == "Nissan Platina (ADH-017-F)"


def test_numero_economico_unico_dentro_de_la_flotilla(mock_db):
    db = _db(mock_db)
    c1 = str(db.clientes.insert_one({"nombre": "Chofer 1", "flotilla_id": "F1"}).inserted_id)
    c2 = str(db.clientes.insert_one({"nombre": "Chofer 2", "flotilla_id": "F1"}).inserted_id)
    c3 = str(db.clientes.insert_one({"nombre": "Otro"}).inserted_id)
    base = {"marca": "Nissan", "modelo": "NP300", "sucursalId": "s1"}

    assert create_vehiculo_handler(_ev({**base, "cliente_id": c1, "numero_economico": "E37"}), None)["statusCode"] == 201
    dup = create_vehiculo_handler(_ev({**base, "cliente_id": c2, "numero_economico": "e37"}), None)
    assert dup["statusCode"] == 409
    # Otra empresa puede usar el mismo número.
    assert create_vehiculo_handler(_ev({**base, "cliente_id": c3, "numero_economico": "E37"}), None)["statusCode"] == 201
    # Vacío = sin número; no choca con nadie.
    vacio = create_vehiculo_handler(_ev({**base, "cliente_id": c2, "numero_economico": "  "}), None)
    assert vacio["statusCode"] == 201
    assert json.loads(vacio["body"])["data"]["numero_economico"] is None


def test_renombrar_proveedor_actualiza_compras_y_catalogo(mock_db):
    db = _db(mock_db)
    pid = str(db.proveedores.insert_one({"nombre": "Refacc. Gzz"}).inserted_id)
    db.compras.insert_one({"folio": "C-1", "proveedor_id": pid, "proveedor_snapshot": {"id": pid, "nombre": "Refacc. Gzz"}})
    db.items.insert_one({"nombre": "Balata", "proveedor_id": pid, "proveedor": "Refacc. Gzz"})

    resp = update_proveedor_handler(_ev({"nombre": "Refacciones González"}, {"id": pid}), None)
    assert resp["statusCode"] == 200

    assert db.compras.find_one({"folio": "C-1"})["proveedor_snapshot"]["nombre"] == "Refacciones González"
    assert db.items.find_one({"nombre": "Balata"})["proveedor"] == "Refacciones González"


def test_contexto_de_venta_trae_la_factura_vigente(mock_db):
    db = _db(mock_db)
    vid = db.ventas.insert_one({"folio": "V-9", "venta_facturada": True, "items": []}).inserted_id
    db.facturasemitidas.insert_one({"ticket": "V-9", "serie": "A", "folio": 2297, "uuid": "u-1",
                                    "tipo_de_comprobante": "I", "estatus": "Cancelada"})
    db.facturasemitidas.insert_one({"ticket": "V-9", "serie": "A", "folio": 2298, "uuid": "u-2",
                                    "tipo_de_comprobante": "I", "estatus": "Vigente"})
    sin = db.ventas.insert_one({"folio": "V-10", "venta_facturada": False, "items": []}).inserted_id

    ctx = _construir_contexto_ventas(db, list(db.ventas.find({})))
    assert ctx[str(vid)]["factura"]["folio"] == 2298
    assert ctx[str(vid)]["factura"]["serie"] == "A"
    assert ctx[str(sin)]["factura"] is None
    assert ObjectId.is_valid(ctx[str(vid)]["factura"]["id"])
