# Observabilidad y Logs

## A. Formato de Salida
Todos los logs de la aplicación deben emitirse en formato JSON estructurado. Esto permite que Grafana o Splunk indexen los campos automáticamente sin necesidad de expresiones regulares complejas.

## B. Campos Obligatorios (El Contrato)
Cada línea de log debe contener, como mínimo:

- **timestamp**: ISO 8601.
- **level**: INFO, WARN, ERROR, DEBUG.
- **service_id**: Nombre del microservicio (ej. `siga-inventory`).
- **tenant_id**: Extraído del `TenantContext`. (Vital para filtrar errores de un solo taller).
- **trace_id**: ID único de la petición (generado por Micrometer Tracing) para seguir el camino de una operación entre microservicios.
- **message**: Descripción clara del evento.

## C. Política de Niveles
- **ERROR**: Eventos que requieren atención inmediata (fallo en BD, error 500).
- **WARN**: Situaciones inesperadas pero recuperables (reintento de conexión, validación de negocio fallida).
- **INFO**: Hitos importantes (inicio de aplicación, orden de servicio creada).
- **DEBUG**: Información técnica para desarrollo (payloads de entrada/salida). Desactivado en producción.