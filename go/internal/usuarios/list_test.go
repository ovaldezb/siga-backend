package usuarios

import (
	"context"
	"testing"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/testmongo"
)

func TestListContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()

	if s := llamar(t, List, req(map[string]any{}), nil); s != 403 {
		t.Fatalf("sin tenant: %d", s)
	}

	s1 := bson.NewObjectID()
	if _, err := db.Collection("sucursales").InsertOne(ctx, bson.D{{Key: "_id", Value: s1}, {Key: "nombre", Value: "Matriz"}}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("usuarios").InsertMany(ctx, []any{
		bson.D{{Key: "email", Value: "a@t.mx"}, {Key: "grupo", Value: "ADMIN"},
			{Key: "sucursales", Value: bson.A{bson.D{{Key: "sucursal", Value: s1.Hex()}}, "borrada"}}},
		bson.D{{Key: "email", Value: "m@t.mx"}, {Key: "grupo", Value: "MECANICO"}},
	}); err != nil {
		t.Fatal(err)
	}

	var users []map[string]any
	if s := llamar(t, List, req(map[string]any{"custom:tenant_id": tenant}), &users); s != 200 || len(users) != 2 {
		t.Fatalf("todos: %d %v", s, users)
	}
	for _, u := range users {
		if u["id"] == nil || u["_id"] != nil {
			t.Fatalf("serialización: %v", u)
		}
		suc := u["sucursales"].([]any)
		if u["email"] == "a@t.mx" && (len(suc) != 1 || suc[0].(map[string]any)["nombre"] != "Matriz") {
			t.Fatalf("sucursales pobladas sin la borrada: %v", suc)
		}
		if u["email"] == "m@t.mx" && len(suc) != 0 {
			t.Fatalf("sin sucursales debe ser lista vacía: %v", suc)
		}
	}

	users = nil
	r := req(map[string]any{"custom:tenant_id": tenant})
	r.QueryStringParameters = map[string]string{"grupo": "MECANICO"}
	if s := llamar(t, List, r, &users); s != 200 || len(users) != 1 || users[0]["email"] != "m@t.mx" {
		t.Fatalf("por grupo: %d %v", s, users)
	}
}
