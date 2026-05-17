"""Tests del enlace público para cliente (cliente_link_manager).

Cubre:
- Firma/verificación HMAC (round trip + tampering + expiración).
- Generación de challenge con datos completos y degradados.
- Hash determinista de respuesta.
- Generación de link interno (Cognito) + lectura desde acceso.
- Flow público: challenge → verify → get → decidir.
- Bloqueo después de MAX_VERIFY_ATTEMPTS intentos.
- Que items con no_cobrar nunca lleguen al cliente.
- Que el cliente NO pueda decidir si la OS ya está APROBADA.
"""

import json
import os
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from bson import ObjectId

# Setear secreto fijo ANTES de importar el módulo para que los hashes sean reproducibles.
os.environ['CLIENT_LINK_SECRET'] = 'test-secret-fixed'
os.environ['CLIENT_LINK_BASE_URL'] = 'https://test.example.com/#/cliente/cotizacion'

from src.handlers.ordenes import cliente_link_manager as clm  # noqa: E402


# ---------- helpers ----------

TENANT = 'tester'
COGNITO_EVENT = {
    'requestContext': {
        'authorizer': {'claims': {'custom:tenant_id': TENANT, 'email': 'asesor@taller.com'}}
    }
}


def _seed_orden(mock_db, telefono='5512345678', placas='ABC-1234', estado='COTIZADO'):
    db = mock_db[f't_{TENANT}']
    vehiculo_id = db['vehiculos'].insert_one({
        'placas': placas, 'marca': 'Nissan', 'modelo': 'Sentra', 'anio': 2018,
    }).inserted_id
    orden_id = db['ordenes_servicio'].insert_one({
        'folio': 'OS-001',
        'estado': estado,
        'cliente_snapshot': {'nombre': 'Juan', 'apellido_paterno': 'Pérez', 'telefono': telefono},
        'vehiculo_id': str(vehiculo_id),
        'vehiculo_snapshot': {'marca': 'Nissan', 'modelo': 'Sentra', 'anio': 2018, 'placas': placas},
        'puntosArreglar': [
            {
                'nombre': 'Frenos',
                'items': [
                    {'nombre': 'Balatas', 'noParte': 'BAL-1', 'piezas': 1, 'precioVenta': 800, 'subtotal': 800, 'aprobado': True},
                    {'nombre': 'Líquido', 'noParte': 'LIQ-1', 'piezas': 1, 'precioVenta': 200, 'subtotal': 200, 'aprobado': True, 'no_cobrar': True},
                ],
            },
            {
                'nombre': 'Suspensión',
                'items': [
                    {'nombre': 'Amortiguador', 'noParte': 'AMO-1', 'piezas': 2, 'precioVenta': 1500, 'subtotal': 3000, 'aprobado': True},
                ],
            },
        ],
        'createdAt': datetime.utcnow(),
    }).inserted_id
    return str(orden_id)


def _path_event(orden_id, claims_event=True):
    ev = {'pathParameters': {'id': orden_id}}
    if claims_event:
        ev.update(COGNITO_EVENT)
    return ev


# ---------- sign / verify ----------

def test_sign_verify_round_trip():
    payload = {'t': 'tenant', 'o': 'orden', 'n': 'nonce', 'exp': int((datetime.utcnow() + timedelta(days=1)).timestamp())}
    token = clm._sign(payload)
    assert clm._verify(token) == payload


def test_verify_rejects_tampered():
    payload = {'t': 'tenant', 'o': 'orden', 'n': 'nonce', 'exp': int((datetime.utcnow() + timedelta(days=1)).timestamp())}
    token = clm._sign(payload)
    body, sig = token.split('.')
    # cambia un caracter de la firma
    bad_sig = ('A' if sig[0] != 'A' else 'B') + sig[1:]
    assert clm._verify(f'{body}.{bad_sig}') is None


def test_verify_rejects_expired():
    payload = {'t': 'x', 'o': 'y', 'n': 'z', 'exp': int((datetime.utcnow() - timedelta(seconds=1)).timestamp())}
    token = clm._sign(payload)
    assert clm._verify(token) is None


# ---------- challenge ----------

def test_challenge_full_uses_both_sources():
    ch = clm._build_challenge('ABC-1234', '5512345678')
    assert ch is not None
    assert ch['degraded'] is False
    assert len(ch['spec']) == 2
    sources = sorted(s['src'] for s in ch['spec'])
    assert sources == ['phone', 'placa']
    # expected_hash debe coincidir con _hash_answer(expected_plain)
    assert clm._hash_answer(ch['expected_plain']) == ch['expected_hash']


def test_challenge_degraded_phone_only():
    ch = clm._build_challenge('', '5512345678')
    assert ch['degraded'] is True
    assert ch['spec'][0]['src'] == 'phone'


