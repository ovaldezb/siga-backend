"""
seed_templates_revision.py — Siembra templates de puntos de revisión a partir
de las órdenes de servicio históricas de cada tenant.

Razón
-----
El módulo de Configuración expone una lista `templates_revision` dentro del
documento `configuracion` del tenant. Cada template es:

    { id, nombre, puntos: [{ nombre, items: [{ nombre, piezas?, precioVenta? }] }] }

…mismo shape que `puntosArreglar` en una OS, de modo que el botón "Inyectar"
materializa puntos+items 1:1 en la orden actual.

Este script analiza las OS existentes (`ordenes_servicio.puntosArreglar`),
agrupa por "firma" (el conjunto normalizado de nombres de puntos), y para
cada firma con ≥ `--min-ocurrencias` apariciones siembra un template con:

  - El conjunto de puntos (preservando orden y capitalización del primer OS).
  - Por cada punto: la **unión deduplicada** de los nombres de items que
    aparecieron en cualquiera de las OS que comparten esa firma.
  - Sin piezas/precio: varían entre OS y meter promedios manchados es peor
    que dejarlo en blanco. El admin puede teclear sugerencias después.

Es idempotente: una firma ya presente en `templates_revision` (mismo conjunto
de nombres de puntos, ignorando orden/capitalización) se omite.

Uso
---
  # Vista previa de un tenant (no escribe nada):
  python scripts/seed_templates_revision.py --tenant <TENANT_ID> --dry-run

  # Sembrar realmente en un tenant:
  python scripts/seed_templates_revision.py --tenant <TENANT_ID>

  # Vista previa de TODOS los tenants de _platform.talleres:
  python scripts/seed_templates_revision.py --todos --dry-run

  # Sembrar realmente en todos:
  python scripts/seed_templates_revision.py --todos

  # Ajustar umbrales (default: min-ocurrencias=2, top-n=20, min-puntos=2):
  python scripts/seed_templates_revision.py --tenant <ID> \
      --min-ocurrencias 3 --top-n 10 --min-puntos 3

Requiere las variables de entorno MONGO_USER / MONGO_PASSWORD / MONGO_HOST
(o un archivo .env en la raíz del repo). Mismo patrón que el resto de scripts.
"""
import os
import re
import sys
import uuid
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from dotenv import load_dotenv

load_dotenv()

# SonarLint S1192: Extract duplicated string literals
KEY_NOMBRE = "nombre"
KEY_ITEMS = "items"
KEY_PUNTOS = "puntos"
KEY_TENANT_ID = "tenantId"
COL_CONFIGURACION = "configuracion"
COL_ORDENES_SERVICIO = "ordenes_servicio"
KEY_TEMPLATES_REVISION = "templates_revision"
KEY_PUNTOS_ARREGLAR = "puntosArreglar"


# ---------- conexión (mismo patrón que reconciliar_os_fugadas.py) -------------

def _client() -> MongoClient:
    user = os.environ.get("MONGO_USER")
    password = os.environ.get("MONGO_PASSWORD")
    host = os.environ.get("MONGO_HOST")
    db_name = os.environ.get("MONGO_DB", "siga")
    if not (user and password and host):
        print("[ERROR] Faltan MONGO_USER / MONGO_PASSWORD / MONGO_HOST (o .env).")
        sys.exit(1)
    uri = f"mongodb+srv://{user}:{password}@{host}/{db_name}?retryWrites=true&w=majority"
    return MongoClient(uri)


def _tenant_db(client, tenant_id):
    return client[f"t_{tenant_id.replace('-', '')}"]


def _listar_tenants(client):
    try:
        talleres = list(client["_platform"]["talleres"].find(
            {}, {KEY_TENANT_ID: 1, "nombreComercial": 1}))
        return [(t.get(KEY_TENANT_ID), t.get("nombreComercial", "?"))
                for t in talleres if t.get(KEY_TENANT_ID)]
    except PyMongoError as e:
        print(f"[WARN] No se pudo leer _platform.talleres: {e}")
        return []


# ---------- normalización ----------------------------------------------------

_WS = re.compile(r"\s+")

def _norm(s: str) -> str:
    """Minúsculas + sin tildes laxas + espacios colapsados. Para deduplicar."""
    if not s:
        return ""
    s = s.strip().lower()
    s = _WS.sub(" ", s)
    return s.translate(str.maketrans("áéíóúüñ", "aeiouun"))


def _firma_y_puntos(puntos_arreglar):
    """Devuelve (firma, puntos_canon).

    - firma: frozenset de nombres de punto normalizados (clave de agrupación).
    - puntos_canon: lista de { 'nombre': str_original, 'items': [dict,...] }
      en el orden en que aparecen, deduplicada por nombre normalizado.
    """
    nombres_norm = []
    puntos_canon = []
    visto = set()
    for p in puntos_arreglar or []:
        if not p:
            continue
        nombre = p.get(KEY_NOMBRE)
        if not isinstance(nombre, str):
            continue
        nombre = nombre.strip()
        if not nombre:
            continue
        clave = _norm(nombre)
        if not clave or clave in visto:
            continue
        visto.add(clave)
        nombres_norm.append(clave)
        puntos_canon.append({
            KEY_NOMBRE: nombre,
            KEY_ITEMS: p.get(KEY_ITEMS) or [],
        })
    if not nombres_norm:
        return None, None
    return frozenset(nombres_norm), puntos_canon


