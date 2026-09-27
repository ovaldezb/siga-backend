package clientes

import (
	"context"
	"encoding/json"
	"testing"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

type paginaClientes struct {
	Items      []map[string]any
	Total      int64
	Page       int64
	Limit      int64
	TotalPages int64 `json:"totalPages"`
}

func listar(t *testing.T, claims map[string]any, qp map[string]string) (int, paginaClientes) {
	t.Helper()
	r := platform.Request{QueryStringParameters: qp}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": claims}}
	resp, err := List(context.Background(), r)
	if err != nil {
		t.Fatalf("handler devolvió error: %v", err)
	}
	var s struct{ Data paginaClientes }
	if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
		t.Fatalf("body: %v %s", err, resp.Body)
	}
	return resp.StatusCode, s.Data
}

func TestListContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()
	juan, ana, pepe := bson.NewObjectID(), bson.NewObjectID(), bson.NewObjectID()
	if _, err := db.Collection("clientes").InsertMany(ctx, []any{
		bson.D{{Key: "_id", Value: juan}, {Key: "nombre", Value: "Juan"}, {Key: "apellido_paterno", Value: "Pérez"}, {Key: "sucursal_id", Value: "s1"}},
		bson.D{{Key: "_id", Value: ana}, {Key: "nombre", Value: "Ana"}, {Key: "telefono", Value: "5512345678"}, {Key: "sucursal_id", Value: "s2"}},
		bson.D{{Key: "_id", Value: pepe}, {Key: "nombre", Value: "José (Pepe)"}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("vehiculos").InsertMany(ctx, []any{
		bson.D{{Key: "cliente_id", Value: juan.Hex()}}, bson.D{{Key: "cliente_id", Value: juan.Hex()}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("ordenes_servicio").InsertMany(ctx, []any{
		bson.D{{Key: "cliente_snapshot", Value: bson.D{{Key: "id", Value: juan.Hex()}}}, {Key: "estado", Value: "COTIZADO"}},
		bson.D{{Key: "cliente_snapshot", Value: bson.D{{Key: "id", Value: juan.Hex()}}}, {Key: "estado", Value: "APROBADO"}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("ventas").InsertMany(ctx, []any{
		bson.D{{Key: "cliente_id", Value: juan.Hex()}, {Key: "saldo_pendiente", Value: 100.105}},
		bson.D{{Key: "cliente_id", Value: juan.Hex()}, {Key: "saldo_pendiente", Value: 50}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("usuarios").InsertOne(ctx, bson.D{
		{Key: "email", Value: "cajero@t.mx"}, {Key: "sucursales", Value: bson.A{bson.D{{Key: "sucursal", Value: "s1"}}}},
	}); err != nil {
		t.Fatal(err)
	}
	admin := map[string]any{"custom:tenant_id": tenant, "cognito:groups": "[ADMIN]"}
	cajero := map[string]any{"custom:tenant_id": tenant, "cognito:groups": "[CAJERO]", "email": "cajero@t.mx"}

	if s, _ := listar(t, map[string]any{}, nil); s != 403 {
		t.Fatalf("sin tenant: %d", s)
	}
	s, p := listar(t, admin, map[string]string{"limit": "2"})
	if s != 200 || p.Total != 3 || len(p.Items) != 2 || p.TotalPages != 2 || p.Page != 1 || p.Limit != 2 {
		t.Fatalf("página: %d %+v", s, p)
	}
	_, p = listar(t, admin, map[string]string{"q": "pérez"})
	if len(p.Items) != 1 {
		t.Fatalf("q: %+v", p)
	}
	j := p.Items[0]
	if j["num_vehiculos"] != 2.0 || j["cotizaciones_pendientes"] != 1.0 || j["saldo_credito"] != 150.11 ||
		j["sucursalId"] != "s1" || j["sucursal_id"] != nil {
		t.Fatalf("Juan: %v", j)
	}
	if _, p = listar(t, admin, map[string]string{"q": "josé (pepe"}); p.Total != 1 || p.Items[0]["num_vehiculos"] != 0.0 {
		t.Fatalf("búsqueda literal: %+v", p)
	}
	if _, p = listar(t, admin, map[string]string{"q": "5512"}); p.Total != 1 {
		t.Fatalf("por teléfono: %+v", p)
	}
	// La cartera es del taller: sin sucursal pedida, el cajero ve a todos.
	if _, p = listar(t, cajero, nil); p.Total != 3 {
		t.Fatalf("cajero sin sucursal: %+v", p)
	}
	if _, p = listar(t, cajero, map[string]string{"sucursalId": "s1"}); p.Total != 1 {
		t.Fatalf("cajero s1: %+v", p)
	}
	if s, _ := listar(t, cajero, map[string]string{"sucursalId": "s2"}); s != 403 {
		t.Fatalf("cajero s2: %d", s)
	}
}
