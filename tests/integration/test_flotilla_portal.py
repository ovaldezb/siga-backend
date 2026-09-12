"""Tests del portal público de flotilla (flotilla_portal_manager).

Cubre:
- Generación / rotación / consulta / revocación del enlace (lado asesor).
- Flow público: challenge → verify → resumen → 360° de la unidad.
- Bloqueo tras MAX_VERIFY_ATTEMPTS PINs incorrectos.
- Que un token de acceso no sirva como sesión (y viceversa).
- Aislamiento: unidades de otra flotilla / otro cliente devuelven 404 (IDOR).
- Sanitización: no salen cortesías, items rechazados ni costos internos.
- Estado de cuenta: saldo de cartera y gasto acumulado agregado en Mongo.
"""

import json
import os
from datetime import datetime, timedelta

import pytest
from bson import ObjectId

# Secreto fijo ANTES de importar, para que las firmas sean reproducibles.
os.environ['CLIENT_LINK_SECRET'] = 'test-secret-fixed'
os.environ['FLOTILLA_PORTAL_BASE_URL'] = 'https://test.example.com/#/flotilla/portal'

from src.handlers.flotillas import flotilla_portal_manager as fpm  # noqa: E402
from src.shared.utils import public_link  # noqa: E402
from src.shared.utils.indexes import reset_cache  # noqa: E402


TENANT = 'tester'
COGNITO_EVENT = {
    'requestContext': {
        'authorizer': {'claims': {'custom:tenant_id': TENANT, 'email': 'asesor@taller.com'}}
    }
}


# ---------- helpers ----------

def _db(mock_db):
    return mock_db[f't_{TENANT}']


def _path_event(flotilla_id, body=None):
    ev = dict(COGNITO_EVENT)
    ev['pathParameters'] = {'id': flotilla_id}
    if body is not None:
        ev['body'] = json.dumps(body)
    return ev


def _data(resp):
    return json.loads(resp['body'])['data']


def _seed_flotilla(mock_db, con_cliente=True):
    """Flotilla con 1 cliente, 2 unidades y 2 OS (una cerrada, una abierta)."""
    reset_cache()
    db = _db(mock_db)
    flot_id = str(db['flotillas'].insert_one({
        'nombre': 'Transportes del Norte',
        'razon_social': 'TRANSPORTES DEL NORTE SA DE CV',
        'tenant_id': TENANT,
    }).inserted_id)

    if not con_cliente:
        return flot_id, None, []

    cliente_id = str(db['clientes'].insert_one({
        'nombre': 'Operaciones',
        'apellido_paterno': 'Norte',
        'flotilla_id': flot_id,
        'telefono': '5512345678',
    }).inserted_id)

    veh1 = str(db['vehiculos'].insert_one({
        'placas': 'AAA-111', 'marca': 'Nissan', 'modelo': 'NP300', 'anio': 2020,
        'color': 'Blanco', 'kilometraje': 80000, 'cliente_id': cliente_id,
        'proximo_cambio_aceite': 85000,
    }).inserted_id)
    veh2 = str(db['vehiculos'].insert_one({
        'placas': 'BBB-222', 'marca': 'Ford', 'modelo': 'Transit', 'anio': 2019,
        'cliente_id': cliente_id,
    }).inserted_id)

    db['ordenes_servicio'].insert_one({
        'folio': 'OS-100',
        'estado': 'ENTREGADO',
        'cliente_snapshot': {'id': cliente_id, 'nombre': 'Operaciones', 'telefono': '5512345678'},
        'vehiculo_id': veh1,
        'kilometraje': 78000,
        'total': 3500.0,
        'facturada': False,
        'createdAt': datetime(2026, 3, 1, 10, 0, 0),
        'puntosArreglar': [{
            'nombre': 'Frenos',
            'items': [
                {'nombre': 'Balatas', 'noParte': 'BAL-1', 'piezas': 2, 'precioVenta': 1500,
                 'subtotal': 3000, 'precioCompra': 900, 'costo_proveedor': 850,
                 'aprobado': True, 'linea_id': 'l1'},
                {'nombre': 'Limpieza cortesía', 'piezas': 1, 'precioVenta': 300,
                 'subtotal': 300, 'no_cobrar': True, 'aprobado': True},
                {'nombre': 'Discos', 'piezas': 2, 'precioVenta': 2000, 'subtotal': 4000,
                 'rechazado': True, 'decision': 'rechazado'},
            ],
        }],
    })
    db['ordenes_servicio'].insert_one({
        'folio': 'OS-101',
        'estado': 'EN_PROCESO',
        'cliente_snapshot': {'id': cliente_id, 'nombre': 'Operaciones'},
        'vehiculo_id': veh2,
        'kilometraje': 120000,
        'total': 1200.0,
        'createdAt': datetime(2026, 5, 10, 9, 0, 0),
        'puntosArreglar': [{'nombre': 'Afinación', 'items': [
            {'nombre': 'Bujías', 'piezas': 4, 'precioVenta': 300, 'subtotal': 1200},
        ]}],
    })

    db['ventas'].insert_many([
        {'folio': 'V-1', 'cliente_id': cliente_id, 'total': 3500.0,
         'saldo_pendiente': 1500.0, 'estado': 'COMPLETADA',
         'createdAt': datetime(2026, 3, 1, 12, 0, 0)},
        {'folio': 'V-2', 'cliente_id': cliente_id, 'total': 800.0,
         'saldo_pendiente': 0.0, 'estado': 'COMPLETADA',
         'createdAt': datetime(2026, 4, 1, 12, 0, 0)},
        {'folio': 'V-3', 'cliente_id': cliente_id, 'total': 9999.0,
         'saldo_pendiente': 5000.0, 'estado': 'CANCELADA',
         'createdAt': datetime(2026, 4, 5, 12, 0, 0)},
    ])

    return flot_id, cliente_id, [veh1, veh2]


