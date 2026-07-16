import json

from src.handlers.admin.talleres_manager import (
    update_my_taller_handler,
    update_taller_handler,
    upload_logo_handler,
)

TENANT = "t-mio"
OTRO_TENANT = "t-ajeno"


def _event(grupos, tenant_id=TENANT, body=None, path_id=None):
    claims = {"cognito:groups": grupos}
    if tenant_id:
        claims["custom:tenant_id"] = tenant_id
    event = {
        "body": json.dumps(body or {}),
        "requestContext": {"authorizer": {"claims": claims}},
    }
    if path_id:
        event["pathParameters"] = {"id": path_id}
    return event


def _seed(mock_db):
    """Dos talleres en la BD de plataforma: el propio y el de otro cliente."""
    db = mock_db["_platform"]
    db.talleres.insert_many([
        {
            "tenantId": TENANT,
            "nombreComercial": "Taller Viejo",
            "direccion": "Calle Falsa 123",
            "adminTelefono": "555-0000",
            "estado": "ACTIVO",
            "precioSuscripcion": 1500,
            "modulos": ["ordenes"],
        },
        {
            "tenantId": OTRO_TENANT,
            "nombreComercial": "Taller Ajeno",
            "estado": "ACTIVO",
            "precioSuscripcion": 1500,
        },
    ])
    return db


def test_admin_actualiza_datos_de_su_taller(mock_db):
    """El caso del reporte: el ADMIN cambia el membrete desde Configuración."""
    db = _seed(mock_db)

    response = update_my_taller_handler(_event(["ADMIN"], body={
        "nombreComercial": "Taller Nuevo",
        "direccion": "Av. Reforma 456",
        "adminTelefono": "555-1111",
    }), None)

    assert response["statusCode"] == 200
    data = json.loads(response["body"])["data"]
    assert data["nombreTaller"] == "Taller Nuevo"

    doc = db.talleres.find_one({"tenantId": TENANT})
    assert doc["nombreComercial"] == "Taller Nuevo"
    assert doc["direccion"] == "Av. Reforma 456"
    assert doc["adminTelefono"] == "555-1111"


def test_admin_no_puede_tocar_datos_comerciales(mock_db):
    """Aunque los mande en el body, precio/estado/modulos no son suyos."""
    db = _seed(mock_db)

    response = update_my_taller_handler(_event(["ADMIN"], body={
        "nombreComercial": "Taller Nuevo",
        "precioSuscripcion": 1,
        "estado": "INACTIVO",
        "modulos": ["*"],
    }), None)

    assert response["statusCode"] == 200
    doc = db.talleres.find_one({"tenantId": TENANT})
    assert doc["precioSuscripcion"] == 1500
    assert doc["estado"] == "ACTIVO"
    assert doc["modulos"] == ["ordenes"]


def test_admin_solo_alcanza_su_propio_tenant(mock_db):
    """El taller se resuelve por el token, no por un id del path."""
    db = _seed(mock_db)

    update_my_taller_handler(_event(["ADMIN"], body={"nombreComercial": "Hackeado"}), None)

    assert db.talleres.find_one({"tenantId": OTRO_TENANT})["nombreComercial"] == "Taller Ajeno"


def test_asesor_no_puede_editar_datos_del_taller(mock_db):
    _seed(mock_db)

    response = update_my_taller_handler(_event(["ASESOR"], body={"nombreComercial": "X"}), None)

    assert response["statusCode"] == 403


def test_campo_vacio_rechazado(mock_db):
    """Un membrete sin nombre no sirve; mejor 400 que un PDF en blanco."""
    db = _seed(mock_db)

    response = update_my_taller_handler(_event(["ADMIN"], body={"nombreComercial": "   "}), None)

    assert response["statusCode"] == 400
    assert db.talleres.find_one({"tenantId": TENANT})["nombreComercial"] == "Taller Viejo"


def test_campos_omitidos_no_se_borran(mock_db):
    """Guardar solo el teléfono no debe vaciar la dirección."""
    db = _seed(mock_db)

    response = update_my_taller_handler(_event(["ADMIN"], body={"adminTelefono": "555-9999"}), None)

    assert response["statusCode"] == 200
    doc = db.talleres.find_one({"tenantId": TENANT})
    assert doc["adminTelefono"] == "555-9999"
    assert doc["direccion"] == "Calle Falsa 123"


def test_super_admin_sigue_editando_cualquier_taller(mock_db):
    """La pantalla Talleres del SUPER_ADMIN no se rompe."""
    db = _seed(mock_db)
    ajeno = db.talleres.find_one({"tenantId": OTRO_TENANT})

    response = update_taller_handler(
        _event(["SUPER_ADMIN"], tenant_id=None, path_id=str(ajeno["_id"]),
               body={"nombreComercial": "Renombrado", "precioSuscripcion": 2000}),
        None,
    )

    assert response["statusCode"] == 200
    doc = db.talleres.find_one({"tenantId": OTRO_TENANT})
    assert doc["nombreComercial"] == "Renombrado"
    assert doc["precioSuscripcion"] == 2000


def test_admin_no_puede_usar_el_endpoint_de_super_admin(mock_db):
    """Cierra el IDOR: PUT /talleres/{id} solo era 'autenticado', sin chequeo de rol."""
    db = _seed(mock_db)
    ajeno = db.talleres.find_one({"tenantId": OTRO_TENANT})

    response = update_taller_handler(
        _event(["ADMIN"], path_id=str(ajeno["_id"]), body={"estado": "INACTIVO"}),
        None,
    )

    assert response["statusCode"] == 403
    assert db.talleres.find_one({"tenantId": OTRO_TENANT})["estado"] == "ACTIVO"


def test_admin_no_puede_subir_logo_de_otro_taller(mock_db):
    db = _seed(mock_db)
    ajeno = db.talleres.find_one({"tenantId": OTRO_TENANT})

    response = upload_logo_handler(
        _event(["ADMIN"], path_id=str(ajeno["_id"]), body={"image": "data:image/png;base64,xxx"}),
        None,
    )

    assert response["statusCode"] == 403