def test_challenge_degraded_placa_only():
    ch = clm._build_challenge('XYZ-9876', '')
    assert ch['degraded'] is True
    assert ch['spec'][0]['src'] == 'placa'


def test_challenge_returns_none_without_data():
    assert clm._build_challenge('', '') is None


def test_digits_only_strips_letters():
    assert clm._digits('ABC-1234') == '1234'
    assert clm._digits('55-1234-5678') == '5512345678'


def test_recompute_expected_is_deterministic():
    spec = [{'src': 'placa', 'mode': 'last', 'count': 2}, {'src': 'phone', 'mode': 'first', 'count': 3}]
    assert clm._recompute_expected(spec, 'ABC-1234', '5512345678') == '34551'


# ---------- create link (interno) ----------

def test_create_link_persists_acceso_and_returns_token(mock_db):
    orden_id = _seed_orden(mock_db)
    resp = clm.create_cliente_link_handler(_path_event(orden_id), None)
    assert resp['statusCode'] == 200
    data = json.loads(resp['body'])['data']
    assert data['url'].startswith('https://test.example.com/')
    assert data['url'].endswith(data['token'])
    assert data['challenge_expected']
    # se persistió
    db = mock_db[f't_{TENANT}']
    acceso = db['cotizacion_acceso'].find_one({'orden_id': orden_id})
    assert acceso is not None
    assert acceso['nonce']
    assert acceso['intentos_verificacion'] == 0


def test_create_link_rotates_nonce(mock_db):
    orden_id = _seed_orden(mock_db)
    resp1 = clm.create_cliente_link_handler(_path_event(orden_id), None)
    token1 = json.loads(resp1['body'])['data']['token']
    resp2 = clm.create_cliente_link_handler(_path_event(orden_id), None)
    token2 = json.loads(resp2['body'])['data']['token']
    assert token1 != token2  # diferente nonce ⇒ diferente token


def test_create_link_fails_without_cliente_data(mock_db):
    orden_id = _seed_orden(mock_db, telefono='', placas='')
    resp = clm.create_cliente_link_handler(_path_event(orden_id), None)
    assert resp['statusCode'] == 400


def test_revoke_link_deletes_acceso(mock_db):
    orden_id = _seed_orden(mock_db)
    clm.create_cliente_link_handler(_path_event(orden_id), None)
    resp = clm.revoke_cliente_link_handler(_path_event(orden_id), None)
    assert resp['statusCode'] == 200
    db = mock_db[f't_{TENANT}']
    assert db['cotizacion_acceso'].find_one({'orden_id': orden_id}) is None


# ---------- flow público ----------

def _bootstrap_session(mock_db, **kwargs):
    """Crea OS + link + verifica con la respuesta correcta. Devuelve session_token."""
    orden_id = _seed_orden(mock_db, **kwargs)
    resp = clm.create_cliente_link_handler(_path_event(orden_id), None)
    body = json.loads(resp['body'])['data']
    token = body['token']
    expected = body['challenge_expected']

    verify_ev = {'body': json.dumps({'token': token, 'answer': expected})}
    vresp = clm.public_verify_handler(verify_ev, None)
    assert vresp['statusCode'] == 200, vresp['body']
    session_token = json.loads(vresp['body'])['data']['session_token']
    return orden_id, token, session_token


def test_get_cotizacion_filters_no_cobrar(mock_db):
    _, _, session_token = _bootstrap_session(mock_db)
    ev = {'queryStringParameters': {'session_token': session_token}}
    resp = clm.public_get_cotizacion_handler(ev, None)
    assert resp['statusCode'] == 200
    data = json.loads(resp['body'])['data']
    # punto "Frenos" tenía 2 items pero uno con no_cobrar → solo debe llegar 1
    frenos = next(p for p in data['puntosArreglar'] if p['nombre'] == 'Frenos')
    assert len(frenos['items']) == 1
    assert frenos['items'][0]['nombre'] == 'Balatas'
    # no debe filtrarse el campo precioCompra (no estaba en seed, pero verifiquemos que no se filtraron campos internos)
    for p in data['puntosArreglar']:
        for it in p['items']:
            assert 'precioCompra' not in it
            assert 'costo_proveedor' not in it


def test_verify_wrong_answer_increments_attempts(mock_db):
    orden_id = _seed_orden(mock_db)
    resp = clm.create_cliente_link_handler(_path_event(orden_id), None)
    token = json.loads(resp['body'])['data']['token']

    for _ in range(clm.MAX_VERIFY_ATTEMPTS):
        vresp = clm.public_verify_handler({'body': json.dumps({'token': token, 'answer': '0000'})}, None)
        assert vresp['statusCode'] == 401

    db = mock_db[f't_{TENANT}']
    acceso = db['cotizacion_acceso'].find_one({'orden_id': orden_id})
    assert acceso['bloqueado_en'] is not None

    # un intento más sigue rechazando con 403 (bloqueado)
    vresp = clm.public_verify_handler({'body': json.dumps({'token': token, 'answer': '0000'})}, None)
    assert vresp['statusCode'] == 403


