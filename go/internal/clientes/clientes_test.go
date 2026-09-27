package clientes

import (
	"context"
	"encoding/json"
	"testing"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

const (
	tenant = "aaaa-cccc"
	dbName = "t_aaaacccc"
)

func req(tenantID, id string) platform.Request {
	r := platform.Request{PathParameters: map[string]string{"id": id}}
	claims := map[string]any{}
	if tenantID != "" {
		claims["custom:tenant_id"] = tenantID
	}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": claims}}
	return r
}

func llamar(t *testing.T, rq platform.Request) (int, map[string]any) {
	t.Helper()
	resp, err := Get(context.Background(), rq)
	if err != nil {
		t.Fatalf("handler devolvió error: %v", err)
	}
	var s struct{ Data map[string]any }
	if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
		t.Fatalf("body no es JSON: %v", err)
	}
	return resp.StatusCode, s.Data
}

func TestValidacionesSinBaseDeDatos(t *testing.T) {
	if s, _ := llamar(t, req("", bson.NewObjectID().Hex())); s != 403 {
		t.Fatalf("sin tenant: %d", s)
	}
	if s, _ := llamar(t, req(tenant, "nope")); s != 400 {
		t.Fatalf("id inválido: %d", s)
	}
}

func TestNumero(t *testing.T) {
	d, _ := bson.ParseDecimal128("1234.565")
	for v, want := range map[any]float64{int32(3): 3, int64(4): 4, 2.5: 2.5, d: 1234.565, "x": 0} {
		if got := numero(v); got != want {
			t.Fatalf("%v: %v", v, got)
		}
	}
}

func TestGetContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()

	oid := bson.NewObjectID()
	sinVentas := bson.NewObjectID()
	_, err := db.Collection("clientes").InsertMany(ctx, []any{
		bson.D{{Key: "_id", Value: oid}, {Key: "nombre", Value: "Juan"},
			{Key: "sucursal_id", Value: "s1"}, {Key: "limite_credito", Value: 5000}},
		bson.D{{Key: "_id", Value: sinVentas}, {Key: "nombre", Value: "Ana"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.Collection("ventas").InsertMany(ctx, []any{
		bson.D{{Key: "cliente_id", Value: oid.Hex()}, {Key: "saldo_pendiente", Value: 100.105}},
		bson.D{{Key: "cliente_id", Value: oid.Hex()}, {Key: "saldo_pendiente", Value: 50}},
		bson.D{{Key: "cliente_id", Value: oid.Hex()}, {Key: "saldo_pendiente", Value: 0}},
		bson.D{{Key: "cliente_id", Value: oid.Hex()}, {Key: "saldo_pendiente", Value: -20}},
		bson.D{{Key: "cliente_id", Value: "otro"}, {Key: "saldo_pendiente", Value: 999}},
	})
	if err != nil {
		t.Fatal(err)
	}

	s, data := llamar(t, req(tenant, oid.Hex()))
	if s != 200 || data["id"] != oid.Hex() || data["nombre"] != "Juan" || data["limite_credito"] != 5000.0 {
		t.Fatalf("cliente: %d %v", s, data)
	}
	if data["sucursalId"] != "s1" {
		t.Fatalf("sucursalId: %v", data)
	}
	if _, ok := data["sucursal_id"]; ok {
		t.Fatal("sucursal_id debía renombrarse")
	}
	if data["saldo_credito"] != 150.11 {
		t.Fatalf("saldo_credito = %v", data["saldo_credito"])
	}

	s, data = llamar(t, req(tenant, sinVentas.Hex()))
	if s != 200 || data["saldo_credito"] != 0.0 {
		t.Fatalf("sin ventas: %d %v", s, data)
	}
	if _, ok := data["sucursalId"]; ok {
		t.Fatal("sin sucursal_id no debe inventar sucursalId")
	}

	if s, _ := llamar(t, req(tenant, bson.NewObjectID().Hex())); s != 404 {
		t.Fatalf("inexistente: %d", s)
	}
}
