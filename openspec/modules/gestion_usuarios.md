# Módulo: Gestión de Usuarios

## Objetivo
Administrar el ciclo de vida de los usuarios (CRUD), controlando sus accesos mediante roles y garantizando el aislamiento de información por taller, además de integrar la creación de identidades directamente con Keycloak.

## Historias de Usuario (BDD)

### Creación de Empleados (Tenant)
- **Como** Administrador del Taller (Admin)
- **Quiero** crear nuevos usuarios indicando su nombre, apellido, email, password, teléfono y rol
- **Para** que mi personal pueda acceder al sistema, quedando asociados automáticamente al `tenant_id` de mi taller.

### Gestión Global (Super Admin)
- **Como** Súper Administrador de la Plataforma
- **Quiero** crear usuarios administrativos propios sin necesidad de asociarlos a un `tenant_id` de taller
- **Para** gestionar el alta de nuevos clientes (Tenants) en todo el sistema.

### Aislamiento y Visibilidad
- **Como** Administrador del Taller
- **Quiero** visualizar en el listado únicamente a los usuarios que pertenecen a mi `tenant_id`
- **Para** garantizar la privacidad de mi personal y no ver información de otros talleres.

## Reglas de Negocio

1. **Estructura de Datos**: El registro de un usuario requiere: `nombre`, `apellido`, `email`, `password`, `telefono`, `rol` y `tenant_id`.
2. **Catálogo de Roles permitidos**:
   - `Super Admin`: Gestor de la plataforma global (no requiere `tenant_id`).
   - `Admin`: Dueño/Gerente de un Taller (asociado a un `tenant_id`).
   - `Asesor`: Empleado del Taller enfocado a CRM y Ventas.
   - `Mecanico`: Empleado enfocado en Operaciones.
3. **Manejo del Tenant ID**: 
   - Debe ser único por taller y generarse de forma automática al registrar una nueva empresa.
   - Al crear un usuario (Asesor o Mecánico), este hereda el `tenant_id` del Administrador que lo está creando.
4. **Excepción Super Admin**: El `SuperAdmin` no tiene un `tenant_id` comercial; solo tiene permisos para visualizar a sus pares y dar de alta o suspender a los talleres.
5. **Aprovisionamiento Cognito**: El endpoint de creación de usuarios en el Backend de SIGA debe comunicarse automáticamente con el API de Cognito para crear la cuenta, asignar el password y guardar el `tenant_id` como un atributo custom del usuario.
   - **Pruebas**: Debe ser posible enviar una petición POST vía Postman al backend y confirmar que el usuario se ha dado de alta exitosamente en Cognito.

## Gestion de usuarios por tenant

ademas de agregar el usuario Admin en Cognito, se requiere crear un tenant en Mongo, para llevar el control administrativo, saber que módulos tiene contratados, como va en sus pagos y si esta o no habilitado (por falta de pago)
quiero poder editar el rol de un usuario, pero sin cambiar su tenant_id