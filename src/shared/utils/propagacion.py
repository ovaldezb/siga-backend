"""Propaga los cambios de un catálogo a las copias que guardan otros documentos.

Las OS, citas, cotizaciones, ventas y compras guardan una copia ("snapshot") del
cliente, del vehículo o del proveedor para no tener que unir
colecciones al listar. El problema: esas copias se congelaban al crear el
documento, así que corregir el nombre de un cliente dejaba el nombre viejo en
todas sus órdenes, ventas y cotizaciones (reporte 2026-10-16).

Aquí vive la regla de qué se propaga y qué no:

- Se propaga la IDENTIDAD (nombre, apellidos, razón social, placas, marca,
  modelo…) y los datos de CONTACTO (teléfono, email). Corregir una errata o un
  teléfono debe verse en todas partes.
- NO se toca lo que es una lectura histórica de ese documento: el kilometraje de
  la OS, el crédito o el nivel de precio del cliente al momento de la venta.
- NO se tocan los CFDI (`facturasemitidas`): una factura timbrada es inmutable;
  corregir el receptor se hace cancelando y refacturando.
- NO se propaga el nombre del mecánico: hay cuentas de usuario reutilizadas para
  otra persona (OS con `mecanico_id` de Juan y nombre de Jesús), y reescribirlo
  cambiaría quién hizo el trabajo en el historial.

Cada función es idempotente y se llama tras guardar el catálogo; los callers la
envuelven en try/except para que un fallo de propagación nunca tumbe el guardado
(el script `scripts/resync_snapshots.py` re-sincroniza lo que quede atrás).
"""
# Campos del cliente que viven también en `cliente_snapshot`.
CAMPOS_CLIENTE = (
    "nombre", "apellido_paterno", "apellido_materno", "razon_social", "telefono", "email",
)

# Campos del vehículo que viven también en `vehiculo_snapshot`. Kilometraje no:
# en la OS es la lectura del odómetro de esa visita.
CAMPOS_VEHICULO = ("marca", "modelo", "anio", "placas", "color", "vin", "numero_economico")

CAMPOS_PROVEEDOR = ("nombre", "rfc", "telefono", "categoria")


def nombre_completo_cliente(cliente: dict) -> str:
    partes = [cliente.get("nombre"), cliente.get("apellido_paterno"), cliente.get("apellido_materno")]
    nombre = " ".join(str(p).strip() for p in partes if p and str(p).strip())
    return nombre or (cliente.get("razon_social") or "").strip()


def descripcion_vehiculo(vehiculo: dict) -> str:
    """Mismo formato que arma la agenda de citas: "Marca Modelo (PLACAS)"."""
    base = " ".join(str(p) for p in (vehiculo.get("marca"), vehiculo.get("modelo")) if p).strip()
    placas = vehiculo.get("placas")
    return f"{base} ({placas})".strip() if placas else base


def _set_snapshot(prefijo: str, doc: dict, campos) -> dict:
    return {f"{prefijo}.{c}": doc.get(c) for c in campos if c in doc}


def propagar_cliente(db, cliente: dict) -> dict:
    """Refresca nombre y contacto del cliente en OS, citas, cotizaciones y ventas."""
    cid = str(cliente.get("_id") or cliente.get("id") or "")
    if not cid:
        return {}
    nombre = nombre_completo_cliente(cliente)
    snap = _set_snapshot("cliente_snapshot", cliente, CAMPOS_CLIENTE)
    resultado = {}

    if snap:
        con_snapshot = {"cliente_snapshot": {"$type": "object"}}
        # Algunas OS viejas sólo traen el id en la raíz (`cliente_id`).
        resultado["ordenes_servicio"] = db["ordenes_servicio"].update_many(
            {"$or": [{"cliente_snapshot.id": cid}, {"cliente_id": cid}], **con_snapshot},
            {"$set": snap}).modified_count
        resultado["cotizaciones"] = db["cotizaciones"].update_many(
            {"cliente_snapshot.id": cid, **con_snapshot}, {"$set": snap}).modified_count

    if nombre:
        resultado["ventas"] = db["ventas"].update_many(
            {"cliente_id": cid, "cliente_nombre": {"$ne": nombre}},
            {"$set": {"cliente_nombre": nombre}}).modified_count
        resultado["citas"] = db["citas"].update_many(
            {"clienteId": cid, "clienteNombre": {"$ne": nombre}},
            {"$set": {"clienteNombre": nombre}}).modified_count
    return resultado


def propagar_vehiculo(db, vehiculo: dict) -> dict:
    """Refresca la identidad del vehículo en OS, cotizaciones, ventas y citas."""
    vid = str(vehiculo.get("_id") or vehiculo.get("id") or "")
    if not vid:
        return {}
    snap = _set_snapshot("vehiculo_snapshot", vehiculo, CAMPOS_VEHICULO)
    resultado = {}
    if snap:
        con_snapshot = {"vehiculo_snapshot": {"$type": "object"}}
        resultado["ordenes_servicio"] = db["ordenes_servicio"].update_many(
            {"vehiculo_id": vid, **con_snapshot}, {"$set": snap}).modified_count
        resultado["ventas"] = db["ventas"].update_many(
            {"vehiculo_id": vid, **con_snapshot}, {"$set": snap}).modified_count
        resultado["cotizaciones"] = db["cotizaciones"].update_many(
            {"$or": [{"vehiculo_snapshot.id": vid}, {"vehiculo_id": vid}], **con_snapshot},
            {"$set": snap}).modified_count

    cita_set: dict = {"vehiculoDesc": descripcion_vehiculo(vehiculo)}
    if vehiculo.get("anio"):
        cita_set["vehiculoAnio"] = vehiculo.get("anio")
    resultado["citas"] = db["citas"].update_many({"vehiculoId": vid}, {"$set": cita_set}).modified_count
    return resultado


def propagar_proveedor(db, proveedor: dict) -> dict:
    """Refresca el proveedor en las compras y el nombre en los artículos del catálogo."""
    pid = str(proveedor.get("_id") or proveedor.get("id") or "")
    if not pid:
        return {}
    resultado = {}
    snap = _set_snapshot("proveedor_snapshot", proveedor, CAMPOS_PROVEEDOR)
    if snap:
        resultado["compras"] = db["compras"].update_many(
            {"proveedor_id": pid, "proveedor_snapshot": {"$type": "object"}}, {"$set": snap}).modified_count
    nombre = (proveedor.get("nombre") or "").strip()
    if nombre:
        resultado["items"] = db["items"].update_many(
            {"proveedor_id": pid, "proveedor": {"$ne": nombre}}, {"$set": {"proveedor": nombre}}).modified_count
    return resultado


def campos_cambiaron(update_doc: dict, campos) -> bool:
    return any(c in update_doc for c in campos)