def test_decidir_marks_items_with_client_link_source(mock_db):
    orden_id, _, session_token = _bootstrap_session(mock_db)
    ev = {
        'body': json.dumps({
            'session_token': session_token,
            'decisiones': [
                {'punto_idx': 0, 'item_idx': 0, 'decision': 'rechazado'},  # balatas
                {'punto_idx': 1, 'item_idx': 0, 'decision': 'aprobado'},   # amortiguador
            ],
        }),
        'requestContext': {'identity': {'sourceIp': '1.2.3.4'}},
        'headers': {'User-Agent': 'pytest'},
    }
    resp = clm.public_decidir_handler(ev, None)
    assert resp['statusCode'] == 200
    data = json.loads(resp['body'])['data']
    assert data['modificados'] == 2
    # Total = 0 (balatas rechazadas) + 3000 (amortiguador) = 3000
    assert data['total'] == 3000.0

    db = mock_db[f't_{TENANT}']
    orden = db['ordenes_servicio'].find_one({'_id': ObjectId(orden_id)})
    bal = orden['puntosArreglar'][0]['items'][0]
    amo = orden['puntosArreglar'][1]['items'][0]
    assert bal['decision'] == 'rechazado'
    assert bal['decision_source'] == 'client_link'
    assert bal['decided_by'] is None
    assert bal['decided_meta'] == {'ip': '1.2.3.4', 'user_agent': 'pytest'}
    assert amo['decision'] == 'aprobado'
    assert amo['decision_source'] == 'client_link'


def test_decidir_blocked_when_orden_is_aprobado(mock_db):
    _, _, session_token = _bootstrap_session(mock_db, estado='APROBADO')
    ev = {
        'body': json.dumps({
            'session_token': session_token,
            'decisiones': [{'punto_idx': 0, 'item_idx': 0, 'decision': 'aprobado'}],
        }),
    }
    resp = clm.public_decidir_handler(ev, None)
    assert resp['statusCode'] == 409


def test_decidir_ignores_no_cobrar_items(mock_db):
    _, _, session_token = _bootstrap_session(mock_db)
    # Intenta decidir sobre el item index 1 del punto 0 (que es no_cobrar)
    ev = {
        'body': json.dumps({
            'session_token': session_token,
            'decisiones': [{'punto_idx': 0, 'item_idx': 1, 'decision': 'aprobado'}],
        }),
    }
    resp = clm.public_decidir_handler(ev, None)
    # ninguna decisión válida ⇒ 400
    assert resp['statusCode'] == 400


def test_get_cotizacion_requires_session_not_access_token(mock_db):
    orden_id = _seed_orden(mock_db)
    resp = clm.create_cliente_link_handler(_path_event(orden_id), None)
    access_token = json.loads(resp['body'])['data']['token']
    # Usar el token de acceso (no de sesión) debe fallar
    ev = {'queryStringParameters': {'session_token': access_token}}
    resp2 = clm.public_get_cotizacion_handler(ev, None)
    assert resp2['statusCode'] == 401


# ---------- integración con ordenes_manager.update_orden_handler ----------

def test_manual_decision_stamps_source_manual(mock_db):
    """Cuando el asesor cambia aprobado→rechazado vía PUT /ordenes/{id}, se estampa source=manual."""
    from src.handlers.ordenes.ordenes_manager import update_orden_handler
    orden_id = _seed_orden(mock_db)
    db = mock_db[f't_{TENANT}']
    orden_actual = db['ordenes_servicio'].find_one({'_id': ObjectId(orden_id)})

    # Construir puntosArreglar con la balata rechazada
    nuevos_puntos = orden_actual['puntosArreglar']
    nuevos_puntos[0]['items'][0]['aprobado'] = False
    nuevos_puntos[0]['items'][0]['rechazado'] = True

    ev = {
        'pathParameters': {'id': orden_id},
        'body': json.dumps({'puntosArreglar': nuevos_puntos}),
        **COGNITO_EVENT,
    }
    resp = update_orden_handler(ev, None)
    assert resp['statusCode'] == 200

    orden = db['ordenes_servicio'].find_one({'_id': ObjectId(orden_id)})
    bal = orden['puntosArreglar'][0]['items'][0]
    assert bal['decision'] == 'rechazado'
    assert bal['decision_source'] == 'manual'
    assert bal['decided_by'] == 'asesor@taller.com'
    assert bal['decided_at']
