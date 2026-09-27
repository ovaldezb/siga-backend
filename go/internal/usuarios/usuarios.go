// Package usuarios atiende el perfil del usuario logueado (port de
// get_me_handler en src/handlers/users/user_manager.py). El resto del CRUD de
// usuarios sigue en Python porque administra Cognito.
package usuarios

import (
	"context"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/sucursales"
)

// poblar sustituye las referencias por la sucursal completa y descarta las que
// apuntan a sucursales que ya no existen, como populate_user_sucursales.
func poblar(user map[string]any, porID map[string]map[string]any) {
	crudas, _ := user["sucursales"].([]any)
	pobladas := make([]map[string]any, 0, len(crudas))
	for _, item := range crudas {
		if s, ok := porID[platform.SucursalRef(item)]; ok {
			pobladas = append(pobladas, s)
		}
	}
	user["sucursales"] = pobladas
}

// Me atiende GET /usuarios/me: perfil del usuario con sus sucursales asignadas.
// Lo llama el login y cada recarga de la app.
func Me(ctx context.Context, req platform.Request) (platform.Response, error) {
	claims := platform.Claims(req)
	tenantID := platform.ClaimString(claims, "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No se encontró un tenantId asociado.", nil), nil
	}
	// Sin email, Python buscaba {"email": None} y podía devolver a otro usuario sin email.
	email := platform.ClaimString(claims, "email")
	if email == "" {
		return platform.JSON(req, 401, "Token sin email de usuario.", nil), nil
	}

	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}

	var doc bson.M
	err = db.Collection("usuarios").FindOne(ctx, bson.D{{Key: "email", Value: email}}).Decode(&doc)
	if err == mongo.ErrNoDocuments {
		return platform.JSON(req, 404, "Usuario no encontrado", nil), nil
	}
	if err != nil {
		return platform.Response{}, err
	}

	todas, err := sucursales.Todas(ctx, db)
	if err != nil {
		return platform.Response{}, err
	}
	porID := make(map[string]map[string]any, len(todas))
	for _, s := range todas {
		if id, ok := s["id"].(string); ok {
			porID[id] = s
		}
	}

	user := platform.Doc(doc)
	poblar(user, porID)
	return platform.JSON(req, 200, "Perfil obtenido", user), nil
}
