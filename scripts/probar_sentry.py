"""
probar_sentry.py — Manda un error de prueba a Sentry para confirmar que el DSN
funciona de punta a punta (proyecto correcto, red, cuota) antes de confiar en él.

Úsalo cuando des de alta el DSN o cuando dudes de si el monitoreo sigue vivo.
No toca la base de datos ni despliega nada: sólo emite un evento.

Uso:
  # Con el DSN en el ambiente (o en el .env del repo):
  python scripts/probar_sentry.py

  # Pasándolo a mano (útil para probar el de prod desde tu máquina):
  python scripts/probar_sentry.py --dsn "https://...@oXXXX.ingest.us.sentry.io/NNNN" --env production

El evento aparece en Sentry como `PruebaDeMonitoreo` con tag origen=script y el
nombre de quien lo lanzó, para distinguirlo de un incidente real.
"""
import argparse
import getpass
import os
import socket
import sys

from dotenv import load_dotenv

load_dotenv()


def main():
    parser = argparse.ArgumentParser(description="Envía un evento de prueba a Sentry.")
    parser.add_argument("--dsn", help="DSN a probar. Por defecto usa SENTRY_DSN del ambiente/.env.")
    parser.add_argument("--env", default=None,
                        help="environment del evento (production / development / local).")
    args = parser.parse_args()

    dsn = args.dsn or os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        print("[ERROR] No hay DSN. Pásalo con --dsn o define SENTRY_DSN en el .env.")
        sys.exit(1)

    entorno = args.env or os.environ.get("SENTRY_ENVIRONMENT") or "local"

    try:
        import sentry_sdk
    except ImportError:
        print("[ERROR] Falta sentry-sdk. Corre: pip install -r requirements.txt")
        sys.exit(1)

    sentry_sdk.init(dsn=dsn, environment=entorno, traces_sample_rate=0.0, send_default_pii=False)

    quien = f"{getpass.getuser()}@{socket.gethostname()}"
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("origen", "script")
        scope.set_tag("lanzado_por", quien)
        try:
            raise RuntimeError(
                f"PruebaDeMonitoreo: evento de verificación lanzado por {quien}. "
                f"Si lo estás viendo en Sentry, el reporte de errores funciona.")
        except RuntimeError as e:
            event_id = sentry_sdk.capture_exception(e)

    entregado = sentry_sdk.flush(timeout=10)
    print(f"Evento enviado. id={event_id}  environment={entorno}")
    if entregado is False:
        print("[!] El flush expiró: el evento pudo no llegar. Revisa red/DSN.")
        sys.exit(1)
    # Sin flechas ni tipografia unicode: la consola de Windows (cp1252) las revienta.
    print(f"Buscalo en Sentry > Issues > filtro environment:{entorno} > 'PruebaDeMonitoreo'.")
    print("Si NO aparece en un minuto: DSN de otro proyecto, o el evento fue filtrado por cuota.")


if __name__ == "__main__":
    main()
