"""Pestañas de la lista de OS (query param `tab`): Activas / Por Cobrar / Pagadas / Canceladas.

El caso que motivó estos tests: una OS cobrada a crédito quedaba FINALIZADO con
`pagada: False` y saldo vivo, y otra entregada con saldo pendiente aparecía como
"Pagada". Además había combinaciones que no caían en ninguna pestaña y
desaparecían de la vista.

Lo que se fija aquí:
- Toda OS cae en EXACTAMENTE una pestaña (partición del universo).
- "Por Cobrar" incluye FINALIZADO y ENTREGADO con saldo > 0, sin importar `pagada`.
- "Pagadas" exige saldo en cero.
"""
import json
from datetime import datetime

from src.handlers.ordenes.ordenes_manager import list_ordenes_handler

TENANT = "tenant-tabs"
TABS = ["activas", "porcobrar", "pagadas", "canceladas"]


def _db(mock_db):
    return mock_db[f"t_{TENANT.replace('-', '')}"]


def _event(tab):
    return {
        "queryStringParameters": {"tab": tab, "limit": "200"},
        "requestContext": {"authorizer": {"claims": {"custom:tenant_id": TENANT}}},
    }


def _folios(tab):
    resp = list_ordenes_handler(_event(tab), None)
    assert resp["statusCode"] == 200, resp["body"]
    return {o["folio"] for o in json.loads(resp["body"])["data"]["items"]}


def _sembrar(mock_db, docs):
    base = {"createdAt": datetime(2026, 9, 1, 10, 0)}
    _db(mock_db)["ordenes_servicio"].insert_many([{**base, **d} for d in docs])


def test_os_a_credito_cae_en_por_cobrar(mock_db):
    """Lo que reportó el usuario: el POS deja la OS a crédito en FINALIZADO +
    pagada:False + saldo vivo, y antes no se veía en ninguna pestaña útil."""
    _sembrar(mock_db, [
        {"folio": "CREDITO-FIN", "estado": "FINALIZADO", "pagada": False, "saldo_pendiente": 1500.0},
        {"folio": "CREDITO-ENT", "estado": "ENTREGADO", "pagada": False, "saldo_pendiente": 900.0},
    ])
    assert _folios("porcobrar") == {"CREDITO-FIN", "CREDITO-ENT"}
    assert _folios("pagadas") == set()


def test_entregada_con_saldo_no_se_reporta_como_pagada(mock_db):
    """Aunque el flujo haya marcado `pagada: True`, si queda saldo es por cobrar."""
    _sembrar(mock_db, [
        {"folio": "ABONO-PARCIAL", "estado": "ENTREGADO", "pagada": True, "saldo_pendiente": 500.0},
        {"folio": "LIQUIDADA", "estado": "ENTREGADO", "pagada": True, "saldo_pendiente": 0.0},
    ])
    assert _folios("porcobrar") == {"ABONO-PARCIAL"}
    assert _folios("pagadas") == {"LIQUIDADA"}


def test_os_terminada_sin_pasar_por_el_pos_sigue_en_por_cobrar(mock_db):
    """Sin venta no hay `saldo_pendiente`: el cobro se detecta por `pagada`."""
    _sembrar(mock_db, [
        {"folio": "SIN-VENTA-FIN", "estado": "FINALIZADO"},
        {"folio": "SIN-VENTA-ENT", "estado": "ENTREGADO", "pagada": False},
        {"folio": "SALDO-NULL", "estado": "FINALIZADO", "pagada": False, "saldo_pendiente": None},
    ])
    assert _folios("porcobrar") == {"SIN-VENTA-FIN", "SIN-VENTA-ENT", "SALDO-NULL"}


def test_activas_no_incluye_terminadas_ni_canceladas(mock_db):
    _sembrar(mock_db, [
        {"folio": "EN-PROCESO", "estado": "EN_PROCESO"},
        {"folio": "RECEPCION", "estado": "RECEPCION"},
        {"folio": "COTIZADA", "estado": "COTIZADO"},
        {"folio": "FINALIZADA", "estado": "FINALIZADO", "pagada": False},
        {"folio": "CANCELADA", "estado": "CANCELADO"},
    ])
    assert _folios("activas") == {"EN-PROCESO", "RECEPCION", "COTIZADA"}
    assert _folios("canceladas") == {"CANCELADA"}


