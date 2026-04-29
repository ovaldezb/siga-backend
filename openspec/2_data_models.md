# SIGA: Data Models Overview

## 1. Patrón Multi-tenant
El aislamiento de los datos se maneja a nivel de aplicación utilizando identificadores de **Tenant (tenant_id)** en cada colección de MongoDB, o utilizando esquemas/bases de datos separadas por Tenant (a definir en base al tamaño de cada cliente).

## 2. Entidades Principales

### Tenant (Taller / Empresa)
Representa a la empresa huésped que utiliza el sistema.
- `tenant_id`
- `nombre_empresa`
- `rfc` / `identificacion_fiscal`
- `modulos_activos` (Array de módulos contratados)
- `configuracion` (Preferencias, logos, monedas)

### Usuario
Representa a los empleados del Taller.
- `usuario_id`
- `tenant_id`
- `nombre`
- `email`
- `rol` (Admin, Ventas, Mecánico, Recepción)
- `activo` (Boolean)

### Cliente
Dueños de los vehículos.
- `cliente_id`
- `tenant_id`
- `nombre`
- `telefono` (Para WhatsApp)
- `email`
- `direccion`
- `vehiculos_resumen`: [
    { "vehiculo_id": "VEH001", "placas": "ABC-123", "marca": "Nissan", "modelo": "Sentra", "año": 2022 },
    { "vehiculo_id": "VEH002", "placas": "XYZ-789", "marca": "Jeep", "modelo": "Wrangler", "año": 2020 }
]

### Vehículo del cliente
Unidad automotriz asociada a un cliente.
- `vehiculo_id`
- `cliente_id`
- `tenant_id`
- `placas`
- `marca`
- `modelo`
- `año`
- `vin`

### Producto / Refacción
Catálogo de partes para venta o uso en taller.
- `producto_id`
- `tenant_id`
- `codigo`
- `nombre`
- `descripcion`
- `precio_compra`
- `precio_venta`
- `stock_actual`

### Orden de Servicio
El documento central de la gestión de taller.
- `orden_id`
- `tenant_id`
- `cliente_id`
- `vehiculo_id`
- `estado` (Recibido, En Diagnóstico, En Reparación, Terminado, Entregado)
- `fecha_ingreso`
- `fecha_estimada_entrega`
- `servicios` (Array de servicios a realizar)
- `refacciones` (Array de productos utilizados)
- `total`
