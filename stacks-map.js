'use strict';

/**
 * stacks-map.js — Migración custom para serverless-plugin-split-stacks
 *
 * Contexto: perFunction:true mueve Lambdas + sus Methods/Resources exclusivos.
 * Sin embargo, los AWS::ApiGateway::Resource compartidos entre >1 función
 * (p.ej. el segmento raíz de /clientes, /vehiculos, /ordenes, etc.) se quedan
 * en el root stack, lo que hace que superemos el límite de 500 recursos.
 *
 * Este archivo fuerza esos recursos compartidos a nested stacks dedicados,
 * reduciendo el root stack a < 100 recursos.
 *
 * Formato de función: (resource, logicalId) => { destination: 'StackName' } | null
 */
module.exports = (resource, logicalId) => {
  const type = resource.Type;

  // API Gateway Resources compartidos (paths como /clientes, /vehiculos, etc.)
  // que perFunction deja en root por ser compartidos entre múltiples funciones.
  if (type === 'AWS::ApiGateway::Resource') {
    return { destination: 'ApiGatewayResources' };
  }

  // Deployment y Stage de API Gateway — siempre en root por defecto, los movemos.
  if (type === 'AWS::ApiGateway::Deployment') {
    return { destination: 'ApiGatewayDeployment' };
  }

  if (type === 'AWS::ApiGateway::Stage') {
    return { destination: 'ApiGatewayDeployment' };
  }

  // Authorizers de API Gateway (Cognito authorizer)
  if (type === 'AWS::ApiGateway::Authorizer') {
    return { destination: 'ApiGatewayDeployment' };
  }

  // Methods OPTIONS compartidos que perFunction deja atrás
  if (type === 'AWS::ApiGateway::Method') {
    return { destination: 'ApiGatewayMethods' };
  }

  // Dejar que perFunction y el resto de estrategias manejen lo demás
  return null;
};
