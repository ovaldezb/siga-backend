"""Copia datos reales (ANONIMIZADOS) de talleres de producción a un taller de pruebas en dev.

El taller de pruebas necesita volumen realista (cortes de caja, ventas, OS, inventario)
para probar reportes y pantallas. Este script:

- LEE producción (credenciales en `.env.prod`) — nunca escribe ahí.
- ESCRIBE sólo en el tenant destino del cluster de dev (`.env`). Se niega a correr si
  el host destino es el mismo que el de origen.
- Anonimiza clientes, teléfonos, correos, RFC, razones sociales, direcciones, placas,
  VIN y nombres del personal con datos ficticios deterministas (el mismo cliente queda
  con el mismo nombre falso en todas sus OS, ventas y citas). Montos, piezas, fechas
  y estados quedan intactos.
- No copia facturas CFDI, certificados, tokens de enlaces públicos, usuarios,
  configuración ni folios. Quita evidencias (sus llaves apuntan al bucket de prod).
- Remapea sucursal y tenant al destino y agrega un sufijo a los folios para que no
  choquen con los del taller de pruebas ni entre talleres de origen.
- Marca cada documento con `_copia_de`, así que `--limpiar` quita todo lo copiado.

Uso:
  python scripts/copiar_datos_a_taller_pruebas.py                 # dry-run (no escribe)
  python scripts/copiar_datos_a_taller_pruebas.py --apply         # borra copias previas y copia
  python scripts/copiar_datos_a_taller_pruebas.py --limpiar       # sólo borra lo copiado
"""
import argparse
import hashlib
import re
from datetime import datetime
from urllib.parse import quote_plus

from dotenv import dotenv_values
from pymongo import MongoClient

DESTINO_TENANT = '32d2a84db4364abaaf58c62b6c2bf147'      # Taller de Pruebas Dos (dev)
DESTINO_SUCURSAL = '6a595301f551528731e7fbd4'            # TALLER DE PRUEBA 2
DESTINO_SUCURSAL_NOMBRE = 'TALLER DE PRUEBA 2'

# Por taller de origen, una sola sucursal: el catálogo se replica por sucursal y
# juntar varias en la sucursal destino duplicaría cada pieza del inventario.
ORIGENES = [
    {
        'nombre': 'Servicio Automotriz Express',
        'tenant': '45b55ae04acf4978ab6b73b23afd49c2',
        'sucursal': '6a025f64293e5ecf36fb0e3a',          # EXPRESS SUR (la operación principal)
        'sucursales_todas': ['6a025f64293e5ecf36fb0e3a', '6a025fb3293e5ecf36fb0e3b', '6a6151e95edded7ca8b72889'],
        'sufijo_folio': '-E',
    },
    {
        'nombre': "Bajisto's Automotive Workshop",
        'tenant': '5927730ae9ab47ddaf0a8ef61fb98f54',
        'sucursal': '6a691ebb2ffeecced2f00316',
        'sucursales_todas': ['6a691ebb2ffeecced2f00316'],
        'sufijo_folio': '-B',
    },
]

# Orden importa sólo para el reporte; no hay FKs en Mongo.
COLECCIONES = [
    'clientes', 'vehiculos', 'proveedores', 'items', 'ordenes_servicio', 'os_events',
    'ventas', 'caja_sesiones', 'compras', 'citas', 'cotizaciones',
    'gastos_fijos_mes', 'gastos_variables', 'inventario_movimientos',
]
# Colecciones del tenant que NO dependen de sucursal: se copian completas.
SIN_SUCURSAL = {'clientes', 'vehiculos', 'proveedores'}

MARCA = '_copia_de'

STAFF_KEYS = {'mecaniconombre', 'usuarionombre', 'usuarioaperturanombre', 'usuariocierrenombre',
              'tecniconombre', 'cobrousuario', 'vendedor', 'capturausuario', 'contacto',
              'responsable', 'mecanico'}
# Texto libre donde pueden venir placas, VIN, teléfonos o correos escritos a mano.
TEXTO_LIBRE = {'vehiculodesc', 'notas', 'nota', 'observaciones', 'motivo', 'motivocancelacion',
               'fallareportada', 'diagnostico', 'referencia', 'concepto', 'descripcion'}

