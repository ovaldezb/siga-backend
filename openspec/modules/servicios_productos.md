# Módulo: Catálogo de Productos y Refacciones

## 1. Objetivo
Gestionar de manera unificada todos los recursos facturables del taller (refacciones y mano de obra), permitiendo un control de inventario diferenciado y una búsqueda ágil para el proceso de venta.

## 2. Definición del Modelo de Datos (Items)

Utilizaremos una sola colección en MongoDB denominada **`items`**.

| Campo | Tipo | Requerido | Descripción |
| :--- | :--- | :---: | :--- |
| `item_id` | UUID | Sí | Identificador único del registro. |
| `tenant_id` | UUID | Sí | Identificador del taller propietario (Garantía de integridad). |
| `tipo` | String | Sí | Valores: `PRODUCTO` o `SERVICIO`. |
| `codigo` | String | Sí | SKU o código interno para búsqueda rápida (ej. FIL-001). |
| `nombre` | String | Sí | Descripción comercial del item. |
| `precio_venta` | Decimal | Sí | Precio al público. |
| `costo` | Decimal | No | Costo de adquisición (Solo para `PRODUCTO`). |
| `maneja_inventario` | Boolean | Sí | Flag para activar lógica de stock. |
| `stock` | Integer | No | Cantidad actual en almacén (Solo si `maneja_inventario` es true). |
| `categoria` | String | No | Ej: Frenos, Suspensión, Afinación, Carrocería. |
|`clave_sat` | String | No | Clave SAT para facturación (Solo para `PRODUCTO`). |
|`unidad_sat` | String | No | Unidad SAT para facturación (Solo para `PRODUCTO`). |

## 3. Reglas de Negocio y Lógica de Aplicación

### A. Lógica de Inventario
- **PRODUCTO:** Al crearse con `maneja_inventario: true`, el sistema debe permitir registrar un stock inicial. Cada vez que el administrador del taller haga entrega de un producto al mecánico que lo va a utilizar, el backend debe restar la cantidad utilizada.
- **SERVICIO:** Se crea con `maneja_inventario: false` y `stock: null`. El sistema ignorará cualquier intento de validación de existencias para estos registros.

### B. Restricciones (Constraints)
- **Unicidad:** El `codigo` debe ser único por cada `tenant_id`. Un taller A puede tener el código `ACEITE`, y el taller B también, sin colisionar.
- **Integridad:** No se puede eliminar un item que tenga historial en Órdenes de Servicio (se debe usar un campo `activo: false` para borrado lógico).

## 4. Endpoints del Microservicio (API Design)

| Método | Endpoint | Descripción |
| :--- | :--- | :--- |
| `GET` | `/api/v1/items` | Lista todos los items del tenant (soporta filtro por tipo). |
| `POST` | `/api/v1/items` | Registra una nueva refacción o servicio. |
| `GET` | `/api/v1/items/{id}` | Detalle de un item específico. |
| `PATCH` | `/api/v1/items/{id}/stock` | Ajuste manual de inventario (Entradas/Salidas). |

## 5. Consideraciones de Arquitectura

### Búsqueda Textual
Dado que el catálogo puede crecer mucho, se recomienda crear un Índice de Texto en MongoDB sobre los campos `nombre` y `codigo`.

```javascript
// Comando Mongo para optimizar búsquedas
db.items.createIndex({ nombre: "text", codigo: "text" })
```

### Caché Local
Como los servicios (mano de obra) no cambian de precio frecuentemente, se recomienda implementar un caché simple en el backend para no consultar a la DB cada vez que el usuario escribe en el buscador de la Orden de Servicio.

> [!TIP]
> El uso de un caché local mejora significativamente la experiencia de usuario en la creación de presupuestos rápidos.

Se debe manejar paginación en la consulta GET /api/v1/items. Debe devolver page, limit, total, y los items.