def _abrir_portal(mock_db, flot_id):
    """Genera el enlace y devuelve (token, pin, session_token)."""
    resp = fpm.create_portal_link_handler(_path_event(flot_id), None)
    assert resp['statusCode'] == 200, resp['body']
    d = _data(resp)
    vresp = fpm.public_verify_handler(
        {'body': json.dumps({'token': d['token'], 'pin': d['pin']})}, None
    )
    assert vresp['statusCode'] == 200, vresp['body']
    return d['token'], d['pin'], _data(vresp)['session_token']


def _sess_event(session_token, **qp):
    return {'queryStringParameters': {'session_token': session_token, **qp}}


# ---------- lado asesor ----------

def test_create_portal_link_devuelve_url_token_y_pin(mock_db):
    flot_id, _, _ = _seed_flotilla(mock_db)
    resp = fpm.create_portal_link_handler(_path_event(flot_id), None)
    assert resp['statusCode'] == 200
    d = _data(resp)

    assert d['url'].startswith('https://test.example.com/#/flotilla/portal/')
    assert d['url'].endswith(d['token'])
    assert len(d['pin']) == fpm.PIN_LENGTH and d['pin'].isdigit()
    assert d['flotilla_nombre'] == 'Transportes del Norte'

    payload = public_link.verify(d['token'])
    assert payload['t'] == TENANT and payload['f'] == flot_id
    assert 's' not in payload  # token de acceso, no de sesión

    acceso = _db(mock_db)['flotilla_acceso'].find_one({'flotilla_id': flot_id})
    assert acceso['pin_hash'] == public_link.hash_answer(d['pin'])
    assert acceso['created_by'] == 'asesor@taller.com'
    assert acceso['intentos_verificacion'] == 0


def test_create_portal_link_rechaza_flotilla_sin_clientes(mock_db):
    flot_id, _, _ = _seed_flotilla(mock_db, con_cliente=False)
    resp = fpm.create_portal_link_handler(_path_event(flot_id), None)
    assert resp['statusCode'] == 400
    assert 'no tiene clientes' in json.loads(resp['body'])['message']


def test_create_portal_link_404_si_flotilla_no_existe(mock_db):
    _seed_flotilla(mock_db)
    resp = fpm.create_portal_link_handler(_path_event(str(ObjectId())), None)
    assert resp['statusCode'] == 404


def test_rotar_invalida_el_token_anterior(mock_db):
    flot_id, _, _ = _seed_flotilla(mock_db)
    viejo = _data(fpm.create_portal_link_handler(_path_event(flot_id), None))
    nuevo = _data(fpm.create_portal_link_handler(_path_event(flot_id), None))

    assert viejo['token'] != nuevo['token']
    assert viejo['pin'] != nuevo['pin'] or True  # el PIN es aleatorio, puede repetirse

    # El token viejo trae un nonce que ya no es el vigente.
    resp = fpm.public_challenge_handler({'queryStringParameters': {'token': viejo['token']}}, None)
    assert resp['statusCode'] == 401
    assert 'ya no es válido' in json.loads(resp['body'])['message']

    ok = fpm.public_challenge_handler({'queryStringParameters': {'token': nuevo['token']}}, None)
    assert ok['statusCode'] == 200


