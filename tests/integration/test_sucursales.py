import json
from bson import ObjectId
from src.handlers.sucursales.sucursales_manager import (
    list_sucursales_handler,
    create_sucursal_handler,
    update_sucursal_handler,
    delete_sucursal_handler,
    get_sucursal_handler
)

TENANT = "tallertest"

def _claims(grupos="[ADMIN]"):
    claims = {"custom:tenant_id": TENANT, "sub": "user-1", "email": "test@taller.com", "name": "Tester"}
    if grupos:
        claims["cognito:groups"] = grupos
    return claims


def _crear(nombre):
    event = {"body": json.dumps({"nombre": nombre}),
             "requestContext": {"authorizer": {"claims": _claims()}}}
    resp = create_sucursal_handler(event, None)
    assert resp["statusCode"] == 201
    return json.loads(resp["body"])["data"]["id"]


def _borrar(sucursal_id, grupos="[ADMIN]"):
    resp = delete_sucursal_handler({
        "pathParameters": {"id": sucursal_id},
        "requestContext": {"authorizer": {"claims": _claims(grupos)}},
    }, None)
    return resp["statusCode"], json.loads(resp["body"])

def test_sucursal_crud_flow(mock_db):
    db = mock_db[f"t_{TENANT}"]
    
    # 1. Create Sucursal
    event_create = {
        "body": json.dumps({
            "nombre": "Sucursal Poniente",
            "direccion": "Av. Principal 123",
            "telefono": "5551234",
            "responsable": "David",
            "serie": "B",
            "codigo_postal": "54321"
        }),
        "requestContext": {"authorizer": {"claims": _claims()}}
    }
    
    resp_create = create_sucursal_handler(event_create, None)
    assert resp_create["statusCode"] == 201
    
    sucursal_id = json.loads(resp_create["body"])["data"]["id"]
    
    # 2. Get Sucursal by ID
    event_get = {
        "pathParameters": {"id": sucursal_id},
        "requestContext": {"authorizer": {"claims": _claims()}}
    }
    
    resp_get = get_sucursal_handler(event_get, None)
    assert resp_get["statusCode"] == 200
    
    data_get = json.loads(resp_get["body"])["data"]
    assert data_get["nombre"] == "Sucursal Poniente"
    assert data_get["serie"] == "B"
    assert data_get["codigo_postal"] == "54321"

    # 3. List Sucursales
    event_list = {
        "requestContext": {"authorizer": {"claims": _claims()}}
    }
    
    resp_list = list_sucursales_handler(event_list, None)
    assert resp_list["statusCode"] == 200
    assert len(json.loads(resp_list["body"])["data"]) == 1

    # 4. Update Sucursal
    event_update = {
        "pathParameters": {"id": sucursal_id},
        "body": json.dumps({
            "nombre": "Sucursal Poniente Editada"
        }),
        "requestContext": {"authorizer": {"claims": _claims()}}
    }
    
    resp_update = update_sucursal_handler(event_update, None)
    assert resp_update["statusCode"] == 200
    assert json.loads(resp_update["body"])["data"]["nombre"] == "Sucursal Poniente Editada"

    # 5. Delete Sucursal (sin historial y no es la única): borra también su folio.
    _crear("Matriz")
    assert db["folios"].count_documents({"sucursal_id": sucursal_id}) == 1
    status, _ = _borrar(sucursal_id)
    assert status == 200
    assert db["sucursales"].count_documents({}) == 1
    assert db["folios"].count_documents({"sucursal_id": sucursal_id}) == 0


def test_borrar_sucursal_con_historial_da_409(mock_db):
    db = mock_db[f"t_{TENANT}"]
    _crear("Matriz")
    sid = _crear("Norte")
    db["ordenes_servicio"].insert_many([{"sucursal_id": sid}, {"sucursal_id": sid}])
    db["ventas"].insert_one({"sucursal_id": sid})
    db["traspasos"].insert_one({"origen_id": "otra", "destino_id": sid})
    db["items"].insert_one({"sucursal_id": sid, "stock": 3})

    status, body = _borrar(sid)
    assert status == 409
    assert body["data"]["ligados"] == [
        "2 órdenes de servicio", "1 ventas", "1 traspasos", "1 productos con existencias"]
    assert "Desactívala" in body["message"]
    assert db["sucursales"].count_documents({"_id": ObjectId(sid)}) == 1


def test_borrar_sucursal_con_usuarios_asignados_da_409(mock_db):
    db = mock_db[f"t_{TENANT}"]
    _crear("Matriz")
    sid = _crear("Norte")
    # Formato real del front ([{"sucursal": id}]) y el legacy de id suelto.
    db["usuarios"].insert_one({"email": "a@b.com", "sucursales": [{"sucursal": sid}]})
    db["usuarios"].insert_one({"email": "c@d.com", "sucursales": [sid]})
    db["usuarios"].insert_one({"email": "e@f.com", "sucursales": [{"sucursal": "otra"}]})
    status, body = _borrar(sid)
    assert status == 409
    assert body["data"]["ligados"] == ["2 usuarios asignados"]


def test_borrar_limpia_replicas_sin_existencias(mock_db):
    db = mock_db[f"t_{TENANT}"]
    _crear("Matriz")
    sid = _crear("Norte")
    db["items"].insert_one({"sucursal_id": sid, "stock": 0, "no_parte": "X1"})
    assert _borrar(sid)[0] == 200
    assert db["items"].count_documents({"sucursal_id": sid}) == 0


def test_no_se_borra_la_unica_sucursal(mock_db):
    sid = _crear("Matriz")
    assert _borrar(sid)[0] == 409


def test_borrar_sucursal_requiere_admin_e_id_valido(mock_db):
    _crear("Matriz")
    sid = _crear("Norte")
    assert _borrar(sid, grupos="[CAJERO]")[0] == 403
    assert _borrar("no-es-objectid")[0] == 400
    assert _borrar(str(ObjectId()))[0] == 404