def _extraer_nombres_de_template(tpl):
    nombres = []
    for p in tpl.get(KEY_PUNTOS) or []:
        if isinstance(p, str):
            nombres.append(p)
        elif isinstance(p, dict):
            n = p.get(KEY_NOMBRE) or ""
            if n:
                nombres.append(n)
    return nombres

def _firmas_ya_existentes(config_doc):
    """Conjunto de firmas (frozenset normalizado) presentes en
    config.templates_revision para no duplicar."""
    existentes = set()
    templates = (config_doc or {}).get(KEY_TEMPLATES_REVISION, []) or []
    for t in templates:
        nombres = _extraer_nombres_de_template(t)
        firma = frozenset(_norm(n) for n in nombres if n.strip())
        if firma:
            existentes.add(firma)
    return existentes


# ---------- merge de items entre OS que comparten firma ----------------------

def _mergear_puntos(lista_de_puntos_canon):
    """Para una misma firma, combina los items de todas las OS.

    Recibe: lista en la que cada elemento es la `puntos_canon` de UNA OS.
    Devuelve: lista [{ nombre, items: [{nombre}] }] con orden = primer
    aparición del punto, capitalización = primer OS que lo trajo, e items
    deduplicados por nombre normalizado (case-insensitive).

    No guardamos piezas/precio porque varían entre OS — sembrarlas con un
    valor sería pretender certeza que no hay. El admin las teclea si quiere.
    """
    nombre_canon_punto: dict = {}   # norm_punto -> nombre_original (primer OS)
    items_por_punto: dict = defaultdict(dict)  # norm_punto -> {norm_item: nombre_orig}
    orden_puntos: list = []         # norm_punto en orden de aparición

    for puntos_de_una_os in lista_de_puntos_canon:
        for p in puntos_de_una_os:
            np = _norm(p[KEY_NOMBRE])
            if not np:
                continue
            if np not in nombre_canon_punto:
                nombre_canon_punto[np] = p[KEY_NOMBRE]
                orden_puntos.append(np)
            for it in p.get(KEY_ITEMS) or []:
                inombre = it.get(KEY_NOMBRE) or ""
                if not isinstance(inombre, str):
                    continue
                inombre = inombre.strip()
                if not inombre:
                    continue
                ni = _norm(inombre)
                if not ni:
                    continue
                if ni not in items_por_punto[np]:
                    items_por_punto[np][ni] = inombre

    out = []
    for np in orden_puntos:
        out.append({
            KEY_NOMBRE: nombre_canon_punto[np],
            KEY_ITEMS: [{KEY_NOMBRE: n} for n in items_por_punto[np].values()],
        })
    return out


# ---------- análisis por tenant ----------------------------------------------

def _proponer_nombre(puntos_mergeados, ocurrencias):
    """Nombre legible para el template; el admin lo renombra después."""
    if not puntos_mergeados:
        return f"Plantilla sugerida ({ocurrencias} OS)"
    primero = puntos_mergeados[0][KEY_NOMBRE].strip()
    base = primero if len(primero) <= 40 else primero[:37].rstrip() + "..."
    return f"{base} ({ocurrencias} OS)"


def _escanear_ordenes(db, firmas_existentes, min_ocurrencias, min_puntos, limit, top_n):
    cursor = db[COL_ORDENES_SERVICIO].find(
        {KEY_PUNTOS_ARREGLAR: {"$exists": True, "$ne": []}},
        {KEY_PUNTOS_ARREGLAR: 1},
    )
    if limit:
        cursor = cursor.limit(limit)

    counter: Counter = Counter()
    acumulador: dict = defaultdict(list)
    total_os = 0
    os_con_puntos = 0

    for orden in cursor:
        total_os += 1
        firma, puntos_canon = _firma_y_puntos(orden.get(KEY_PUNTOS_ARREGLAR))
        if firma is None or len(firma) < min_puntos:
            continue
        os_con_puntos += 1
        counter[firma] += 1
        acumulador[firma].append(puntos_canon)

    candidatos = [
        (firma, count) for firma, count in counter.most_common()
        if count >= min_ocurrencias and firma not in firmas_existentes
    ][:top_n]
    
    return total_os, os_con_puntos, candidatos, counter, acumulador

