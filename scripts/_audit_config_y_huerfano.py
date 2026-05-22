"""Auditoría READ-ONLY pre-cleanup. Borrar tras usar.

1) Para cada tenant DB: imprime el doc configuracion con tenant_id=None,
   diciendo qué tiene dentro (claves, tamaño, _id, presencia de campos
   no triviales) para decidir si es seguro borrar.

2) Para el tenant DB huérfano t_3a0da675...: lista colecciones, conteos,
   y el doc configuracion (si existe), para decidir si es de prueba o
   un tenant real que se cayó del registro.
"""
import os
import json
from pymongo import MongoClient

ORPHAN_DB = "t_3a0da675d27748bfbbfec817471c252c"

c = MongoClient(
    f"mongodb+srv://{os.environ['MONGO_USER']}:{os.environ['MONGO_PASSWORD']}"
    f"@{os.environ['MONGO_HOST']}/?retryWrites=true&w=majority"
)


def resumen_doc(doc):
    """Resume un doc Mongo (qué claves trae, cuántos elementos en listas)."""
    out = {}
    for k, v in doc.items():
        if isinstance(v, list):
            out[k] = f"list[{len(v)}]"
        elif isinstance(v, dict):
            out[k] = f"dict({len(v)} keys)"
        elif isinstance(v, str):
            out[k] = f"str({len(v)} chars)" if len(v) > 60 else repr(v)
        else:
            out[k] = repr(v)
    return out


print("=" * 70)
print("1) Docs configuracion con tenant_id:None — contenido completo")
print("=" * 70)
for db_name in sorted(c.list_database_names()):
    if not db_name.startswith("t_"):
        continue
    nulls = list(c[db_name]["configuracion"].find({"tenant_id": None}))
    if not nulls:
        continue
    print(f"\n[{db_name}]  {len(nulls)} doc(s) con tenant_id:None")
    for d in nulls:
        d_id = d.pop("_id")
        print(f"  _id = {d_id}")
        resumen = resumen_doc(d)
        if not resumen:
            print(f"    (doc completamente vacío excepto _id)")
        else:
            print(f"    claves: {list(resumen.keys())}")
            for k, v in resumen.items():
                print(f"      - {k} = {v}")
        # ¿Hay algo que NO sea defaults conocidos? Mostrar campos
        # interesantes con sample.
        interesantes = [k for k in d
                        if k not in ("tenant_id", "metodos_pago", "marcas",
                                     "gastos_fijos_catalogo", "templates_revision",
                                     "tasas", "permisos_modulos")]
        if interesantes:
            print(f"    *** CAMPOS NO ESTÁNDAR: {interesantes}")
            for k in interesantes:
                print(f"        {k} = {json.dumps(d[k], default=str)[:200]}")

print()
print("=" * 70)
print(f"2) Tenant huérfano {ORPHAN_DB} — qué tiene dentro")
print("=" * 70)
if ORPHAN_DB not in c.list_database_names():
    print("  (no existe)")
else:
    db = c[ORPHAN_DB]
    colls = sorted(db.list_collection_names())
    print(f"  Colecciones: {len(colls)}")
    for coll in colls:
        count = db[coll].count_documents({})
        print(f"    - {coll:<35}  {count:>6} docs")
    # Pista clave: ¿tiene OS reales? ¿tiene clientes? ¿qué dice configuracion?
    print()
    print("  --- configuracion (todos los docs) ---")
    for d in db["configuracion"].find():
        d_id = d.pop("_id")
        print(f"    _id={d_id}")
        for k, v in resumen_doc(d).items():
            print(f"      - {k} = {v}")
    print()
    print("  --- Muestra de hasta 3 OS (folio, estado, fecha, cliente) ---")
    for o in db["ordenes_servicio"].find({}, {
            "folio": 1, "estado": 1, "createdAt": 1,
            "cliente_snapshot.nombre": 1}).limit(3):
        print(f"    {o.get('folio','?'):<20} {o.get('estado','?'):<12} "
              f"{str(o.get('createdAt','?'))[:19]:<20} "
              f"cliente={(o.get('cliente_snapshot') or {}).get('nombre','?')}")
    # Sucursales / usuarios — indicio de tenant en uso
    if "sucursales" in colls:
        for s in db["sucursales"].find({}, {"nombre": 1, "responsable": 1}).limit(5):
            print(f"    sucursal: {s.get('nombre')!r} resp={s.get('responsable')!r}")
