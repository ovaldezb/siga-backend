# Estrategia de Optimización de Base de Datos (MongoDB)

Este documento detalla los índices recomendados para asegurar el rendimiento y la escalabilidad del sistema SIGA, especialmente para las búsquedas dinámicas (Search-as-you-type).

## 1. Base de Datos Global (`_platform`)

### Colección: `vehiculos`
Utilizada para catálogos globales de marcas y modelos.
- **Índice de Catálogo**: `db.vehiculos.createIndex({ "marca": 1, "modelo": 1 })`
  - *Propósito*: Optimizar las consultas `.distinct()` y filtrados por marca/modelo.
- **Índice de Búsqueda General**: `db.vehiculos.createIndex({ "marca": "text", "modelo": "text" })`
  - *Propósito*: Permitir búsquedas globales de texto.

### Colección: `talleres`
- **Índice de Tenant**: `db.talleres.createIndex({ "tenantId": 1 }, { unique: true })`
  - *Propósito*: Búsqueda instantánea de configuración de taller y módulos durante el login.
- **Índice de Email**: `db.talleres.createIndex({ "adminEmail": 1 })`

## 2. Bases de Datos por Tenant (`tenant_{id}`)

### Colección: `clientes`
- **Índice Compuesto**: `db.clientes.createIndex({ "nombre": 1, "apellido_paterno": 1 })`
  - *Propósito*: Optimizar el listado ordenado y búsquedas por nombre.
- **Índice de Teléfono**: `db.clientes.createIndex({ "telefono": 1 })`
  - *Propósito*: Búsquedas rápidas por número telefónico (común en recepción).
- **Índice de Búsqueda Dinámica**: `db.clientes.createIndex({ "nombre": "text", "apellido_paterno": "text", "telefono": "text" })`
  - *Propósito*: Soporte para la búsqueda unificada en la Orden de Servicio.

### Colección: `ordenes_servicio`
- **Índice de Folio**: `db.ordenes_servicio.createIndex({ "folio": 1 }, { unique: true })`
- **Índice de Cliente**: `db.ordenes_servicio.createIndex({ "cliente_id": 1 })`
- **Índice de Estado**: `db.ordenes_servicio.createIndex({ "estado": 1, "createdAt": -1 })`
  - *Propósito*: Optimizar el tablero principal (Dashboard) filtrado por estado y ordenado por fecha.

---

## 3. Recomendaciones Avanzadas (MongoDB Atlas)

### Atlas Search (Lucene)
Para una experiencia de búsqueda superior, se recomienda configurar **Atlas Search Indexes** en lugar de expresiones regulares (`$regex`) para los campos de búsqueda en el frontend.
- **Beneficios**:
  - Soporte para **Fuzzy Search** (errores de dedo).
  - Autocompletado nativo.
  - Mayor eficiencia de CPU en comparación con regex.

### TTL Indexes
- Utilizar en colecciones de logs o sesiones temporales si se implementan en el futuro.
  `db.logs.createIndex({ "createdAt": 1 }, { expireAfterSeconds: 7776000 })` (90 días).