def _imprimir_candidatos_y_generar(candidatos, acumulador):
    nuevos_templates = []
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "") + "Z"
    for idx, (firma, count) in enumerate(candidatos, 1):
        puntos_mergeados = _mergear_puntos(acumulador[firma])
        nombre = _proponer_nombre(puntos_mergeados, count)
        n_items = sum(len(p[KEY_ITEMS]) for p in puntos_mergeados)
        tpl = {
            "id": "tpl_" + uuid.uuid4().hex[:12],
            KEY_NOMBRE: nombre,
            KEY_PUNTOS: puntos_mergeados,
            "_origen": "seed_templates_revision",
            "_seeded_at": now_iso,
            "_ocurrencias": count,
        }
        nuevos_templates.append(tpl)
        print(f"     {idx:2d}. [{count:>3} OS] {nombre}  "
              f"— {len(puntos_mergeados)} punto(s), {n_items} item(s)")
        for p in puntos_mergeados:
            n_it = len(p[KEY_ITEMS])
            sufijo = f" ({n_it} items)" if n_it else ""
            print(f"           - {p[KEY_NOMBRE]}{sufijo}")
            for it in p[KEY_ITEMS]:
                print(f"               · {it[KEY_NOMBRE]}")
    return nuevos_templates

def analizar_tenant(db, etiqueta, *, min_ocurrencias, top_n, min_puntos, limit, dry_run):
    """Recorre OS, agrupa, mergea items, propone templates y opcionalmente los inserta."""
    tenant_id = etiqueta.split("(")[-1].rstrip(")") if "(" in etiqueta else etiqueta
    config_filter = {"tenant_id": tenant_id}
    config_doc = db[COL_CONFIGURACION].find_one(config_filter) or {}
    firmas_existentes = _firmas_ya_existentes(config_doc)

    total_os, os_con_puntos, candidatos, counter, acumulador = _escanear_ordenes(
        db, firmas_existentes, min_ocurrencias, min_puntos, limit, top_n
    )

    print(f"  Analizadas {total_os} OS, {os_con_puntos} con >= {min_puntos} puntos únicos.")

    if not candidatos:
        omitidos = sum(1 for f in counter if f in firmas_existentes)
        print(f"  [OK] Nada nuevo que sembrar para {etiqueta}.")
        if omitidos:
            print(f"       ({omitidos} firma(s) ya estaban en templates_revision; "
                  f"el resto no alcanza --min-ocurrencias={min_ocurrencias}.)")
        return 0

    print(f"  Candidatos a sembrar para {etiqueta}: {len(candidatos)}")
    nuevos_templates = _imprimir_candidatos_y_generar(candidatos, acumulador)

    if dry_run:
        print(f"  [DRY-RUN] {len(nuevos_templates)} template(s) NO insertados.")
        return len(nuevos_templates)

    db[COL_CONFIGURACION].update_one(
        config_filter,
        {"$setOnInsert": {"tenant_id": tenant_id, KEY_TEMPLATES_REVISION: []}},
        upsert=True,
    )
    res = db[COL_CONFIGURACION].update_one(
        config_filter,
        {"$push": {KEY_TEMPLATES_REVISION: {"$each": nuevos_templates}}},
    )
    if res.modified_count:
        print(f"  [OK] {len(nuevos_templates)} template(s) insertados en {etiqueta}.")
    else:
        print(f"  [WARN] update_one no modificó el doc de {etiqueta} (¿permisos?).")
    return len(nuevos_templates)


# ---------- CLI --------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Siembra templates_revision (con items) desde las OS históricas del tenant.")
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--tenant", help="Tenant ID concreto a procesar.")
    grupo.add_argument("--todos", action="store_true",
                       help="Procesar todos los tenants de _platform.talleres.")
    parser.add_argument("--dry-run", action="store_true",
                        help="No escribe; solo muestra qué se sembraría.")
    parser.add_argument("--min-ocurrencias", type=int, default=2,
                        help="Mínimo de OS que comparten la firma (default 2).")
    parser.add_argument("--top-n", type=int, default=20,
                        help="Máximo de templates a sembrar por tenant (default 20).")
    parser.add_argument("--min-puntos", type=int, default=2,
                        help="Mínimo de puntos únicos por OS para considerar su firma (default 2).")
    parser.add_argument("--limit", type=int, default=0,
                        help="Limita cuántas OS escanear por tenant (0 = sin límite).")
    args = parser.parse_args()

    if args.min_ocurrencias < 1 or args.top_n < 1 or args.min_puntos < 1:
        print("[ERROR] --min-ocurrencias, --top-n y --min-puntos deben ser >= 1.")
        sys.exit(2)

    client = _client()
    print("Conectado a MongoDB.\n")
    if args.dry_run:
        print("** Modo DRY-RUN — no se escribirá nada. **\n")

    kwargs = dict(
        min_ocurrencias=args.min_ocurrencias,
        top_n=args.top_n,
        min_puntos=args.min_puntos,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    total = 0
    if args.todos:
        tenants = _listar_tenants(client)
        if not tenants:
            print("No se encontraron tenants en _platform.talleres.")
            sys.exit(1)
        for tid, nombre in tenants:
            etiqueta = f"{nombre} ({tid})"
            print(f"\n>> {etiqueta}")
            total += analizar_tenant(_tenant_db(client, tid), etiqueta, **kwargs)
    else:
        print(f">> {args.tenant}")
        total += analizar_tenant(_tenant_db(client, args.tenant), args.tenant, **kwargs)

    print()
    accion = "sembrado(s)" if not args.dry_run else "candidato(s) (dry-run)"
    print(f"Total: {total} template(s) {accion}.")


if __name__ == "__main__":
    main()
