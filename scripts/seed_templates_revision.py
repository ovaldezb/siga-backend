"""
seed_templates_revision.py — Siembra templates de puntos de revisión a partir
de las órdenes de servicio históricas de cada tenant.

Razón
-----
El módulo de Configuración expone una lista `templates_revision` dentro del
documento `configuracion` del tenant (formato `{id, nombre, puntos: string[]}`)
que el módulo de Órdenes de Servicio inyecta con un click. Hasta hoy ese
arreglo arrancaba vacío y el admin tenía que tipear los templates a mano.

Este script analiza las OS existentes (`ordenes_servicio.puntosArreglar`),
agrupa por "firma" (el conjunto normalizado de nombres de puntos), descarta
las firmas con poca repetición y siembra las top-N como templates sugeridos.
NO toca `puntosArreglar` ni ninguna OS: solo agrega entradas al array
`templates_revision` del doc `configuracion`.

Es idempotente: una firma ya presente en `templates_revision` (mismo conjunto
de puntos, ignorando orden y mayúsculas) se omite y NO duplica el template.

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
from dotenv import load_dotenv

load_dotenv()


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
    # Mismo esquema que get_tenant_db(): prefijo "t_" + uuid sin guiones.
    return client[f"t_{tenant_id.replace('-', '')}"]


def _listar_tenants(client):
    """Tenants desde _platform.talleres (igual que reconciliar_os_fugadas)."""
    try:
        talleres = list(client["_platform"]["talleres"].find(
            {}, {"tenantId": 1, "nombreComercial": 1}))
        return [(t.get("tenantId"), t.get("nombreComercial", "?"))
                for t in talleres if t.get("tenantId")]
    except Exception as e:
        print(f"[WARN] No se pudo leer _platform.talleres: {e}")
        return []


# ---------- normalización / fingerprint ---------------------------------------

_WS = re.compile(r"\s+")

def _norm(s: str) -> str:
    """Normaliza un nombre de punto para comparar: minúsculas, sin tildes
    laxas, espacios colapsados. Se usa SOLO para firma; el template guarda
    el texto original."""
    if not s:
        return ""
    s = s.strip().lower()
    s = _WS.sub(" ", s)
    # Tildes comunes en español → sin tilde (suficiente para deduplicar,
    # no es transliteración perfecta).
    trad = str.maketrans("áéíóúüñ", "aeiouun")
    return s.translate(trad)


def _firma_puntos(puntos_arreglar):
    """Devuelve (firma_set, lista_original_dedup) o (None, None) si no aplica.

    firma_set: frozenset de nombres normalizados — sirve como clave de grupo
               y para chequear duplicados contra templates ya guardados.
    lista_original_dedup: lista de strings originales (preserva la primera
               capitalización vista de cada nombre, en el orden de aparición).
    """
    nombres_norm = []
    nombres_orig = []
    visto = set()
    for p in (puntos_arreglar or []):
        nombre = (p or {}).get("nombre")
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
        nombres_orig.append(nombre)
    if not nombres_norm:
        return None, None
    return frozenset(nombres_norm), nombres_orig


def _firmas_ya_existentes(config_doc):
    """Conjunto de firmas (frozenset normalizado) presentes en
    config.templates_revision para no duplicar."""
    existentes = set()
    for t in (config_doc or {}).get("templates_revision", []) or []:
        puntos = t.get("puntos") or []
        firma = frozenset(_norm(p) for p in puntos if isinstance(p, str) and p.strip())
        if firma:
            existentes.add(firma)
    return existentes


# ---------- análisis por tenant ----------------------------------------------

def _proponer_nombre(nombres_orig, ocurrencias):
    """Heurística de nombre legible: si el primer punto tiene una pista
    fuerte (ej. 'Afinación'), úsalo; si no, etiqueta genérica con el
    conteo para que el admin lo renombre."""
    if not nombres_orig:
        return f"Plantilla sugerida ({ocurrencias} OS)"
    primero = nombres_orig[0].strip()
    # Si el primer punto cabe en una etiqueta, úsalo recortado.
    base = primero if len(primero) <= 40 else primero[:37].rstrip() + "..."
    return f"{base} ({ocurrencias} OS)"


def analizar_tenant(db, etiqueta, *, min_ocurrencias, top_n, min_puntos, limit, dry_run):
    """Lee OS del tenant, agrupa firmas, propone templates y (si no es dry-run)
    los hace push al array `templates_revision` del doc `configuracion`."""

    # Documento de configuración del tenant. Si no existe aún, lo dejamos vivo
    # para el primer GET /configuracion que lo crea con defaults; aquí solo
    # garantizamos que el array esté presente para hacer $push sin romper.
    # Filtro por tenant_id como hace el handler (`{"tenant_id": tenant_id}`).
    tenant_id = etiqueta.split("(")[-1].rstrip(")") if "(" in etiqueta else etiqueta
    config_filter = {"tenant_id": tenant_id}
    config_doc = db["configuracion"].find_one(config_filter) or {}
    firmas_existentes = _firmas_ya_existentes(config_doc)

    cursor = db["ordenes_servicio"].find(
        {"puntosArreglar": {"$exists": True, "$ne": []}},
        {"puntosArreglar.nombre": 1, "createdAt": 1, "folio": 1},
    )
    if limit:
        cursor = cursor.limit(limit)

    counter: Counter = Counter()
    representativos: dict = defaultdict(list)  # firma -> lista de nombres_orig
    total_os = 0
    os_con_puntos = 0

    for orden in cursor:
        total_os += 1
        firma, nombres_orig = _firma_puntos(orden.get("puntosArreglar"))
        if firma is None or len(firma) < min_puntos:
            continue
        os_con_puntos += 1
        counter[firma] += 1
        # Guardamos solo el primer representativo (memoria); todos los demás
        # tendrán las mismas claves normalizadas por definición de firma.
        if firma not in representativos:
            representativos[firma] = nombres_orig

    print(f"  Analizadas {total_os} OS, {os_con_puntos} con >= {min_puntos} puntos únicos.")

    candidatos = [
        (firma, count) for firma, count in counter.most_common()
        if count >= min_ocurrencias and firma not in firmas_existentes
    ][:top_n]

    if not candidatos:
        omitidos = sum(1 for f in counter if f in firmas_existentes)
        print(f"  [OK] Nada nuevo que sembrar para {etiqueta}.")
        if omitidos:
            print(f"       ({omitidos} firma(s) ya estaban en templates_revision; "
                  f"el resto no alcanza --min-ocurrencias={min_ocurrencias}.)")
        return 0

    print(f"  Candidatos a sembrar para {etiqueta}: {len(candidatos)}")
    nuevos_templates = []
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "") + "Z"
    for idx, (firma, count) in enumerate(candidatos, 1):
        nombres_orig = representativos[firma]
        nombre = _proponer_nombre(nombres_orig, count)
        tpl = {
            "id": "tpl_" + uuid.uuid4().hex[:12],
            "nombre": nombre,
            "puntos": nombres_orig,
            "_origen": "seed_templates_revision",
            "_seeded_at": now_iso,
            "_ocurrencias": count,
        }
        nuevos_templates.append(tpl)
        print(f"     {idx:2d}. [{count:>3} OS] {nombre}")
        for p in nombres_orig:
            print(f"           - {p}")

    if dry_run:
        print(f"  [DRY-RUN] {len(nuevos_templates)} template(s) NO insertados.")
        return len(nuevos_templates)

    # Upsert del doc para garantizar que existe + push de los nuevos templates.
    # Usar $push con $each es atómico y respeta cualquier template que ya esté.
    db["configuracion"].update_one(
        config_filter,
        {
            "$setOnInsert": {"tenant_id": tenant_id, "templates_revision": []},
        },
        upsert=True,
    )
    res = db["configuracion"].update_one(
        config_filter,
        {"$push": {"templates_revision": {"$each": nuevos_templates}}},
    )
    if res.modified_count:
        print(f"  [OK] {len(nuevos_templates)} template(s) insertados en {etiqueta}.")
    else:
        print(f"  [WARN] update_one no modificó el doc de {etiqueta} (¿permisos?).")
    return len(nuevos_templates)


# ---------- CLI ---------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Siembra templates_revision desde las OS históricas del tenant.")
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--tenant", help="Tenant ID concreto a procesar.")
    grupo.add_argument("--todos", action="store_true",
                       help="Procesar todos los tenants de _platform.talleres.")
    parser.add_argument("--dry-run", action="store_true",
                        help="No escribe; solo muestra qué se sembraría.")
    parser.add_argument("--min-ocurrencias", type=int, default=2,
                        help="Mínimo de OS que comparten la firma para considerarla template (default 2).")
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
