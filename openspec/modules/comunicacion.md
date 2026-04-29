# Módulo: Comunicación Automática

## Objetivo
Mantener informado al cliente de forma proactiva y automática sobre el estado de su vehículo, cotizaciones y recordatorios, utilizando Email y WhatsApp.

## Historias de Usuario (BDD)

### Envío de Cotizaciones
- **Como** Vendedor
- **Quiero** presionar un botón de "Enviar por WhatsApp" en la pantalla de cotización
- **Para** que el cliente reciba un enlace a su cotización y la pueda aprobar en línea.

### Notificaciones de Estado
- **Como** Cliente
- **Quiero** recibir un mensaje automático cuando mi auto esté listo para recogerse
- **Para** poder organizarme e ir por él sin tener que llamar al taller para preguntar.

### Recordatorios de Servicio Mantenimiento
- **Como** Administrador del Taller
- **Quiero** que el sistema envíe recordatorios automáticos (ej. "Toca cambio de aceite") basándose en el tiempo transcurrido desde la última visita
- **Para** incentivar el retorno del cliente y generar más ventas.

## Reglas de Negocio
1. **Consentimiento:** El envío de WhatsApp o Email debe respetar las preferencias del cliente, permitiendo el opt-out si lo solicita.
2. **Procesamiento Asíncrono:** Todas las comunicaciones salientes se enviarán a través de una cola de mensajes (ej. RabbitMQ) para no bloquear la interfaz del usuario principal y permitir reintentos en caso de fallo con los proveedores (Twilio, SendGrid, Meta API).
3. **Plantillas:** Los mensajes enviados deben basarse en plantillas pre-aprobadas, permitiendo inyectar variables como `{{nombre_cliente}}` o `{{total_cotizacion}}`.
