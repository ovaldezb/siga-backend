package citas

import (
	"context"
	"encoding/json"
	"testing"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

const (
	tenant = "aaaa-2222"
	dbName = "t_aaaa2222"
)

type paginaCitas struct {
	Items []map[string]any
	Total int64
}

func listar(t *testing.T, claims map[string]any, qp map[string]string) (int, paginaCitas) {
	t.Helper()
	r := platform.Request{QueryStringParameters: qp}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": claims}}
	resp, err := List(context.Background(), r)
	if err != nil {
		t.Fatalf("handler devolvió error: %v", err)
	}
	var s struct{ Data paginaCitas }
	if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
		t.Fatalf("body: %v %s", err, resp.Body)
	}
	return resp.StatusCode, s.Data
}

func servicios(p paginaCitas) []string {
	out := make([]string, len(p.Items))
	for i, c := range p.Items {
		out[i], _ = c["servicio"].(string)
	}
	return out
}

func TestListContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()
	juan := bson.NewObjectID()
	if _, err := db.Collection("clientes").InsertOne(ctx, bson.D{{Key: "_id", Value: juan}, {Key: "telefono", Value: "5511"}}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("vehiculos").InsertMany(ctx, []any{
		bson.D{{Key: "cliente_id", Value: juan.Hex()}}, bson.D{{Key: "cliente_id", Value: juan.Hex()}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("ordenes_servicio").InsertMany(ctx, []any{
		bson.D{{Key: "cliente_snapshot", Value: bson.D{{Key: "id", Value: juan.Hex()}}}, {Key: "estado", Value: "EN_PROCESO"}},
		bson.D{{Key: "cliente_snapshot", Value: bson.D{{Key: "id", Value: juan.Hex()}}}, {Key: "estado", Value: "RECEPCION"}},
		bson.D{{Key: "cliente_snapshot", Value: bson.D{{Key: "id", Value: juan.Hex()}}}, {Key: "estado", Value: "COTIZADO"}},
	}); err != nil {
		t.Fatal(err)
	}
	cita := func(servicio, fecha, hora, estado, suc, cliente string) bson.D {
		d := bson.D{{Key: "servicio", Value: servicio}, {Key: "fecha", Value: fecha}, {Key: "horaInicio", Value: hora},
			{Key: "estado", Value: estado}, {Key: "sucursal_id", Value: suc}}
		if cliente != "" {
			d = append(d, bson.E{Key: "clienteId", Value: cliente})
		}
		return d
	}
	if _, err := db.Collection("citas").InsertMany(ctx, []any{
		cita("A", "2026-09-01", "09:00", "PENDIENTE", "s1", juan.Hex()),
		cita("B", "2026-09-02", "10:00", "CANCELADA", "s1", ""),
		cita("C", "2026-09-02", "16:00", "CONFIRMADA", "s2", "no-es-oid"),
		cita("Frenos (del)", "2026-08-15", "09:00", "pospuesta", "s1", ""),
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
	s, p := listar(t, admin, nil)
	if s != 200 || p.Total != 4 || servicios(p)[0] != "C" || servicios(p)[1] != "B" || servicios(p)[3] != "Frenos (del)" {
		t.Fatalf("orden fecha/hora desc: %d %v", s, servicios(p))
	}
	a := p.Items[2]
	if a["num_vehiculos"] != 2.0 || a["os_pendientes"] != 1.0 || a["cotizaciones_pendientes"] != 2.0 || a["cliente_telefono"] != "5511" {
		t.Fatalf("enriquecida: %v", a)
	}
	// Sin cliente, num_vehiculos vale 1 (como Python); con clienteId inválido también.
	if b := p.Items[1]; b["num_vehiculos"] != 1.0 || b["cliente_telefono"] != "" {
		t.Fatalf("sin cliente: %v", b)
	}
	if c := p.Items[0]; c["num_vehiculos"] != 1.0 || c["cliente_telefono"] != "" {
		t.Fatalf("clienteId inválido: %v", c)
	}

	casos := []struct {
		qp   map[string]string
		want int64
	}{
		{map[string]string{"estado": "todos"}, 4},
		{map[string]string{"estado": "CANCELADA"}, 1},
		{map[string]string{"estado_in": "PENDIENTE, CONFIRMADA"}, 2},
		{map[string]string{"estado_ne": "CANCELADA,pospuesta"}, 2},
		{map[string]string{"estado": "PENDIENTE", "estado_ne": "CANCELADA"}, 3}, // estado_ne reemplaza al estado exacto
		{map[string]string{"estado_in": "PENDIENTE,CANCELADA", "estado_ne": "CANCELADA"}, 1},
		{map[string]string{"fecha_desde": "2026-09-02"}, 2},
		{map[string]string{"fecha_hasta": "2026-09-01"}, 2},
		{map[string]string{"q": "frenos (del"}, 1},
		{map[string]string{"sucursal_id": "s2"}, 1},
	}
	for _, c := range casos {
		if _, p := listar(t, admin, c.qp); p.Total != c.want {
			t.Fatalf("%v: %d", c.qp, p.Total)
		}
	}
	if _, p = listar(t, cajero, nil); p.Total != 3 {
		t.Fatalf("cajero: %+v", p)
	}
	if s, _ := listar(t, cajero, map[string]string{"sucursal_id": "s2"}); s != 403 {
		t.Fatalf("cajero s2: %d", s)
	}
}
