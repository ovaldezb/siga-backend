# Módulo: Gestión de Taller

## Objetivo
Visualizar y administrar el flujo de trabajo operativo dentro del taller, asignando tareas a los mecánicos y dando seguimiento al estado de cada vehículo.

## Historias de Usuario (BDD)

### Tablero Kanban de Vehículos
- **Como** Jefe de Taller
- **Quiero** ver un tablero visual (tipo Kanban) con los vehículos en sus diferentes estados (Recibido, Diagnóstico, Reparación, Listo para Entregar)
- **Para** conocer de un vistazo la carga de trabajo y los cuellos de botella.

### Asignación de Mecánicos
- **Como** Asesor de Servicio o Jefe de Taller
- **Quiero** asignar una orden de servicio a un mecánico específico
- **Para** responsabilizar a alguien de la ejecución de las tareas.

### Checklist de Recepción
- **Como** Asesor de Servicio
- **Quiero** llenar un checklist digital del estado visual del vehículo y sus pertenencias (nivel de gasolina, golpes, llanta de refacción) al recibirlo
- **Para** evitar malentendidos o reclamaciones con el cliente al momento de la entrega.

## Reglas de Negocio
1. **Flujo de Estados:** El cambio de estado de un vehículo debe ser secuencial o lógico (ej. no puede pasar de "Recibido" a "Terminado" sin haber pasado por "Reparación").
2. **Notificación Automática:** Al mover un vehículo a la columna "Listo para Entregar", el sistema debe disparar un evento que envíe una notificación al cliente por WhatsApp (módulo Comunicación).



