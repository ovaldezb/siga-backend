# 🛠️ Módulo: Orden de Servicio (OS)

## 📌 Objetivo
Gestionar el ciclo de vida completo de la reparación de un vehículo, desde la recepción y diagnóstico hasta la entrega final, garantizando el control de inventario en tiempo real y la transparencia en la aprobación de costos por parte del cliente.
La orden de servicio se compone de 3 secciones principales: 
- Cliente
- Vehiculo
- Servicios y refacciones

como parte final se agrega una garantia y una fecha estimada de entrega y el costo total de la orden. de igual manera se puede agregar notas y observaciones de cada sección.

La Orden de Servicio debe ser creada de forma atomica, es decir, que si falla la creacion de un elemento, se debe revertir toda la operacion, antes de crear una orden de servicio, se debe verificar si el cliente y el vehiculo ya existen en la base de datos, si no existen, se deben crear, para que la orden de servicio contenga toda la informacion necesaria, adicionalmente el producto y/o servicio se debe poder agregar desde la orden de servicio, si no existe en la base de datos, se debe crear, si es un producto se debe agregar con un stock de 1 por default

---

## 🏗️ Modelo de Datos (MongoDB)

La colección `ordenes_servicio` utiliza un modelo de **Snapshot** para el Cliente y Vehículo, asegurando que la información histórica no cambie si el perfil original es editado posteriormente.

### 📄 Estructura del Documento
| Campo | Tipo | Descripción |
| :--- | :--- | :--- |
| `os_id` | UUID | Identificador único de la orden. |
| `folio` | String | Identificador legible (ej: OS-2026-001). |
| `tenant_id` | UUID | Discriminador de taller (Multi-tenancy). |
| `estado` | Enum | `BORRADOR`, `COTIZADO`, `APROBADO`, `EN_PROCESO`, `FINALIZADO`, `ENTREGADO`. |
| `cliente_snapshot` | Object | Datos de contacto al momento de la apertura. |
|`puntosArreglar`|Array<Object>|Array de puntos a reparar, donde cada punto tiene un nombre y una lista de items |
|`vehiculo_id`|UUID|ID del vehiculo asociado a la orden de servicio|
| `falla_reportada` | String | Descripción inicial del cliente. |
| `diagnostico` | String | Notas técnicas del mecánico. |
| `mecanico_id` | UUID | ID del empleado responsable. |
| `danos_previos` | Array<String> | Registro visual/textual de daños estéticos. |
danos previos para cada lado del vehiculo, derecho, izquierdo, frontal y trasero, 
| `anticipo` | Decimal | Pago inicial registrado. |
| `fecha_entrega` | DateTime | Fecha compromiso con el cliente. |
| `garantia` | Object | Vigencia y condiciones de la reparación. |

---

## 🚗 Vehículo del Cliente (Unidad Física)

A diferencia del catálogo global de modelos, el **Vehículo del Cliente** representa la unidad física que entra al taller.

| Campo | Tipo | Descripción |
| :--- | :--- | :--- |
| `vehiculo_id` | UUID | Identificador único de la unidad. |
| `cliente_id` | UUID | Propietario de la unidad. |
| `tenant_id` | UUID | Taller donde se registró. |
| `placas` | String | Matrícula del vehículo. |
| `vin` | String | Número de serie (Vehicle Identification Number). |
| `marca` | String | Marca (ej: Nissan). |
| `modelo` | String | Modelo (ej: Sentra). |
| `año` | Integer | Año de fabricación. |
| `color` | String | Color actual de la unidad. |
| `kilometraje` | Integer | Lectura del odómetro al momento del ingreso. |

> [!NOTE]
> Al crear una OS, si el `vin` o las `placas` no existen en la base de datos del taller, el sistema debe registrar automáticamente este vehículo y asociarlo al cliente indicado.

