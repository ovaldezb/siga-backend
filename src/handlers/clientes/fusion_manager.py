"""POST /clientes/{id}/fusionar — une un cliente duplicado con el que se conserva.

La cartera es del taller, no de la sucursal, pero durante meses el listado de
Clientes filtraba por la sucursal activa: quien atendía en otra sucursal no
encontraba al cliente y lo volvía a dar de alta (reporte 2026-10-28). El
resultado son dos registros de la misma persona con su historial partido.

Fusionar mueve TODO lo del duplicado al cliente `{id}` y después lo borra:
vehículos, OS, cotizaciones, ventas (con su saldo de crédito y abonos) y citas.
Los datos de contacto o fiscales que le falten al que se conserva se toman del
duplicado; los que ya tiene no se pisan. El duplicado se archiva completo en
`clientes_fusionados` (quién, cuándo y cuánto se movió) para poder auditarlo.

Es idempotente ante fallos: el duplicado se borra al final, así que si algo
truena a la mitad basta con repetir la petición. Los CFDI (`facturasemitidas`)
no se tocan: no guardan el id del cliente y una factura timbrada es inmutable.
Sólo ADMIN: es una operación de limpieza de catálogo que no se deshace sola.
"""
import json

from aws_lambda_powertools import Logger

from src.shared.infrastructure.database import get_tenant_db
from src.shared.utils.auth_utils import get_claims, is_admin, parse_object_id
from src.shared.utils.date_utils import iso_utc
from src.shared.utils.propagacion import propagar_cliente
from src.shared.utils.response_handler import create_response, handle_exception

logger = Logger()

RFC_GENERICOS = {"XAXX010101000", "XEXX010101000"}

# Campos que se heredan del duplicado sólo si el que se conserva no los tiene.
CAMPOS_HEREDABLES = (
    "apellido_materno", "telefono", "email", "razon_social", "regimen_fiscal",
    "uso_cfdi", "codigo_postal", "flotilla_id",
)


def _vacio(valor) -> bool:
    return valor is None or (isinstance(valor, str) and not valor.strip())


def _datos_heredados(conservado: dict, duplicado: dict) -> dict:
    cambios = {}
    for campo in CAMPOS_HEREDABLES:
        if _vacio(conservado.get(campo)) and not _vacio(duplicado.get(campo)):
            cambios[campo] = duplicado[campo]

    rfc_c = (conservado.get("rfc") or "").strip().upper()
    rfc_d = (duplicado.get("rfc") or "").strip().upper()
    if (not rfc_c or rfc_c in RFC_GENERICOS) and rfc_d and rfc_d not in RFC_GENERICOS:
        cambios["rfc"] = rfc_d
        # El régimen y el tipo de persona van con el RFC: si se hereda uno, los demás también.
        for campo in ("tipo_persona", "regimen_fiscal", "uso_cfdi", "codigo_postal", "razon_social"):
            if not _vacio(duplicado.get(campo)):
                cambios[campo] = duplicado[campo]

    # Crédito: se queda la línea más amplia. Las deudas de ambos se suman solas
    # al mover las ventas, así que el límite menor dejaría al cliente sobregirado.
    for campo, tipo in (("limite_credito", float), ("dias_credito", int)):
        try:
            mayor = max(tipo(conservado.get(campo) or 0), tipo(duplicado.get(campo) or 0))
        except (TypeError, ValueError):
            continue
        if mayor != tipo(conservado.get(campo) or 0):
            cambios[campo] = mayor
    return cambios


def _resumen_vehiculos(conservado: dict, duplicado: dict) -> list:
    vistos, salida = set(), []
    for v in (conservado.get("vehiculos_resumen") or []) + (duplicado.get("vehiculos_resumen") or []):
        vid = v.get("id") if isinstance(v, dict) else None
        if vid and vid in vistos:
            continue
        if vid:
            vistos.add(vid)
        salida.append(v)
    return salida


