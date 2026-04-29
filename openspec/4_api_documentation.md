# 4. Documentación de la API (Swagger/OpenAPI)

El proyecto utiliza **SpringDoc OpenAPI 3** para la generación automática de documentación interactiva. Esta herramienta permite a los desarrolladores de Frontend y otros colaboradores explorar y probar los endpoints sin necesidad de herramientas externas como Postman (aunque estas siguen siendo válidas).

## Acceso a la Documentación

Una vez que la aplicación está en ejecución, se puede acceder a la documentación en las siguientes rutas:

*   **Swagger UI (Interactiva):** [http://localhost:8081/swagger-ui/index.html](http://localhost:8081/swagger-ui/index.html)
*   **OpenAPI Spec (JSON):** [http://localhost:8081/v3/api-docs](http://localhost:8081/v3/api-docs)

## Configuración de Seguridad en Swagger

Dado que la mayoría de los endpoints requieren autenticación Bearer Token, se debe configurar el token en la interfaz de Swagger para poder realizar pruebas:

1.  Hacer clic en el botón **"Authorize"** en la parte superior derecha de la interfaz de Swagger UI.
2.  Ingresar un token JWT válido (obtenido de Keycloak).
3.  Hacer clic en **"Authorize"** y luego en **"Close"**.
4.  Ahora todas las peticiones realizadas desde la interfaz incluirán el encabezado `Authorization: Bearer <token>`.

## Estándares de Documentación

Para mantener la documentación clara, se siguen estas prácticas:

1.  **Etiquetas (Tags):** Cada controlador debe estar anotado con `@Tag(name = "...", description = "...")` para agrupar los endpoints por módulo funcional.
2.  **Operaciones:** Usar `@Operation` para describir brevemente qué hace cada endpoint si el nombre del método no es lo suficientemente descriptivo.
3.  **Respuestas:** Los DTOs de respuesta deben estar bien anotados con validaciones de Jakarta (que Swagger interpreta automáticamente para mostrar los esquemas).

## Rutas Públicas de Documentación

Para facilitar el desarrollo, las rutas de Swagger están excluidas de la seguridad de Spring Security en la clase `SecurityConfig`:

```java
.requestMatchers("/v3/api-docs/**", "/swagger-ui/**", "/swagger-ui.html").permitAll()
```