### Puntos a Arreglar (`puntosArreglar`)
una order de servicio debe contener una lista de Puntos a Arreglar y cada punto contendrá uno o más items, que pueden ser refacciones (Productos) o mano de obra (Servicios)
```json
{
    "puntosArreglar":[
        "punto":{"nombre":"afinacion","items":[item1, item2]},
        "punto":{"nombre":"fallas","items":[item1, item2]},
        "punto":{"nombre":"frenos delanteros","items":[item1, item2]},
    ]
}```
---
### 🧩 Detalle de Partidas (`items`)
```json
{
    "id": "UUID",
    "tenantId": "UUID",
    "tipo": "PRODUCTO",
    "codigo": "CODE123",
    "nombre": "Pastillas de freno cerámicas",
    "precioVenta": 850.00,
    "precioCompra": 500.00,
    "manejaInventario": true,
    "stock": 10,
    "noParte": "123456789",
    "marca": "Brembo",
    "piezas":4,
    "categoria": "Frenos",
    "claveSat": "123456",
    "unidadSat": "PZA",
    "proveedor":"Stock Lic",
    "aprobado":true,
    "activo": true
}
```

---

## ⚙️ Lógica Transaccional

> [!IMPORTANT]
> La creación de la Orden de Servicio debe ser **Atómica**. Si el cliente, el vehículo o algún ítem no existen previamente, el sistema debe permitir su creación "al vuelo" dentro de la misma transacción. Si falla la creación de cualquier componente, se debe revertir toda la operación (Rollback).

### 🔄 Ejemplo de Flujo Atómico:
1. **Validación/Creación de Cliente:** Se verifica existencia; si no existe, se crea.
2. **Validación/Creación de Vehículo:** Se verifica existencia; si no existe, se crea.
3. **Validación/Creación de Ítems:** Se verifican códigos; si no existen, se agregan al catálogo.
4. **Persistencia de la OS:** Se genera el documento con los snapshots correspondientes.

---

## 🛣️ Flujo de Trabajo (Workflow)

El ciclo de vida estándar de una orden sigue estos pasos:

1. **Apertura (`RECEPCION`):** Registro inicial de datos, cliente, vehículo y falla reportada.
2. **Diagnóstico y Cotización (`COTIZADO`):** El mecánico o el vendedor añade los ítems necesarios y el presupuesto.
3. **Autorización (`APROBADO`):** El cliente valida y aprueba los costos (parcial o totalmente).
4. **Ejecución (`EN_PROCESO` / `FINALIZADO`):** Se realizan los trabajos (los items se descuentan del inventario al momento de ser entregados al Mecanico, de esta forma se tiene el stock actualizado, hay trabajos que pueden tardar 2 semanas, por lo cua pareceria que tengo piezas en stock, cuando ya fueron usadas en ordenes que estan en progreso).
5. **Cierre y Entrega (`ENTREGADO`):** Liquidación del saldo y entrega física de la unidad.

6. **Cancelar Orden de Servicio (`CANCELADO`):** Se cancela la orden de servicio y se devuelve el anticipo al cliente.

> [!NOTE]
> En caso de error en cualquier paso del flujo de creación inicial, el sistema debe mostrar un mensaje claro al usuario indicando qué elemento falló.

---

## 🧪 Estrategia de Pruebas (Casos de Uso Transaccionales)

Se deben incluir pruebas de integración para validar que la lógica transaccional funcione correctamente al crear la orden de servicio en los siguientes escenarios de existencia previa:

| # | Escenario de Datos | Comportamiento Esperado |
| :--- | :--- | :--- |
| 1 | **Solo existen Cliente y Vehículo** | Se crean los Ítems faltantes y luego la OS. |
| 2 | **Solo existen Cliente e Ítems** | Se crea el Vehículo faltante y luego la OS. |
| 3 | **Solo existen Vehículo e Ítems** | Se crea el Cliente faltante y luego la OS. |
| 4 | **No existe ningún elemento** | Se crea Cliente, Vehículo e Ítems en una sola transacción antes de la OS. |
| 5 | **Existen todos los elementos** | Se procede directamente a la creación de la OS usando los datos existentes. |

> [!CAUTION]
> En cualquier escenario, si falla la creación de un elemento dependiente (ej. falla guardado de cliente), la transacción **no debe persistir** el vehículo ni los ítems ni la OS.