def fusionar_cliente_handler(event, context):
    try:
        claims = get_claims(event)
        tenant_id = claims.get("custom:tenant_id")
        if not tenant_id:
            return create_response(403, "No se encontró un tenantId asociado.")
        if not is_admin(claims):
            return create_response(403, "Sólo un administrador puede fusionar clientes.")

        conservado_id = (event.get("pathParameters") or {}).get("id") or ""
        body = json.loads(event.get("body") or "{}")
        duplicado_id = str(body.get("duplicado_id") or "").strip()

        oid_c, err = parse_object_id(conservado_id)
        if err:
            return create_response(400, err)
        oid_d, err = parse_object_id(duplicado_id)
        if err:
            return create_response(400, "El campo 'duplicado_id' es obligatorio y debe ser un ID válido.")
        if oid_c == oid_d:
            return create_response(400, "No se puede fusionar un cliente consigo mismo.")

        db = get_tenant_db(tenant_id)
        conservado = db.clientes.find_one({"_id": oid_c})
        if not conservado:
            return create_response(404, "No se encontró el cliente que se conserva.")
        duplicado = db.clientes.find_one({"_id": oid_d})
        if not duplicado:
            return create_response(404, "No se encontró el cliente duplicado (¿ya se fusionó?).")

        flotilla_c, flotilla_d = conservado.get("flotilla_id"), duplicado.get("flotilla_id")
        if flotilla_c and flotilla_d and flotilla_c != flotilla_d:
            return create_response(
                409,
                "Los dos clientes pertenecen a flotillas distintas. Saca a uno de su flotilla antes de fusionarlos.",
            )

        cid, did = conservado_id, duplicado_id
        movidos = {
            "vehiculos": db["vehiculos"].update_many(
                {"cliente_id": did}, {"$set": {"cliente_id": cid}}).modified_count,
            # Las OS guardan al cliente en el snapshot; algunas viejas sólo en la raíz.
            "ordenes_servicio": db["ordenes_servicio"].update_many(
                {"cliente_snapshot.id": did}, {"$set": {"cliente_snapshot.id": cid}}).modified_count
            + db["ordenes_servicio"].update_many(
                {"cliente_id": did}, {"$set": {"cliente_id": cid}}).modified_count,
            "cotizaciones": db["cotizaciones"].update_many(
                {"cliente_snapshot.id": did}, {"$set": {"cliente_snapshot.id": cid}}).modified_count,
            # Con las ventas viaja el saldo de crédito y sus abonos.
            "ventas": db["ventas"].update_many(
                {"cliente_id": did}, {"$set": {"cliente_id": cid}}).modified_count,
            "citas": db["citas"].update_many(
                {"clienteId": did}, {"$set": {"clienteId": cid}}).modified_count
            + db["citas"].update_many(
                {"cliente_id": did}, {"$set": {"cliente_id": cid}}).modified_count,
        }

        cambios = _datos_heredados(conservado, duplicado)
        cambios["vehiculos_resumen"] = _resumen_vehiculos(conservado, duplicado)
        cambios["updatedAt"] = iso_utc()
        db.clientes.update_one({"_id": oid_c}, {"$set": cambios})
        conservado.update(cambios)

        # Lo movido aún trae el nombre y teléfono del duplicado en sus snapshots.
        try:
            propagar_cliente(db, conservado)
        except Exception as prop_err:
            logger.warning(f"No se pudo propagar el cliente {cid} tras fusionar: {prop_err}")

        archivo = dict(duplicado)
        archivo["cliente_original_id"] = did
        archivo.pop("_id", None)
        archivo.update({
            "fusionado_en": cid,
            "fusionado_por": claims.get("email") or claims.get("sub"),
            "fusionado_at": iso_utc(),
            "movidos": movidos,
            "heredados": sorted(k for k in cambios if k not in ("vehiculos_resumen", "updatedAt")),
        })
        db["clientes_fusionados"].insert_one(archivo)
        db.clientes.delete_one({"_id": oid_d})

        logger.info("Cliente fusionado", extra={"conservado": cid, "duplicado": did, "movidos": movidos})

        conservado["id"] = str(conservado.pop("_id"))
        if "sucursal_id" in conservado:
            conservado["sucursalId"] = conservado.pop("sucursal_id")
        return create_response(200, "Clientes fusionados", {
            "cliente": conservado,
            "movidos": movidos,
            "heredados": archivo["heredados"],
        })
    except json.JSONDecodeError:
        return create_response(400, "El cuerpo de la petición no es JSON válido.")
    except Exception as e:
        return handle_exception(e)
