package compras

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
	tenant = "aaaa-ffff"
	dbName = "t_aaaaffff"
)

type cxp struct {
	Items      []map[string]any `json:"items"`
	TotalSaldo float64          `json:"total_saldo"`
	Count      int              `json:"count"`
}

func llamar(t *testing.T, tenantID string, qp map[string]string) (int, cxp) {
	t.Helper()
	r := platform.Request{QueryStringParameters: qp}
	claims := map[string]any{}
	if tenantID != "" {
		claims["custom:tenant_id"] = tenantID
	}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": claims}}
	resp, err := CxP(context.Background(), r)
	if err != nil {
		t.Fatalf("handler devolvió error: %v", err)
	}
	var s struct{ Data cxp }
	if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
		t.Fatalf("body no es JSON: %v (%s)", err, resp.Body)
	}
	return resp.StatusCode, s.Data
}

func TestSinTenant(t *testing.T) {
	if s, _ := llamar(t, "", nil); s != 403 {
		t.Fatalf("sin tenant: %d", s)
	}
}

func TestCxPContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	col := c.Database(dbName).Collection("compras")
	ctx := context.Background()
	base := time.Date(2026, 9, 1, 10, 0, 0, 0, time.UTC)
	if _, err := col.InsertMany(ctx, []any{
		bson.D{{Key: "folio", Value: "C1"}, {Key: "proveedor_id", Value: "p1"}, {Key: "sucursal_id", Value: "s1"},
			{Key: "estado", Value: "RECIBIDA"}, {Key: "saldo_pendiente", Value: 100.105}, {Key: "createdAt", Value: base}},
		bson.D{{Key: "folio", Value: "C2"}, {Key: "proveedor_id", Value: "p2"}, {Key: "sucursal_id", Value: "s1"},
			{Key: "saldo_pendiente", Value: int32(50)}, {Key: "createdAt", Value: base.Add(time.Hour)}},
		bson.D{{Key: "folio", Value: "PAGADA"}, {Key: "saldo_pendiente", Value: 0}},
		bson.D{{Key: "folio", Value: "CANCELADA"}, {Key: "estado", Value: "CANCELADA"}, {Key: "saldo_pendiente", Value: 999}},
	}); err != nil {
		t.Fatal(err)
	}

	s, r := llamar(t, tenant, nil)
	if s != 200 || r.Count != 2 || r.TotalSaldo != 150.11 || r.Items[0]["folio"] != "C2" || r.Items[1]["folio"] != "C1" {
		t.Fatalf("todas: %d %+v", s, r)
	}
	if r.Items[1]["createdAt"] != "2026-09-01T10:00:00Z" || r.Items[1]["id"] == nil {
		t.Fatalf("serialización: %v", r.Items[1])
	}
	if _, r := llamar(t, tenant, map[string]string{"proveedor_id": "p1", "sucursal_id": "s1"}); r.Count != 1 || r.TotalSaldo != 100.11 {
		t.Fatalf("filtro proveedor: %+v", r)
	}
	if _, r := llamar(t, tenant, map[string]string{"sucursal_id": "s9"}); r.Count != 0 || r.Items == nil {
		t.Fatalf("vacío debe ser lista: %+v", r)
	}
}