NOMBRES = ['Alejandro', 'María', 'José', 'Guadalupe', 'Juan', 'Fernanda', 'Luis', 'Daniela', 'Carlos',
           'Sofía', 'Miguel', 'Valeria', 'Jorge', 'Ximena', 'Ricardo', 'Andrea', 'Eduardo', 'Paola',
           'Roberto', 'Mariana', 'Fernando', 'Carmen', 'Arturo', 'Lucía', 'Héctor', 'Regina', 'Raúl',
           'Natalia', 'Sergio', 'Diana', 'Óscar', 'Alejandra', 'Manuel', 'Gabriela', 'Pedro', 'Karla']
APELLIDOS = ['García', 'Martínez', 'López', 'Hernández', 'González', 'Pérez', 'Rodríguez', 'Sánchez',
             'Ramírez', 'Cruz', 'Flores', 'Gómez', 'Morales', 'Vázquez', 'Reyes', 'Jiménez', 'Torres',
             'Díaz', 'Gutiérrez', 'Ruiz', 'Mendoza', 'Aguilar', 'Ortiz', 'Castillo', 'Romero', 'Silva',
             'Herrera', 'Medina', 'Castro', 'Vargas', 'Ramos', 'Núñez', 'Salazar', 'Domínguez']
CALLES = ['Av. Juárez', 'Calle Hidalgo', 'Av. Revolución', 'Calle Morelos', 'Blvd. Independencia',
          'Calle Allende', 'Av. Reforma', 'Calle Zaragoza', 'Av. Insurgentes', 'Calle Guerrero']
EMPRESAS = ['Logística', 'Transportes', 'Distribuidora', 'Servicios', 'Comercializadora', 'Grupo']

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _h(*partes) -> int:
    return int(hashlib.sha256('|'.join(str(p) for p in partes).encode()).hexdigest(), 16)


def _norm(key: str) -> str:
    return key.replace('_', '').lower()


