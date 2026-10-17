"""Bitácora de auditoría del taller (colección `bitacora`, append-only).

Registra acciones sensibles que se permiten sin autorización previa pero que un
administrador debe poder revisar después: quién, cuándo, en qué venta y por
cuánto. Los handlers sólo insertan; lo único mutable de un evento es su marca de
revisión (`revisada`, `revisada_por`, `revisada_at`), que no altera lo registrado.

Tipos de evento:
- `PRECIO_MANO_OBRA` — en el POS se cobró una línea de mano de obra (servicio) a
  un precio distinto al del catálogo o al de la OS. El detalle lleva el precio de
  referencia, el aplicado y la diferencia total.
"""
from datetime import datetime
from bson import ObjectId

TIPO_PRECIO_MANO_OBRA = "PRECIO_MANO_OBRA"

# Tolerancia de redondeo al comparar precios (centavos).
_TOLERANCIA = 0.005

_indices_asegurados: set[str] = set()


def asegurar_indices(db, tenant_id: str) -> None:
    if tenant_id in _indices_asegurados:
        return
    db["bitacora"].create_index([("tipo", 1), ("ts", -1)], name="tipo_ts")
    db["bitacora"].create_index([("revisada", 1), ("ts", -1)], name="revisada_ts")
    db["bitacora"].create_index([("venta_id", 1)], name="venta")
    _indices_asegurados.add(tenant_id)


def _float(valor, defecto=0.0):
    try:
        return float(valor)
    except (TypeError, ValueError):
        return defecto


def _precio_coincide(precio: float, referencias: list[float], incluye_iva: bool) -> bool:
    """El POS cobra con IVA cuando el cajero activa "Incluir IVA": ese ×1.16 no es
    un cambio de precio, así que también cuenta como coincidencia."""
    for ref in referencias:
        if abs(precio - ref) <= _TOLERANCIA:
            return True
        if incluye_iva and abs(precio - round(ref * 1.16, 2)) <= _TOLERANCIA:
            return True
    return False


def detectar_precios_mano_obra(db, items: list, os_doc: dict | None) -> list[dict]:
    """Marca las líneas de mano de obra cobradas a un precio distinto del de referencia.

    La referencia se resuelve en el servidor, nunca con lo que mande el front:
      1. Línea importada de una OS (`linea_id`) → el precio pactado en la OS.
      2. Servicio del catálogo → cualquiera de sus precios (público, taller,
         cliente, distribuidor): elegir otra lista de precio no es "precio manual".
    Sin referencia verificable (captura libre sin catálogo ni OS) no se marca.
    Las líneas con `autorizacion_id` ya quedaron auditadas en `autorizaciones`.

    Muta cada línea marcada (`precio_manual`, `precio_referencia`) y devuelve el
    detalle de cada una para insertarlo en la bitácora.
    """
    lineas_os = {}
    for punto in (os_doc or {}).get('puntosArreglar', []) or []:
        for linea in (punto.get('items') or []):
            if linea.get('linea_id'):
                lineas_os[linea['linea_id']] = linea

    ids_catalogo = []
    for it in items:
        prod = it.get('producto') or {}
        if prod.get('tipo') == 'SERVICIO' and not lineas_os.get(it.get('linea_id') or ''):
            pid = prod.get('id')
            if pid and pid != 'manual':
                ids_catalogo.append(pid)
    catalogo = {}
    if ids_catalogo:
        oids = [ObjectId(i) for i in ids_catalogo if ObjectId.is_valid(i)]
        for doc in db["items"].find({"_id": {"$in": oids}},
                                    {"precio_venta": 1, "precio_taller": 1, "precio_cliente": 1,
                                     "precio_distribuidor": 1, "nombre": 1}):
            catalogo[str(doc["_id"])] = doc

    eventos = []
    for it in items:
        prod = it.get('producto') or {}
        if prod.get('tipo') != 'SERVICIO' or it.get('autorizacion_id'):
            continue
        precio = _float(it.get('precio_unitario'))
        linea_os = lineas_os.get(it.get('linea_id') or '')
        if linea_os:
            ref = 0.0 if linea_os.get('no_cobrar') else _float(linea_os.get('precioVenta', linea_os.get('precio_venta')))
            referencias, origen = [ref], 'OS'
        elif str(prod.get('id')) in catalogo:
            doc = catalogo[str(prod.get('id'))]
            ref = _float(doc.get('precio_venta'))
            referencias = [ref] + [_float(doc.get(k)) for k in ('precio_taller', 'precio_cliente', 'precio_distribuidor')
                                   if _float(doc.get(k)) > 0]
            origen = 'CATALOGO'
        else:
            continue

        if _precio_coincide(precio, referencias, bool(it.get('incluye_iva'))):
            continue

        cantidad = int(_float(it.get('cantidad'), 1)) or 1
        it['precio_manual'] = True
        it['precio_referencia'] = round(ref, 2)
        eventos.append({
            'item_id': prod.get('id'),
            'linea_id': it.get('linea_id'),
            'nombre': prod.get('nombre') or it.get('nombre') or 'Mano de obra',
            'cantidad': cantidad,
            'precio_referencia': round(ref, 2),
            'precio_aplicado': round(precio, 2),
            'diferencia_total': round((precio - ref) * cantidad, 2),
            'origen_referencia': origen,
            'cortesia': it.get('tipo_precio') == 'cortesia',
        })
    return eventos


def construir_evento(tipo: str, *, tenant_id: str, sucursal_id, actor: dict, detalle: dict, **contexto) -> dict:
    return {
        "tenant_id": tenant_id,
        "tipo": tipo,
        "ts": datetime.utcnow(),
        "sucursal_id": sucursal_id,
        "actor": actor,
        "detalle": detalle,
        "revisada": False,
        **contexto,
    }
