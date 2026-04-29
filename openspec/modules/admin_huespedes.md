# Módulo: Administración de Huéspedes (Tenants)

## Objetivo
Gestionar los talleres y empresas registrados en el ecosistema SIGA, permitiendo habilitar o deshabilitar módulos del sistema dependiendo de las necesidades y el plan contratado por cada huésped.

## Historias de Usuario (BDD)

### Alta de Taller
- **Como** Súper Administrador del Sistema
- **Quiero** dar de alta un nuevo Taller (Tenant) en la plataforma
- **Para** que la empresa pueda empezar a usar el software SIGA.

### Configuración de Módulos
- **Como** Súper Administrador del Sistema
- **Quiero** seleccionar a qué módulos tiene acceso un Taller (ej. habilitar "Ventas" pero deshabilitar "CRM Avanzado")
- **Para** adaptar el sistema a su suscripción.

### Suspensión de Servicio
- **Como** Súper Administrador del Sistema
- **Quiero** suspender el acceso de un Tenant
- **Para** restringir el servicio en caso de falta de pago o cancelación.

## Reglas de Negocio
1. **Acceso Condicional:** Si un módulo no está en el array de `modulos_activos` del Tenant, ninguna ruta ni API de ese módulo debe ser accesible por los usuarios del Tenant.
2. **Jerarquía:** Solo los Súper Administradores del Sistema (dueños de SIGA) tienen acceso a este módulo. Los administradores locales del taller no ven esta información.
