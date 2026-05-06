# Especificación de CI/CD - Backend SIGA

Esta especificación detalla el proceso de Integración y Despliegue Continuo (CI/CD) para el backend del Sistema Integral de Gestión Automotriz (SIGA) utilizando GitHub Actions y Serverless Framework.

## Arquitectura de Ramas y Entornos

El flujo de trabajo se basa en dos ramas principales que corresponden a los entornos de ejecución:

| Rama | Entorno | Activador (Trigger) | Stage de Serverless |
| :--- | :--- | :--- | :--- |
| `develop` | Desarrollo | `push` a la rama | `dev` |
| `main` | Producción | `push` (vía merge) a la rama | `prod` |

---

## Flujos de Trabajo (Workflows)

### 1. Despliegue a Desarrollo
Se dispara automáticamente cada vez que hay un `push` a la rama `develop`.

**Pasos:**
1. **Checkout**: Clonar el repositorio.
2. **Setup Python**: Configurar la versión de Python 3.11.
3. **Instalar Dependencias**: 
   - `pip install -r requirements.txt`
   - `npm install -g serverless`
   - `npm install` (para plugins de serverless como `serverless-python-requirements`).
4. **Deploy**: Ejecutar `sls deploy --stage dev`.

### 2. Despliegue a Producción
Se dispara automáticamente cada vez que se realiza un merge a la rama `main`.

**Pasos:**
1. **Checkout**: Clonar el repositorio.
2. **Setup Python**: Configurar la versión de Python 3.11.
3. **Instalar Dependencias**: Igual que en desarrollo.
4. **Deploy**: Ejecutar `sls deploy --stage prod`.

---

## Credenciales y Secretos de GitHub

Para que GitHub Actions pueda interactuar con AWS y configurar la aplicación, se deben agregar los siguientes **Secrets** en el repositorio (`Settings > Secrets and variables > Actions > Secrets`):

### Credenciales de AWS
- `AWS_ACCESS_KEY_ID`: ID de la llave de acceso del usuario IAM con permisos para desplegar.
- `AWS_SECRET_ACCESS_KEY`: Llave de acceso secreta del usuario IAM.

### Variables de Aplicación (Base de Datos)
Estas variables se pasan al entorno de ejecución de Lambda a través de Serverless:
- `MONGO_USER`: Usuario de la base de datos.
- `MONGO_PASSWORD`: Contraseña de la base de datos.
- `MONGO_HOST`: Host de la base de datos.
- `MONGO_DB`: Nombre de la base de datos (por defecto `siga`).

---

## Variables de Entorno en la Acción

Para manejar las variables de ambiente en GitHub Actions, se recomienda usar **Environments** de GitHub para separar `dev` de `prod`.

1. Crea dos entornos en GitHub: `Development` y `Production`.
2. Agrega los secretos específicos para cada entorno (ej. una `MONGO_URI` distinta para desarrollo y producción).
3. En el archivo `.yml` de la acción, referencia el entorno correspondiente:

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: Development # O Production según la rama
    steps:
      - name: Deploy
        run: sls deploy --stage dev
        env:
          MONGO_URI: ${{ secrets.MONGO_URI }}
          # ... demás variables
```

Esto permite que la misma acción use diferentes valores dependiendo de la rama a la que se está desplegando.