def test_get_portal_link_no_rota_y_devuelve_pin(mock_db):
    flot_id, _, _ = _seed_flotilla(mock_db)
    creado = _data(fpm.create_portal_link_handler(_path_event(flot_id), None))
    leido = _data(fpm.get_portal_link_handler(_path_event(flot_id), None))

    assert leido['token'] == creado['token']
    assert leido['pin'] == creado['pin']
    assert leido['expirado'] is False
    assert leido['num_accesos'] == 0


def test_get_portal_link_404_sin_portal(mock_db):
    flot_id, _, _ = _seed_flotilla(mock_db)
    resp = fpm.get_portal_link_handler(_path_event(flot_id), None)
    assert resp['statusCode'] == 404


def test_revoke_portal_link(mock_db):
    flot_id, _, _ = _seed_flotilla(mock_db)
    token, pin, _s = _abrir_portal(mock_db, flot_id)

    resp = fpm.revoke_portal_link_handler(_path_event(flot_id), None)
    assert resp['statusCode'] == 200
    assert fpm.revoke_portal_link_handler(_path_event(flot_id), None)['statusCode'] == 404

    # Revocado: ni challenge ni verify funcionan con el token emitido.
    assert fpm.public_challenge_handler(
        {'queryStringParameters': {'token': token}}, None)['statusCode'] == 401
    assert fpm.public_verify_handler(
        {'body': json.dumps({'token': token, 'pin': pin})}, None)['statusCode'] == 401


def test_handlers_internos_requieren_tenant(mock_db):
    flot_id, _, _ = _seed_flotilla(mock_db)
    sin_claims = {'pathParameters': {'id': flot_id}, 'requestContext': {}}
    for h in (fpm.create_portal_link_handler, fpm.get_portal_link_handler,
              fpm.revoke_portal_link_handler):
        assert h(sin_claims, None)['statusCode'] == 403


# ---------- autenticación pública ----------

def test_challenge_describe_el_pin(mock_db):
    flot_id, _, _ = _seed_flotilla(mock_db)
    d = _data(fpm.create_portal_link_handler(_path_event(flot_id), None))
    resp = fpm.public_challenge_handler({'queryStringParameters': {'token': d['token']}}, None)
    assert resp['statusCode'] == 200
    ch = _data(resp)
    assert ch['flotilla_nombre'] == 'Transportes del Norte'
    assert ch['pin_length'] == fpm.PIN_LENGTH
    assert ch['intentos_restantes'] == fpm.MAX_VERIFY_ATTEMPTS
    assert 'PIN' in ch['prompt']


def test_challenge_rechaza_token_invalido(mock_db):
    _seed_flotilla(mock_db)
    assert fpm.public_challenge_handler(
        {'queryStringParameters': {'token': 'basura.basura'}}, None)['statusCode'] == 401
    assert fpm.public_challenge_handler({'queryStringParameters': {}}, None)['statusCode'] == 401


def test_challenge_rechaza_token_expirado(mock_db):
    flot_id, _, _ = _seed_flotilla(mock_db)
    fpm.create_portal_link_handler(_path_event(flot_id), None)
    vencido = public_link.sign({
        't': TENANT, 'f': flot_id, 'n': 'x',
        'exp': int((datetime.utcnow() - timedelta(seconds=1)).timestamp()),
    })
    assert fpm.public_challenge_handler(
        {'queryStringParameters': {'token': vencido}}, None)['statusCode'] == 401


def test_verify_pin_correcto_abre_sesion(mock_db):
    flot_id, _, _ = _seed_flotilla(mock_db)
    d = _data(fpm.create_portal_link_handler(_path_event(flot_id), None))
    resp = fpm.public_verify_handler(
        {'body': json.dumps({'token': d['token'], 'pin': d['pin']})},
        None,
    )
    assert resp['statusCode'] == 200
    sess = _data(resp)['session_token']
    payload = public_link.verify(sess)
    assert payload['s'] == 1 and payload['f'] == flot_id

    acceso = _db(mock_db)['flotilla_acceso'].find_one({'flotilla_id': flot_id})
    assert acceso['num_accesos'] == 1
    assert acceso['ultimo_acceso']


