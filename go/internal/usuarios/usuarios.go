// Package usuarios atiende el perfil del usuario logueado y el listado (port de
// get_me_handler y list_users_handler en src/handlers/users/user_manager.py).
// El resto del CRUD de usuarios sigue en Python porque administra Cognito.
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

	porID, err := sucursalesPorID(ctx, db)
	if err != nil {
		return platform.Response{}, err
	}

	user := platform.Doc(doc)
	poblar(user, porID)
	return platform.JSON(req, 200, "Perfil obtenido", user), nil
}

// List atiende GET /usuarios[?grupo=…]: los usuarios del taller con sus
// sucursales pobladas, como list_users_handler.
func List(ctx context.Context, req platform.Request) (platform.Response, error) {
	tenantID := platform.ClaimString(platform.Claims(req), "custom:tenant_id")
	if tenantID == "" {
		// Python no lo validaba y get_tenant_db respondía 400.
		return platform.JSON(req, 403, "No se encontró un tenantId asociado.", nil), nil
	}
	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}
	porID, err := sucursalesPorID(ctx, db)
	if err != nil {
		return platform.Response{}, err
	}

	filtro := bson.D{}
	if grupo := req.QueryStringParameters["grupo"]; grupo != "" {
		filtro = bson.D{{Key: "grupo", Value: grupo}}
	}
	cur, err := db.Collection("usuarios").Find(ctx, filtro)
	if err != nil {
		return platform.Response{}, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return platform.Response{}, err
	}
	users := make([]map[string]any, 0, len(docs))
	for _, d := range docs {
		u := platform.Doc(d)
		poblar(u, porID)
		users = append(users, u)
	}
	return platform.JSON(req, 200, "Usuarios obtenidos", users), nil
}

func sucursalesPorID(ctx context.Context, db *mongo.Database) (map[string]map[string]any, error) {
	todas, err := sucursales.Todas(ctx, db)
	if err != nil {
		return nil, err
	}
	porID := make(map[string]map[string]any, len(todas))
	for _, s := range todas {
		if id, ok := s["id"].(string); ok {
			porID[id] = s
		}
	}
	return porID, nil
}
