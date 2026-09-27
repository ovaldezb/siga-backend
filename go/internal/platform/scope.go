package platform

import (
	"context"
	"slices"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"
)

// SucursalRef saca el id de un elemento de usuarios.sucursales. El front guarda
// {"sucursal": id}; se aceptan también las formas legacy que lee
// get_user_allowed_sucursales. En Python un id suelto daba 500 (str.get).
func SucursalRef(item any) string {
	switch x := item.(type) {
	case string:
		return x
	case map[string]any:
		for _, k := range []string{"sucursal", "id", "sucursal_id"} {
			if s, ok := x[k].(string); ok && s != "" {
				return s
			}
		}
	}
	return ""
}

const (
	MsgSinSucursales = "Usuario sin sucursales asignadas. Contacte al administrador."
	MsgSucursalAjena = "No tiene acceso a esta sucursal."
)

// SucursalesPermitidas replica get_user_allowed_sucursales: nil para ADMIN o
// SUPER_ADMIN (sin restricción); para los demás, las sucursales asignadas en
// `usuarios`, vacío si no tiene. Python las cacheaba por contenedor sin
// expirar; aquí se leen en cada request para que un cambio de asignación
// aplique de inmediato.
func SucursalesPermitidas(ctx context.Context, claims map[string]any, db *mongo.Database) ([]string, error) {
	if IsAdmin(claims) {
		return nil, nil
	}
	email := ClaimString(claims, "email")
	if email == "" || ClaimString(claims, "custom:tenant_id") == "" {
		return []string{}, nil
	}

	var user bson.M
	err := db.Collection("usuarios").FindOne(ctx, bson.D{{Key: "email", Value: email}},
		options.FindOne().SetProjection(bson.D{{Key: "sucursales", Value: 1}})).Decode(&user)
	if err == mongo.ErrNoDocuments {
		return []string{}, nil
	}
	if err != nil {
		return nil, err
	}

	crudas, _ := Doc(user)["sucursales"].([]any)
	out := make([]string, 0, len(crudas))
	vistas := make(map[string]bool, len(crudas))
	for _, item := range crudas {
		if sid := SucursalRef(item); sid != "" && !vistas[sid] {
			vistas[sid] = true
			out = append(out, sid)
		}
	}
	return out, nil
}

// SucursalScope replica resolve_sucursal_scope: qué sucursales filtrar en una
// consulta según lo pedido y lo permitido. Devuelve el filtro (nil = no
// filtrar), un mensaje de violación de scope para responder 403 (o "") y el
// error de base de datos.
func SucursalScope(ctx context.Context, claims map[string]any, db *mongo.Database, pedida string) ([]string, string, error) {
	permitidas, err := SucursalesPermitidas(ctx, claims, db)
	if err != nil {
		return nil, "", err
	}
	if permitidas == nil { // admin: lo que pida, sin restricción
		if pedida != "" {
			return []string{pedida}, "", nil
		}
		return nil, "", nil
	}
	if len(permitidas) == 0 {
		return nil, MsgSinSucursales, nil
	}
	if pedida != "" {
		if slices.Contains(permitidas, pedida) {
			return []string{pedida}, "", nil
		}
		return nil, MsgSucursalAjena, nil
	}
	return permitidas, "", nil
}

// FiltroSucursal traduce el scope al valor de `sucursal_id` en un filtro de
// Mongo: el id directo si es uno solo, {$in: [...]} si son varios.
func FiltroSucursal(scope []string) any {
	if len(scope) == 1 {
		return scope[0]
	}
	return bson.D{{Key: "$in", Value: scope}}
}
