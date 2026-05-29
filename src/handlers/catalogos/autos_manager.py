"""Catálogo global de autos (base `_platform`).

Marcas de vehículos: viven en la colección `_platform.marcas`. Es un catálogo
administrable (Create / Read / Update) desde el módulo de Configuración.
Por requerimiento del negocio NO se expone borrado: una marca se desactiva
(`activa = False`) pero nunca se elimina, para no romper vehículos históricos.

`list_marcas_handler` devuelve la unión del catálogo activo + las marcas que ya
existen en vehículos reales (legacy), de modo que ningún selector se quede vacío
durante la migración.
"""

import json
import re
from datetime import datetime

from bson import ObjectId
from bson.errors import InvalidId

from src.shared.utils.response_handler import create_response, handle_exception
from src.shared.utils.auth_utils import is_admin, get_claims
from src.shared.infrastructure.database import MongoDBConnection


def _get_claims(event):
    return get_claims(event)


def _norm(nombre):
    """Normaliza un nombre de marca para comparar / deduplicar."""
    return re.sub(r'\s+', ' ', (nombre or '').strip()).lower()


def _platform_db():
    return MongoDBConnection.get_client()["_platform"]


def list_marcas_handler(event, context):
    """GET /catalogos/autos/marcas
    Lista de nombres de marcas para selectores. Une el catálogo administrable
    (`_platform.marcas` activas) con las marcas legacy presentes en vehículos.
    """
    try:
        db = _platform_db()

        nombres = {m.get("nombre") for m in db["marcas"].find({"activa": True}, {"nombre": 1})}
        # Legacy: marcas escritas directamente en vehículos aunque no estén en el catálogo.
        nombres |= {x for x in db["vehiculos"].distinct("marca") if x}

        return create_response(200, "Marcas recuperadas", sorted(n for n in nombres if n))

    except Exception as e:
        return handle_exception(e)


def list_marcas_admin_handler(event, context):
    """GET /catalogos/autos/marcas/admin
    Devuelve el catálogo completo (incluye inactivas) con id/activa para la UI de
    Configuración. Siembra la colección desde las marcas legacy en el primer acceso.
    """
    try:
        db = _platform_db()
        col = db["marcas"]

        if col.count_documents({}) == 0:
            now = datetime.utcnow()
            legacy = sorted({x for x in db["vehiculos"].distinct("marca") if x})
            if legacy:
                col.insert_many([
                    {"nombre": n, "nombre_norm": _norm(n), "activa": True,
                     "createdAt": now, "updatedAt": now}
                    for n in legacy
                ])

        docs = list(col.find().sort("nombre", 1))
        marcas = [{
            "id": str(d["_id"]),
            "nombre": d.get("nombre"),
            "activa": bool(d.get("activa", True)),
        } for d in docs]

        return create_response(200, "Catálogo de marcas", marcas)

    except Exception as e:
        return handle_exception(e)


def create_marca_handler(event, context):
    """POST /catalogos/autos/marcas  body: {nombre}"""
    try:
        claims = _get_claims(event)
        if not is_admin(claims):
            return create_response(403, "No tiene permisos para administrar marcas.")

        body = json.loads(event.get('body', '{}'))
        nombre = (body.get('nombre') or '').strip()
        if not nombre:
            return create_response(400, "El nombre de la marca es obligatorio.")

        db = _platform_db()
        col = db["marcas"]
        norm = _norm(nombre)

        if col.find_one({"nombre_norm": norm}):
            return create_response(409, f"La marca '{nombre}' ya existe.")

        now = datetime.utcnow()
        doc = {"nombre": nombre, "nombre_norm": norm, "activa": True,
               "createdAt": now, "updatedAt": now}
        inserted = col.insert_one(doc)

        return create_response(201, "Marca creada", {
            "id": str(inserted.inserted_id),
            "nombre": nombre,
            "activa": True,
        })

    except Exception as e:
        return handle_exception(e)


def update_marca_handler(event, context):
    """PUT /catalogos/autos/marcas/{id}  body: {nombre?, activa?}
    Permite renombrar o activar/desactivar. No existe borrado por diseño.
    """
    try:
        claims = _get_claims(event)
        if not is_admin(claims):
            return create_response(403, "No tiene permisos para administrar marcas.")

        marca_id = (event.get('pathParameters') or {}).get('id')
        try:
            oid = ObjectId(marca_id)
        except (InvalidId, TypeError):
            return create_response(400, "id de marca inválido.")

        body = json.loads(event.get('body', '{}'))
        db = _platform_db()
        col = db["marcas"]

        update = {"updatedAt": datetime.utcnow()}
        if 'nombre' in body:
            nombre = (body.get('nombre') or '').strip()
            if not nombre:
                return create_response(400, "El nombre no puede quedar vacío.")
            norm = _norm(nombre)
            dup = col.find_one({"nombre_norm": norm, "_id": {"$ne": oid}})
            if dup:
                return create_response(409, f"Ya existe otra marca '{nombre}'.")
            update['nombre'] = nombre
            update['nombre_norm'] = norm
        if 'activa' in body:
            update['activa'] = bool(body.get('activa'))

        res = col.update_one({"_id": oid}, {"$set": update})
        if res.matched_count == 0:
            return create_response(404, "Marca no encontrada.")

        doc = col.find_one({"_id": oid})
        return create_response(200, "Marca actualizada", {
            "id": str(doc["_id"]),
            "nombre": doc.get("nombre"),
            "activa": bool(doc.get("activa", True)),
        })

    except Exception as e:
        return handle_exception(e)


def list_modelos_handler(event, context):
    """
    Obtiene la lista única de modelos para una marca específica.
    """
    try:
        query_params = event.get('queryStringParameters') or {}
        marca = query_params.get('marca')

        if not marca:
            return create_response(400, "El parámetro 'marca' es obligatorio")

        db = _platform_db()

        # Obtenemos modelos únicos filtrados por marca (insensible a mayúsculas)
        regex = re.compile(f"^{re.escape(marca)}$", re.IGNORECASE)
        modelos = sorted(db["vehiculos"].distinct("modelo", {"marca": regex}))

        return create_response(200, f"Modelos para {marca} recuperados", modelos)

    except Exception as e:
        return handle_exception(e)
