# SIGA: System Architecture

## 1. Visión General
El Sistema Integral de Gestión Automotriz (SIGA) es una plataforma modular y escalable diseñada para la administración eficiente de talleres mecánicos y refaccionarias. Opera bajo un modelo SaaS (Software as a Service) multi-tenant, garantizando aislamiento de datos y configuraciones por cada taller.
El sistema debe contar con IaaS con el framework Serverless, debe generar una plantilla de infraestructura como código, que contenga la creación de las lambdas, la creación de los endpoints de api gateway y la configuración de S3 para almacenamiento de imagenes, no se debe usar s3 para nada mas, no se debe usar s3 para almacenar datos, solo imagenes y videos.

## 2. Metodologías
- **Spec-Driven Development (SDD):** Las especificaciones de OpenSpec dirigen el desarrollo.
- **Behavior-Driven Development (BDD):** Definición de funcionalidades en base al comportamiento esperado del usuario final.
- **Test-Driven Development (TDD):** Implementación de pruebas automatizadas antes de escribir código de negocio.

## 3. Stack Tecnológico
| Capa             | Tecnología                  | Propósito                                                |
| ---------------- | --------------------------- | -------------------------------------------------------- |
| **Frontend**     | Angular                     | Interfaz de usuario dinámica y responsiva (SPA).         |
| **Backend**      | AWS Serverless Python / AWS API Gateway / AWS Lambda / MongoDB         | Serverless Framework |  Cognito | Lambda | IaC    
| **Ecosistema**   | AWS      Correo(SES)   Mensajes(SNS)    S3    | Code as Infrastructure | Github Actions| Python Lambdas Layer  | Github  
| **Persistencia** | MongoDB                     | Base de datos NoSQL flexible, ideal para catálogos.      |
| **Seguridad**    | Cognito | Cifrar datos sensibles en MongoDB
| **Infraestructura**| AWS Cloud


## 4. Requerimientos No Funcionales
- **Multi-tenancy:** Aislamiento estricto de la base de datos o colecciones por taller/empresa.
- **Escalabilidad:** Arquitectura de servicios serverless
- **Baja Latencia:** APIs optimizadas para tiempos de respuesta rápidos.
- **Procesamiento Asíncrono:** Uso de colas (AWS SQS) para reportes pesados, envío de emails y mensajes de WhatsApp.
- **Seguridad:** Encriptación de datos sensibles en reposo y en tránsito, con auditoría de eventos.
- **Logging:** Centralización de logs para facilitar el monitoreo y debugging.

## 5. Python Serverless Architecture
Framework: Serverless Framework
Se debe seguir la arquitectura limpia.
captura las excepciones con un handlerAdvice y devuelve un objeto estandarizado.
En caso de error en la aplicacion se debe loggear el error en un archivo y mostrar un mensaje generico al usuario.
mandar mensajes al log para saber que la lambda fue ejecutada y con que parametros.

## 6. Logout automatico
Después de 15 minutos de inactividad el sistema los debe desloguear
El token de seguridad debe durar 15 minutos
