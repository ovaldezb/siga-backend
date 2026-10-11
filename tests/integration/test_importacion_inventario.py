"""Importación masiva de inventario: preview → aplicar → deshacer."""
import json

from src.handlers.inventario.importacion_manager import (
    preview_importacion_handler,
    aplicar_importacion_handler,
    list_importaciones_handler,
    deshacer_importacion_handler,
)

TENANT = "taller_test"
DB_NAME = "t_taller_test"


def _evento(body=None, grupo="ADMIN", email="admin@taller.test", path=None, qs=None):
    claims = {"custom:tenant_id": TENANT, "email": email, "sub": "u1", "name": "Admin"}
    if grupo:
        claims["cognito:groups"] = grupo
    ev = {"requestContext": {"authorizer": {"claims": claims}}}
    if body is not None:
        ev["body"] = json.dumps(body)
    if path:
        ev["pathParameters"] = path
    if qs:
        ev["queryStringParameters"] = qs
    return ev


def _datos(resp):
    return json.loads(resp["body"])


def _sucursales(mock_db, cuantas=2):
    db = mock_db[DB_NAME]
    return [str(db.sucursales.insert_one({"nombre": f"Sucursal {n + 1}", "activa": True}).inserted_id)
            for n in range(cuantas)]


def _item(mock_db, sucursal, **extra):
    doc = {"tipo": "PRODUCTO", "no_parte": "FIL-100", "nombre": "Filtro", "precio_venta": 200.0,
           "precio_compra": 90.0, "costo_promedio": 90.0, "stock": 5, "maneja_inventario": True,
           "sucursal_id": sucursal, "marca": "GONHER"}
    doc.update(extra)
    return mock_db[DB_NAME].items.insert_one(doc).inserted_id


def test_preview_clasifica_y_no_escribe(mock_db):
    matriz, _ = _sucursales(mock_db)
    _item(mock_db, matriz)
    filas = [
        {"fila": 2, "no_parte": "FIL-100", "precio_venta": "$250.00", "existencia": 8},
        {"fila": 3, "no_parte": "BUJ-1", "nombre": "Bujía", "precio_venta": 80, "existencia": 12},
        {"fila": 4, "no_parte": "BUJ-2", "nombre": "Sin precio"},
        {"fila": 5, "no_parte": "FIL-100 ", "marca": "GONHER"},
        {"fila": 6, "no_parte": "X-1", "nombre": "Malo", "precio_venta": "abc"},
    ]
    resp = preview_importacion_handler(_evento({"sucursal_id": matriz, "filas": filas}), None)
    assert resp["statusCode"] == 200
    data = _datos(resp)["data"]
    estados = {f["fila"]: f["estado"] for f in data["filas"]}
    # FIL-100 viene dos veces (fila 2 y 5): las dos quedan en error
    assert estados == {2: "ERROR", 3: "NUEVO", 4: "ERROR", 5: "ERROR", 6: "ERROR"}
    assert data["resumen"]["nuevos"] == 1 and data["resumen"]["errores"] == 4
    assert mock_db[DB_NAME].items.count_documents({}) == 1, "el preview no escribe"


def test_preview_muestra_antes_y_despues(mock_db):
    matriz, _ = _sucursales(mock_db)
    _item(mock_db, matriz)
    filas = [{"no_parte": "fil-100", "precio_venta": 250, "existencia": 8, "marca": "GONHER"}]
    data = _datos(preview_importacion_handler(_evento({"sucursal_id": matriz, "filas": filas}), None))["data"]
    fila = data["filas"][0]
    assert fila["estado"] == "ACTUALIZA"
    assert fila["cambios"]["precio_venta"] == {"antes": 200.0, "despues": 250.0}
    assert fila["cambios"]["stock"] == {"antes": 5, "despues": 8}
    assert "marca" not in fila["cambios"], "igual que antes ⇒ no es cambio"


def test_aplicar_crea_actualiza_replica_y_registra_kardex(mock_db):
    matriz, sur = _sucursales(mock_db)
    db = mock_db[DB_NAME]
    item_id = _item(mock_db, matriz)
    _item(mock_db, sur, stock=2)  # el hermano de la otra sucursal
    filas = [
        {"no_parte": "FIL-100", "precio_venta": 250, "existencia": 8},
        {"no_parte": "BUJ-1", "nombre": "Bujía", "precio_venta": 80, "existencia": 12},
        {"no_parte": "MAL", "nombre": "Sin precio"},
    ]
    resp = aplicar_importacion_handler(_evento({"sucursal_id": matriz, "filas": filas, "archivo": "inv.xlsx"}), None)
    assert resp["statusCode"] == 200, resp["body"]

    actualizado = db.items.find_one({"_id": item_id})
    assert actualizado["precio_venta"] == 250 and actualizado["stock"] == 8

    hermano = db.items.find_one({"no_parte": "FIL-100", "sucursal_id": sur})
    assert hermano["precio_venta"] == 250, "el precio se propaga"
    assert hermano["stock"] == 2, "la existencia NO se propaga"

    nuevo = db.items.find_one({"no_parte": "BUJ-1", "sucursal_id": matriz})
    assert nuevo["stock"] == 12
    clon = db.items.find_one({"no_parte": "BUJ-1", "sucursal_id": sur})
    assert clon["stock"] == 0

    movs = list(db.inventario_movimientos.find({"concepto": "IMPORTACION"}))
    assert sorted(m["cantidad"] for m in movs) == [3, 12]

    lote = db.importaciones_inventario.find_one({})
    assert lote["estado"] == "APLICADA"
    assert lote["resumen"]["errores"] == 1


