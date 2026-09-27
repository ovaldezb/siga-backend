package catalogos

import (
	"context"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

// ListModulos atiende GET /modulos: el catálogo global de módulos contratables
// (`_platform.modulos`) sin su _id, como list_modulos_handler de Python.
func ListModulos(ctx context.Context, req platform.Request) (platform.Response, error) {
	db, err := platform.PlatformDB()
	if err != nil {
		return platform.Response{}, err
	}
	cur, err := db.Collection("modulos").Find(ctx, bson.D{},
		options.Find().SetProjection(bson.D{{Key: "_id", Value: 0}}))
	if err != nil {
		return platform.Response{}, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return platform.Response{}, err
	}
	modulos := make([]map[string]any, 0, len(docs))
	for _, d := range docs {
		modulos = append(modulos, platform.Doc(d))
	}
	return platform.JSON(req, 200, "Módulos recuperados", modulos), nil
}