def test_os_abierta_con_anticipo_no_desaparece(mock_db):
    """Un anticipo puede dejar saldo en una OS todavía abierta. Antes esa
    combinación no cumplía ninguna condición y la orden se perdía de la vista."""
    _sembrar(mock_db, [
        {"folio": "ANTICIPO", "estado": "EN_PROCESO", "pagada": False, "saldo_pendiente": 300.0},
    ])
    assert _folios("activas") == {"ANTICIPO"}


def test_cancelada_cobrada_solo_aparece_en_canceladas(mock_db):
    """Una OS cancelada después de cobrada no debe contarse como pagada."""
    _sembrar(mock_db, [
        {"folio": "CANC-PAGADA", "estado": "CANCELADO", "pagada": True, "saldo_pendiente": 0.0},
    ])
    assert _folios("canceladas") == {"CANC-PAGADA"}
    assert _folios("pagadas") == set()


def test_las_pestanas_son_una_particion_del_universo(mock_db):
    """Barre todas las combinaciones de (estado, pagada, saldo_pendiente): cada OS
    tiene que aparecer en una pestaña y sólo una. Es la garantía de que ninguna
    orden puede volver a desaparecer de la vista."""
    estados = ["RECEPCION", "COTIZADO", "APROBADO", "EN_PROCESO",
               "FINALIZADO", "ENTREGADO", "CANCELADO"]
    docs = []
    for estado in estados:
        for pagada in (True, False, None):        # None = campo ausente
            for saldo in (None, 0.0, 1500.0):    # None = campo ausente
                doc = {"folio": f"{estado}|{pagada}|{saldo}", "estado": estado}
                if pagada is not None:
                    doc["pagada"] = pagada
                if saldo is not None:
                    doc["saldo_pendiente"] = saldo
                docs.append(doc)
    _sembrar(mock_db, docs)
    esperados = {d["folio"] for d in docs}

    por_tab = {tab: _folios(tab) for tab in TABS}

    vistos = set()
    duplicados = set()
    for folios in por_tab.values():
        duplicados |= vistos & folios
        vistos |= folios

    assert duplicados == set(), f"OS en más de una pestaña: {sorted(duplicados)}"
    assert esperados - vistos == set(), f"OS que no salen en ninguna pestaña: {sorted(esperados - vistos)}"
    assert sum(len(f) for f in por_tab.values()) == len(docs)


def test_reparto_coincide_con_el_predicado_del_frontend(mock_db):
    """Réplica de `tabDe()` (ordenes-servicio.component.ts). El backend pagina y el
    front vuelve a filtrar encima: si no dan el mismo reparto, el total miente."""
    def tab_de(estado, pagada, saldo):
        s = saldo or 0
        if estado == "CANCELADO":
            return "canceladas"
        if estado in ("FINALIZADO", "ENTREGADO") and (s > 0 or not pagada):
            return "porcobrar"
        if (bool(pagada) or estado == "ENTREGADO") and s <= 0:
            return "pagadas"
        return "activas"

    estados = ["RECEPCION", "COTIZADO", "APROBADO", "EN_PROCESO",
               "FINALIZADO", "ENTREGADO", "CANCELADO"]
    docs, esperado_por_folio = [], {}
    for estado in estados:
        for pagada in (True, False, None):
            for saldo in (None, 0.0, 1500.0):
                folio = f"{estado}|{pagada}|{saldo}"
                doc = {"folio": folio, "estado": estado}
                if pagada is not None:
                    doc["pagada"] = pagada
                if saldo is not None:
                    doc["saldo_pendiente"] = saldo
                docs.append(doc)
                esperado_por_folio[folio] = tab_de(estado, pagada, saldo)
    _sembrar(mock_db, docs)

    for tab in TABS:
        esperado = {f for f, t in esperado_por_folio.items() if t == tab}
        assert _folios(tab) == esperado, f"pestaña {tab} no coincide con el frontend"
