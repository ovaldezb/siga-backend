"""Importación masiva de inventario desde la plantilla de Excel.

Flujo pensado para que un error de captura no termine en el catálogo:

  1. `POST /items/importar/preview` — ensayo en seco. Clasifica cada fila (NUEVO,
     ACTUALIZA, SIN_CAMBIOS, ERROR) y devuelve el antes → después de cada campo.
     No escribe nada.
  2. `POST /items/importar` — aplica. Vuelve a analizar contra la base (no confía
     en el preview) y sólo escribe las filas válidas. Todo queda registrado como
     un lote en `importaciones_inventario` con el antes/después de cada artículo
     tocado en `importaciones_inventario_cambios`.
  3. `POST /items/importaciones/{id}/deshacer` — revierte el lote completo, pero
     sólo si nada de lo importado se movió después (una venta, un ajuste, una
     edición). Si hay conflictos no revierte nada y dice cuáles son: deshacer a
     medias dejaría el inventario en un estado que nadie capturó.

Reglas de la plantilla:
  · El número de parte es la llave: si ya existe en la sucursal se actualiza, si no
    se da de alta.
  · Celda vacía = "no tocar". Por importación no se borra un dato.
  · `existencia` es el conteo físico (valor absoluto), no una cantidad a sumar. La
    diferencia contra el sistema queda en el kardex como IMPORTACION.
"""
import json
import re
from datetime import datetime

from aws_lambda_powertools import Logger
from bson import ObjectId
from pymongo import UpdateOne

from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.auth_utils import get_claims, get_groups, resolve_sucursal_scope
from src.shared.utils.date_utils import iso_utc
from src.shared.utils.response_handler import create_response, handle_exception
from src.handlers.inventario.items_manager import (
    CAMPOS_CATALOGO_COMPARTIDOS,
    _es_inventariable,
    _sucursales_tenant,
    _variantes_no_parte,
)

logger = Logger()

MAX_FILAS = 2000
ROLES_IMPORTACION = {"ADMIN", "SUPER_ADMIN", "GERENTE"}
TIPOS = {"PRODUCTO", "SERVICIO"}

CAMPOS_TEXTO = ("nombre", "marca", "categoria", "clave_sat", "unidad_sat")
CAMPOS_PRECIO = ("precio_venta", "precio_taller", "precio_cliente", "precio_distribuidor", "precio_compra")

COL_LOTES = "importaciones_inventario"
COL_CAMBIOS = "importaciones_inventario_cambios"


# ─── Utilidades ──────────────────────────────────────────────────────────────

