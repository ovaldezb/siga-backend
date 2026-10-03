package flotillas

import (
	"context"
	"encoding/json"
	"testing"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

func TestListContraMongo(t *testing.T) {
	const tenant, dbName = "aaaa-3333", "t_aaaa3333"
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()
	llamar := func(tenantID string) (int, []map[string]any) {
		t.Helper()
		r := platform.Request{}
		claims := map[string]any{}
		if tenantID != "" {
			claims["custom:tenant_id"] = tenantID
		}
		r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": claims}}
		resp, err := List(ctx, r)
		if err != nil {
			t.Fatal(err)
		}
		var s struct{ Data []map[string]any }
		if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
			t.Fatalf("body: %v %s", err, resp.Body)
		}
		return resp.StatusCode, s.Data
	}

	if s, _ := llamar(""); s != 403 {
		t.Fatalf("sin tenant: %d", s)
	}
	if s, f := llamar(tenant); s != 200 || f == nil || len(f) != 0 {
		t.Fatalf("sin flotillas: %d %v", s, f)
	}

	f1, f2 := bson.NewObjectID(), bson.NewObjectID()
	c1, c2, c3 := bson.NewObjectID(), bson.NewObjectID(), bson.NewObjectID()
	if _, err := db.Collection("flotillas").InsertMany(ctx, []any{
		bson.D{{Key: "_id", Value: f1}, {Key: "nombre", Value: "Transportes A"}},
		bson.D{{Key: "_id", Value: f2}, {Key: "nombre", Value: "Vacía"}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("clientes").InsertMany(ctx, []any{
		bson.D{{Key: "_id", Value: c1}, {Key: "flotilla_id", Value: f1.Hex()}},
		bson.D{{Key: "_id", Value: c2}, {Key: "flotilla_id", Value: f1.Hex()}},
		bson.D{{Key: "_id", Value: c3}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("vehiculos").InsertMany(ctx, []any{
		bson.D{{Key: "cliente_id", Value: c1.Hex()}},
		bson.D{{Key: "cliente_id", Value: c1.Hex()}},
		bson.D{{Key: "cliente_id", Value: c2.Hex()}},
		bson.D{{Key: "cliente_id", Value: c3.Hex()}},
	}); err != nil {
		t.Fatal(err)
	}

	s, f := llamar(tenant)
	if s != 200 || len(f) != 2 {
		t.Fatalf("flotillas: %d %v", s, f)
	}
	por := map[string]map[string]any{}
	for _, x := range f {
		por[x["nombre"].(string)] = x
	}
	if a := por["Transportes A"]; a["id"] != f1.Hex() || a["num_clientes"] != 2.0 || a["num_vehiculos"] != 3.0 {
		t.Fatalf("Transportes A: %v", a)
	}
	if v := por["Vacía"]; v["num_clientes"] != 0.0 || v["num_vehiculos"] != 0.0 {
		t.Fatalf("Vacía: %v", v)
	}
}
