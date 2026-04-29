# Módulo: Administración de Usuarios

## Objetivo
Permitir a cada Taller (Tenant) gestionar a sus empleados, asignándoles perfiles con permisos específicos para acceder a diversas partes de la plataforma SIGA.
Existen 4 tipos de usuario: SuperAdmin, Admin, Ventas, Mecanico

## Historias de Usuario (BDD)

### Autenticación
- **Como** usuario del sistema
- **Quiero** poder iniciar sesión con mi correo electrónico y contraseña (vía KeyCloak)
- **Para** acceder a las herramientas correspondientes a mi rol dentro de la empresa.

### Gestión de Empleados
- **Como** Administrador del Taller
- **Quiero** agregar, editar y desactivar usuarios
- **Para** controlar quién tiene acceso al sistema de mi taller.

### Roles y Permisos
- **Como** Administrador del Taller
- **Quiero** asignar roles (Ventas, Mecánico, Recepción) a los usuarios
- **Para** restringir el acceso a información sensible o acciones destructivas (ej. el Mecánico no debe ver información financiera, sólo las órdenes de servicio asignadas a él).

## Reglas de Negocio
1. **Unicidad de correo:** El email de un usuario debe ser único a nivel global dentro de Cognito.
2. **Aislamiento:** Un usuario de un Tenant A no puede ver ni modificar usuarios del Tenant B.
3. **Desactivación:** Los usuarios no se eliminan físicamente (soft delete), se cambian a estado inactivo para preservar la integridad referencial en órdenes de servicio históricas.
