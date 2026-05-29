'use strict';

/**
 * stacks-map.js — Migración custom para serverless-plugin-split-stacks
 *
 * IMPORTANTE: Todos los recursos de API Gateway deben ir al MISMO nested stack.
 * AWS::ApiGateway::Deployment tiene una dependencia implícita de CloudFormation
 * con todos los AWS::ApiGateway::Method. Si están en stacks distintos se genera
 * una dependencia circular que CloudFormation rechaza.
 *
 * force: true es necesario para mover recursos que estaban en nested stacks
 * anteriores (de la era perGroupFunction), ignorando su ubicación previa.
 * Para API Gateway es seguro — el endpoint URL no cambia.
 *
 * Formato: (resource, logicalId) => { destination, force?, allowSuffix? } | null
 */
module.exports = (resource, logicalId) => {
  const type = resource.Type;

  // Todos los tipos de API Gateway van al MISMO stack para evitar dependencias
  // circulares. El Deployment depende implícitamente de los Methods/Resources.
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
    return { destination: 'ApiGateway', force: true, allowSuffix: true };
  }

  // Dejar que perFunction maneje el resto (Lambdas, LogGroups, Permissions, etc.)
  return null;
};
