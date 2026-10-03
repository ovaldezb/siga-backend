package ordenes

import (
	"context"
	"encoding/json"
	"testing"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

func pedirOrden(t *testing.T, claims map[string]any, id string) (int, map[string]any) {
	t.Helper()
	r := platform.Request{PathParameters: map[string]string{"id": id}}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": claims}}
	resp, err := Get(context.Background(), r)
	if err != nil {
		if _, ok := err.(*platform.ClientError); ok {
			return 400, nil
		}
		t.Fatalf("handler devolvió error: %v", err)
	}
	var s struct{ Data map[string]any }
	if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
		t.Fatalf("body: %v %s", err, resp.Body)
	}
	return resp.StatusCode, s.Data
}

func TestGetOrdenContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()
	veh := bson.NewObjectID()
	if _, err := db.Collection("vehiculos").InsertOne(ctx, bson.D{
		{Key: "_id", Value: veh}, {Key: "placas", Value: "NUEVA"}, {Key: "sucursal_id", Value: "s1"},
	}); err != nil {
		t.Fatal(err)
	}
	cotizada, conBorrado := bson.NewObjectID(), bson.NewObjectID()
	if _, err := db.Collection("ordenes_servicio").InsertMany(ctx, []any{
		bson.D{{Key: "_id", Value: cotizada}, {Key: "estado", Value: "COTIZADO"}, {Key: "sucursal_id", Value: "s2"},
			{Key: "vehiculo_id", Value: veh.Hex()}, {Key: "total", Value: 900.0},
			{Key: "vehiculo_snapshot", Value: bson.D{{Key: "placas", Value: "VIEJA"}}},
			{Key: "puntosArreglar", Value: bson.A{bson.D{{Key: "items", Value: bson.A{
				bson.D{{Key: "descripcion", Value: "Balata"}, {Key: "precioVenta", Value: 500}, {Key: "cantidad", Value: 2}},
			}}}}},
			{Key: "inventario", Value: bson.A{bson.D{{Key: "costo", Value: 5}, {Key: "item_id", Value: "x"}}, "basura"}}},
		bson.D{{Key: "_id", Value: conBorrado}, {Key: "estado", Value: "APROBADO"},
			{Key: "vehiculo_id", Value: bson.NewObjectID().Hex()},
			{Key: "vehiculo_snapshot", Value: bson.D{{Key: "placas", Value: "SNAP"}, {Key: "sucursal_id", Value: "s9"}}}},
	}); err != nil {
		t.Fatal(err)
	}
	admin := map[string]any{"custom:tenant_id": tenant, "cognito:groups": "[ADMIN]"}
	mecanico := map[string]any{"custom:tenant_id": tenant, "cognito:groups": "[MECANICO]"}

	if s, _ := pedirOrden(t, map[string]any{}, cotizada.Hex()); s != 403 {
		t.Fatalf("sin tenant: %d", s)
	}
	if s, _ := pedirOrden(t, admin, "nope"); s != 400 {
		t.Fatalf("id inválido: %d", s)
	}
	if s, _ := pedirOrden(t, admin, bson.NewObjectID().Hex()); s != 404 {
		t.Fatalf("inexistente: %d", s)
	}

	s, o := pedirOrden(t, admin, cotizada.Hex())
	vs := o["vehiculo_snapshot"].(map[string]any)
	if s != 200 || o["id"] != cotizada.Hex() || o["sucursalId"] != "s2" || o["sucursal_id"] != nil ||
		vs["placas"] != "NUEVA" || vs["sucursalId"] != "s1" || o["cliente_link_enviado"] != false || o["total"] != 900.0 {
		t.Fatalf("cotizada: %d %v", s, o)
	}
	if _, err := db.Collection("cotizacion_acceso").InsertOne(ctx, bson.D{{Key: "orden_id", Value: cotizada.Hex()}}); err != nil {
		t.Fatal(err)
	}
	if _, o := pedirOrden(t, admin, cotizada.Hex()); o["cliente_link_enviado"] != true {
		t.Fatalf("con enlace: %v", o["cliente_link_enviado"])
	}

	// Vehículo borrado: se queda el snapshot guardado tal cual.
	_, o = pedirOrden(t, admin, conBorrado.Hex())
	if vs := o["vehiculo_snapshot"].(map[string]any); vs["placas"] != "SNAP" || vs["sucursal_id"] != "s9" {
		t.Fatalf("snapshot guardado: %v", vs)
	}
	if _, ok := o["cliente_link_enviado"]; ok {
		t.Fatal("cliente_link_enviado solo aplica a COTIZADO")
	}

	_, o = pedirOrden(t, mecanico, cotizada.Hex())
	item := o["puntosArreglar"].([]any)[0].(map[string]any)["items"].([]any)[0].(map[string]any)
	inv := o["inventario"].([]any)[0].(map[string]any)
	if _, ok := o["total"]; ok || item["precioVenta"] != nil || item["cantidad"] != 2.0 || inv["costo"] != nil || inv["item_id"] != "x" {
		t.Fatalf("mecánico sin importes: %v / %v / %v", o["total"], item, inv)
	}
}
