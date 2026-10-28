package novedades

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

func pedir(tenant, grupo string, qp map[string]string) platform.Request {
	r := platform.Request{QueryStringParameters: qp}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": map[string]any{
		"sub": "u1", "custom:tenant_id": tenant, "cognito:groups": grupo}}}
	return r
}

func TestListContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, "_platform")
	db := c.Database("_platform")
	ctx := context.Background()
	now := time.Now().UTC()

	todos, paraBajitos, paraOtro, vieja, oculta := bson.NewObjectID(), bson.NewObjectID(), bson.NewObjectID(), bson.NewObjectID(), bson.NewObjectID()
	if _, err := db.Collection("novedades").InsertMany(ctx, []any{
		bson.D{{Key: "_id", Value: todos}, {Key: "titulo", Value: "Para todos"}, {Key: "activa", Value: true}, {Key: "createdAt", Value: now.Add(-time.Hour)}},
		bson.D{{Key: "_id", Value: paraBajitos}, {Key: "titulo", Value: "Flotillas"}, {Key: "activa", Value: true}, {Key: "createdAt", Value: now},
			{Key: "tenant_ids", Value: bson.A{"bajitos"}}},
		bson.D{{Key: "_id", Value: paraOtro}, {Key: "activa", Value: true}, {Key: "createdAt", Value: now}, {Key: "tenant_ids", Value: bson.A{"otro"}}},
		bson.D{{Key: "_id", Value: vieja}, {Key: "activa", Value: true}, {Key: "createdAt", Value: now.AddDate(0, 0, -200)}},
		bson.D{{Key: "_id", Value: oculta}, {Key: "activa", Value: false}, {Key: "createdAt", Value: now}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("novedades_lecturas").InsertOne(ctx, bson.D{
		{Key: "sub", Value: "u1"}, {Key: "leidas", Value: bson.A{todos.Hex()}},
	}); err != nil {
		t.Fatal(err)
	}

	leer := func(r platform.Request) (ids []string, sinLeer int) {
		resp, err := List(ctx, r)
		if err != nil || resp.StatusCode != 200 {
			t.Fatalf("status %d err %v", resp.StatusCode, err)
		}
		var s struct {
			Data struct {
				Items   []map[string]any
				SinLeer int `json:"sin_leer"`
			}
		}
		_ = json.Unmarshal([]byte(resp.Body), &s)
		for _, it := range s.Data.Items {
			ids = append(ids, it["id"].(string))
		}
		return ids, s.Data.SinLeer
	}

	ids, sinLeer := leer(pedir("bajitos", "ADMIN", nil))
	if len(ids) != 2 || ids[0] != paraBajitos.Hex() || ids[1] != todos.Hex() || sinLeer != 1 {
		t.Errorf("bajitos: ids %v sin_leer %d", ids, sinLeer)
	}
	ids, _ = leer(pedir("tercero", "ADMIN", nil))
	if len(ids) != 1 || ids[0] != todos.Hex() {
		t.Errorf("otro taller no debe ver avisos ajenos: %v", ids)
	}
	ids, _ = leer(pedir("", "SUPER_ADMIN", map[string]string{"todas": "1"}))
	if len(ids) != 5 {
		t.Errorf("super admin con todas=1 ve %d, esperaba 5", len(ids))
	}
}