def test_verify_acepta_pin_como_array_de_slots(mock_db):
    flot_id, _, _ = _seed_flotilla(mock_db)
    d = _data(fpm.create_portal_link_handler(_path_event(flot_id), None))
    resp = fpm.public_verify_handler(
        {'body': json.dumps({'token': d['token'], 'pin': list(d['pin'])})}, None
    )
    assert resp['statusCode'] == 200


def test_verify_bloquea_tras_max_intentos(mock_db):
    flot_id, _, _ = _seed_flotilla(mock_db)
    d = _data(fpm.create_portal_link_handler(_path_event(flot_id), None))
    malo = '000000' if d['pin'] != '000000' else '111111'

    for i in range(fpm.MAX_VERIFY_ATTEMPTS):
        resp = fpm.public_verify_handler(
            {'body': json.dumps({'token': d['token'], 'pin': malo})}, None)
        assert resp['statusCode'] == 401
        assert _data(resp)['intentos_restantes'] == fpm.MAX_VERIFY_ATTEMPTS - 1 - i

    # Bloqueado: ni el PIN correcto entra.
    resp = fpm.public_verify_handler(
        {'body': json.dumps({'token': d['token'], 'pin': d['pin']})}, None)
    assert resp['statusCode'] == 403
    assert fpm.public_challenge_handler(
        {'queryStringParameters': {'token': d['token']}}, None)['statusCode'] == 403

    # El asesor rota y se desbloquea.
    nuevo = _data(fpm.create_portal_link_handler(_path_event(flot_id), None))
    assert fpm.public_verify_handler(
        {'body': json.dumps({'token': nuevo['token'], 'pin': nuevo['pin']})},
        None)['statusCode'] == 200


def test_verify_exitoso_resetea_intentos(mock_db):
    flot_id, _, _ = _seed_flotilla(mock_db)
    d = _data(fpm.create_portal_link_handler(_path_event(flot_id), None))
    malo = '000000' if d['pin'] != '000000' else '111111'
    fpm.public_verify_handler({'body': json.dumps({'token': d['token'], 'pin': malo})}, None)
    fpm.public_verify_handler({'body': json.dumps({'token': d['token'], 'pin': d['pin']})}, None)
    acceso = _db(mock_db)['flotilla_acceso'].find_one({'flotilla_id': flot_id})
    assert acceso['intentos_verificacion'] == 0


def test_no_se_pueden_confundir_token_y_session_token(mock_db):
    flot_id, _, _ = _seed_flotilla(mock_db)
    token, pin, sess = _abrir_portal(mock_db, flot_id)

    # session_token en el endpoint de challenge/verify → 401.
    assert fpm.public_challenge_handler(
        {'queryStringParameters': {'token': sess}}, None)['statusCode'] == 401
    assert fpm.public_verify_handler(
        {'body': json.dumps({'token': sess, 'pin': pin})}, None)['statusCode'] == 401

    # token de acceso donde se espera sesión → 401.
    assert fpm.public_resumen_handler(_sess_event(token), None)['statusCode'] == 401


def test_resumen_rechaza_sesion_de_flotilla_revocada(mock_db):
    flot_id, _, _ = _seed_flotilla(mock_db)
    _t, _p, sess = _abrir_portal(mock_db, flot_id)
    fpm.revoke_portal_link_handler(_path_event(flot_id), None)
    assert fpm.public_resumen_handler(_sess_event(sess), None)['statusCode'] == 401


# ---------- resumen de flota ----------

def test_resumen_lista_unidades_con_ultimo_servicio(mock_db):
    flot_id, _cli, (veh1, veh2) = _seed_flotilla(mock_db)
    _t, _p, sess = _abrir_portal(mock_db, flot_id)

    resp = fpm.public_resumen_handler(_sess_event(sess), None)
    assert resp['statusCode'] == 200
    d = _data(resp)

    assert d['flotilla']['nombre'] == 'Transportes del Norte'
    assert d['kpis']['unidades'] == 2
    assert d['kpis']['servicios_historicos'] == 2
    assert d['kpis']['os_abiertas'] == 1

    placas = [u['placas'] for u in d['unidades']]
    assert placas == ['AAA-111', 'BBB-222']

    u1 = next(u for u in d['unidades'] if u['id'] == veh1)
    assert u1['ultimo_servicio']['folio'] == 'OS-100'
    assert u1['ultimo_servicio']['total'] == 3500.0
    assert u1['num_servicios'] == 1
    assert u1['os_abiertas'] == 0
    assert u1['titular'] == 'Operaciones Norte'
    assert u1['proximo_cambio_aceite'] == 85000

    u2 = next(u for u in d['unidades'] if u['id'] == veh2)
    assert u2['os_abiertas'] == 1


