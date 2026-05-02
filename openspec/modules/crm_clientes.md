# Módulo: CRM y Clientes

## Objetivo
Centralizar la información de contacto de los clientes del taller y mantener un historial detallado de las interacciones y los vehículos asociados a cada cliente.

## Historias de Usuario (BDD)

### Expediente de Cliente
- **Como** Asesor de Servicio
- **Quiero** registrar a un nuevo cliente con sus datos de contacto y múltiples vehículos
- **Para** agilizar la apertura de futuras órdenes de servicio.

### Historial de Servicios
- **Como** Asesor de Servicio
- **Quiero** ver el historial completo de reparaciones y servicios de un vehículo específico
- **Para** ofrecer mantenimientos preventivos o conocer fallas recurrentes.

### Gestión de Contactos
- **Como** Asesor
- **Quiero** registrar notas y seguimiento sobre un cliente potencial (ej. cotizaciones pendientes)
- **Para** concretar más ventas de servicios.


## Reglas de Negocio
1. **Unicidad de Vehículo:** Un número de serie (VIN) o placa debe ser único por Taller. Si un vehículo cambia de dueño, se reasigna al nuevo cliente.
2. **Privacidad de Datos:** Los datos del cliente (teléfono, email) deben estar accesibles fácilmente en pantalla para envío de notificaciones por WhatsApp/Email.
3. **Búsqueda Rápida:** El sistema debe soportar búsqueda rápida por placa, nombre de cliente o teléfono.
