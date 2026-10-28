// Package novedades sirve los avisos de la plataforma (port de
// list_novedades_handler). Un aviso sin `tenant_ids` es para todos los
// talleres; con `tenant_ids` sólo lo ven los usuarios de esos talleres.
package novedades

import (
	"context"
	"slices"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

const (
	// Lo que un usuario ve en su campana: lo publicado en este periodo.
	ventanaDias = 120
	limite      = 50
)

// List atiende GET /novedades: las vigentes para el taller del usuario, con su
// marca `leida`. SUPER_ADMIN con ?todas=1 recibe también las ocultas y las
// dirigidas a cualquier taller (para administrarlas).
func List(ctx context.Context, req platform.Request) (platform.Response, error) {
	claims := platform.Claims(req)
	sub := platform.ClaimString(claims, "sub")
	if sub == "" {
		return platform.JSON(req, 403, "No autorizado", nil), nil
	}
	db, err := platform.PlatformDB()
	if err != nil {
		return platform.Response{}, err
	}

	filtro := bson.D{}
	if !(req.QueryStringParameters["todas"] == "1" && slices.Contains(platform.Groups(claims), "SUPER_ADMIN")) {
		filtro = Filtro(platform.ClaimString(claims, "custom:tenant_id"), time.Now())
	}

	leidas, err := leidasDe(ctx, db, sub)
	if err != nil {
		return platform.Response{}, err
	}
	cur, err := db.Collection("novedades").Find(ctx, filtro,
		options.Find().SetSort(bson.D{{Key: "createdAt", Value: -1}}).SetLimit(limite))
	if err != nil {
		return platform.Response{}, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return platform.Response{}, err
	}

	items := make([]map[string]any, 0, len(docs))
	sinLeer := 0
	for _, d := range docs {
		n := platform.Doc(d)
		id, _ := n["id"].(string)
		n["leida"] = leidas[id]
		activa, ok := n["activa"].(bool)
		if !n["leida"].(bool) && (!ok || activa) {
			sinLeer++
		}
		items = append(items, n)
	}
	return platform.JSON(req, 200, "Novedades", map[string]any{
		"items":    items,
		"sin_leer": sinLeer,
	}), nil
}

// Filtro: activas, dentro de la ventana, y para todos o para este taller.
func Filtro(tenantID string, ahora time.Time) bson.D {
	paraQuien := bson.A{
		bson.D{{Key: "tenant_ids", Value: bson.D{{Key: "$exists", Value: false}}}},
		bson.D{{Key: "tenant_ids", Value: nil}},
		bson.D{{Key: "tenant_ids", Value: bson.D{{Key: "$size", Value: 0}}}},
	}
	if tenantID != "" {
		paraQuien = append(paraQuien, bson.D{{Key: "tenant_ids", Value: tenantID}})
	}
	return bson.D{
		{Key: "activa", Value: true},
		{Key: "createdAt", Value: bson.D{{Key: "$gte", Value: ahora.UTC().AddDate(0, 0, -ventanaDias)}}},
		{Key: "$or", Value: paraQuien},
	}
}

func leidasDe(ctx context.Context, db *mongo.Database, sub string) (map[string]bool, error) {
	var lectura struct {
		Leidas []string `bson:"leidas"`
	}
	err := db.Collection("novedades_lecturas").FindOne(ctx, bson.D{{Key: "sub", Value: sub}},
		options.FindOne().SetProjection(bson.D{{Key: "leidas", Value: 1}})).Decode(&lectura)
	if err != nil && err != mongo.ErrNoDocuments {
		return nil, err
	}
	out := make(map[string]bool, len(lectura.Leidas))
	for _, id := range lectura.Leidas {
		out[id] = true
	}
	return out, nil
}
