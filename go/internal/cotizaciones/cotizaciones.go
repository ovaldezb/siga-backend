// Package cotizaciones atiende el listado de cotizaciones (port de
// list_cotizaciones_handler en src/handlers/cotizaciones/cotizaciones_manager.py).
// El detalle vive en internal/detalle; alta, edición y conversión a OS siguen
// en Python.
package cotizaciones

import (
	"context"
	"strconv"
	"strings"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

const (
	tipoPlantilla    = "PLANTILLA"
	tipoCliente      = "CLIENTE"
	limitePorDefecto = 200
)

// List atiende GET /cotizaciones[?tipo=…&status=…&sucursalId=…&limit=…]. Las
// plantillas son de todo el taller; las cotizaciones a cliente respetan el
// scope de sucursal del usuario.
func List(ctx context.Context, req platform.Request) (platform.Response, error) {
	claims := platform.Claims(req)
	tenantID := platform.ClaimString(claims, "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No se encontró un tenantId asociado.", nil), nil
	}
	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}
	qp := req.QueryStringParameters

	scope, violacion, err := platform.SucursalScope(ctx, claims, db, qp["sucursalId"])
	if err != nil {
		return platform.Response{}, err
	}
	if violacion != "" {
		return platform.JSON(req, 403, violacion, nil), nil
	}

	filtro := bson.D{{Key: "tenant_id", Value: tenantID}}
	tipo := strings.ToUpper(qp["tipo"])
	if tipo != "" {
		if tipo != tipoPlantilla && tipo != tipoCliente {
			return platform.JSON(req, 400, "tipo inválido. Permitidos: ['CLIENTE', 'PLANTILLA']", nil), nil
		}
		filtro = append(filtro, bson.E{Key: "tipo", Value: tipo})
	}

	if tipo != tipoPlantilla && scope != nil {
		sucursal := platform.FiltroSucursal(scope)
		if tipo == "" {
			// Sin tipo: todas las plantillas más las de cliente dentro del scope.
			filtro = append(filtro, bson.E{Key: "$or", Value: bson.A{
				bson.D{{Key: "tipo", Value: tipoPlantilla}},
				bson.D{{Key: "tipo", Value: tipoCliente}, {Key: "sucursal_id", Value: sucursal}},
			}})
		} else {
			filtro = append(filtro, bson.E{Key: "sucursal_id", Value: sucursal})
		}
	}

	if status := qp["status"]; status != "" {
		filtro = append(filtro, bson.E{Key: "status", Value: strings.ToUpper(status)})
	}

	limite := int64(limitePorDefecto)
	if raw, ok := qp["limit"]; ok {
		// int() de Python acepta espacios alrededor; un texto no numérico daba 400.
		n, err := strconv.ParseInt(strings.TrimSpace(raw), 10, 64)
		if err != nil {
			return platform.Response{}, platform.BadRequest("limit inválido: %q", raw)
		}
		limite = n
	}

	cur, err := db.Collection("cotizaciones").Find(ctx, filtro,
		options.Find().SetSort(bson.D{{Key: "createdAt", Value: -1}}).SetLimit(limite))
	if err != nil {
		return platform.Response{}, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return platform.Response{}, err
	}
	out := make([]map[string]any, 0, len(docs))
	for _, d := range docs {
		out = append(out, platform.Doc(d))
	}
	return platform.JSON(req, 200, "Cotizaciones obtenidas", out), nil
}
