'use strict';

/**
 * stacks-map.js — Migración custom para serverless-plugin-split-stacks
 *
 * PROBLEMA QUE RESUELVE
 * ---------------------
 * CloudFormation topa en 500 recursos por stack. El root stack de siga-backend-dev
 * llegó a 509 y el deploy empezó a fallar con:
 *   "Number of resources, 509, is greater than maximum allowed, 500"
 *
 * Este servicio usa `httpApi` (API Gateway **v2**), no `http` (REST v1). La versión
 * anterior de este archivo mapeaba tipos `AWS::ApiGateway::*` (v1) que este stack
 * NUNCA genera, así que esa regla era letra muerta: las 133 Integrations y las 133
 * Routes de HTTP API se quedaban TODAS en el root stack. Sumadas a los 133 nested
 * stacks que crea `perFunction` (uno por lambda), el root se saturó.
 *
 * ESTRATEGIA
 * ----------
 * 1. `AWS::ApiGatewayV2::Integration` → nested stacks, CON `force: true`.
 *    Es la palanca grande: saca ~133 recursos del root de un golpe.
 *
 *    ¿Por qué es seguro forzar aquí? Una Integration no tiene nombre físico único:
 *    CloudFormation puede crear la copia nueva en el nested stack, re-apuntar la
 *    Route (que sigue en root) al nuevo IntegrationId vía Output/Parameter, y recién
 *    entonces borrar la vieja en la fase de cleanup. No hay colisión posible.
 *
 * 2. `AWS::ApiGatewayV2::Route` → nested stacks, SIN force.
 *    Aquí forzar SÍ sería destructivo: una Route tiene identidad única (RouteKey
 *    "GET /usuarios" dentro del mismo Api). Al migrar con force, CloudFormation
 *    intentaría crear la Route nueva ANTES de borrar la vieja → "Route with key ...
 *    already exists" y rollback. Sin force, el plugin salta las rutas ya desplegadas
 *    en root y solo manda las NUEVAS al nested stack: no baja el conteo hoy, pero
 *    frena el crecimiento (cada endpoint nuevo deja de sumar al root).
 *
 * 3. `AWS::Lambda::Permission` → nested stack, CON `force: true`.
 *    Mismo razonamiento que Integration: no tiene nombre físico único (el statement
 *    id lo genera CFN), así que duplicar temporalmente no colisiona. Esto recupera
 *    además los permisos "congelados" en root de antes de que existiera el split.
 *
 * 4. `AWS::Lambda::Function` y `AWS::Logs::LogGroup` NO se tocan aquí: los maneja
 *    la estrategia `perFunction` del plugin. Y JAMÁS deben forzarse — FunctionName
 *    y LogGroupName sí son únicos, forzar los haría chocar en el update.
 *
 * NOTA SOBRE `force` Y RECURSOS YA DESPLEGADOS
 * -------------------------------------------
 * El plugin ignora cualquier recurso que ya exista en el stack desplegado
 * (`lib/migrate-new-resources.js`: `if (logicalId in this.existingResources &&
 * !migration.force) return;`). Por eso el archivo anterior solo afectaba a recursos
 * nuevos y el root nunca adelgazaba. `force: true` es lo que permite rescatar lo
 * que ya está en root — y por eso solo se usa en los tipos donde es demostrablemente
 * seguro (puntos 1 y 3).
 *
 * Formato: (resource, logicalId) => { destination, allowSuffix?, force? } | null
 */

// Integrations y Routes se reparten en varios nested stacks. Motivo: un nested
// stack admite 500 recursos pero solo 200 Parameters y 200 Outputs, y el plugin
// `stackHasRoom` solo vigila recursos y outputs — nunca parámetros. Cada Integration
// migrada consume 1 Parameter (el ARN de su lambda) y 1 Output (su IntegrationId,
// que la Route lee); cada Route consume 1 Parameter (ese IntegrationId). Con 133
// endpoints, un stack único quedaría a ~135/200 parámetros: pasa hoy y revienta
// con ~65 endpoints más, sin que `allowSuffix` lo detecte. Repartir evita ese muro.
//
// Cambiar este número es seguro (reasignar una Integration solo la recrea, y las
// Routes ya desplegadas quedan fijadas a su stack por migrate-existing-resources),
// pero genera churn en el deploy siguiente. No tocarlo sin motivo.
const INTEGRATION_BUCKETS = 4;

// Hash determinista (FNV-ish sobre el logical id). Estable entre deploys: la misma
// función cae siempre en el mismo bucket, sin importar cuántos endpoints se agreguen.
function bucketOf(logicalId, buckets) {
  let hash = 0;
  for (let i = 0; i < logicalId.length; i++) {
    hash = (hash * 31 + logicalId.charCodeAt(i)) >>> 0;
  }
  return hash % buckets;
}

const BUCKET_LABELS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

module.exports = (resource, logicalId) => {
  const type = resource.Type;

  // 1. Integraciones HTTP API → fuera del root, incluidas las ya desplegadas.
  if (type === 'AWS::ApiGatewayV2::Integration') {
    const label = BUCKET_LABELS[bucketOf(logicalId, INTEGRATION_BUCKETS)];
    return { destination: `HttpApiIntegrations${label}`, force: true };
  }

  // 2. Rutas HTTP API → solo las nuevas (forzar rompería por RouteKey duplicada).
  if (type === 'AWS::ApiGatewayV2::Route') {
    const label = BUCKET_LABELS[bucketOf(logicalId, INTEGRATION_BUCKETS)];
    return { destination: `HttpApiRoutes${label}`, allowSuffix: true };
  }

  // 3. Permisos de invocación → fuera del root, incluidos los ya desplegados.
  //    Sin buckets a propósito: los permisos ya viven en este nested stack y
  //    repartirlos ahora recrearía 133 recursos en el mismo deploy que buscamos
  //    desatascar. Va a ~135/200 parámetros; al acercarse a ~190 endpoints, aplicar
  //    aquí el mismo bucketOf() que arriba (es seguro, solo genera churn).
  if (type === 'AWS::Lambda::Permission') {
    return { destination: 'LambdaPermissions', allowSuffix: true, force: true };
  }

  // 4. Lambdas, LogGroups, etc.: los reparte `perFunction`.
  return null;
};
