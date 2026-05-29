'use strict';

/**
 * stacks-map.js — Migración custom para serverless-plugin-split-stacks
 *
 * Problema: La configuración previa (perGroupFunction) dejó recursos en nested
 * stacks con nombres diferentes. Al cambiar estrategia, migrate-existing-resources
 * re-fija esos recursos a sus stacks viejos, saturando unos y dejando demasiados
 * en root.
 *
 * Solución: force: true obliga al plugin a mover TODOS los recursos de API Gateway
 * a los nuevos nested stacks dedicados, ignorando su ubicación previa.
 *
 * NOTA: force puede causar delete+recreate de recursos. Para API Gateway esto
 * es seguro (no hay pérdida de datos). El endpoint URL no cambia.
 *
 * Formato: (resource, logicalId) => { destination, force? } | null
 */
module.exports = (resource, logicalId) => {
  const type = resource.Type;

  // AWS::ApiGateway::Resource — paths compartidos entre múltiples funciones.
  // perFunction los deja en root; los forzamos a un nested stack dedicado.
  if (type === 'AWS::ApiGateway::Resource') {
    return { destination: 'ApiGatewayResources', force: true, allowSuffix: true };
  }

  // AWS::ApiGateway::Method — incluye OPTIONS (CORS) compartidos y los que
  // perFunction no pudo asignar a una sola función.
  if (type === 'AWS::ApiGateway::Method') {
    return { destination: 'ApiGatewayMethods', force: true, allowSuffix: true };
  }

  // AWS::ApiGateway::Deployment y Stage — siempre generados en root.
  if (type === 'AWS::ApiGateway::Deployment') {
    return { destination: 'ApiGatewayDeployment', force: true };
  }

  if (type === 'AWS::ApiGateway::Stage') {
    return { destination: 'ApiGatewayDeployment', force: true };
  }

  // Authorizer de Cognito en API Gateway.
  if (type === 'AWS::ApiGateway::Authorizer') {
    return { destination: 'ApiGatewayDeployment', force: true };
  }

  // AWS::ApiGateway::GatewayResponse — respuestas de error globales (4xx/5xx CORS).
  if (type === 'AWS::ApiGateway::GatewayResponse') {
    return { destination: 'ApiGatewayDeployment', force: true };
  }

  // Dejar que perFunction maneje el resto (Lambdas, LogGroups, Permissions, etc.)
  return null;
};
