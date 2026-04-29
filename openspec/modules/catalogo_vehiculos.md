# Módulo: Catálogo Global de Vehículos

Este módulo gestiona la base de datos maestra de vehículos (marcas, modelos, etc.) que es compartida por todos los talleres (tenants) del sistema.

## 1. Almacenamiento

A diferencia de los datos operativos de los talleres, el catálogo de vehículos se almacena en una base de datos global denominada **`siga_catalogs`**. 

- **Estrategia:** Se utiliza un `catalogMongoTemplate` específico que omite la lógica de ruteo por `tenant_id`.
- **Colección:** `vehiculos`

## 2. Estructura del Dato (Vehiculo)

| Campo | Tipo | Descripción |
| :--- | :--- | :--- |
| `marca` | String | Marca del vehículo (ej: Toyota) |
| `modelo` | String | Modelo del vehículo (ej: Corolla) |
| `tipo` | String | Tipo de carrocería (ej: Sedán, SUV) |
| `segmento` | String | Clasificación de mercado (ej: Segmento C) |
| `origen` | String | Región de origen (ej: Asia, Europa) |
| `paisOrigen` | String | País específico de fabricación |

## 3. Importación Masiva (CSV)

Se proporciona un endpoint para la carga inicial y actualizaciones masivas del catálogo.

- **Endpoint:** `POST /api/v1/vehiculos/import`
- **Formato:** Multipart File (CSV)
- **Codificación:** UTF-8
- **Estructura del CSV:**

```csv
Marca,Modelo,Tipo,Segmento,Origen,País origen
Toyota,Corolla,Sedan,C,Asia,Japón
Ford,F-150,Pickup,Full-size,Norteamérica,Estados Unidos
```

> [!NOTE]
> La primera línea del archivo (encabezados) es ignorada durante la importación.

## 4. Consulta (Paginada)

Cualquier usuario autenticado puede consultar el catálogo global con paginación:
- **Endpoint:** `GET /api/v1/vehiculos`
- **Parámetros Opcionales:**
    - `page`: Número de página (empieza en 0). Por defecto `0`.
    - `size`: Cantidad de registros por página. Por defecto `20`.
    - `sort`: Campo para ordenar (ej: `marca,asc`).

**Ejemplo de respuesta:**
El sistema devuelve un objeto `Page` de Spring que incluye la lista de vehículos y metadatos sobre la paginación (`totalElements`, `totalPages`, etc.).
