package sucursales

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

func TestGetContraMongo(t *testing.T) {
	const tenant, dbName = "aaaa-7777", "t_aaaa7777"
	c := testmongo.Conectar(t, dbName)
	ctx := context.Background()
	llamar := func(tenantID, id string) (int, map[string]any, error) {
		r := platform.Request{PathParameters: map[string]string{"id": id}}
		claims := map[string]any{}
		if tenantID != "" {
			claims["custom:tenant_id"] = tenantID
		}
		r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": claims}}
		resp, err := Get(ctx, r)
		var s struct{ Data map[string]any }
		_ = json.Unmarshal([]byte(resp.Body), &s)
		return resp.StatusCode, s.Data, err
	}

	oid := bson.NewObjectID()
	if _, err := c.Database(dbName).Collection("sucursales").InsertOne(ctx, bson.D{
		{Key: "_id", Value: oid}, {Key: "nombre", Value: "Matriz"},
		{Key: "createdAt", Value: time.Date(2026, 5, 1, 8, 0, 0, 0, time.UTC)},
	}); err != nil {
		t.Fatal(err)
	}

	if s, _, _ := llamar("", oid.Hex()); s != 403 {
		t.Fatalf("sin tenant: %d", s)
	}
	if _, _, err := llamar(tenant, "nope"); err == nil {
		t.Fatal("id inválido debía dar ClientError (400)")
	}
	s, d, err := llamar(tenant, oid.Hex())
	if err != nil || s != 200 || d["id"] != oid.Hex() || d["nombre"] != "Matriz" || d["createdAt"] != "2026-05-01T08:00:00Z" {
		t.Fatalf("detalle: %d %v %v", s, err, d)
	}
	if s, _, _ := llamar(tenant, bson.NewObjectID().Hex()); s != 404 {
		t.Fatalf("inexistente: %d", s)
	}
}
