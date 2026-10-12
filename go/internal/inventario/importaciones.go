package inventario

import (
	"context"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

const limiteImportaciones = 20

// rolesImportacion replica ROLES_IMPORTACION de importacion_manager.py.
var rolesImportacion = map[string]bool{"ADMIN": true, "SUPER_ADMIN": true, "GERENTE": true}

func puedeImportar(claims map[string]any) bool {
	for _, g := range platform.Groups(claims) {
		if rolesImportacion[g] {
			return true
		}
	}
	return false
}

// Importaciones atiende GET /items/importaciones?sucursal_id=…: las 20 importaciones
// de inventario más recientes de la sucursal (port de list_importaciones_handler).
// Preview, aplicar y deshacer siguen en Python.
func Importaciones(ctx context.Context, req platform.Request) (platform.Response, error) {
	claims := platform.Claims(req)
	tenantID := platform.ClaimString(claims, "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No se encontró un tenantId asociado.", nil), nil
	}
	if !puedeImportar(claims) {
		return platform.JSON(req, 403, "Solo un administrador o el gerente de la sucursal puede importar inventario.", nil), nil
	}
	qp := req.QueryStringParameters
	sucursalID := qp["sucursal_id"]
	if sucursalID == "" {
		sucursalID = qp["sucursalId"]
	}
	if sucursalID == "" {
		return platform.JSON(req, 400, "Indique la sucursal a la que se importa.", nil), nil
	}

	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}
	if _, violacion, err := platform.SucursalScope(ctx, claims, db, sucursalID); err != nil {
		return platform.Response{}, err
	} else if violacion != "" {
		return platform.JSON(req, 403, violacion, nil), nil
	}

	cur, err := db.Collection("importaciones_inventario").Find(ctx,
		bson.D{{Key: "sucursal_id", Value: sucursalID}},
		options.Find().SetSort(bson.D{{Key: "createdAt", Value: -1}}).SetLimit(limiteImportaciones))
	if err != nil {
		return platform.Response{}, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return platform.Response{}, err
	}
	lotes := make([]map[string]any, 0, len(docs))
	for _, d := range docs {
		lote := platform.Doc(d)
		estado, _ := lote["estado"].(string)
		lote["puede_deshacer"] = estado == "APLICADA" || estado == "INCOMPLETA"
		lotes = append(lotes, lote)
	}
	return platform.JSON(req, 200, "Importaciones", lotes), nil
}
