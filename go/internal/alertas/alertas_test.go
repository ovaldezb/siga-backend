package alertas

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

const (
	tenant = "cccc6666"
	dbName = "t_cccc6666"
)

func pedir(grupo string, qp map[string]string) platform.Request {
	r := platform.Request{QueryStringParameters: qp}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": map[string]any{
		"custom:tenant_id": tenant, "cognito:groups": grupo, "email": "x@taller.test"}}}
	return r
}

func TestMecanicoNoVeImportes(t *testing.T) {
	resp, err := Resumen(context.Background(), pedir("MECANICO", nil))
	if err != nil || resp.StatusCode != 403 {
		t.Fatalf("status %d err %v", resp.StatusCode, err)
	}
}

func TestResumenContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()
	now := time.Now().UTC()

	osCancelada, osCredito, osSinVenta := bson.NewObjectID(), bson.NewObjectID(), bson.NewObjectID()
	if _, err := db.Collection("ordenes_servicio").InsertMany(ctx, []any{
		bson.D{{Key: "_id", Value: osCancelada}, {Key: "estado", Value: "CANCELADO"}, {Key: "sucursal_id", Value: "s1"}},
		bson.D{{Key: "_id", Value: osCredito}, {Key: "folio", Value: "OS-1"}, {Key: "estado", Value: "FINALIZADO"}, {Key: "sucursal_id", Value: "s1"},
			{Key: "venta_id", Value: "v1"}, {Key: "saldo_pendiente", Value: 1500}, {Key: "total", Value: 2000},
			{Key: "cliente_snapshot", Value: bson.D{{Key: "nombre", Value: "Agro"}, {Key: "apellido_paterno", Value: ""}}},
			{Key: "bitacora_estados", Value: bson.A{bson.D{{Key: "estado", Value: "FINALIZADO"}, {Key: "fecha", Value: "2026-10-01T10:00:00Z"}}}}},
		bson.D{{Key: "_id", Value: osSinVenta}, {Key: "folio", Value: "OS-2"}, {Key: "estado", Value: "FINALIZADO"}, {Key: "sucursal_id", Value: "s1"},
			{Key: "total", Value: 800}, {Key: "vehiculo_snapshot", Value: bson.D{{Key: "marca", Value: "Nissan"}, {Key: "placas", Value: "AAA"}}}},
		bson.D{{Key: "estado", Value: "FINALIZADO"}, {Key: "sucursal_id", Value: "s2"}, {Key: "total", Value: 999}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("ventas").InsertMany(ctx, []any{
		// Crédito viejo (vencido) y uno reciente del mismo cliente.
		bson.D{{Key: "cliente_id", Value: "c1"}, {Key: "cliente_nombre", Value: "Agro"}, {Key: "saldo_pendiente", Value: 1500},
			{Key: "orden_id", Value: osCredito.Hex()}, {Key: "sucursal_id", Value: "s1"}, {Key: "createdAt", Value: now.AddDate(0, 0, -45)}},
		bson.D{{Key: "cliente_id", Value: "c1"}, {Key: "cliente_nombre", Value: "Agro"}, {Key: "saldo_pendiente", Value: 500},
			{Key: "sucursal_id", Value: "s1"}, {Key: "createdAt", Value: now.AddDate(0, 0, -3)},
			{Key: "items", Value: bson.A{
				bson.D{{Key: "costo_pendiente", Value: true}, {Key: "precio_unitario", Value: 250}, {Key: "cantidad", Value: 2}},
				bson.D{{Key: "costo_pendiente", Value: false}, {Key: "precio_unitario", Value: 99}},
			}}},
		// No cuentan: anulada, de OS cancelada, liquidada y de otra sucursal.
		bson.D{{Key: "saldo_pendiente", Value: 700}, {Key: "estado", Value: "ANULADA"}, {Key: "sucursal_id", Value: "s1"}},
		bson.D{{Key: "saldo_pendiente", Value: 300}, {Key: "orden_id", Value: osCancelada.Hex()}, {Key: "sucursal_id", Value: "s1"}, {Key: "createdAt", Value: now}},
		bson.D{{Key: "saldo_pendiente", Value: 0}, {Key: "sucursal_id", Value: "s1"}},
		bson.D{{Key: "saldo_pendiente", Value: 900}, {Key: "sucursal_id", Value: "s2"}, {Key: "createdAt", Value: now}},
	}); err != nil {
		t.Fatal(err)
	}

	resp, err := Resumen(ctx, pedir("ADMIN", map[string]string{"sucursal_id": "s1"}))
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("status %d err %v %s", resp.StatusCode, err, resp.Body)
	}
	var sobre struct {
		Data struct {
			Cobranza struct {
				SaldoTotal float64 `json:"saldo_total"`
				Documentos int
				Vencido    struct{ Saldo float64 }
				Clientes   []map[string]any
			}
			CostosPendientes struct {
				Lineas       int
				ImporteVenta float64 `json:"importe_venta"`
			} `json:"costos_pendientes"`
			OrdenesPorCerrar struct {
				Total    int
				ACredito struct{ Count int } `json:"a_credito"`
				SinVenta struct {
					Count int
					Total float64
				} `json:"sin_venta"`
			} `json:"ordenes_por_cerrar"`
		}
	}
	if err := json.Unmarshal([]byte(resp.Body), &sobre); err != nil {
		t.Fatal(err)
	}
	d := sobre.Data
	if d.Cobranza.SaldoTotal != 2000 || d.Cobranza.Documentos != 2 || d.Cobranza.Vencido.Saldo != 1500 {
		t.Errorf("cobranza = %+v", d.Cobranza)
	}
	if len(d.Cobranza.Clientes) != 1 || d.Cobranza.Clientes[0]["saldo"].(float64) != 2000 {
		t.Errorf("clientes = %v", d.Cobranza.Clientes)
	}
	if d.CostosPendientes.Lineas != 1 || d.CostosPendientes.ImporteVenta != 500 {
		t.Errorf("costos = %+v", d.CostosPendientes)
	}
	if d.OrdenesPorCerrar.Total != 2 || d.OrdenesPorCerrar.ACredito.Count != 1 ||
		d.OrdenesPorCerrar.SinVenta.Count != 1 || d.OrdenesPorCerrar.SinVenta.Total != 800 {
		t.Errorf("ordenes = %+v", d.OrdenesPorCerrar)
	}

	resp, _ = PorCerrar(ctx, pedir("ADMIN", map[string]string{"sucursal_id": "s1"}))
	var lista struct {
		Data struct{ Items []map[string]any }
	}
	_ = json.Unmarshal([]byte(resp.Body), &lista)
	porFolio := map[string]map[string]any{}
	for _, it := range lista.Data.Items {
		porFolio[it["folio"].(string)] = it
	}
	if porFolio["OS-1"]["tipo"] != tipoCredito || porFolio["OS-1"]["fecha_finalizado"] != "2026-10-01T10:00:00Z" {
		t.Errorf("OS-1 = %v", porFolio["OS-1"])
	}
	if porFolio["OS-2"]["tipo"] != tipoSinVenta || porFolio["OS-2"]["vehiculo"] != "Nissan AAA" {
		t.Errorf("OS-2 = %v", porFolio["OS-2"])
	}
}
