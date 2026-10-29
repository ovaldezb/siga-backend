package clientes

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

func TestHistorialContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()
	norte, sur := bson.NewObjectID(), bson.NewObjectID()
	cid := bson.NewObjectID().Hex()
	ayer := time.Now().Add(-24 * time.Hour)
	if _, err := db.Collection("sucursales").InsertMany(ctx, []any{
		bson.D{{Key: "_id", Value: norte}, {Key: "nombre", Value: "Norte"}},
		bson.D{{Key: "_id", Value: sur}, {Key: "nombre", Value: "Sur"}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("ordenes_servicio").InsertMany(ctx, []any{
		bson.D{{Key: "folio", Value: "OS-1"}, {Key: "cliente_snapshot", Value: bson.D{{Key: "id", Value: cid}}},
			{Key: "sucursal_id", Value: norte.Hex()}, {Key: "createdAt", Value: ayer}},
		bson.D{{Key: "folio", Value: "OS-2"}, {Key: "cliente_id", Value: cid},
			{Key: "sucursal_id", Value: sur.Hex()}, {Key: "createdAt", Value: time.Now()}},
		bson.D{{Key: "folio", Value: "OTRO"}, {Key: "cliente_snapshot", Value: bson.D{{Key: "id", Value: "x"}}}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("ventas").InsertMany(ctx, []any{
		bson.D{{Key: "cliente_id", Value: cid}, {Key: "total", Value: 100.555}, {Key: "sucursal_id", Value: norte.Hex()},
			{Key: "items", Value: bson.A{
				bson.D{{Key: "producto", Value: bson.D{{Key: "nombre", Value: "Aceite"}}}, {Key: "cantidad", Value: 2}},
				bson.D{{Key: "nombre", Value: "Filtro"}},
			}}},
		bson.D{{Key: "cliente_id", Value: cid}, {Key: "total", Value: 50}, {Key: "sucursal_id", Value: norte.Hex()},
			{Key: "items", Value: bson.A{bson.D{{Key: "nombre", Value: "Aceite"}, {Key: "cantidad", Value: 1}}}}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("citas").InsertOne(ctx, bson.D{{Key: "clienteId", Value: cid}, {Key: "fecha", Value: "2026-10-01"}}); err != nil {
		t.Fatal(err)
	}

	r := platform.Request{PathParameters: map[string]string{"id": cid}}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": map[string]any{"custom:tenant_id": tenant}}}
	resp, err := Historial(ctx, r)
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("historial: %v %d %s", err, resp.StatusCode, resp.Body)
	}
	var s struct {
		Data struct {
			Citas    []map[string]any
			Ordenes  []map[string]any
			Ventas   []map[string]any
			Metricas struct {
				TotalGastado   float64          `json:"total_gastado"`
				TotalVisitas   int              `json:"total_visitas"`
				ResumenCompras []map[string]any `json:"resumen_compras"`
				PorSucursal    []map[string]any `json:"por_sucursal"`
			}
		}
	}
	if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
		t.Fatal(err)
	}
	d := s.Data
	if len(d.Citas) != 1 || len(d.Ordenes) != 2 || len(d.Ventas) != 2 {
		t.Fatalf("conteos: %d %d %d", len(d.Citas), len(d.Ordenes), len(d.Ventas))
	}
	if d.Ordenes[0]["folio"] != "OS-2" || d.Ordenes[0]["sucursalNombre"] != "Sur" {
		t.Errorf("orden más reciente primero y con sucursal: %v", d.Ordenes[0])
	}
	if d.Metricas.TotalGastado != 150.56 || d.Metricas.TotalVisitas != 2 {
		t.Errorf("métricas: %+v", d.Metricas)
	}
	if rc := d.Metricas.ResumenCompras; len(rc) != 2 || rc[0]["nombre"] != "Aceite" || rc[0]["cantidad"] != float64(3) {
		t.Errorf("resumen: %v", rc)
	}
	if ps := d.Metricas.PorSucursal; len(ps) != 2 || ps[0]["nombre"] != "Norte" || ps[0]["ventas"] != float64(2) {
		t.Errorf("por sucursal: %v", ps)
	}

	mec := platform.Request{PathParameters: map[string]string{"id": cid}}
	mec.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": map[string]any{
		"custom:tenant_id": tenant, "cognito:groups": "MECANICO"}}}
	if resp, _ := Historial(ctx, mec); resp.StatusCode != 403 {
		t.Errorf("mecánico: %d", resp.StatusCode)
	}
}