def test_deshacer_revierte_todo(mock_db):
    matriz, sur = _sucursales(mock_db)
    db = mock_db[DB_NAME]
    item_id = _item(mock_db, matriz)
    _item(mock_db, sur, stock=2)
    filas = [
        {"no_parte": "FIL-100", "precio_venta": 250, "existencia": 8},
        {"no_parte": "BUJ-1", "nombre": "Bujía", "precio_venta": 80, "existencia": 12},
    ]
    lote_id = _datos(aplicar_importacion_handler(_evento({"sucursal_id": matriz, "filas": filas}), None))["data"]["lote_id"]

    resp = deshacer_importacion_handler(_evento(path={"id": lote_id}), None)
    assert resp["statusCode"] == 200, resp["body"]

    original = db.items.find_one({"_id": item_id})
    assert original["precio_venta"] == 200 and original["stock"] == 5
    assert db.items.find_one({"no_parte": "FIL-100", "sucursal_id": sur})["precio_venta"] == 200
    assert db.items.count_documents({"no_parte": "BUJ-1"}) == 0
    assert db.importaciones_inventario.find_one({})["estado"] == "DESHECHA"

    otra = deshacer_importacion_handler(_evento(path={"id": lote_id}), None)
    assert otra["statusCode"] == 409


def test_deshacer_se_niega_si_algo_se_movio_despues(mock_db):
    matriz, _ = _sucursales(mock_db, 1) + [None]
    db = mock_db[DB_NAME]
    item_id = _item(mock_db, matriz)
    filas = [
        {"no_parte": "FIL-100", "existencia": 8},
        {"no_parte": "BUJ-1", "nombre": "Bujía", "precio_venta": 80, "existencia": 12},
    ]
    lote_id = _datos(aplicar_importacion_handler(_evento({"sucursal_id": matriz, "filas": filas}), None))["data"]["lote_id"]
    db.items.update_one({"_id": item_id}, {"$inc": {"stock": -1}})  # una venta

    resp = deshacer_importacion_handler(_evento(path={"id": lote_id}), None)
    assert resp["statusCode"] == 409
    data = _datos(resp)["data"]
    assert data["total_conflictos"] == 1
    assert data["conflictos"][0]["no_parte"] == "FIL-100"
    # Nada se revirtió: la bujía sigue ahí
    assert db.items.count_documents({"no_parte": "BUJ-1"}) == 1


def test_deshacer_exige_ir_de_la_mas_reciente_a_la_mas_vieja(mock_db):
    matriz, _ = _sucursales(mock_db, 1) + [None]
    _item(mock_db, matriz)
    ev = lambda precio: _evento({"sucursal_id": matriz, "filas": [{"no_parte": "FIL-100", "precio_venta": precio}]})
    primero = _datos(aplicar_importacion_handler(ev(250), None))["data"]["lote_id"]
    # mongomock no avanza el reloj entre llamadas: forzamos que la segunda sea posterior
    mock_db[DB_NAME].importaciones_inventario_cambios.update_many({}, {"$set": {"createdAt": "2026-01-01T00:00:00Z"}})
    mock_db[DB_NAME].importaciones_inventario.update_many({}, {"$set": {"createdAt": "2026-01-01T00:00:00Z"}})
    aplicar_importacion_handler(ev(300), None)

    resp = deshacer_importacion_handler(_evento(path={"id": primero}), None)
    assert resp["statusCode"] == 409
    assert "más reciente" in _datos(resp)["message"]


def test_permisos_y_scope(mock_db):
    matriz, sur = _sucursales(mock_db)
    db = mock_db[DB_NAME]
    db.usuarios.insert_one({"email": "gerente@taller.test", "sucursales": [{"sucursal": matriz}]})
    filas = [{"no_parte": "BUJ-1", "nombre": "Bujía", "precio_venta": 80}]

    asesor = preview_importacion_handler(_evento({"sucursal_id": matriz, "filas": filas}, grupo="ASESOR"), None)
    assert asesor["statusCode"] == 403

    gerente_propia = preview_importacion_handler(
        _evento({"sucursal_id": matriz, "filas": filas}, grupo="GERENTE", email="gerente@taller.test"), None)
    assert gerente_propia["statusCode"] == 200

    gerente_ajena = preview_importacion_handler(
        _evento({"sucursal_id": sur, "filas": filas}, grupo="GERENTE", email="gerente@taller.test"), None)
    assert gerente_ajena["statusCode"] == 403


def test_listado_de_importaciones(mock_db):
    matriz, _ = _sucursales(mock_db, 1) + [None]
    aplicar_importacion_handler(_evento({"sucursal_id": matriz, "filas": [
        {"no_parte": "BUJ-1", "nombre": "Bujía", "precio_venta": 80}]}), None)
    resp = list_importaciones_handler(_evento(qs={"sucursal_id": matriz}), None)
    lotes = _datos(resp)["data"]
    assert len(lotes) == 1 and lotes[0]["puede_deshacer"] is True


def test_numero_de_parte_numerico_de_excel_no_lleva_punto_cero(mock_db):
    matriz, _ = _sucursales(mock_db, 1) + [None]
    data = _datos(preview_importacion_handler(_evento({"sucursal_id": matriz, "filas": [
        {"no_parte": 12345.0, "nombre": "Pieza", "precio_venta": 10}]}), None))["data"]
    assert data["filas"][0]["no_parte"] == "12345"
