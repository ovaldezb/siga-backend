'use strict';

/**
 * stacks-map.js — Migración custom para serverless-plugin-split-stacks
 *
 * Mueve recursos de API Gateway compartidos (usados por >1 función) a un
 * nested stack dedicado. El plugin perFunction ya maneja los recursos
 * únicos por función.
 *
 * SIN force: los recursos que ya existen en el root stack desplegado se
 * OMITEN (el plugin los salta). Solo los recursos NUEVOS (nunca deployados,
 * como cotizaciones, contabilidad, compras, caja) se migran aquí.
 * Esto evita el conflicto de CloudFormation de crear un path de API Gateway
 * mientras el mismo path ya existe en el root stack.
 *
 * allowSuffix: si el nested stack llega a 500 recursos, el plugin crea
 * automáticamente ApiGateway2NestedStack, etc.
 *
 * Formato: (resource, logicalId) => { destination, allowSuffix? } | null
 */
module.exports = (resource, logicalId) => {
  const type = resource.Type;

  // Todos los tipos de API Gateway al mismo nested stack (evita dependencias
  // circulares: Deployment depende implícitamente de todos los Methods).
  const apiGatewayTypes = [
    'AWS::ApiGateway::Resource',
    'AWS::ApiGateway::Method',
    'AWS::ApiGateway::Deployment',
    'AWS::ApiGateway::Stage',
    'AWS::ApiGateway::Authorizer',
    'AWS::ApiGateway::GatewayResponse',
    'AWS::ApiGateway::BasePathMapping',
    'AWS::ApiGateway::UsagePlan',
    'AWS::ApiGateway::ApiKey',
  ];

  if (apiGatewayTypes.includes(type)) {
    return { destination: 'ApiGateway', allowSuffix: true };
  }

  // Lambda Permissions también van a un nested stack (son muchas y saturan root)
  if (type === 'AWS::Lambda::Permission') {
    return { destination: 'LambdaPermissions', allowSuffix: true };
  }

  // Dejar que perFunction maneje Lambdas, LogGroups, etc.
  return null;
};