def test_resumen_estado_de_cuenta(mock_db):
    flot_id, _, _ = _seed_flotilla(mock_db)
    _t, _p, sess = _abrir_portal(mock_db, flot_id)
    d = _data(fpm.public_resumen_handler(_sess_event(sess), None))

    # Cartera: sólo V-1 debe saldo; V-3 está cancelada y no cuenta.
    assert d['estado_cuenta']['saldo_total'] == 1500.0
    folios = [doc['folio'] for doc in d['estado_cuenta']['documentos']]
    assert folios == ['V-1']
    assert d['kpis']['saldo_pendiente'] == 1500.0

    # Gasto acumulado: V-1 + V-2, sin la cancelada.
    assert d['kpis']['gasto_total'] == 4300.0

    # OS cerrada y no facturada aparece como pendiente de CFDI.
    sin_factura = d['estado_cuenta']['os_sin_facturar']
    assert [o['folio'] for o in sin_factura] == ['OS-100']


def test_os_cancelada_se_ve_pero_no_cuenta_como_servicio(mock_db):
    flot_id, cliente_id, (veh1, _v2) = _seed_flotilla(mock_db)
    _db(mock_db)['ordenes_servicio'].insert_one({
        'folio': 'OS-999',
        'estado': 'CANCELADO',
        'cliente_snapshot': {'id': cliente_id, 'nombre': 'Operaciones'},
        'vehiculo_id': veh1,
        'total': 7777.0,
        'createdAt': datetime(2026, 6, 1, 10, 0, 0),
        'puntosArreglar': [{'nombre': 'Motor', 'items': [
            {'nombre': 'Junta', 'piezas': 1, 'precioVenta': 7777, 'subtotal': 7777},
        ]}],
    })
    _t, _p, sess = _abrir_portal(mock_db, flot_id)

    resumen = _data(fpm.public_resumen_handler(_sess_event(sess), None))
    assert resumen['kpis']['servicios_historicos'] == 2  # la cancelada no cuenta
    u1 = next(u for u in resumen['unidades'] if u['id'] == veh1)
    assert u1['num_servicios'] == 1
    assert u1['ultimo_servicio']['folio'] == 'OS-100'  # no la cancelada, aunque es más nueva

    unidad = _data(fpm.public_vehiculo_handler(_sess_event(sess, vehiculo_id=veh1), None))
    # Aparece en el historial (el cliente puede preguntar por ella)…
    assert [os['folio'] for os in unidad['historial']] == ['OS-999', 'OS-100']
    # …pero no infla métricas.
    assert unidad['metricas']['num_servicios'] == 1
    assert unidad['metricas']['gasto_total'] == 3500.0


def test_resumen_de_flotilla_sin_unidades(mock_db):
    """Una flotilla puede quedar sin vehículos; el portal no debe romperse."""
    flot_id, _, _ = _seed_flotilla(mock_db)
    _db(mock_db)['vehiculos'].delete_many({})
    _db(mock_db)['ordenes_servicio'].delete_many({})
    _t, _p, sess = _abrir_portal(mock_db, flot_id)

    d = _data(fpm.public_resumen_handler(_sess_event(sess), None))
    assert d['kpis']['unidades'] == 0
    assert d['unidades'] == []


# ---------- 360° de la unidad ----------

