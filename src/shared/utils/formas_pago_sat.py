"""Forma de pago del SAT (catálogo c_FormaPago) para los métodos de pago del taller.

Cada método configurado en `configuracion.metodos_pago[]` lleva su `codigo_sat`;
las ventas lo copian a `pagos[].forma_pago_sat` y a `forma_pago_sat` para que la
factura salga con la forma de pago correcta. Los talleres dados de alta antes de
que existiera el campo no lo tienen, así que aquí se deduce del id / nombre del
método cuando falta. Nunca se sobreescribe un código ya capturado.
"""
import unicodedata

# c_FormaPago (CFDI 4.0).
CATALOGO_FORMA_PAGO = {
    "01": "Efectivo",
    "02": "Cheque nominativo",
    "03": "Transferencia electrónica de fondos",
    "04": "Tarjeta de crédito",
    "05": "Monedero electrónico",
    "06": "Dinero electrónico",
    "08": "Vales de despensa",
    "12": "Dación en pago",
    "13": "Pago por subrogación",
    "14": "Pago por consignación",
    "15": "Condonación",
    "17": "Compensación",
    "23": "Novación",
    "24": "Confusión",
    "25": "Remisión de deuda",
    "26": "Prescripción o caducidad",
    "27": "A satisfacción del acreedor",
    "28": "Tarjeta de débito",
    "29": "Tarjeta de servicios",
    "30": "Aplicación de anticipos",
    "31": "Intermediario pagos",
    "99": "Por definir",
}

# Los cuatro métodos que trae todo taller nuevo.
SAT_POR_METODO_BASE = {
    "efectivo": "01",
    "tarjeta": "04",
    "transferencia": "03",
    "credito": "99",
}


def _norm(valor):
    """Mayúsculas y sin acentos: 'Crédito' -> 'CREDITO'."""
    texto = unicodedata.normalize("NFKD", str(valor or "")).encode("ascii", "ignore").decode()
    return texto.strip().upper()


def codigo_por_nombre(nombre):
    """Deduce la forma de pago del nombre del método. '' si no hay forma de saberlo."""
    n = _norm(nombre)
    if not n:
        return ""
    if n.lower() in SAT_POR_METODO_BASE:
        return SAT_POR_METODO_BASE[n.lower()]
    if "DEBITO" in n:
        return "28"
    if "CHEQUE" in n:
        return "02"
    if "TRANSFER" in n or "SPEI" in n:
        return "03"
    if "EFECTIVO" in n:
        return "01"
    if "VALE" in n:
        return "08"
    if "MONEDERO" in n:
        return "05"
    if "ANTICIPO" in n:
        return "30"
    # "Tarjeta de crédito" antes que "crédito" a secas (fiado = por definir).
    if "TARJETA" in n:
        return "04"
    if "CREDITO" in n:
        return "99"
    return ""


def codigo_de_metodo_config(metodo_cfg):
    """codigo_sat de un método de la configuración, o el deducido si no lo trae."""
    codigo = str((metodo_cfg or {}).get("codigo_sat") or "").strip()
    if codigo:
        return codigo
    cfg = metodo_cfg or {}
    return SAT_POR_METODO_BASE.get(str(cfg.get("id") or "").lower()) or codigo_por_nombre(cfg.get("nombre"))


def resolver_forma_pago_sat(metodo, metodos_config=None):
    """Forma de pago SAT para el `metodo` de un pago ('tarjeta', 'EFECTIVO',
    '1787163598147', 'TARJETA DE DEBITO'...). Busca primero en la configuración
    del taller (por id y luego por nombre) y si no, lo deduce del texto."""
    if not metodo:
        return ""
    buscado = str(metodo).strip()
    metodos_config = [m for m in (metodos_config or []) if isinstance(m, dict)]
    for m in metodos_config:
        if str(m.get("id") or "").strip() == buscado:
            return codigo_de_metodo_config(m)
    buscado_norm = _norm(buscado)
    for m in metodos_config:
        if _norm(m.get("nombre")) == buscado_norm or _norm(m.get("id")) == buscado_norm:
            return codigo_de_metodo_config(m)
    return codigo_por_nombre(buscado)


def completar_codigos_config(metodos):
    """Rellena `codigo_sat` en los métodos que no lo tienen. Devuelve (lista, cambió)."""
    salida = []
    cambio = False
    for m in metodos or []:
        if not isinstance(m, dict):
            salida.append(m)
            continue
        if not str(m.get("codigo_sat") or "").strip():
            codigo = codigo_de_metodo_config(m)
            if codigo:
                m = {**m, "codigo_sat": codigo}
                cambio = True
        salida.append(m)
    return salida, cambio


def deduplicar_ids(metodos):
    """Dos métodos con el mismo `id` se pisan en el POS (se cobra con el primero).
    A partir del segundo se les da un id propio derivado del nombre. Devuelve
    (lista, cambió)."""
    vistos = set()
    salida = []
    cambio = False
    for m in metodos or []:
        if not isinstance(m, dict):
            salida.append(m)
            continue
        mid = str(m.get("id") or "").strip()
        if mid and mid not in vistos:
            vistos.add(mid)
            salida.append(m)
            continue
        base = "".join(c if c.isalnum() else "_" for c in _norm(m.get("nombre")).lower()).strip("_") or "metodo"
        nuevo, n = base, 2
        while nuevo in vistos:
            nuevo, n = f"{base}_{n}", n + 1
        vistos.add(nuevo)
        salida.append({**m, "id": nuevo})
        cambio = True
    return salida, cambio