def _persona(semilla):
    n = _h('persona', semilla)
    return (NOMBRES[n % len(NOMBRES)],
            APELLIDOS[(n // 7) % len(APELLIDOS)],
            APELLIDOS[(n // 131) % len(APELLIDOS)])


def _telefono(semilla):
    return '55' + str(_h('tel', semilla) % 10**8).zfill(8)


def _email(semilla):
    nombre, ap, _ = _persona(semilla)
    base = f"{nombre}.{ap}".lower()
    for a, b in (('á', 'a'), ('é', 'e'), ('í', 'i'), ('ó', 'o'), ('ú', 'u'), ('ñ', 'n'), ('ó', 'o')):
        base = base.replace(a, b)
    return f"{base}{_h('mail', semilla) % 100}@ejemplo.com"


def _rfc(semilla):
    n = _h('rfc', semilla)
    letras = ''.join(chr(65 + (n >> (5 * i)) % 26) for i in range(4))
    return f"{letras}{80 + n % 20:02d}{1 + n % 12:02d}{1 + n % 28:02d}{chr(65 + n % 26)}{chr(65 + (n // 26) % 26)}{n % 10}"


def _placas(semilla):
    n = _h('placas', semilla)
    return f"{chr(65 + n % 26)}{chr(65 + (n // 26) % 26)}{chr(65 + (n // 676) % 26)}-{n % 1000:03d}-{chr(65 + (n // 9) % 26)}"


def _vin(semilla):
    alfabeto = 'ABCDEFGHJKLMNPRSTUVWXYZ0123456789'  # sin I, O, Q como un VIN real
    n = _h('vin', semilla)
    return '3' + ''.join(alfabeto[(n >> (5 * i)) % len(alfabeto)] for i in range(16))


def _direccion(semilla):
    n = _h('dir', semilla)
    return f"{CALLES[n % len(CALLES)]} {10 + n % 990}, Col. Centro"


class Anonimizador:
    """Reemplazos deterministas: la misma entrada siempre da el mismo dato falso."""

    def __init__(self, sucursales_origen, tenant_origen, sufijo_folio, reemplazos=None):
        self.sucursales_origen = set(sucursales_origen)
        self.tenant_origen = tenant_origen
        self.sufijo_folio = sufijo_folio
        # original -> falso, de mayor a menor largo para no pisar subcadenas.
        self.reemplazos = sorted((reemplazos or {}).items(), key=lambda kv: -len(kv[0]))

    def texto(self, v: str) -> str:
        for original, falso in self.reemplazos:
            if original in v:
                v = v.replace(original, falso)
        return v

    # --- Cliente (doc de `clientes` o un snapshot con su id) ---
    def cliente(self, d: dict, cliente_id):
        semilla = f"cli:{cliente_id}" if cliente_id else f"cli-nombre:{d.get('nombre')}{d.get('apellido_paterno')}"
        nombre, ap, am = _persona(semilla)
        if 'nombre' in d:
            d['nombre'] = nombre
        if 'apellido_paterno' in d:
            d['apellido_paterno'] = ap
        if 'apellido_materno' in d:
            d['apellido_materno'] = am
        if d.get('razon_social'):
            d['razon_social'] = f"{EMPRESAS[_h('emp', semilla) % len(EMPRESAS)]} {ap} SA de CV" \
                if (d.get('tipo_persona') or '').upper() == 'MORAL' else f"{nombre} {ap} {am}"
        return f"{nombre} {ap}"

    def nombre_cliente(self, cliente_id, original):
        nombre, ap, _ = _persona(f"cli:{cliente_id}" if cliente_id else f"cli-nombre:{original}")
        return f"{nombre} {ap}"

    def staff(self, original):
        nombre, ap, _ = _persona(f"staff:{original}")
        return f"{nombre} {ap}"

    def folio(self, valor: str):
        return valor if valor.endswith(self.sufijo_folio) else valor + self.sufijo_folio

    # --- Recorrido genérico ---
    def valor(self, key, v, padre):
        k = _norm(key) if isinstance(key, str) else ''
        if isinstance(v, dict):
            if k in ('clientesnapshot', 'cliente'):
                v = self.doc(v)
                self.cliente(v, v.get('id') or padre.get('cliente_id'))
                return v
            return self.doc(v)
        if isinstance(v, list):
            return [self.valor(key, x, padre) for x in v]
        if not isinstance(v, str) or not v:
            return v
        # Identidad del taller destino
        if v in self.sucursales_origen:
            return DESTINO_SUCURSAL
        if v == self.tenant_origen:
            return DESTINO_TENANT
        if k in ('sucursalnombre',):
            return DESTINO_SUCURSAL_NOMBRE
        # Folios con sufijo para respetar los índices únicos
        if 'folio' in k:
            return self.folio(v)
        # Datos personales. El personal primero: a veces su "nombre" es su correo.
        if k in STAFF_KEYS or (k.endswith('nombre') and ('by' in k or 'por' in k or 'usuario' in k)):
            return self.staff(v)
        if EMAIL_RE.match(v):
            return _email(f"mail:{v.lower()}")
        if k in ('clientenombre', 'nombrecliente'):
            return self.nombre_cliente(padre.get('cliente_id') or padre.get('clienteId'), v)
        if k in ('telefono', 'celular', 'whatsapp', 'clientetelefono', 'telefonocliente',
                 'proveedortelefono', 'admintelefono'):
            return _telefono(f"tel:{v}")
        if k in ('email', 'correo'):
            return _email(f"mail:{v.lower()}")
        if k == 'rfc':
            return _rfc(v)
        if k in ('direccion', 'domicilio', 'calle'):
            return _direccion(v)
        if k == 'razonsocial':
            nombre, ap, am = _persona(f"razon:{v}")
            return f"{nombre} {ap} {am}"
        if k in TEXTO_LIBRE:
            return self.texto(v)
        if k in ('placas', 'placa'):
            return _placas(v.upper())
        if k in ('vin', 'numeroserie', 'niv'):
            return _vin(v.upper())
        return v

    def doc(self, d: dict) -> dict:
        out = {}
        for key, v in d.items():
            k = _norm(key)
            if k in ('evidencia', 'evidencias', 'ip', 'useragent', 'logourl'):
                continue  # llaves de S3 de prod y metadatos de red
            out[key] = self.valor(key, v, d)
        return out


def _cliente_mongo(env_file):
    v = dotenv_values(env_file)
    uri = (f"mongodb+srv://{quote_plus(v['MONGO_USER'])}:{quote_plus(v['MONGO_PASSWORD'])}"
           f"@{v['MONGO_HOST']}/?retryWrites=true&w=majority")
    return MongoClient(uri, serverSelectionTimeoutMS=20000), v['MONGO_HOST']


def _en_sucursal(doc, origen):
    """Documentos de otra sucursal del mismo taller se quedan fuera (ver ORIGENES)."""
    for campo in ('sucursal_id', 'sucursalId'):
        if campo in doc and doc[campo]:
            return doc[campo] == origen['sucursal']
    return True


def _cerrar_caja(sesion, anon):
    """Una sucursal sólo puede tener una caja ABIERTA (índice único) y el destino ya
    tiene la suya: las sesiones abiertas copiadas se cierran cuadradas al último movimiento."""
    if sesion.get('estado') != 'ABIERTA':
        return sesion
    movs = sesion.get('movimientos') or []
    fechas = [m.get('fecha') for m in movs if m.get('fecha')]
    esperado = (float(sesion.get('monto_inicial') or 0) + float(sesion.get('total_ventas') or 0)
                + float(sesion.get('total_entradas') or 0) - float(sesion.get('total_salidas') or 0))
    sesion.update({
        'estado': 'CERRADA',
        'fecha_cierre': max(fechas) if fechas else sesion.get('fecha_apertura'),
        'monto_esperado': round(esperado, 2),
        'monto_final': round(esperado, 2),
        'diferencia': 0.0,
        'usuario_cierre_id': sesion.get('usuario_apertura_id'),
        'usuario_cierre_nombre': sesion.get('usuario_apertura_nombre'),
        'cerrada_por_copia': True,
    })
    return sesion


def preparar(prod, origen, ahora):
    src = prod[f"t_{origen['tenant']}"]
    reemplazos = {}
    for v in src.vehiculos.find({}, {'placas': 1, 'vin': 1}):
        if v.get('placas') and len(v['placas'].strip()) >= 5:
            reemplazos[v['placas'].strip()] = _placas(v['placas'].strip().upper())
        if v.get('vin') and len(v['vin'].strip()) >= 8:
            reemplazos[v['vin'].strip()] = _vin(v['vin'].strip().upper())
    for c in src.clientes.find({}, {'telefono': 1, 'email': 1}):
        if c.get('telefono') and len(str(c['telefono'])) >= 8:
            reemplazos[str(c['telefono'])] = _telefono(f"tel:{c['telefono']}")
        if c.get('email'):
            reemplazos[c['email']] = _email(f"mail:{c['email'].lower()}")
    anon = Anonimizador(origen['sucursales_todas'], origen['tenant'], origen['sufijo_folio'], reemplazos)
    lotes = {}
    ordenes_copiadas = set()
    for col in COLECCIONES:
        docs = []
        for d in src[col].find({}):
            if col == 'os_events':
                if d.get('orden_id') not in ordenes_copiadas:
                    continue
                d.pop('meta', None)
            elif col not in SIN_SUCURSAL and not _en_sucursal(d, origen):
                continue
            nuevo = anon.doc(d)
            if col == 'clientes':
                anon.cliente(nuevo, str(d['_id']))
                nuevo['flotilla_id'] = None  # las flotillas no se copian
            if col == 'caja_sesiones':
                nuevo = _cerrar_caja(nuevo, anon)
            if col == 'ordenes_servicio':
                ordenes_copiadas.add(str(d['_id']))
            nuevo[MARCA] = {'origen': 'prod', 'taller': origen['nombre'], 'copiado': ahora}
            docs.append(nuevo)
        lotes[col] = docs
    return lotes


def _sensibles(prod, origen):
    """Valores originales que no deben sobrevivir a la anonimización.

    Nombres: sólo completos (nombre + apellido); un nombre suelto como "Arturo"
    coincide con los nombres ficticios y con palabras de refacciones. Placas y VIN:
    sólo si mezclan letras y números, por la misma razón.
    """
    src = prod[f"t_{origen['tenant']}"]
    vistos = set()

    def alfanum(x):
        return isinstance(x, str) and re.search(r'[A-Za-z]', x) and re.search(r'\d', x) and len(x.strip()) >= 5

    for c in src.clientes.find({}, {'nombre': 1, 'apellido_paterno': 1, 'telefono': 1, 'email': 1, 'rfc': 1}):
        if c.get('nombre') and c.get('apellido_paterno'):
            vistos.add(f"{c['nombre'].strip()} {c['apellido_paterno'].strip()}")
        vistos.update(str(x) for x in (c.get('telefono'), c.get('email')) if x and len(str(x)) >= 8)
        if alfanum(c.get('rfc')):
            vistos.add(c['rfc'])
    for v in src.vehiculos.find({}, {'placas': 1, 'vin': 1}):
        vistos.update(x.strip() for x in (v.get('placas'), v.get('vin')) if alfanum(x))
    for u in src.usuarios.find({}, {'email': 1, 'nombre': 1, 'apellido_paterno': 1, 'name': 1}):
        if u.get('email'):
            vistos.add(u['email'])
        completo = u.get('name') or f"{u.get('nombre') or ''} {u.get('apellido_paterno') or ''}".strip()
        if ' ' in completo:
            vistos.add(completo)
    return {x for x in vistos if isinstance(x, str) and x.strip()}


def verificar(prod, lotes_por_origen):
    import json
    fugas = []
    for origen, lotes in lotes_por_origen:
        texto = json.dumps({c: lotes[c] for c in lotes}, default=str, ensure_ascii=False)
        for valor in _sensibles(prod, origen):
            if json.dumps(valor, ensure_ascii=False)[1:-1] in texto:
                fugas.append((origen['nombre'], valor))
    return fugas


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='escribe en el taller de pruebas (dev)')
    ap.add_argument('--limpiar', action='store_true', help='sólo borra lo copiado antes')
    args = ap.parse_args()

    prod, host_prod = _cliente_mongo('.env.prod')
    dev, host_dev = _cliente_mongo('.env')
    if host_prod == host_dev:
        raise SystemExit('[ABORTADO] .env apunta al mismo cluster que .env.prod: el destino debe ser dev.')
    destino = dev[f"t_{DESTINO_TENANT}"]
    if not destino.sucursales.find_one({'_id': __import__('bson').ObjectId(DESTINO_SUCURSAL)}):
        raise SystemExit('[ABORTADO] La sucursal destino no existe en el taller de pruebas.')

    print(f"Origen (solo lectura): {host_prod}")
    print(f"Destino: {host_dev} / t_{DESTINO_TENANT}")

    if args.limpiar:
        for col in COLECCIONES:
            r = destino[col].delete_many({MARCA: {'$exists': True}})
            print(f"  {col:24s} borrados {r.deleted_count}")
        return

    ahora = datetime.utcnow()
    lotes_por_origen = [(o, preparar(prod, o, ahora)) for o in ORIGENES]

    print('\nDocumentos a copiar:')
    for col in COLECCIONES:
        partes = '  '.join(f"{o['nombre'][:12]}={len(l[col])}" for o, l in lotes_por_origen)
        print(f"  {col:24s} {partes}")

    # Choques contra índices únicos (folios) con lo que ya tiene el destino.
    for col in ('ordenes_servicio', 'cotizaciones'):
        existentes = {d['folio'] for d in destino[col].find({MARCA: {'$exists': False}}, {'folio': 1}) if d.get('folio')}
        nuevos = [d.get('folio') for _, l in lotes_por_origen for d in l[col] if d.get('folio')]
        dup = (set(nuevos) & existentes) | {f for f in nuevos if nuevos.count(f) > 1}
        if dup:
            raise SystemExit(f"[ABORTADO] folios duplicados en {col}: {sorted(dup)[:5]}")

    fugas = verificar(prod, lotes_por_origen)
    if fugas:
        print(f"\n[ABORTADO] {len(fugas)} dato(s) personal(es) sin anonimizar:")
        for taller, valor in fugas[:10]:
            print(f"  {taller}: {valor[:3]}*** (len {len(valor)})")
        raise SystemExit(1)
    print('\nVerificación: ningún nombre, teléfono, correo, RFC, placa o VIN original quedó en la copia.')

    ejemplo = next((d for _, l in lotes_por_origen for d in l['ventas']), None)
    if ejemplo:
        print('\nEjemplo de venta anonimizada:', {k: ejemplo.get(k) for k in ('folio', 'cliente_nombre', 'usuario_nombre', 'total', 'sucursal_id')})

    if not args.apply:
        print('\nDRY-RUN: no se escribió nada. Corre con --apply para copiar.')
        return

    for col in COLECCIONES:
        borrados = destino[col].delete_many({MARCA: {'$exists': True}}).deleted_count
        docs = [d for _, l in lotes_por_origen for d in l[col]]
        # El destino puede traer documentos de una copia manual anterior con el mismo
        # _id (sin la marca). Se respetan: no se pisan ni se duplican.
        previos = {x['_id'] for x in destino[col].find({'_id': {'$in': [d['_id'] for d in docs]}}, {'_id': 1})}
        docs = [d for d in docs if d['_id'] not in previos]
        if docs:
            destino[col].insert_many(docs, ordered=False)
        print(f"  {col:24s} borrados {borrados:4d}  insertados {len(docs):4d}  ya existían {len(previos)}")
    print('\nListo. Para quitar todo lo copiado: --limpiar')


if __name__ == '__main__':
    main()
