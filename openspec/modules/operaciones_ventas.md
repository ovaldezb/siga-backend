# Módulo: Operaciones y Ventas

## Objetivo
Gestionar el ciclo financiero de las reparaciones y las ventas de mostrador, abarcando desde la cotización hasta la facturación electrónica.

## Historias de Usuario (BDD)

### Cotizaciones
- **Como** Asesor de Servicio o Vendedor
- **Quiero** generar una cotización agregando mano de obra y refacciones, y enviarla por WhatsApp/Email al cliente
- **Para** que el cliente la apruebe antes de iniciar el trabajo.

### Órdenes de Servicio (Punto de Venta)
- **Como** Asesor de Servicio
- **Quiero** convertir una cotización aprobada en una Orden de Servicio activa
- **Para** iniciar los trabajos en el taller y reservar el stock de las refacciones.

### Facturación Electrónica
- **Como** Cajero o Administrador
- **Quiero** generar un comprobante fiscal (CFDI) a partir de una orden de servicio terminada y pagada
- **Para** cumplir con las obligaciones fiscales locales y entregar el recibo al cliente.

## Reglas de Negocio
1. **Aprobación de Cotización:** Una orden de servicio no puede avanzar al estado "En Reparación" si el cliente no ha aprobado la cotización de los trabajos.
2. **Cálculo de Totales:** El sistema debe sumar automáticamente el costo de refacciones + mano de obra, y aplicar el IVA/impuestos correspondientes según la configuración del Tenant.
3. **Restricción de Edición:** Una orden facturada queda bloqueada; no se le pueden agregar ni quitar conceptos.
