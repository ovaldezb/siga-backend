package usuarios

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/sucursales"
	"siga-backend/go/internal/testmongo"
)

const (
	tenant = "aaaa-bbbb"
	dbName = "t_aaaabbbb"
)

type sobre struct {
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data"`
}

func req(claims map[string]any) platform.Request {
	r := platform.Request{}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": claims}}
	return r
}

func llamar(t *testing.T, h platform.Handler, r platform.Request, data any) int {
	t.Helper()
	resp, err := h(context.Background(), r)
	if err != nil {
		t.Fatalf("handler devolvió error: %v", err)
	}
	var s sobre
	if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
		t.Fatalf("body no es JSON: %v", err)
	}
	if data != nil && len(s.Data) > 0 {
		if err := json.Unmarshal(s.Data, data); err != nil {
			t.Fatalf("data: %v (%s)", err, s.Data)
		}
	}
	return resp.StatusCode
}

func TestValidacionesSinBaseDeDatos(t *testing.T) {
	if s := llamar(t, Me, req(map[string]any{"email": "a@b.com"}), nil); s != 403 {
		t.Fatalf("sin tenant: %d", s)
	}
	if s := llamar(t, Me, req(map[string]any{"custom:tenant_id": tenant}), nil); s != 401 {
		t.Fatalf("sin email: %d", s)
	}
	if s := llamar(t, sucursales.List, req(map[string]any{}), nil); s != 403 {
		t.Fatalf("sucursales sin tenant: %d", s)
	}
}

func TestMeYSucursales(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()
	creada := time.Date(2026, 5, 1, 10, 0, 0, 0, time.UTC)

	res, err := db.Collection("sucursales").InsertMany(ctx, []any{
		bson.D{{Key: "nombre", Value: "Matriz"}, {Key: "createdAt", Value: creada}},
		bson.D{{Key: "nombre", Value: "Norte"}},
		bson.D{{Key: "nombre", Value: "Sur"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	matriz := res.InsertedIDs[0].(bson.ObjectID).Hex()
	norte := res.InsertedIDs[1].(bson.ObjectID).Hex()

	_, err = db.Collection("usuarios").InsertOne(ctx, bson.D{
		{Key: "email", Value: "admin@taller.com"},
		{Key: "grupo", Value: "ADMIN"},
		{Key: "createdAt", Value: creada},
		{Key: "sucursales", Value: bson.A{
			bson.D{{Key: "sucursal", Value: matriz}},
			norte, // legacy: id suelto (en Python daba 500)
			bson.D{{Key: "sucursal", Value: bson.NewObjectID().Hex()}}, // borrada: se descarta
		}},
	})
	if err != nil {
		t.Fatal(err)
	}

	claims := map[string]any{"custom:tenant_id": tenant, "email": "admin@taller.com"}

	var lista []map[string]any
	if s := llamar(t, sucursales.List, req(claims), &lista); s != 200 {
		t.Fatalf("list: %d", s)
	}
	if len(lista) != 3 || lista[0]["id"] != matriz || lista[0]["createdAt"] != "2026-05-01T10:00:00Z" {
		t.Fatalf("lista = %v", lista)
	}
	if _, ok := lista[0]["_id"]; ok {
		t.Fatal("no debe exponer _id")
	}

	var me struct {
		ID         string           `json:"id"`
		Email      string           `json:"email"`
		CreatedAt  string           `json:"createdAt"`
		Sucursales []map[string]any `json:"sucursales"`
	}
	if s := llamar(t, Me, req(claims), &me); s != 200 {
		t.Fatalf("me: %d", s)
	}
	if me.ID == "" || me.Email != "admin@taller.com" || me.CreatedAt != "2026-05-01T10:00:00Z" {
		t.Fatalf("me = %+v", me)
	}
	if len(me.Sucursales) != 2 || me.Sucursales[0]["nombre"] != "Matriz" || me.Sucursales[1]["id"] != norte {
		t.Fatalf("sucursales = %v", me.Sucursales)
	}

	otro := map[string]any{"custom:tenant_id": tenant, "email": "nadie@taller.com"}
	if s := llamar(t, Me, req(otro), nil); s != 404 {
		t.Fatalf("usuario inexistente: %d", s)
	}
}