def _texto(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        # Excel entrega "12345" como 12345.0: el número de parte no lleva ".0".
        v = int(v)
    return str(v).strip()


def _numero(v):
    """(valor, error). Acepta "$1,250.50"; vacío ⇒ (None, None)."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None, None
    if isinstance(v, bool):
        return None, "no es un número"
    if isinstance(v, (int, float)):
        n = float(v)
    else:
        limpio = re.sub(r"[\s$]", "", str(v)).replace(",", "")
        try:
            n = float(limpio)
        except ValueError:
            return None, f"'{v}' no es un número"
    if n != n or n in (float("inf"), float("-inf")):
        return None, "no es un número"
    if n < 0:
        return None, "no puede ser negativo"
    return round(n, 4), None


def _igual(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(float(a) - float(b)) < 1e-6
    if a in (None, "") and b in (None, ""):
        return True
    return a == b


def _usuario(claims):
    return {
        "usuario_id": claims.get("sub"),
        "usuario_nombre": claims.get("name") or claims.get("email"),
    }


def _puede_importar(claims):
    return bool(set(get_groups(claims)) & ROLES_IMPORTACION)


def _contexto(event):
    """(claims, tenant_id, db, sucursal_id, body, error_response)."""
    claims = get_claims(event)
    tenant_id = claims.get("custom:tenant_id")
    if not tenant_id:
        return None, None, None, None, None, create_response(403, "No se encontró un tenantId asociado.")
    if not _puede_importar(claims):
        return None, None, None, None, None, create_response(
            403, "Solo un administrador o el gerente de la sucursal puede importar inventario.")

    body = json.loads(event.get("body") or "{}")
    qp = event.get("queryStringParameters") or {}
    sucursal_id = (body.get("sucursal_id") or body.get("sucursalId")
                   or qp.get("sucursal_id") or qp.get("sucursalId"))
    if not sucursal_id:
        return None, None, None, None, None, create_response(400, "Indique la sucursal a la que se importa.")

    db = get_tenant_db(tenant_id)
    _, scope_err = resolve_sucursal_scope(claims, db, sucursal_id)
    if scope_err:
        return None, None, None, None, None, create_response(403, scope_err)
    return claims, tenant_id, db, str(sucursal_id), body, None


# ─── Análisis (compartido por preview y aplicar) ─────────────────────────────

def _leer_fila(cruda):
    """Normaliza una fila de la plantilla. Devuelve (datos, errores)."""
    errores = []
    datos: dict = {"no_parte": _texto(cruda.get("no_parte"))}
    for campo in CAMPOS_TEXTO:
        datos[campo] = _texto(cruda.get(campo))
    datos["proveedor"] = _texto(cruda.get("proveedor"))

    tipo = _texto(cruda.get("tipo")).upper()
    if tipo and tipo not in TIPOS:
        errores.append(f"tipo '{tipo}' no válido (use PRODUCTO o SERVICIO)")
        tipo = ""
    datos["tipo"] = tipo

    for campo in CAMPOS_PRECIO:
        valor, err = _numero(cruda.get(campo))
        if err:
            errores.append(f"{campo}: {err}")
        datos[campo] = valor

    existencia, err = _numero(cruda.get("existencia"))
    if err:
        errores.append(f"existencia: {err}")
    elif existencia is not None and not float(existencia).is_integer():
        errores.append("existencia: debe ser un número entero")
        existencia = None
    datos["existencia"] = int(existencia) if existencia is not None else None

    if not datos["no_parte"]:
        errores.append("falta el número de parte")
    return datos, errores


def _cambios_actualizacion(datos, actual, proveedores, avisos):
    """Campo → {antes, despues} sólo de lo que de verdad cambia."""
    cambios = {}

    def poner(campo, nuevo):
        anterior = actual.get(campo)
        if not _igual(anterior, nuevo):
            cambios[campo] = {"antes": anterior, "despues": nuevo}

    for campo in CAMPOS_TEXTO:
        if datos[campo]:
            poner(campo, datos[campo])
    for campo in CAMPOS_PRECIO:
        if datos[campo] is not None:
            poner(campo, datos[campo])

    # El costo promedio lo lleva compras; sólo se siembra si el artículo no tenía.
    if "precio_compra" in cambios and not actual.get("costo_promedio"):
        poner("costo_promedio", datos["precio_compra"])

    if datos["proveedor"]:
        prov = proveedores.get(datos["proveedor"].lower())
        nombre = prov["nombre"] if prov else datos["proveedor"]
        if not _igual((actual.get("proveedor") or "").lower(), nombre.lower()):
            poner("proveedor", nombre)
            poner("proveedor_id", str(prov["_id"]) if prov else None)

    # La clave SAT cambió: su descripción vieja ya no le corresponde.
    if "clave_sat" in cambios and actual.get("descripcion_clave_sat"):
        poner("descripcion_clave_sat", "")
    if "unidad_sat" in cambios and actual.get("descripcion_unidad_sat"):
        poner("descripcion_unidad_sat", "")

    if datos["tipo"] and datos["tipo"] != (actual.get("tipo") or "PRODUCTO"):
        avisos.append("el tipo no se cambia por importación; se conserva " + (actual.get("tipo") or "PRODUCTO"))

    if datos["existencia"] is not None:
        if _es_inventariable(actual):
            poner("stock", datos["existencia"])
        else:
            avisos.append("este artículo no maneja inventario; la existencia se ignora")
    return cambios


def _documento_nuevo(datos, proveedores, tenant_id, sucursal_id, avisos):
    tipo = datos["tipo"] or "PRODUCTO"
    prov = proveedores.get(datos["proveedor"].lower()) if datos["proveedor"] else None
    costo = datos["precio_compra"] or 0.0
    doc = {
        "tipo": tipo,
        "nombre": datos["nombre"],
        "no_parte": datos["no_parte"],
        "precio_venta": datos["precio_venta"],
        "precio_taller": datos["precio_taller"] or 0.0,
        "precio_cliente": datos["precio_cliente"] or 0.0,
        "precio_distribuidor": datos["precio_distribuidor"] or 0.0,
        "precio_compra": costo,
        "costo_promedio": costo,
        "precio_incluye_iva": True,
        "iva_exento": False,
        "categoria": datos["categoria"] or "GENERAL",
        "marca": datos["marca"],
        "proveedor": prov["nombre"] if prov else datos["proveedor"],
        "proveedor_id": str(prov["_id"]) if prov else None,
        "sucursal_id": sucursal_id,
        "tenant_id": tenant_id,
        "activo": True,
        "icon": "ri-archive-line" if tipo == "PRODUCTO" else "ri-tools-line",
    }
    if tipo == "PRODUCTO":
        doc.update({
            "maneja_inventario": True,
            "stock": datos["existencia"] or 0,
            "clave_sat": datos["clave_sat"] or None,
            "unidad_sat": datos["unidad_sat"] or None,
        })
    else:
        if datos["existencia"]:
            avisos.append("los servicios no llevan existencia; se ignora")
        doc.update({"maneja_inventario": False, "stock": None})
    return doc


def _analizar(db, tenant_id, sucursal_id, filas):
    """Plan de importación. No escribe nada."""
    leidas = []
    for i, cruda in enumerate(filas):
        if not isinstance(cruda, dict):
            continue
        datos, errores = _leer_fila(cruda)
        try:
            numero = int(cruda.get("fila") or i + 2)
        except (TypeError, ValueError):
            numero = i + 2
        leidas.append({"fila": numero, "datos": datos, "errores": errores, "avisos": []})

    # Mismo número de parte en dos filas: no hay forma de saber cuál vale.
    por_np = {}
    for r in leidas:
        np = r["datos"]["no_parte"].lower()
        if np:
            por_np.setdefault(np, []).append(r["fila"])
    for r in leidas:
        repetidas = por_np.get(r["datos"]["no_parte"].lower(), [])
        if len(repetidas) > 1:
            otras = ", ".join(str(f) for f in repetidas if f != r["fila"])
            r["errores"].append(f"número de parte repetido en el archivo (fila {otras})")

    variantes = _variantes_no_parte([r["datos"]["no_parte"] for r in leidas])
    existentes = {}
    if variantes:
        for d in db["items"].find({"sucursal_id": sucursal_id, "no_parte": {"$in": variantes}}):
            existentes.setdefault((d.get("no_parte") or "").strip().lower(), d)

    proveedores = {}
    for p in db["proveedores"].find({}, {"nombre": 1}):
        if p.get("nombre"):
            proveedores.setdefault(p["nombre"].strip().lower(), p)

    resultado = []
    for r in leidas:
        datos = r["datos"]
        actual = existentes.get(datos["no_parte"].lower())
        fila = {
            "fila": r["fila"],
            "no_parte": datos["no_parte"],
            "nombre": datos["nombre"] or (actual or {}).get("nombre") or "",
            "errores": r["errores"],
            "avisos": r["avisos"],
            "cambios": {},
        }
        if not r["errores"]:
            if actual:
                fila["item_id"] = str(actual["_id"])
                fila["cambios"] = _cambios_actualizacion(datos, actual, proveedores, fila["avisos"])
                fila["estado"] = "ACTUALIZA" if fila["cambios"] else "SIN_CAMBIOS"
            else:
                if not datos["nombre"]:
                    fila["errores"].append("artículo nuevo sin nombre")
                if datos["precio_venta"] is None:
                    fila["errores"].append("artículo nuevo sin precio de venta")
                if not fila["errores"]:
                    fila["nuevo"] = _documento_nuevo(datos, proveedores, tenant_id, sucursal_id, fila["avisos"])
                    fila["estado"] = "NUEVO"
                    if datos["proveedor"] and not fila["nuevo"]["proveedor_id"]:
                        fila["avisos"].append(
                            f"el proveedor '{datos['proveedor']}' no está en tu catálogo; se guarda sólo como texto")
        if fila["errores"]:
            fila["estado"] = "ERROR"
            fila["cambios"] = {}
        resultado.append(fila)

    resumen = {
        "total": len(resultado),
        "nuevos": sum(1 for f in resultado if f["estado"] == "NUEVO"),
        "actualizados": sum(1 for f in resultado if f["estado"] == "ACTUALIZA"),
        "sin_cambios": sum(1 for f in resultado if f["estado"] == "SIN_CAMBIOS"),
        "errores": sum(1 for f in resultado if f["estado"] == "ERROR"),
    }
    return resultado, resumen


def _filas_del_body(body):
    filas = body.get("filas")
    if not isinstance(filas, list) or not filas:
        return None, create_response(400, "El archivo no trae filas para importar.")
    if len(filas) > MAX_FILAS:
        return None, create_response(
            400, f"El archivo trae {len(filas)} filas; el máximo por importación es {MAX_FILAS}. Divídalo en partes.")
    return filas, None


def _vista(fila):
    """Fila tal como la ve el front (sin el documento interno)."""
    return {k: v for k, v in fila.items() if k != "nuevo"}


# ─── Handlers ────────────────────────────────────────────────────────────────

def preview_importacion_handler(event, context):
    """POST /items/importar/preview — ensayo en seco, no escribe nada."""
    try:
        _, tenant_id, db, sucursal_id, body, err = _contexto(event)
        if err:
            return err
        filas, err = _filas_del_body(body)
        if err:
            return err
        resultado, resumen = _analizar(db, tenant_id, sucursal_id, filas)
        return create_response(200, "Vista previa de la importación", {
            "resumen": resumen,
            "filas": [_vista(f) for f in resultado],
        })
    except Exception as e:
        return handle_exception(e, event)


def _replicas(db, tenant_id, sucursal_id, nuevos):
    """Clones con stock 0 en las demás sucursales activas, como el alta manual."""
    destinos = [s for s in _sucursales_tenant(db) if s["activa"] and s["id"] != sucursal_id]
    if not destinos or not nuevos:
        return []
    variantes = _variantes_no_parte([d["no_parte"] for d in nuevos])
    ya = set()
    for d in db["items"].find({"no_parte": {"$in": variantes},
                               "sucursal_id": {"$in": [s["id"] for s in destinos]}},
                              {"no_parte": 1, "sucursal_id": 1}):
        ya.add((str(d.get("sucursal_id")), (d.get("no_parte") or "").strip().lower()))

    clones = []
    for base in nuevos:
        for s in destinos:
            if (s["id"], base["no_parte"].lower()) in ya:
                continue
            clone = {k: v for k, v in base.items() if k != "_id"}
            clone["sucursal_id"] = s["id"]
            clone["tenant_id"] = tenant_id
            clone["stock"] = 0 if _es_inventariable(clone) else None
            clone["clonado_de"] = str(base["_id"])
            clones.append(clone)
    return clones


def _propagaciones(db, sucursal_id, actualizaciones):
    """Cambios de catálogo hacia el mismo número de parte en otras sucursales.

    Devuelve [(item_id, antes, despues)] con lo que cambia en cada hermano, para
    poder deshacerlo igual que lo importado. Nunca toca stock ni costos.
    """
    por_np = {}
    for fila in actualizaciones:
        cat = {k: c["despues"] for k, c in fila["cambios"].items() if k in CAMPOS_CATALOGO_COMPARTIDOS}
        if cat:
            por_np[fila["no_parte"].lower()] = cat
    if not por_np:
        return []
    salida = []
    for d in db["items"].find({"no_parte": {"$in": _variantes_no_parte(list(por_np))},
                               "sucursal_id": {"$ne": sucursal_id}}):
        cat = por_np.get((d.get("no_parte") or "").strip().lower())
        if not cat:
            continue
        antes = {k: d.get(k) for k, v in cat.items() if not _igual(d.get(k), v)}
        if antes:
            salida.append((d["_id"], antes, {k: cat[k] for k in antes}))
    return salida


def aplicar_importacion_handler(event, context):
    """POST /items/importar — aplica las filas válidas y registra el lote para deshacer."""
    lote_id = None
    db = None
    try:
        claims, tenant_id, db, sucursal_id, body, err = _contexto(event)
        if err:
            return err
        filas, err = _filas_del_body(body)
        if err:
            return err
        compartir = body.get("compartir_entre_sucursales", True) is not False

        resultado, resumen = _analizar(db, tenant_id, sucursal_id, filas)
        nuevas = [f for f in resultado if f["estado"] == "NUEVO"]
        actualizaciones = [f for f in resultado if f["estado"] == "ACTUALIZA"]
        if not nuevas and not actualizaciones:
            return create_response(400, "No hay filas válidas con cambios para importar.", {
                "resumen": resumen, "filas": [_vista(f) for f in resultado]})

        ahora = iso_utc()
        lote_id = ObjectId()
        usuario = _usuario(claims)

        # 1. Todo lo que se va a escribir, con su antes/después.
        docs_nuevos = []
        for f in nuevas:
            doc = dict(f["nuevo"], _id=ObjectId(), createdAt=ahora, importacion_id=str(lote_id))
            docs_nuevos.append(doc)
        clones = _replicas(db, tenant_id, sucursal_id, docs_nuevos) if compartir else []
        for c in clones:
            c.update(_id=ObjectId(), createdAt=ahora, importacion_id=str(lote_id))
        propagaciones = _propagaciones(db, sucursal_id, actualizaciones) if compartir else []

        cambios = []
        for d in docs_nuevos + clones:
            cambios.append({"accion": "CREADO", "item_id": d["_id"],
                            "despues": {"stock": d.get("stock"), "precio_venta": d.get("precio_venta"),
                                        "nombre": d.get("nombre")}})
        for f in actualizaciones:
            cambios.append({"accion": "MODIFICADO", "item_id": ObjectId(f["item_id"]),
                            "antes": {k: c["antes"] for k, c in f["cambios"].items()},
                            "despues": {k: c["despues"] for k, c in f["cambios"].items()}})
        for item_id, antes, despues in propagaciones:
            cambios.append({"accion": "MODIFICADO", "item_id": item_id, "antes": antes,
                            "despues": despues, "propagado": True})
        for c in cambios:
            c.update(lote_id=lote_id, createdAt=ahora)

        # 2. El lote se registra ANTES de escribir: si la Lambda muere a la mitad,
        #    queda INCOMPLETA y el deshacer sabe distinguir lo aplicado de lo que no.
        sucursal = db["sucursales"].find_one({"_id": ObjectId(sucursal_id)}, {"nombre": 1}) \
            if ObjectId.is_valid(sucursal_id) else None
        db[COL_LOTES].insert_one({
            "_id": lote_id,
            "tenant_id": tenant_id,
            "sucursal_id": sucursal_id,
            "sucursal_nombre": (sucursal or {}).get("nombre"),
            "archivo": str(body.get("archivo") or "")[:200],
            "estado": "APLICANDO",
            "compartir_entre_sucursales": compartir,
            "resumen": {**resumen, "replicados": len(clones), "propagados": len(propagaciones)},
            "createdAt": ahora,
            **usuario,
        })
        if cambios:
            db[COL_CAMBIOS].insert_many(cambios)

        # 3. Escrituras.
        if docs_nuevos or clones:
            db["items"].insert_many(docs_nuevos + clones)
        ops = [UpdateOne({"_id": ObjectId(f["item_id"]), "sucursal_id": sucursal_id},
                         {"$set": {**{k: c["despues"] for k, c in f["cambios"].items()}, "updatedAt": ahora}})
               for f in actualizaciones]
        ops += [UpdateOne({"_id": item_id}, {"$set": {**despues, "updatedAt": ahora}})
                for item_id, _, despues in propagaciones]
        if ops:
            db["items"].bulk_write(ops, ordered=False)

        # 4. Kardex: la diferencia de existencia queda como movimiento de importación.
        movimientos = []
        for d in docs_nuevos:
            if d.get("stock"):
                movimientos.append((str(d["_id"]), d["nombre"], 0, d["stock"]))
        for f in actualizaciones:
            c = f["cambios"].get("stock")
            if c:
                movimientos.append((f["item_id"], f["nombre"], c["antes"] or 0, c["despues"]))
        if movimientos:
            db["inventario_movimientos"].insert_many([{
                "tenant_id": tenant_id,
                "item_id": item_id,
                "item_nombre": nombre,
                "sucursal_id": sucursal_id,
                "cantidad": despues - antes,
                "stock_anterior": antes,
                "stock_resultante": despues,
                "concepto": "IMPORTACION",
                "motivo": f"Importación de inventario {body.get('archivo') or ''}".strip(),
                "importacion_id": str(lote_id),
                "createdAt": datetime.utcnow(),
                **usuario,
            } for item_id, nombre, antes, despues in movimientos])

        db[COL_LOTES].update_one({"_id": lote_id}, {"$set": {"estado": "APLICADA"}})

        partes = []
        if nuevas:
            partes.append(f"{len(nuevas)} nuevo(s)")
        if actualizaciones:
            partes.append(f"{len(actualizaciones)} actualizado(s)")
        if resumen["errores"]:
            partes.append(f"{resumen['errores']} fila(s) con error omitidas")
        return create_response(200, "Importación aplicada: " + ", ".join(partes) + ".", {
            "lote_id": str(lote_id),
            "resumen": {**resumen, "replicados": len(clones), "propagados": len(propagaciones)},
        })
    except Exception as e:
        if lote_id is not None and db is not None:
            try:
                db[COL_LOTES].update_one({"_id": lote_id, "estado": "APLICANDO"},
                                         {"$set": {"estado": "INCOMPLETA"}})
            except Exception:
                pass
        return handle_exception(e, event)


def list_importaciones_handler(event, context):
    """GET /items/importaciones?sucursal_id= — últimas importaciones de la sucursal.

    En producción lo atiende la lambda Go `items_importaciones_list`
    (go/internal/inventario/importaciones.go); éste queda para rollback.
    """
    try:
        claims, _, db, sucursal_id, _, err = _contexto(event)
        if err:
            return err
        lotes = list(db[COL_LOTES].find({"sucursal_id": sucursal_id}).sort("createdAt", -1).limit(20))
        for l in lotes:
            l["id"] = str(l.pop("_id"))
            l["puede_deshacer"] = l.get("estado") in ("APLICADA", "INCOMPLETA")
        return create_response(200, "Importaciones", lotes)
    except Exception as e:
        return handle_exception(e, event)


def deshacer_importacion_handler(event, context):
    """POST /items/importaciones/{id}/deshacer — revierte un lote completo o nada."""
    try:
        claims = get_claims(event)
        tenant_id = claims.get("custom:tenant_id")
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")
        if not _puede_importar(claims):
            return create_response(403, "Solo un administrador o el gerente de la sucursal puede deshacer una importación.")

        lote_oid = ObjectId(event["pathParameters"]["id"])
        db = get_tenant_db(tenant_id)
        lote = db[COL_LOTES].find_one({"_id": lote_oid})
        if not lote:
            return create_response(404, "Importación no encontrada.")
        _, scope_err = resolve_sucursal_scope(claims, db, lote.get("sucursal_id"))
        if scope_err:
            return create_response(403, scope_err)
        if lote.get("estado") == "DESHECHA":
            return create_response(409, "Esta importación ya se deshizo.")
        if lote.get("estado") == "APLICANDO":
            return create_response(409, "La importación todavía se está aplicando; intente en un momento.")

        cambios = list(db[COL_CAMBIOS].find({"lote_id": lote_oid}))
        ids = [c["item_id"] for c in cambios]

        # Una importación posterior sobre los mismos artículos se deshace primero:
        # revertir ésta pisaría lo que aquélla dejó.
        posteriores = {c["lote_id"] for c in db[COL_CAMBIOS].find(
            {"item_id": {"$in": ids}, "lote_id": {"$ne": lote_oid}, "createdAt": {"$gt": lote["createdAt"]}},
            {"lote_id": 1})}
        if posteriores:
            vivas = list(db[COL_LOTES].find({"_id": {"$in": list(posteriores)},
                                             "estado": {"$in": ["APLICADA", "INCOMPLETA", "APLICANDO"]}},
                                            {"createdAt": 1, "archivo": 1}))
            if vivas:
                return create_response(409, "Hay una importación más reciente sobre los mismos artículos; "
                                            "deshaga esa primero.", {
                    "importaciones": [{"id": str(v["_id"]), "archivo": v.get("archivo"),
                                       "createdAt": v.get("createdAt")} for v in vivas]})

        actuales = {d["_id"]: d for d in db["items"].find({"_id": {"$in": ids}})}
        borrar, restaurar, conflictos = [], [], []
        for c in cambios:
            actual = actuales.get(c["item_id"])
            despues = c.get("despues") or {}
            if c["accion"] == "CREADO":
                if actual is None:
                    continue  # ya no existe: nada que revertir
                if all(_igual(actual.get(k), v) for k, v in despues.items()):
                    borrar.append(actual)
                else:
                    conflictos.append((actual, "se modificó después de importarlo (existencia o precio)"))
                continue
            if actual is None:
                conflictos.append(({"_id": c["item_id"]}, "el artículo se eliminó después de importarlo"))
                continue
            antes = c.get("antes") or {}
            if all(_igual(actual.get(k), v) for k, v in despues.items()):
                restaurar.append((actual, antes, despues))
            elif all(_igual(actual.get(k), v) for k, v in antes.items()):
                continue  # el lote quedó incompleto y este cambio nunca se aplicó
            else:
                distintos = [k for k, v in despues.items() if not _igual(actual.get(k), v)]
                conflictos.append((actual, "cambió después de importarlo: " + ", ".join(distintos)))

        if conflictos:
            return create_response(409, f"No se puede deshacer: {len(conflictos)} artículo(s) tuvieron movimientos "
                                        "después de la importación. No se revirtió nada.", {
                "conflictos": [{"item_id": str(a.get("_id")), "no_parte": a.get("no_parte"),
                                "nombre": a.get("nombre"), "sucursal_id": a.get("sucursal_id"),
                                "motivo": motivo} for a, motivo in conflictos[:100]],
                "total_conflictos": len(conflictos),
            })

        ahora = iso_utc()
        usuario = _usuario(claims)
        movimientos = []
        if borrar:
            db["items"].delete_many({"_id": {"$in": [d["_id"] for d in borrar]}})
            for d in borrar:
                if d.get("stock") and d.get("sucursal_id") == lote["sucursal_id"]:
                    movimientos.append((d, d["stock"], 0))
        if restaurar:
            db["items"].bulk_write([UpdateOne({"_id": a["_id"]}, {"$set": {**antes, "updatedAt": ahora}})
                                    for a, antes, _ in restaurar], ordered=False)
            for a, antes, despues in restaurar:
                if "stock" in despues:
                    movimientos.append((a, despues["stock"], antes.get("stock") or 0))
        if movimientos:
            db["inventario_movimientos"].insert_many([{
                "tenant_id": tenant_id,
                "item_id": str(d["_id"]),
                "item_nombre": d.get("nombre"),
                "sucursal_id": d.get("sucursal_id"),
                "cantidad": hacia - desde,
                "stock_anterior": desde,
                "stock_resultante": hacia,
                "concepto": "IMPORTACION_DESHECHA",
                "motivo": f"Se deshizo la importación {lote.get('archivo') or ''}".strip(),
                "importacion_id": str(lote_oid),
                "createdAt": datetime.utcnow(),
                **usuario,
            } for d, desde, hacia in movimientos])

        db[COL_LOTES].update_one({"_id": lote_oid}, {"$set": {
            "estado": "DESHECHA", "deshecha_at": ahora,
            "deshecha_por": usuario["usuario_nombre"],
        }})
        return create_response(200, f"Importación deshecha: {len(borrar)} artículo(s) eliminado(s) y "
                                    f"{len(restaurar)} restaurado(s).", {
            "eliminados": len(borrar), "restaurados": len(restaurar)})
    except Exception as e:
        return handle_exception(e, event)