def test_vehiculo_360_historial_sanitizado(mock_db):
    flot_id, _cli, (veh1, _veh2) = _seed_flotilla(mock_db)
    _t, _p, sess = _abrir_portal(mock_db, flot_id)

    resp = fpm.public_vehiculo_handler(_sess_event(sess, vehiculo_id=veh1), None)
    assert resp['statusCode'] == 200
    d = _data(resp)

    assert d['vehiculo']['placas'] == 'AAA-111'
    assert d['vehiculo']['titular'] == 'Operaciones Norte'
    assert d['metricas']['num_servicios'] == 1
    assert d['metricas']['gasto_total'] == 3500.0

    os100 = d['historial'][0]
    assert os100['folio'] == 'OS-100'
    assert os100['total'] == 3500.0
    assert os100['kilometraje'] == 78000

    items = [i for p in os100['puntos'] for i in p['items']]
    nombres = [i['nombre'] for i in items]
    assert nombres == ['Balatas']                      # cortesía y rechazado fuera
    assert 'Limpieza cortesía' not in nombres
    assert 'Discos' not in nombres

    # Los items solo traen campos públicos: ni costos ni ids internos.
    assert set(items[0]) == {'nombre', 'noParte', 'piezas', 'precioVenta', 'subtotal', 'estado'}
    for i in items:
        assert 'precioCompra' not in i and 'costo_proveedor' not in i
        assert 900 not in i.values() and 850 not in i.values()

    # Y ninguna llave prohibida sobrevive en ningún nivel del payload.
    def _llaves(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield k
                yield from _llaves(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from _llaves(v)

    prohibidas = {'precioCompra', 'costo_proveedor', 'proveedor_id', 'linea_id',
                  'costo', 'margen', 'no_cobrar', 'bitacora_estados', 'mecanico_id'}
    assert prohibidas.isdisjoint(set(_llaves(d)))


def test_vehiculo_360_incluye_citas_vigentes(mock_db):
    flot_id, cliente_id, (veh1, _v2) = _seed_flotilla(mock_db)
    db = _db(mock_db)
    db['citas'].insert_many([
        {'vehiculoId': veh1, 'clienteId': cliente_id, 'fecha': '2026-06-01',
         'horaInicio': '09:00', 'estado': 'confirmada', 'servicio': 'Afinación',
         'notas': 'nota interna del asesor'},
        {'vehiculoId': veh1, 'clienteId': cliente_id, 'fecha': '2026-05-01',
         'horaInicio': '08:00', 'estado': 'completada', 'servicio': 'Ya pasó'},
        {'vehiculoId': veh1, 'clienteId': cliente_id, 'fecha': '2026-07-01',
         'horaInicio': '10:00', 'estado': 'cancelada', 'servicio': 'Cancelada'},
    ])

    _t, _p, sess = _abrir_portal(mock_db, flot_id)
    d = _data(fpm.public_vehiculo_handler(_sess_event(sess, vehiculo_id=veh1), None))

    assert [c['servicio'] for c in d['citas']] == ['Afinación']
    assert d['citas'][0]['hora'] == '09:00'
    # Las notas del asesor no son para el cliente.
    assert 'notas' not in d['citas'][0]


def test_vehiculo_360_de_otra_flotilla_es_404(mock_db):
    """El vehiculo_id llega por query: sin validar alcance sería un IDOR al tenant."""
    flot_id, _cli, _vehs = _seed_flotilla(mock_db)
    db = _db(mock_db)

    otro_cliente = str(db['clientes'].insert_one({
        'nombre': 'Ajeno', 'flotilla_id': str(ObjectId()),
    }).inserted_id)
    ajeno = str(db['vehiculos'].insert_one({
        'placas': 'ZZZ-999', 'marca': 'Honda', 'cliente_id': otro_cliente,
    }).inserted_id)
    suelto = str(db['vehiculos'].insert_one({
        'placas': 'YYY-888', 'marca': 'Mazda', 'cliente_id': str(ObjectId()),
    }).inserted_id)

    _t, _p, sess = _abrir_portal(mock_db, flot_id)
    for vid in (ajeno, suelto, str(ObjectId()), 'no-es-un-id'):
        resp = fpm.public_vehiculo_handler(_sess_event(sess, vehiculo_id=vid), None)
        assert resp['statusCode'] == 404, vid
        assert 'no encontrada' in json.loads(resp['body'])['message']


def test_vehiculo_360_requiere_vehiculo_id(mock_db):
    flot_id, _, _ = _seed_flotilla(mock_db)
    _t, _p, sess = _abrir_portal(mock_db, flot_id)
    assert fpm.public_vehiculo_handler(_sess_event(sess), None)['statusCode'] == 400


def test_vehiculo_360_sin_sesion(mock_db):
    flot_id, _cli, (veh1, _v2) = _seed_flotilla(mock_db)
    ev = {'queryStringParameters': {'vehiculo_id': veh1}}
    assert fpm.public_vehiculo_handler(ev, None)['statusCode'] == 401


def test_portal_no_expone_handlers_de_escritura():
    """Contrato del feature: el portal es de solo lectura."""
    publicos = [n for n in dir(fpm) if n.startswith('public_') and n.endswith('_handler')]
    assert sorted(publicos) == [
        'public_challenge_handler',
        'public_resumen_handler',
        'public_vehiculo_handler',
        'public_verify_handler',
    ]
