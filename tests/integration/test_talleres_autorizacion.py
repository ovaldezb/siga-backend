import json

from src.handlers.admin.talleres_manager import update_taller_handler, upload_logo_handler

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
            "nombreComercial": "Taller Propio",
            "estado": "ACTIVO",
            "precioSuscripcion": 1500,
        },
        {
            "tenantId": OTRO_TENANT,
            "nombreComercial": "Taller Ajeno",
            "estado": "ACTIVO",
            "precioSuscripcion": 1500,
        },
    ])
    return db


def test_super_admin_edita_cualquier_taller(mock_db):
    """La pantalla Talleres del SUPER_ADMIN sigue funcionando."""
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


def test_admin_no_puede_editar_talleres(mock_db):
    """PUT /talleres/{id} solo pedía estar autenticado: un ADMIN podía desactivar
    el taller de otro cliente conociendo su id."""
    db = _seed(mock_db)
    ajeno = db.talleres.find_one({"tenantId": OTRO_TENANT})

    response = update_taller_handler(
        _event(["ADMIN"], path_id=str(ajeno["_id"]), body={"estado": "INACTIVO"}),
        None,
    )

    assert response["statusCode"] == 403
    assert db.talleres.find_one({"tenantId": OTRO_TENANT})["estado"] == "ACTIVO"


def test_asesor_no_puede_editar_talleres(mock_db):
    """Ni siquiera el taller propio: esto es gestión de plataforma."""
    db = _seed(mock_db)
    propio = db.talleres.find_one({"tenantId": TENANT})

    response = update_taller_handler(
        _event(["ASESOR"], path_id=str(propio["_id"]), body={"precioSuscripcion": 1}),
        None,
    )

    assert response["statusCode"] == 403
    assert db.talleres.find_one({"tenantId": TENANT})["precioSuscripcion"] == 1500


def test_admin_no_puede_subir_logo_de_otro_taller(mock_db):
    db = _seed(mock_db)
    ajeno = db.talleres.find_one({"tenantId": OTRO_TENANT})

    response = upload_logo_handler(
        _event(["ADMIN"], path_id=str(ajeno["_id"]), body={"image": "data:image/png;base64,xxx"}),
        None,
    )

    assert response["statusCode"] == 403
