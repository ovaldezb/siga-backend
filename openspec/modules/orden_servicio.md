# 🛠️ Módulo: Orden de Servicio (OS)

## 📌 Objetivo
Gestionar el ciclo de vida completo de la reparación de un vehículo, desde la recepción y diagnóstico hasta la entrega final, garantizando el control de inventario en tiempo real y la transparencia en la aprobación de costos por parte del cliente.

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
| `vehiculo_snapshot`| Object | Datos técnicos del vehículo recibido. |
| `items` | Array<Item> | Listado polimórfico de refacciones y servicios. |
| `falla_reportada` | String | Descripción inicial del cliente. |
| `diagnostico` | String | Notas técnicas del mecánico. |
| `mecanico_id` | UUID | ID del empleado responsable. |
| `danos_previos` | Array<String> | Registro visual/textual de daños estéticos. |
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

### 🧩 Detalle de Partidas (`items`)
```json
{
    "id": "UUID",
    "tenantId": "UUID",
    "tipo": "PRODUCTO",
    "codigo": "CODE123",
    "nombre": "Pastillas de freno cerámicas",
    "precioVenta": 850.00,
    "costo": 500.00,
    "manejaInventario": true,
    "stock": 10,
    "categoria": "Frenos",
    "claveSat": "123456",
    "unidadSat": "PZA",
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

1. **Apertura (`BORRADOR`):** Registro inicial de datos, cliente, vehículo y falla reportada.
2. **Diagnóstico y Cotización (`COTIZADO`):** El mecánico añade los ítems necesarios y el presupuesto.
3. **Autorización (`APROBADO`):** El cliente valida y aprueba los costos (parcial o totalmente).
4. **Ejecución (`EN_PROCESO` / `FINALIZADO`):** Se realizan los trabajos y se descuenta el inventario.
5. **Cierre y Entrega (`ENTREGADO`):** Liquidación del saldo y entrega física de la unidad.

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