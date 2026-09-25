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


CODIGO_CREDITO = "99"

# Nombre corto del concepto para reportes y cortes (el del catálogo es muy largo).
_ETIQUETAS = {
    "01": "Efectivo",
    "02": "Cheque",
    "03": "Transferencia",
    "04": "Tarjeta de crédito",
    "28": "Tarjeta de débito",
    "29": "Tarjeta de servicios",
    "99": "Crédito (por cobrar)",
}

# Cómo cuenta el cajero cada forma de pago en el arqueo.
GRUPO_EFECTIVO = "efectivo"
GRUPO_TARJETA = "tarjeta"
GRUPO_OTROS = "otros"
_GRUPO_ARQUEO = {"01": GRUPO_EFECTIVO, "04": GRUPO_TARJETA, "28": GRUPO_TARJETA, "29": GRUPO_TARJETA}


def etiqueta_forma_pago(codigo):
    """'28' -> 'Tarjeta de débito'. Vacío -> 'Sin clasificar'."""
    codigo = str(codigo or "").strip()
    if not codigo:
        return "Sin clasificar"
    return _ETIQUETAS.get(codigo) or CATALOGO_FORMA_PAGO.get(codigo) or f"Forma {codigo}"


def grupo_arqueo(codigo):
    """Renglón del conteo físico al que pertenece la forma de pago. None = crédito (no es dinero)."""
    codigo = str(codigo or "").strip()
    if codigo == CODIGO_CREDITO:
        return None
    return _GRUPO_ARQUEO.get(codigo, GRUPO_OTROS)


def forma_pago_de(pago, metodos_config=None):
    """Código SAT de un pago/abono/movimiento: el guardado o, si no trae, el deducido de su método."""
    if not isinstance(pago, dict):
        return ""
    return str(pago.get("forma_pago_sat") or "").strip() or resolver_forma_pago_sat(pago.get("metodo"), metodos_config)


def es_credito(pago, metodos_config=None):
    """Un pago a crédito no es dinero recibido: genera cuenta por cobrar. Se reconoce
    por su código 99 aunque el método tenga un id propio del taller."""
    if not isinstance(pago, dict):
        return False
    if str(pago.get("metodo") or "").strip().upper() == "CREDITO":
        return True
    return forma_pago_de(pago, metodos_config) == CODIGO_CREDITO


def esperado_por_concepto(sesion, metodos_config=None):
    """Lo que debería haber en cada renglón del arqueo de una sesión de caja.

    - Efectivo: fondo inicial + cobros en efectivo + entradas manuales − salidas en efectivo.
    - Tarjeta: cobros con tarjeta de crédito, débito o servicios (vouchers de terminal).
    - Otros: transferencias, cheques y demás.
    Las entradas/salidas manuales (sin método) son movimientos del cajón: efectivo.
    """
    tot = {GRUPO_EFECTIVO: float(sesion.get("monto_inicial") or 0), GRUPO_TARJETA: 0.0, GRUPO_OTROS: 0.0}
    for mov in sesion.get("movimientos") or []:
        try:
            monto = float(mov.get("monto") or 0)
        except (TypeError, ValueError):
            continue
        tipo = str(mov.get("tipo") or "").upper()
        if tipo not in ("VENTA", "ENTRADA", "SALIDA"):
            continue
        if mov.get("forma_pago_sat") or mov.get("metodo"):
            grupo = grupo_arqueo(forma_pago_de(mov, metodos_config))
        else:
            grupo = GRUPO_EFECTIVO
        if grupo is None:
            continue
        tot[grupo] += -monto if tipo == "SALIDA" else monto
    return {k: round(v, 2) for k, v in tot.items()}


def acumular_por_forma_pago(ventas, metodos_config=None):
    """Ingreso de las ventas acumulado por forma de pago SAT, en renglones listos para
    reporte: [{metodo (etiqueta), forma_pago_sat, monto, pct}], de mayor a menor.

    Se agrupa por código (01, 04, 28, 03, 99…) y no por el texto del método: los
    métodos propios del taller se guardan con id numérico y un mismo concepto
    quedaba partido en varios renglones, o dos conceptos en uno (tarjeta de crédito
    y de débito con el mismo id). El ingreso de cada venta es su `total`; si lo
    recibido supera el total (cambio en efectivo) se escala para que cuadre.
    """
    def num(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    acumulado = {}
    for v in ventas:
        ingreso = num(v.get("total") or v.get("subtotal"))
        pagos = [p for p in (v.get("pagos") or []) if isinstance(p, dict) and num(p.get("monto")) > 0]
        if not pagos:
            forma = str(v.get("forma_pago_sat") or "").strip() or \
                resolver_forma_pago_sat(v.get("metodo_pago"), metodos_config)
            acumulado[forma] = acumulado.get(forma, 0.0) + ingreso
            continue
        suma = sum(num(p.get("monto")) for p in pagos)
        factor = ingreso / suma if suma > ingreso > 0 else 1.0
        for p in pagos:
            forma = forma_pago_de(p, metodos_config)
            acumulado[forma] = acumulado.get(forma, 0.0) + num(p.get("monto")) * factor
    total = sum(acumulado.values())
    filas = [{
        "metodo": etiqueta_forma_pago(forma), "forma_pago_sat": forma or None,
        "monto": round(monto, 2),
        "pct": round(monto / total * 100, 1) if total > 0 else 0.0,
    } for forma, monto in acumulado.items() if monto > 0]
    return sorted(filas, key=lambda f: f["monto"], reverse=True)


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
